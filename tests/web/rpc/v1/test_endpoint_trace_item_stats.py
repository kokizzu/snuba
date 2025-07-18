from datetime import timedelta
from typing import Any

import pytest
from sentry_protos.snuba.v1.downsampled_storage_pb2 import DownsampledStorageConfig
from sentry_protos.snuba.v1.endpoint_trace_item_stats_pb2 import (
    AttributeDistribution,
    AttributeDistributionsRequest,
    StatsType,
    TraceItemStatsRequest,
)
from sentry_protos.snuba.v1.error_pb2 import Error as ErrorProto
from sentry_protos.snuba.v1.request_common_pb2 import RequestMeta, TraceItemType
from sentry_protos.snuba.v1.trace_item_attribute_pb2 import AttributeKey, AttributeValue
from sentry_protos.snuba.v1.trace_item_filter_pb2 import (
    ComparisonFilter,
    TraceItemFilter,
)
from sentry_protos.snuba.v1.trace_item_pb2 import AnyValue

from snuba.datasets.storages.factory import get_storage
from snuba.datasets.storages.storage_key import StorageKey
from snuba.web.rpc.v1.endpoint_trace_item_stats import EndpointTraceItemStats
from tests.base import BaseApiTest
from tests.helpers import write_raw_unprocessed_events
from tests.web.rpc.v1.test_utils import (
    BASE_TIME,
    END_TIMESTAMP,
    START_TIMESTAMP,
    gen_item_message,
)


@pytest.fixture(autouse=False)
def setup_teardown(clickhouse_db: None, redis_db: None) -> None:
    items_storage = get_storage(StorageKey("eap_items"))
    messages = [
        gen_item_message(
            start_timestamp=BASE_TIME - timedelta(minutes=i),
            attributes={
                "low_cardinality": AnyValue(string_value=f"{i // 40}"),
                "sentry.sdk.name": AnyValue(string_value="sentry.python.django"),
            },
            remove_default_attributes=True,
        )
        for i in range(120)
    ]
    write_raw_unprocessed_events(items_storage, messages)  # type: ignore


@pytest.mark.clickhouse_db
@pytest.mark.redis_db
class TestTraceItemAttributesStats(BaseApiTest):
    def test_basic(self) -> None:
        message = TraceItemStatsRequest(
            meta=RequestMeta(
                project_ids=[1],
                organization_id=1,
                cogs_category="something",
                referrer="something",
                start_timestamp=START_TIMESTAMP,
                end_timestamp=END_TIMESTAMP,
                trace_item_type=TraceItemType.TRACE_ITEM_TYPE_SPAN,
            ),
            stats_types=[
                StatsType(
                    attribute_distributions=AttributeDistributionsRequest(
                        max_buckets=10,
                        max_attributes=100,
                    )
                )
            ],
        )

        response = self.app.post(
            "/rpc/EndpointTraceItemStats/v1", data=message.SerializeToString()
        )
        error_proto = ErrorProto()
        if response.status_code != 200:
            error_proto.ParseFromString(response.data)
        assert response.status_code == 200, error_proto

    def test_basic_with_data(self, setup_teardown: Any) -> None:
        message = TraceItemStatsRequest(
            meta=RequestMeta(
                project_ids=[1],
                organization_id=1,
                cogs_category="something",
                referrer="something",
                start_timestamp=START_TIMESTAMP,
                end_timestamp=END_TIMESTAMP,
                trace_item_type=TraceItemType.TRACE_ITEM_TYPE_SPAN,
                downsampled_storage_config=DownsampledStorageConfig(
                    mode=DownsampledStorageConfig.MODE_HIGHEST_ACCURACY,
                ),
            ),
            stats_types=[
                StatsType(
                    attribute_distributions=AttributeDistributionsRequest(
                        max_buckets=10,
                        max_attributes=100,
                    )
                )
            ],
        )
        response = EndpointTraceItemStats().execute(message)
        expected_sdk_name_stats = AttributeDistribution(
            attribute_name="sentry.sdk.name",
            buckets=[
                AttributeDistribution.Bucket(
                    label="sentry.python.django",
                    value=120,
                )
            ],
        )

        assert response.results[0].HasField("attribute_distributions")
        assert (
            expected_sdk_name_stats
            in response.results[0].attribute_distributions.attributes
        )

        expected_low_cardinality_stat = AttributeDistribution(
            attribute_name="low_cardinality",
            buckets=[
                AttributeDistribution.Bucket(label="0", value=40),
                AttributeDistribution.Bucket(label="1", value=40),
                AttributeDistribution.Bucket(label="2", value=40),
            ],
        )

        match = False
        for stat in response.results[0].attribute_distributions.attributes:
            if stat.attribute_name == "low_cardinality":
                for bucket in expected_low_cardinality_stat.buckets:
                    match = True
                    assert bucket in stat.buckets

        assert match

    def test_with_filter(self, setup_teardown: Any) -> None:
        message = TraceItemStatsRequest(
            meta=RequestMeta(
                project_ids=[1, 2, 3],
                organization_id=1,
                cogs_category="something",
                referrer="something",
                start_timestamp=START_TIMESTAMP,
                end_timestamp=END_TIMESTAMP,
                trace_item_type=TraceItemType.TRACE_ITEM_TYPE_SPAN,
                downsampled_storage_config=DownsampledStorageConfig(
                    mode=DownsampledStorageConfig.MODE_HIGHEST_ACCURACY,
                ),
            ),
            filter=TraceItemFilter(
                comparison_filter=ComparisonFilter(
                    key=AttributeKey(
                        type=AttributeKey.TYPE_STRING, name="low_cardinality"
                    ),
                    op=ComparisonFilter.OP_EQUALS,
                    value=AttributeValue(val_str="0"),
                )
            ),
            stats_types=[
                StatsType(
                    attribute_distributions=AttributeDistributionsRequest(
                        max_buckets=10,
                        max_attributes=100,
                    )
                )
            ],
        )
        response = EndpointTraceItemStats().execute(message)
        expected_sdk_name_stats = AttributeDistribution(
            attribute_name="sentry.sdk.name",
            buckets=[
                AttributeDistribution.Bucket(label="sentry.python.django", value=40)
            ],
        )

        assert response.results[0].HasField("attribute_distributions")
        assert (
            expected_sdk_name_stats
            in response.results[0].attribute_distributions.attributes
        )

        expected_low_cardinality_stats = AttributeDistribution(
            attribute_name="low_cardinality",
            buckets=[AttributeDistribution.Bucket(label="0", value=40)],
        )

        assert (
            expected_low_cardinality_stats
            in response.results[0].attribute_distributions.attributes
        )
