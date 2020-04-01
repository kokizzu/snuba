import logging
import os
import time
from datetime import datetime
from typing import Any, Mapping, NamedTuple
from uuid import UUID

import jsonschema
import sentry_sdk
import simplejson as json
from flask import Flask, Response, redirect, render_template
from flask import request as http_request
from markdown import markdown
from werkzeug.exceptions import BadRequest

from snuba import environment, settings, state, util
from snuba.clusters import cluster
from snuba.consumer import KafkaMessageMetadata
from snuba.datasets.dataset import Dataset
from snuba.datasets.factory import (
    InvalidDatasetError,
    enforce_table_writer,
    ensure_not_internal,
    get_dataset,
    get_enabled_dataset_names,
)
from snuba.datasets.schemas.tables import TableSchema
from snuba.redis import redis_client
from snuba.request import Request
from snuba.request.request_settings import HTTPRequestSettings
from snuba.request.schema import RequestSchema
from snuba.request.validation import validate_request_content
from snuba.subscriptions.codecs import SubscriptionDataCodec
from snuba.subscriptions.data import InvalidSubscriptionError, PartitionId
from snuba.subscriptions.subscription import SubscriptionCreator, SubscriptionDeleter
from snuba.util import local_dataset_mode
from snuba.utils.metrics.backends.wrapper import MetricsWrapper
from snuba.utils.metrics.timer import Timer
from snuba.utils.streams.kafka import KafkaPayload
from snuba.utils.streams.types import Message, Partition, Topic
from snuba.web.converters import DatasetConverter
from snuba.web import RawQueryException
from snuba.web.query import parse_and_run_query


metrics = MetricsWrapper(environment.metrics, "api")

logger = logging.getLogger("snuba.api")


class WebQueryResult(NamedTuple):
    # TODO: Give a better abstraction to QueryResult
    payload: Mapping[str, Any]
    status: int


try:
    import uwsgi
except ImportError:

    def check_down_file_exists() -> bool:
        return False


else:

    def check_down_file_exists() -> bool:
        try:
            return os.stat("/tmp/snuba.down").st_mtime > uwsgi.started_on
        except OSError:
            return False


def check_clickhouse() -> bool:
    """
    Checks if all the tables in all the enabled datasets exist in ClickHouse
    """
    try:
        for name in get_enabled_dataset_names():
            dataset = get_dataset(name)

            for storage in dataset.get_all_storages():
                clickhouse_ro = storage.get_cluster().get_clickhouse_ro()
                clickhouse_tables = clickhouse_ro.execute("show tables")
                source = storage.get_schemas().get_read_schema()
                if isinstance(source, TableSchema):
                    table_name = source.get_table_name()
                    if (table_name,) not in clickhouse_tables:
                        return False

        return True

    except Exception:
        return False


application = Flask(__name__, static_url_path="")
application.testing = settings.TESTING
application.debug = settings.DEBUG
application.url_map.converters["dataset"] = DatasetConverter


@application.errorhandler(BadRequest)
def handle_bad_request(exception: BadRequest):
    cause = getattr(exception, "__cause__", None)
    if isinstance(cause, json.errors.JSONDecodeError):
        data = {"error": {"type": "json", "message": str(cause)}}
    elif isinstance(cause, jsonschema.ValidationError):
        data = {
            "error": {
                "type": "schema",
                "message": cause.message,
                "path": list(cause.path),
                "schema": cause.schema,
            }
        }
    else:
        data = {"error": {"type": "request", "message": str(exception)}}

    def default_encode(value):
        # XXX: This is necessary for rendering schema defaults values that are
        # generated by callables, rather than constants.
        if callable(value):
            return value()
        else:
            raise TypeError()

    return (
        json.dumps(data, indent=4, default=default_encode),
        400,
        {"Content-Type": "application/json"},
    )


@application.errorhandler(InvalidDatasetError)
def handle_invalid_dataset(exception: InvalidDatasetError):
    data = {"error": {"type": "dataset", "message": str(exception)}}
    return (
        json.dumps(data, sort_keys=True, indent=4),
        404,
        {"Content-Type": "application/json"},
    )


@application.route("/")
def root():
    with open("README.md") as f:
        return render_template("index.html", body=markdown(f.read()))


@application.route("/css/<path:path>")
def send_css(path):
    return application.send_static_file(os.path.join("css", path))


@application.route("/img/<path:path>")
@application.route("/snuba/web/static/img/<path:path>")
def send_img(path):
    return application.send_static_file(os.path.join("img", path))


