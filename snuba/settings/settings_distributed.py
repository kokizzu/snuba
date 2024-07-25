import os

CLUSTERS = [
    {
        "host": os.environ.get("CLICKHOUSE_HOST", "127.0.0.1"),
        "port": int(os.environ.get("CLICKHOUSE_PORT", 9000)),
        "max_connections": int(os.environ.get("CLICKHOUSE_MAX_CONNECTIONS", 1)),
        "block_connections": bool(
            os.environ.get("CLICKHOUSE_BLOCK_CONNECTIONS", False)
        ),
        "user": os.environ.get("CLICKHOUSE_USER", "default"),
        "password": os.environ.get("CLICKHOUSE_PASSWORD", ""),
        "database": os.environ.get("CLICKHOUSE_DATABASE", "default"),
        "http_port": int(os.environ.get("CLICKHOUSE_HTTP_PORT", 8123)),
        "storage_sets": {
            "cdc",
            "discover",
            "events",
            "events_ro",
            "metrics",
            "migrations",
            "outcomes",
            "querylog",
            "sessions",
            "transactions",
            "profiles",
            "functions",
            "replays",
            "generic_metrics_sets",
            "generic_metrics_distributions",
            "search_issues",
            "generic_metrics_counters",
            "spans",
            "events_analytics_platform",
            "group_attributes",
            "generic_metrics_gauges",
            "metrics_summaries",
            "profile_chunks",
        },
        "single_node": False,
        "cluster_name": "cluster_one_sh",
        "distributed_cluster_name": "cluster_one_sh",
    },
]
