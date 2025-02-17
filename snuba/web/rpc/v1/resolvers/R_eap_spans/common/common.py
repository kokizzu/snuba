from typing import Final, Mapping, Sequence, Set

from sentry_protos.snuba.v1.trace_item_attribute_pb2 import (
    AttributeKey,
    VirtualColumnContext,
)

from snuba.query import Query
from snuba.query.dsl import Functions as f
from snuba.query.dsl import column, literal, literals_array
from snuba.query.expressions import Expression, SubscriptableReference
from snuba.web.rpc.common.exceptions import BadSnubaRPCRequestException

# These are the columns which aren't stored in attr_str_ nor attr_num_ in clickhouse
NORMALIZED_COLUMNS: Final[Mapping[str, AttributeKey.Type.ValueType]] = {
    "sentry.organization_id": AttributeKey.Type.TYPE_INT,
    "sentry.project_id": AttributeKey.Type.TYPE_INT,
    "sentry.service": AttributeKey.Type.TYPE_STRING,
    "sentry.span_id": AttributeKey.Type.TYPE_STRING,  # this is converted by a processor on the storage
    "sentry.parent_span_id": AttributeKey.Type.TYPE_STRING,  # this is converted by a processor on the storage
    "sentry.segment_id": AttributeKey.Type.TYPE_STRING,  # this is converted by a processor on the storage
    "sentry.segment_name": AttributeKey.Type.TYPE_STRING,
    "sentry.is_segment": AttributeKey.Type.TYPE_BOOLEAN,
    "sentry.duration_ms": AttributeKey.Type.TYPE_DOUBLE,
    "sentry.exclusive_time_ms": AttributeKey.Type.TYPE_DOUBLE,
    "sentry.retention_days": AttributeKey.Type.TYPE_INT,
    "sentry.name": AttributeKey.Type.TYPE_STRING,
    "sentry.sampling_weight": AttributeKey.Type.TYPE_DOUBLE,
    "sentry.sampling_factor": AttributeKey.Type.TYPE_DOUBLE,
    "sentry.timestamp": AttributeKey.Type.TYPE_UNSPECIFIED,
    "sentry.start_timestamp": AttributeKey.Type.TYPE_UNSPECIFIED,
    "sentry.end_timestamp": AttributeKey.Type.TYPE_UNSPECIFIED,
}

TIMESTAMP_COLUMNS: Final[Set[str]] = {
    "sentry.timestamp",
    "sentry.start_timestamp",
    "sentry.end_timestamp",
}


