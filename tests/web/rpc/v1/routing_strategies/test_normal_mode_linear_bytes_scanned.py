from unittest.mock import MagicMock, patch

import pytest
from sentry_protos.snuba.v1.downsampled_storage_pb2 import DownsampledStorageConfig
from sentry_protos.snuba.v1.endpoint_time_series_pb2 import TimeSeriesRequest
from sentry_protos.snuba.v1.request_common_pb2 import RequestMeta

from snuba import state
from snuba.downsampled_storage_tiers import Tier
from snuba.query.query_settings import HTTPQuerySettings
from snuba.utils.metrics.timer import Timer
from snuba.web import QueryResult
from snuba.web.rpc.v1.resolvers.R_eap_items.resolver_time_series import build_query
from snuba.web.rpc.v1.resolvers.R_eap_items.storage_routing.routing_strategies.normal_mode_linear_bytes_scanned import (
    NormalModeLinearBytesScannedRoutingStrategy,
)
from snuba.web.rpc.v1.resolvers.R_eap_items.storage_routing.routing_strategies.storage_routing import (
    RoutingContext,
)


@pytest.mark.redis_db
@pytest.mark.parametrize(
    "most_downsampled_query_bytes_scanned, bytes_scanned_limit, expected_tier, expected_estimated_bytes_scanned",
    [
        (100, 200, Tier.TIER_64, 100),
        (100, 900, Tier.TIER_8, 800),
        (100, 6500, Tier.TIER_1, 6400),
        (100, 51300, Tier.TIER_1, 6400),
    ],
)
def test_get_target_tier(
    most_downsampled_query_bytes_scanned: int,
    bytes_scanned_limit: int,
    expected_tier: Tier,
    expected_estimated_bytes_scanned: int,
) -> None:
    timer = MagicMock(spec=Timer)
    strategy = NormalModeLinearBytesScannedRoutingStrategy()
    context = RoutingContext(MagicMock(), timer, MagicMock(), MagicMock())

    state.set_config(
        "NormalModeLinearBytesScannedRoutingStrategy_bytes_scanned_per_query_limit",
        bytes_scanned_limit,
    )
    target_tier = strategy._get_target_tier(
        most_downsampled_tier_query_result=QueryResult(result={"profile": {"progress_bytes": most_downsampled_query_bytes_scanned}}, extra={}),  # type: ignore
        routing_context=context,
    )
    assert target_tier == expected_tier
    assert (
        context.extra_info["estimated_target_tier_bytes_scanned"]
        == expected_estimated_bytes_scanned
    )


@pytest.mark.redis_db
def test_most_downsampled_tier_query_bytes_scanned_exceeds_limit() -> None:
    routing_context = RoutingContext(
        in_msg=TimeSeriesRequest(
            meta=RequestMeta(
                downsampled_storage_config=DownsampledStorageConfig(
                    mode=DownsampledStorageConfig.MODE_BEST_EFFORT
                )
            )
        ),
        timer=Timer(name="doesntmatter"),
        build_query=build_query,  # type: ignore
        query_settings=HTTPQuerySettings(),
    )

    state.set_config(
        "NormalModeLinearBytesScannedRoutingStrategy_bytes_scanned_per_query_limit",
        99,
    )

    high_bytes_query_result = QueryResult(
        result={
            "profile": {
                "progress_bytes": 100,
            }
        },
        extra={"stats": {}, "sql": "", "experiments": {}},
    )

    strategy = NormalModeLinearBytesScannedRoutingStrategy()

    with patch.object(
        strategy,
        "_run_query_on_most_downsampled_tier",
        return_value=high_bytes_query_result,
    ):
        tier, _ = strategy._decide_tier_and_query_settings(routing_context)

        assert tier == Tier.TIER_64


@pytest.mark.redis_db
def test_target_tier_is_1_if_most_downsampled_query_bytes_scanned_is_0() -> None:
    timer = MagicMock(spec=Timer)
    strategy = NormalModeLinearBytesScannedRoutingStrategy()
    context = RoutingContext(MagicMock(), timer, MagicMock(), MagicMock())

    state.set_config(
        "NormalModeLinearBytesScannedRoutingStrategy_bytes_scanned_per_query_limit",
        10000,
    )
    target_tier = strategy._get_target_tier(
        most_downsampled_tier_query_result=QueryResult(result={"profile": {"progress_bytes": 0}}, extra={}),  # type: ignore
        routing_context=context,
    )
    assert target_tier == Tier.TIER_1


def test_preflight_and_best_effort_mode_are_normal_mode() -> None:
    timer = MagicMock(spec=Timer)
    strategy = NormalModeLinearBytesScannedRoutingStrategy()
    context = RoutingContext(MagicMock(), timer, MagicMock(), MagicMock())

    context.in_msg.meta.downsampled_storage_config.mode = (
        DownsampledStorageConfig.MODE_PREFLIGHT
    )
    assert strategy._is_normal_mode(context)

    context.in_msg.meta.downsampled_storage_config.mode = (
        DownsampledStorageConfig.MODE_BEST_EFFORT
    )
    assert strategy._is_normal_mode(context)
