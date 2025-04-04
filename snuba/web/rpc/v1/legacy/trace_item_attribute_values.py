import uuid

from google.protobuf.json_format import MessageToDict
from sentry_protos.snuba.v1.endpoint_trace_item_attributes_pb2 import (
    TraceItemAttributeValuesRequest,
)

from snuba.attribution.appid import AppID
from snuba.attribution.attribution_info import AttributionInfo
from snuba.datasets.entities.entity_key import EntityKey
from snuba.datasets.entities.factory import get_entity
from snuba.query import OrderBy, OrderByDirection, SelectedExpression
from snuba.query.data_source.simple import Entity
from snuba.query.dsl import Functions as f
from snuba.query.dsl import column, literal, literals_array
from snuba.query.expressions import Expression
from snuba.query.logical import Query
from snuba.query.query_settings import HTTPQuerySettings
from snuba.request import Request as SnubaRequest
from snuba.web.rpc.common.common import (
    base_conditions_and,
    convert_filter_offset,
    treeify_or_and_conditions,
    truncate_request_meta_to_day,
)
from snuba.web.rpc.common.exceptions import BadSnubaRPCRequestException


def _build_base_conditions_and(request: TraceItemAttributeValuesRequest) -> Expression:
    if request.value_substring_match:
        return (
            base_conditions_and(
                request.meta,
                f.equals(column("attr_key"), literal(request.key.name)),
                # multiSearchAny has special treatment with ngram bloom filters
                # https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree#functions-support
                f.multiSearchAny(
                    column("attr_value"),
                    literals_array(None, [literal(request.value_substring_match)]),
                ),
                convert_filter_offset(request.page_token.filter_offset),
            )
            if request.page_token.HasField("filter_offset")
            else base_conditions_and(
                request.meta,
                f.equals(column("attr_key"), literal(request.key.name)),
                f.multiSearchAny(
                    column("attr_value"),
                    literals_array(None, [literal(request.value_substring_match)]),
                ),
            )
        )
    else:
        return (
            base_conditions_and(
                request.meta,
                f.equals(column("attr_key"), literal(request.key.name)),
                convert_filter_offset(request.page_token.filter_offset),
            )
            if request.page_token.HasField("filter_offset")
            else base_conditions_and(
                request.meta,
                f.equals(column("attr_key"), literal(request.key.name)),
            )
        )


def _build_query(request: TraceItemAttributeValuesRequest) -> Query:
    if request.limit > 1000:
        raise BadSnubaRPCRequestException("Limit can be at most 1000")

    entity = Entity(
        key=EntityKey("spans_str_attrs"),
        schema=get_entity(EntityKey("spans_str_attrs")).get_data_model(),
        sample=None,
    )

    truncate_request_meta_to_day(request.meta)

    res = Query(
        from_clause=entity,
        selected_columns=[
            SelectedExpression(
                name="attr_value",
                expression=f.distinct(column("attr_value", alias="attr_value")),
            ),
        ],
        condition=_build_base_conditions_and(request),
        order_by=[
            OrderBy(direction=OrderByDirection.ASC, expression=column("attr_value")),
        ],
        limit=request.limit,
        offset=request.page_token.offset,
    )
    treeify_or_and_conditions(res)
    return res


def build_snuba_request(
    request: TraceItemAttributeValuesRequest,
) -> SnubaRequest:
    return SnubaRequest(
        id=uuid.uuid4(),
        original_body=MessageToDict(request),
        query=_build_query(request),
        query_settings=HTTPQuerySettings(),
        attribution_info=AttributionInfo(
            referrer=request.meta.referrer,
            team="eap",
            feature="eap",
            tenant_ids={
                "organization_id": request.meta.organization_id,
                "referrer": request.meta.referrer,
            },
            app_id=AppID("eap"),
            parent_api="trace_item_values",
        ),
    )
