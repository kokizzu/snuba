import uuid
from typing import Type

from google.protobuf.json_format import MessageToDict
from sentry_protos.snuba.v1.endpoint_trace_item_attributes_pb2 import (
    TraceItemAttributeNamesRequest,
    TraceItemAttributeNamesResponse,
)
from sentry_protos.snuba.v1.request_common_pb2 import PageToken, TraceItemType
from sentry_protos.snuba.v1.trace_item_attribute_pb2 import AttributeKey, AttributeValue
from sentry_protos.snuba.v1.trace_item_filter_pb2 import (
    ComparisonFilter,
    TraceItemFilter,
)

from snuba.attribution.appid import AppID
from snuba.attribution.attribution_info import AttributionInfo
from snuba.datasets.entities.entity_key import EntityKey
from snuba.datasets.entities.factory import get_entity
from snuba.datasets.pluggable_dataset import PluggableDataset
from snuba.datasets.storages.factory import get_storage
from snuba.datasets.storages.storage_key import StorageKey
from snuba.query import OrderBy, OrderByDirection, SelectedExpression
from snuba.query.composite import CompositeQuery
from snuba.query.data_source.simple import Entity, Storage
from snuba.query.dsl import Functions as f
from snuba.query.dsl import and_cond, column, in_cond, not_cond
from snuba.query.expressions import Expression, Lambda
from snuba.query.logical import Query
from snuba.query.query_settings import HTTPQuerySettings
from snuba.reader import Row
from snuba.request import Request as SnubaRequest
from snuba.web import QueryResult
from snuba.web.query import run_query
from snuba.web.rpc import RPCEndpoint
from snuba.web.rpc.common.common import (
    base_conditions_and,
    convert_filter_offset,
    next_monday,
    prev_monday,
    project_id_and_org_conditions,
    treeify_or_and_conditions,
)
from snuba.web.rpc.common.debug_info import extract_response_meta
from snuba.web.rpc.common.exceptions import BadSnubaRPCRequestException
from snuba.web.rpc.proto_visitor import ProtoVisitor, TraceItemFilterWrapper

# max value the user can provide for 'limit' in their request
MAX_REQUEST_LIMIT = 1000


def convert_to_snuba_request(req: TraceItemAttributeNamesRequest) -> SnubaRequest:
    if req.limit > MAX_REQUEST_LIMIT:
        raise BadSnubaRPCRequestException(
            f"Limit can be at most {MAX_REQUEST_LIMIT}, but was {req.limit}. For larger limits please utilize pagination via the page_token variable in your request."
        )

    if req.type == AttributeKey.Type.TYPE_STRING:
        entity = Entity(
            key=EntityKey("spans_str_attrs"),
            schema=get_entity(EntityKey("spans_str_attrs")).get_data_model(),
            sample=None,
        )
    elif req.type in (
        AttributeKey.Type.TYPE_FLOAT,
        AttributeKey.Type.TYPE_DOUBLE,
        AttributeKey.Type.TYPE_INT,
        AttributeKey.Type.TYPE_BOOLEAN,
    ):
        entity = Entity(
            key=EntityKey("spans_num_attrs"),
            schema=get_entity(EntityKey("spans_num_attrs")).get_data_model(),
            sample=None,
        )
    else:
        raise BadSnubaRPCRequestException(
            f"Attribute type '{req.type}' is not supported. Supported types are: TYPE_STRING, TYPE_FLOAT, TYPE_INT, TYPE_BOOLEAN"
        )

    condition = (
        base_conditions_and(
            req.meta,
            f.like(column("attr_key"), f"%{req.value_substring_match}%"),
            convert_filter_offset(req.page_token.filter_offset),
        )
        if req.page_token.HasField("filter_offset")
        else base_conditions_and(
            req.meta, f.like(column("attr_key"), f"%{req.value_substring_match}%")
        )
    )

    query = Query(
        from_clause=entity,
        selected_columns=[
            SelectedExpression(
                name="attr_key",
                expression=column("attr_key", alias="attr_key"),
            ),
        ],
        condition=condition,
        order_by=[
            OrderBy(direction=OrderByDirection.ASC, expression=column("attr_key")),
        ],
        groupby=[
            column("attr_key", alias="attr_key"),
        ],
        limit=req.limit,
        offset=req.page_token.offset,
    )
    treeify_or_and_conditions(query)

    return SnubaRequest(
        id=uuid.UUID(req.meta.request_id),
        original_body=MessageToDict(req),
        query=query,
        query_settings=HTTPQuerySettings(),
        attribution_info=AttributionInfo(
            referrer=req.meta.referrer,
            team="eap",
            feature="eap",
            tenant_ids={
                "organization_id": req.meta.organization_id,
                "referrer": req.meta.referrer,
            },
            app_id=AppID("eap"),
            parent_api=EndpointTraceItemAttributeNames.config_key(),
        ),
    )