def attribute_key_to_expression(attr_key: AttributeKey) -> Expression:
    def _build_label_mapping_key(attr_key: AttributeKey) -> str:
        return attr_key.name + "_" + AttributeKey.Type.Name(attr_key.type)

    if attr_key.type == AttributeKey.Type.TYPE_UNSPECIFIED:
        raise BadSnubaRPCRequestException(
            f"attribute key {attr_key.name} must have a type specified"
        )
    alias = _build_label_mapping_key(attr_key)

    if attr_key.name == "sentry.trace_id":
        if attr_key.type == AttributeKey.Type.TYPE_STRING:
            return f.CAST(column("trace_id"), "String", alias=alias)
        raise BadSnubaRPCRequestException(
            f"Attribute {attr_key.name} must be requested as a string, got {attr_key.type}"
        )

    if attr_key.name in TIMESTAMP_COLUMNS:
        if attr_key.type == AttributeKey.Type.TYPE_STRING:
            return f.CAST(
                column(attr_key.name[len("sentry.") :]), "String", alias=alias
            )
        if attr_key.type == AttributeKey.Type.TYPE_INT:
            return f.CAST(column(attr_key.name[len("sentry.") :]), "Int64", alias=alias)
        if (
            attr_key.type == AttributeKey.Type.TYPE_FLOAT
            or attr_key.type == AttributeKey.Type.TYPE_DOUBLE
        ):
            return f.CAST(
                column(attr_key.name[len("sentry.") :]), "Float64", alias=alias
            )
        raise BadSnubaRPCRequestException(
            f"Attribute {attr_key.name} must be requested as a string, float, or integer, got {attr_key.type}"
        )

    if attr_key.name in NORMALIZED_COLUMNS:
        # the second if statement allows Sentry to send TYPE_FLOAT to Snuba when Snuba still has to be backward compatible with TYPE_FLOATS
        if NORMALIZED_COLUMNS[attr_key.name] == attr_key.type or (
            attr_key.type == AttributeKey.Type.TYPE_FLOAT
            and NORMALIZED_COLUMNS[attr_key.name] == AttributeKey.Type.TYPE_DOUBLE
        ):
            return column(attr_key.name[len("sentry.") :], alias=attr_key.name)
        raise BadSnubaRPCRequestException(
            f"Attribute {attr_key.name} must be requested as {NORMALIZED_COLUMNS[attr_key.name]}, got {attr_key.type}"
        )

    # End of special handling, just send to the appropriate bucket
    if attr_key.type == AttributeKey.Type.TYPE_STRING:
        return SubscriptableReference(
            alias=alias, column=column("attr_str"), key=literal(attr_key.name)
        )
    if (
        attr_key.type == AttributeKey.Type.TYPE_FLOAT
        or attr_key.type == AttributeKey.Type.TYPE_DOUBLE
    ):
        return SubscriptableReference(
            alias=alias, column=column("attr_num"), key=literal(attr_key.name)
        )
    if attr_key.type == AttributeKey.Type.TYPE_INT:
        return f.CAST(
            SubscriptableReference(
                alias=None, column=column("attr_num"), key=literal(attr_key.name)
            ),
            "Nullable(Int64)",
            alias=alias,
        )
    if attr_key.type == AttributeKey.Type.TYPE_BOOLEAN:
        return f.CAST(
            SubscriptableReference(
                alias=None,
                column=column("attr_num"),
                key=literal(attr_key.name),
            ),
            "Nullable(Boolean)",
            alias=alias,
        )
    raise BadSnubaRPCRequestException(
        f"Attribute {attr_key.name} had an unknown or unset type: {attr_key.type}"
    )


def apply_virtual_columns(
    query: Query, virtual_column_contexts: Sequence[VirtualColumnContext]
) -> None:
    """Injects virtual column mappings into the clickhouse query. Works with NORMALIZED_COLUMNS on the table or
    dynamic columns in attr_str

    attr_num not supported because mapping on floats is a bad idea

    Example:

        SELECT
          project_name AS `project_name`,
          attr_str['release'] AS `release`,
          attr_str['sentry.sdk.name'] AS `sentry.sdk.name`,
        ... rest of query

        contexts:
            [   {from_column_name: project_id, to_column_name: project_name, value_map: {1: "sentry", 2: "snuba"}} ]


        Query will be transformed into:

        SELECT
        -- see the project name column transformed and the value mapping injected
          transform( CAST( project_id, 'String'), array( '1', '2'), array( 'sentry', 'snuba'), 'unknown') AS `project_name`,
        --
          attr_str['release'] AS `release`,
          attr_str['sentry.sdk.name'] AS `sentry.sdk.name`,
        ... rest of query

    """

    if not virtual_column_contexts:
        return

    mapped_column_to_context = {c.to_column_name: c for c in virtual_column_contexts}

    def transform_expressions(expression: Expression) -> Expression:
        # virtual columns will show up as `attr_str[virtual_column_name]` or `attr_num[virtual_column_name]`
        if not isinstance(expression, SubscriptableReference):
            return expression

        if expression.column.column_name != "attr_str":
            return expression
        context = mapped_column_to_context.get(str(expression.key.value))
        if context:
            attribute_expression = attribute_key_to_expression(
                AttributeKey(
                    name=context.from_column_name,
                    type=NORMALIZED_COLUMNS.get(
                        context.from_column_name, AttributeKey.TYPE_STRING
                    ),
                )
            )
            return f.transform(
                f.CAST(attribute_expression, "String"),
                literals_array(None, [literal(k) for k in context.value_map.keys()]),
                literals_array(None, [literal(v) for v in context.value_map.values()]),
                literal(
                    context.default_value if context.default_value != "" else "unknown"
                ),
                alias=context.to_column_name,
            )

        return expression

    query.transform_expressions(transform_expressions)
