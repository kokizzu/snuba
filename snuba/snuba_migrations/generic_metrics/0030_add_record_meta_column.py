from typing import Sequence

from snuba.clickhouse.columns import Column, UInt
from snuba.clusters.storage_sets import StorageSetKey
from snuba.migrations import migration, operations
from snuba.migrations.columns import MigrationModifiers as Modifiers


class Migration(migration.ClickhouseNodeMigration):
    blocking = False
    local_table_name = "generic_metric_counters_raw_local"
    storage_set_key = StorageSetKey.GENERIC_METRICS_COUNTERS

    def forwards_ops(self) -> Sequence[operations.SqlOperation]:
        return [
            operations.AddColumn(
                storage_set=self.storage_set_key,
                table_name=self.local_table_name,
                column=Column("record_meta", UInt(8, Modifiers(default=str("0")))),
                target=operations.OperationTarget.LOCAL,
                after="materialization_version",
            ),
        ]

    def backwards_ops(self) -> Sequence[operations.SqlOperation]:
        return [
            operations.DropColumn(
                column_name="record_meta",
                storage_set=self.storage_set_key,
                table_name=self.local_table_name,
                target=operations.OperationTarget.LOCAL,
            ),
        ]