class AttributeKeyCollector(ProtoVisitor):
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def visit_TraceItemFilterWrapper(
        self, trace_item_filter_wrapper: TraceItemFilterWrapper
    ) -> None:
        trace_item_filter = trace_item_filter_wrapper.underlying_proto
        if trace_item_filter.HasField("exists_filter"):
            self.keys.add(trace_item_filter.exists_filter.key.name)
        elif trace_item_filter.HasField("comparison_filter"):
            self.keys.add(trace_item_filter.comparison_filter.key.name)


def convert_to_attributes(
    query_res: QueryResult, attribute_type: AttributeKey.Type.ValueType
) -> list[TraceItemAttributeNamesResponse.Attribute]:
    def t(row: Row) -> TraceItemAttributeNamesResponse.Attribute:
        # our query to snuba only selected 1 column, attr_key
        # so the result should only have 1 item per row
        vals = row.values()
        assert len(vals) == 1
        attr_name = list(vals)[0]
        return TraceItemAttributeNamesResponse.Attribute(
            name=attr_name, type=attribute_type
        )

    return list(map(t, query_res.result["data"]))


def get_co_occurring_attributes_date_condition(
    request: TraceItemAttributeNamesRequest,
) -> Expression:
    # round the lower timestamp to the previous monday
    lower_ts = request.meta.start_timestamp.ToDatetime().replace(
        hour=0, minute=0, second=0
    )
    lower_ts = prev_monday(lower_ts)

    # round the upper timestamp to the next monday
    upper_ts = request.meta.end_timestamp.ToDatetime().replace(
        hour=0, minute=0, second=0
    )
    upper_ts = next_monday(upper_ts)

    return and_cond(
        f.less(
            column("date"),
            f.toDate(upper_ts),
        ),
        f.greaterOrEquals(
            column("date"),
            f.toDate(lower_ts),
        ),
    )


