import pytest

from snuba.admin.production_queries import prod_queries
from snuba.datasets.factory import get_dataset
from snuba.query.exceptions import InvalidQueryException


def test_validate_projects_with_subquery() -> None:
    # make sure that a subquery does not raise an exception when it specifies project ids
    # the larger query does not specify the project id and this is still considered valid because the subquery filters
    # on project_id = 1
    query = """MATCH {MATCH (events) SELECT time, group_id, count() AS event_count BY time, group_id WHERE timestamp >= toDateTime('2023-11-20T16:02:34.565803') AND timestamp < toDateTime('2023-11-27T16:02:34.565803') AND project_id=1 HAVING event_count > 1 ORDER BY time ASC GRANULARITY 3600} SELECT quantiles(90)(event_count) BY group_id WHERE timestamp >= toDateTime('2023-11-20T16:02:34.565803') AND timestamp < toDateTime('2023-11-27T16:02:34.565803')"""
    prod_queries._validate_projects_in_query(
        body={"query": query, "dataset": "events"},
        dataset=get_dataset("events"),
        is_mql=False,
    )


def test_disallowed_project_ids() -> None:
    # project_id 42069 is not an allowed project id, make sure we don't allow the query through
    query = """MATCH (events) SELECT time, group_id, count() AS event_count BY time, group_id WHERE timestamp >= toDateTime('2023-11-20T16:02:34.565803') AND timestamp < toDateTime('2023-11-27T16:02:34.565803') AND project_id=42069 HAVING event_count > 1 ORDER BY time ASC GRANULARITY 3600"""
    with pytest.raises(InvalidQueryException):
        prod_queries._validate_projects_in_query(
            body={"query": query, "dataset": "events"},
            dataset=get_dataset("events"),
            is_mql=False,
        )


def test_with_joins() -> None:
    query = """MATCH (si: search_issues) -[attributes]-> (g: group_attributes) SELECT g.group_id, ifNull(multiply(toUInt64(max(si.timestamp)), 1000), 0) AS `score` BY g.group_id WHERE si.project_id IN array(1) AND g.project_id IN array(1) AND si.timestamp >= toDateTime('2024-06-17T22:43:14.617430') AND si.timestamp < toDateTime('2024-06-24T22:43:14.617430') AND g.group_id IN array(5001473500) AND si.project_id=1 AND g.project_id=1"""
    prod_queries._validate_projects_in_query(
        body={"query": query, "dataset": "events"},
        dataset=get_dataset("events"),
        is_mql=False,
    )


def test_fail_with_joins() -> None:
    query = """MATCH (si: search_issues) -[attributes]-> (g: group_attributes) SELECT g.group_id, ifNull(multiply(toUInt64(max(si.timestamp)), 1000), 0) AS `score` BY g.group_id WHERE si.project_id IN array(42069) AND g.project_id IN array(42069) AND si.timestamp >= toDateTime('2024-06-17T22:43:14.617430') AND si.timestamp < toDateTime('2024-06-24T22:43:14.617430') AND g.group_id IN array(5001473500) AND si.project_id=1 AND g.project_id=1"""
    with pytest.raises(InvalidQueryException):
        prod_queries._validate_projects_in_query(
            body={"query": query, "dataset": "events"},
            dataset=get_dataset("events"),
            is_mql=False,
        )
