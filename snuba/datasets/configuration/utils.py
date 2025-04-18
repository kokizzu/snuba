from __future__ import annotations

from typing import Any, Type, TypedDict

from snuba.clickhouse.columns import (
    Array,
    Column,
    Date,
    DateTime,
    DateTime64,
    Enum,
    Float,
    Nested,
    SchemaModifiers,
    String,
    Tuple,
    UInt,
)
from snuba.query.processors.condition_checkers import ConditionChecker
from snuba.query.processors.physical import ClickhouseQueryProcessor
from snuba.utils.schemas import (
    UUID,
    AggregateFunction,
    Bool,
    ColumnType,
    FixedString,
    Int,
    IPv4,
    IPv6,
    Map,
    SimpleAggregateFunction,
)


class QueryProcessorDefinition(TypedDict):
    processor: str
    args: dict[str, Any]


class MandatoryConditionCheckerDefinition(TypedDict):
    condition: str
    args: dict[str, Any]


def get_query_processors(
    query_processor_objects: list[QueryProcessorDefinition],
) -> list[ClickhouseQueryProcessor]:
    return [
        ClickhouseQueryProcessor.get_from_name(qp["processor"]).from_kwargs(
            **qp.get("args", {})
        )
        for qp in query_processor_objects
    ]


def get_mandatory_condition_checkers(
    mandatory_condition_checkers_objects: list[MandatoryConditionCheckerDefinition],
) -> list[ConditionChecker]:
    return [
        ConditionChecker.get_from_name(mc["condition"]).from_kwargs(
            **mc.get("args", {})
        )
        for mc in mandatory_condition_checkers_objects
    ]


NUMBER_COLUMN_TYPES: dict[str, Any] = {
    "Int": Int,
    "UInt": UInt,
    "Float": Float,
}

SIMPLE_COLUMN_TYPES: dict[str, Type[ColumnType[SchemaModifiers]]] = {
    **NUMBER_COLUMN_TYPES,
    "String": String,
    "DateTime": DateTime,
    "Date": Date,
    "UUID": UUID,
    "IPv4": IPv4,
    "IPv6": IPv6,
    "Bool": Bool,
}


def __parse_simple(
    col: dict[str, Any], modifiers: SchemaModifiers | None
) -> ColumnType[SchemaModifiers]:
    return SIMPLE_COLUMN_TYPES[col["type"]](modifiers)


def __parse_number(
    col: dict[str, Any], modifiers: SchemaModifiers | None
) -> ColumnType[SchemaModifiers]:
    col_type = NUMBER_COLUMN_TYPES[col["type"]](col["args"]["size"], modifiers)
    assert (
        isinstance(col_type, UInt)
        or isinstance(col_type, Float)
        or isinstance(col_type, Int)
    )
    return col_type


def __parse_column_type(col: dict[str, Any]) -> ColumnType[SchemaModifiers]:
    # TODO: Add more of Column/Value types as needed

    column_type: ColumnType[SchemaModifiers] | None = None

    modifiers: SchemaModifiers | None = None
    if "args" in col and "schema_modifiers" in col["args"]:
        modifiers = SchemaModifiers(
            "nullable" in col["args"]["schema_modifiers"],
            "readonly" in col["args"]["schema_modifiers"],
        )
    if col["type"] in NUMBER_COLUMN_TYPES:
        column_type = __parse_number(col, modifiers)
    elif col["type"] in SIMPLE_COLUMN_TYPES:
        column_type = __parse_simple(col, modifiers)
    elif col["type"] == "Nested":
        column_type = Nested(parse_columns(col["args"]["subcolumns"]), modifiers)
    elif col["type"] == "Map":
        column_type = Map(
            __parse_column_type(col["args"]["key"]),
            __parse_column_type(col["args"]["value"]),
            modifiers,
        )
    elif col["type"] == "Array":
        column_type = Array(__parse_column_type(col["args"]["inner_type"]), modifiers)
    elif col["type"] == "Tuple":
        types = [__parse_column_type(typ) for typ in col["args"]["inner_types"]]
        column_type = Tuple(tuple(types), modifiers)
    elif col["type"] == "AggregateFunction":
        column_type = AggregateFunction(
            col["args"]["func"],
            [__parse_column_type(c) for c in col["args"]["arg_types"]],
            modifiers,
        )
    elif col["type"] == "SimpleAggregateFunction":
        column_type = SimpleAggregateFunction(
            col["args"]["func"],
            [__parse_column_type(c) for c in col["args"]["arg_types"]],
            modifiers,
        )
    elif col["type"] == "FixedString":
        column_type = FixedString(col["args"]["length"], modifiers)
    elif col["type"] == "Enum":
        column_type = Enum(col["args"]["values"], modifiers)
    elif col["type"] == "DateTime64":
        column_type = DateTime64(
            precision=col["args"].get("precision", 3),
            timezone=col["args"].get("timezone"),
            modifiers=modifiers,
        )
    assert column_type is not None
    return column_type


def parse_columns(columns: list[dict[str, Any]]) -> list[Column[SchemaModifiers]]:
    return [Column(col["name"], __parse_column_type(col)) for col in columns]