def get_co_occurring_attributes(
    request: TraceItemAttributeNamesRequest,
) -> SnubaRequest:
    """Constructs the clickhouse query for co-occurring attributes:


      The query at the end looks something like this:

      SELECT DISTINCT attr_key
      FROM
      (
          SELECT arrayJoin(arrayFilter(attr -> ((NOT ((attr.2) IN ['test_tag_1_0'])) AND startsWith(attr.2, 'test_')), arrayMap(x -> ('TYPE_STRING', x), attributes_string))) AS attr_key
          FROM eap_item_co_occurring_attrs_1_local
          WHERE (item_type = 1) AND (project_id IN [1]) AND (organization_id = 1) AND (date < toDateTime(toDate('2025-03-17', 'Universal'))) AND (date >= toDateTime(toDate('2025-03-10', 'Universal')))

          -- This is a faster way of looking up whether all attributes co-exist, it uses an array of hashes. This avoids string equality comparisons
          AND hasAll(attribute_keys_hash, [cityHash64('test_tag_1_0')])
          --

          ORDER BY attr_key ASC
          LIMIT 0, 10000
      )

      **Explanation:**

      1. This query would narrow down the granules to scan using the primary key (stored in memory):

          `(organization_id, project_id, date, item_type, key_val_hash)`

      2. The following line checks to see that the events contain all of the co-occurring attributes
          `hasAll(attribute_keys_hash, [cityHash64('test_tag_1_0')])`

          - This hits the bloom filter index on `attribute_keys_hash` and prevents granules that do not have all of the co-occurring attributes from being scanned
          - loading `attributes_string_hash` is orders of magnitude faster than the `attributes_string` array because all elements are fixed size (UInt64) and
              equality can be checked in a single CPU instruction (or fewer if SIMD instructions are used)
          - Clickhouse automatically puts this clause into the [PREWHERE](https://clickhouse.com/docs/sql-reference/statements/select/prewhere)
      3. The inner query surfaces all co-occurring attributes with `allocation_policy.is_throttled` on an event-by-event basis, stopping at 1000 attributes

      ```sql
      -- each of the co-occurring attributes becomes a row sent to the outer query
      arrayJoin(
              arrayFilter(
                  attr -> NOT in(attr, ['test_tag_1_0']),
                  attributes_string
              )
      ) AS attr_key
      ```
    4. . The outer query deduplicates the attributes sent by the inner query to return to the user distinct co-occurring attributes

      **The following things make this query more performant than searching the source table:**

          - The attribute keys are NOT bucketed. Since the functionality has to process ALL the attributes, all the bucket files would have to be opened for each granule.
          This way Clickhouse only has to open 1 file
          - The attribute keys are deduplicated, resulting in less data to scan (~95% row reduction rate)
          - there is a bloom filter index on all key values
    """
    # get all attribute keys from the filter
    collector = AttributeKeyCollector()
    TraceItemFilterWrapper(request.intersecting_attributes_filter).accept(collector)
    attribute_keys_to_search = collector.keys
    storage_key = StorageKey("eap_item_co_occurring_attrs")

    storage = Storage(
        key=storage_key,
        schema=get_storage(storage_key).get_schema().get_columns(),
        sample=None,
    )

    condition = and_cond(
        and_cond(
            project_id_and_org_conditions(request.meta),
            # timestamp should be converted to start and end of week
            get_co_occurring_attributes_date_condition(request),
        ),
        f.hasAll(
            column("attribute_keys_hash"),
            f.array(*[f.cityHash64(k) for k in attribute_keys_to_search]),
        ),
    )

    if request.meta.trace_item_type != TraceItemType.TRACE_ITEM_TYPE_UNSPECIFIED:
        condition = and_cond(
            f.equals(column("item_type"), request.meta.trace_item_type), condition
        )

    string_array = f.arrayMap(
        Lambda(None, ("x",), f.tuple("TYPE_STRING", column("x"))),
        column("attributes_string"),
    )

    double_array = f.arrayMap(
        Lambda(None, ("x",), f.tuple("TYPE_DOUBLE", column("x"))),
        column("attributes_float"),
    )
    array_func = None
    if request.type == AttributeKey.Type.TYPE_STRING:
        array_func = string_array
    elif request.type in (
        AttributeKey.Type.TYPE_FLOAT,
        AttributeKey.Type.TYPE_DOUBLE,
        AttributeKey.Type.TYPE_INT,
    ):
        array_func = double_array
    else:
        array_func = f.arrayConcat(
            f.arrayMap(
                Lambda(None, ("x",), f.tuple("TYPE_STRING", column("x"))),
                column("attributes_string"),
            ),
            f.arrayMap(
                Lambda(None, ("x",), f.tuple("TYPE_DOUBLE", column("x"))),
                column("attributes_float"),
            ),
        )

    attr_filter = not_cond(
        in_cond(column("attr.2"), f.array(*attribute_keys_to_search))
    )
    if request.value_substring_match:
        attr_filter = and_cond(
            attr_filter, f.startsWith(column("attr.2"), request.value_substring_match)
        )

    inner_query = Query(
        from_clause=storage,
        selected_columns=[
            SelectedExpression(
                name="attr_key",
                expression=f.arrayJoin(
                    f.arrayFilter(
                        Lambda(None, ("attr",), attr_filter),
                        array_func,
                    ),
                    alias="attr_key",
                ),
            ),
        ],
        condition=condition,
        order_by=[
            OrderBy(direction=OrderByDirection.ASC, expression=column("attr_key")),
        ],
        # chosen arbitrarily to be a high number
        limit=10000,
    )

    full_query = CompositeQuery(
        from_clause=inner_query,
        selected_columns=[
            SelectedExpression(
                name="attr_key", expression=f.distinct(column("attr_key"))
            )
        ],
        limit=1000,
    )
    treeify_or_and_conditions(full_query)
    settings = HTTPQuerySettings()
    settings.push_clickhouse_setting("max_execution_time", 1)
    settings.push_clickhouse_setting("timeout_overflow_mode", "break")
    snuba_request = SnubaRequest(
        id=uuid.UUID(request.meta.request_id),
        original_body=MessageToDict(request),
        query=full_query,
        query_settings=settings,
        attribution_info=AttributionInfo(
            referrer=request.meta.referrer,
            team="eap",
            feature="eap",
            tenant_ids={
                "organization_id": request.meta.organization_id,
                "referrer": request.meta.referrer,
            },
            app_id=AppID("eap"),
            parent_api=EndpointTraceItemAttributeNames.config_key(),
        ),
    )
    return snuba_request