@application.route("/dashboard")
@application.route("/dashboard.<fmt>")
def dashboard(fmt="html"):
    if fmt == "json":
        result = {
            "queries": state.get_queries(),
            "concurrent": {k: state.get_concurrent(k) for k in ["global"]},
            "rates": {k: state.get_rates(k) for k in ["global"]},
        }
        return (json.dumps(result), 200, {"Content-Type": "application/json"})
    else:
        return application.send_static_file("dashboard.html")


@application.route("/config")
@application.route("/config.<fmt>", methods=["GET", "POST"])
def config(fmt="html"):
    if fmt == "json":
        if http_request.method == "GET":
            return (
                json.dumps(state.get_raw_configs()),
                200,
                {"Content-Type": "application/json"},
            )
        elif http_request.method == "POST":
            state.set_configs(
                json.loads(http_request.data),
                user=http_request.headers.get("x-forwarded-email"),
            )
            return (
                json.dumps(state.get_raw_configs()),
                200,
                {"Content-Type": "application/json"},
            )
    else:
        return application.send_static_file("config.html")


@application.route("/config/changes.json")
def config_changes():
    return (
        json.dumps(state.get_config_changes()),
        200,
        {"Content-Type": "application/json"},
    )


@application.route("/health")
def health():
    down_file_exists = check_down_file_exists()
    thorough = http_request.args.get("thorough", False)
    clickhouse_health = check_clickhouse() if thorough else True

    if not down_file_exists and clickhouse_health:
        body = {"status": "ok"}
        status = 200
    else:
        body = {
            "down_file_exists": down_file_exists,
        }
        if thorough:
            body["clickhouse_ok"] = clickhouse_health
        status = 502

    return (json.dumps(body), status, {"Content-Type": "application/json"})


def parse_request_body(http_request):
    with sentry_sdk.start_span(description="parse_request_body", op="parse"):
        metrics.timing("http_request_body_length", len(http_request.data))
        try:
            return json.loads(http_request.data)
        except json.errors.JSONDecodeError as error:
            raise BadRequest(str(error)) from error


@application.route("/query", methods=["GET", "POST"])
@util.time_request("query")
def unqualified_query_view(*, timer: Timer):
    if http_request.method == "GET":
        return redirect(f"/{settings.DEFAULT_DATASET_NAME}/query", code=302)
    elif http_request.method == "POST":
        body = parse_request_body(http_request)
        dataset = get_dataset(body.pop("dataset", settings.DEFAULT_DATASET_NAME))
        return dataset_query(dataset, body, timer)
    else:
        assert False, "unexpected fallthrough"


@application.route("/<dataset:dataset>/query", methods=["GET", "POST"])
@util.time_request("query")
def dataset_query_view(*, dataset: Dataset, timer: Timer):
    if http_request.method == "GET":
        schema = RequestSchema.build_with_extensions(
            dataset.get_extensions(), HTTPRequestSettings
        )
        return render_template(
            "query.html",
            query_template=json.dumps(schema.generate_template(), indent=4,),
        )
    elif http_request.method == "POST":
        body = parse_request_body(http_request)
        return dataset_query(dataset, body, timer)
    else:
        assert False, "unexpected fallthrough"


def dataset_query(dataset: Dataset, body, timer: Timer) -> Response:
    assert http_request.method == "POST"
    ensure_not_internal(dataset)
    ensure_table_exists(dataset)
    return format_result(
        run_query(
            dataset,
            validate_request_content(
                body,
                RequestSchema.build_with_extensions(
                    dataset.get_extensions(), HTTPRequestSettings
                ),
                timer,
                dataset,
                http_request.referrer,
            ),
            timer,
        )
    )


def run_query(dataset: Dataset, request: Request, timer: Timer) -> WebQueryResult:
    try:
        result = parse_and_run_query(dataset, request, timer)
        payload = {**result.result, "timing": timer.for_json()}
        if settings.STATS_IN_RESPONSE or request.settings.get_debug():
            payload.update(result.extra)
        return WebQueryResult(payload, 200)
    except RawQueryException as e:
        return WebQueryResult(
            {
                "error": {"type": e.err_type, "message": e.message, **e.meta},
                "sql": e.sql,
                "stats": e.stats,
                "timing": timer.for_json(),
            },
            429 if e.err_type == "rate-limited" else 500,
        )


def format_result(result: WebQueryResult) -> Response:
    return Response(
        json.dumps(result.payload), result.status, {"Content-Type": "application/json"},
    )