def convert_co_occurring_results_to_attributes(
    query_res: QueryResult,
) -> list[TraceItemAttributeNamesResponse.Attribute]:
    def t(row: Row) -> TraceItemAttributeNamesResponse.Attribute:
        # our query to snuba only selected 1 column, attr_key
        # so the result should only have 1 item per row
        vals = row.values()
        assert len(vals) == 1
        attr_type, attr_name = list(vals)[0]
        assert isinstance(attr_type, str)
        return TraceItemAttributeNamesResponse.Attribute(
            name=attr_name, type=getattr(AttributeKey.Type, attr_type)
        )

    return list(map(t, query_res.result.get("data", [])))


class EndpointTraceItemAttributeNames(
    RPCEndpoint[TraceItemAttributeNamesRequest, TraceItemAttributeNamesResponse]
):
    @classmethod
    def version(cls) -> str:
        return "v1"

    @classmethod
    def request_class(cls) -> Type[TraceItemAttributeNamesRequest]:
        return TraceItemAttributeNamesRequest

    @classmethod
    def response_class(cls) -> Type[TraceItemAttributeNamesResponse]:
        return TraceItemAttributeNamesResponse

    def _build_response(
        self,
        req: TraceItemAttributeNamesRequest,
        res: QueryResult,
    ) -> TraceItemAttributeNamesResponse:
        attributes = convert_to_attributes(res, req.type)
        page_token = (
            PageToken(offset=req.page_token.offset + len(attributes))
            if req.page_token.HasField("offset") or len(attributes) == 0
            else PageToken(
                filter_offset=TraceItemFilter(
                    comparison_filter=ComparisonFilter(
                        key=AttributeKey(
                            type=AttributeKey.TYPE_STRING, name="attr_key"
                        ),
                        op=ComparisonFilter.OP_GREATER_THAN,
                        value=AttributeValue(val_str=attributes[-1].name),
                    )
                )
            )
        )
        return TraceItemAttributeNamesResponse(
            attributes=attributes,
            page_token=page_token,
            meta=extract_response_meta(
                req.meta.request_id, req.meta.debug, [res], [self._timer]
            ),
        )

    def _execute(
        self, in_msg: TraceItemAttributeNamesRequest
    ) -> TraceItemAttributeNamesResponse:
        if not in_msg.meta.request_id:
            in_msg.meta.request_id = str(uuid.uuid4())
        if in_msg.HasField("intersecting_attributes_filter"):
            snuba_request = get_co_occurring_attributes(in_msg)
            res = run_query(
                dataset=PluggableDataset(name="eap", all_entities=[]),
                request=snuba_request,
                timer=self._timer,
            )
            return TraceItemAttributeNamesResponse(
                attributes=convert_co_occurring_results_to_attributes(res),
                meta=extract_response_meta(
                    in_msg.meta.request_id, in_msg.meta.debug, [res], [self._timer]
                ),
            )
        else:
            snuba_request = convert_to_snuba_request(in_msg)
            res = run_query(
                dataset=PluggableDataset(name="eap", all_entities=[]),
                request=snuba_request,
                timer=self._timer,
            )
            return self._build_response(in_msg, res)