@application.errorhandler(InvalidSubscriptionError)
def handle_subscription_error(exception: InvalidSubscriptionError):
    data = {"error": {"type": "subscription", "message": str(exception)}}
    return (
        json.dumps(data, indent=4),
        400,
        {"Content-Type": "application/json"},
    )


@application.route("/<dataset:dataset>/subscriptions", methods=["POST"])
@util.time_request("subscription")
def create_subscription(*, dataset: Dataset, timer: Timer):
    ensure_not_internal(dataset)
    subscription = SubscriptionDataCodec().decode(http_request.data)
    # TODO: Check for valid queries with fields that are invalid for subscriptions. For
    # example date fields and aggregates.
    identifier = SubscriptionCreator(dataset).create(subscription, timer)
    return (
        json.dumps({"subscription_id": str(identifier)}),
        202,
        {"Content-Type": "application/json"},
    )


@application.route(
    "/<dataset:dataset>/subscriptions/<int:partition>/<key>", methods=["DELETE"]
)
def delete_subscription(*, dataset: Dataset, partition: int, key: str):
    ensure_not_internal(dataset)
    SubscriptionDeleter(dataset, PartitionId(partition)).delete(UUID(key))
    return "ok", 202, {"Content-Type": "text/plain"}


if application.debug or application.testing:
    # These should only be used for testing/debugging. Note that the database name
    # is checked to avoid scary production mishaps.

    _ensured = {}

    def ensure_table_exists(dataset: Dataset, force: bool = False) -> None:
        if not force and _ensured.get(dataset, False):
            return

        assert local_dataset_mode(), "Cannot create table in distributed mode"

        from snuba.migrations import migrate

        # We cannot build distributed tables this way. So this only works in local
        # mode.
        for storage in dataset.get_all_storages():
            clickhouse_rw = storage.get_cluster().get_clickhouse_rw()
            for statement in storage.get_schemas().get_create_statements():
                clickhouse_rw.execute(statement.statement)

        migrate.run(dataset)

        _ensured[dataset] = True

    @application.route("/tests/<dataset:dataset>/insert", methods=["POST"])
    def write(*, dataset: Dataset):
        from snuba.processor import ProcessorAction

        ensure_table_exists(dataset)

        rows = []
        offset_base = int(round(time.time() * 1000))
        for index, message in enumerate(json.loads(http_request.data)):
            offset = offset_base + index
            processed_message = (
                enforce_table_writer(dataset)
                .get_stream_loader()
                .get_processor()
                .process_message(
                    message, KafkaMessageMetadata(offset=offset, partition=0,)
                )
            )
            if processed_message:
                assert processed_message.action is ProcessorAction.INSERT
                rows.extend(processed_message.data)

        enforce_table_writer(dataset).get_writer().write(rows)

        return ("ok", 200, {"Content-Type": "text/plain"})

    @application.route("/tests/<dataset:dataset>/eventstream", methods=["POST"])
    def eventstream(*, dataset: Dataset):
        ensure_table_exists(dataset)
        record = json.loads(http_request.data)

        version = record[0]
        if version != 2:
            raise RuntimeError("Unsupported protocol version: %s" % record)

        message: Message[KafkaPayload] = Message(
            Partition(Topic("topic"), 0),
            0,
            KafkaPayload(None, http_request.data),
            datetime.now(),
        )

        type_ = record[1]
        if type_ == "insert":
            from snuba.consumer import ConsumerWorker

            worker = ConsumerWorker(dataset, metrics=metrics)
        else:
            from snuba.replacer import ReplacerWorker
            # TODO: Fix
            clickhouse_rw = cluster.get_clickhouse_rw()
            worker = ReplacerWorker(clickhouse_rw, dataset, metrics=metrics)

        processed = worker.process_message(message)
        if processed is not None:
            batch = [processed]
            worker.flush_batch(batch)

        return ("ok", 200, {"Content-Type": "text/plain"})

    @application.route("/tests/<dataset:dataset>/drop", methods=["POST"])
    def drop(*, dataset: Dataset):
        for storage in dataset.get_all_storages():
            clickhouse_rw = storage.get_cluster().get_clickhouse_rw()
            for statement in storage.get_schemas().get_drop_statements():
                clickhouse_rw.execute(statement.statement)

        ensure_table_exists(dataset, force=True)
        redis_client.flushdb()
        return ("ok", 200, {"Content-Type": "text/plain"})

    @application.route("/tests/error")
    def error():
        1 / 0


else:

    def ensure_table_exists(dataset: Dataset, force: bool = False) -> None:
        pass
