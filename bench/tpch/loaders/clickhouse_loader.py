"""ClickHouse TPC-H loader."""
from __future__ import annotations

import os
import sys
import time

import clickhouse_connect

sys.path.insert(0, "/bench/tpch")
from schema import ddl_clickhouse, table_names  # noqa: E402


CH_HOST = os.environ.get("CH_HOST", "bench-clickhouse")
CH_PORT = int(os.environ.get("CH_PORT", "8123"))


def _client():
    return clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT)


def _ch_file_path(data_dir: str, table: str) -> str:
    # /data in the bench container is mounted as
    # /var/lib/clickhouse/user_files/data in the ClickHouse container.
    rel = data_dir.removeprefix("/data/").strip("/")
    return f"data/{rel}/{table}.parquet"


def load(data_dir: str) -> dict:
    client = _client()
    t0 = time.perf_counter()
    rows = 0
    for table in reversed(table_names()):
        client.command(f"DROP TABLE IF EXISTS {table}")
    for table in table_names():
        client.command(ddl_clickhouse(table))
        client.command(
            f"INSERT INTO {table} SELECT * FROM file('{_ch_file_path(data_dir, table)}', Parquet)"
        )
        rows += client.query(f"SELECT count() FROM {table}").result_rows[0][0]
    elapsed = time.perf_counter() - t0
    size = client.query(
        """
        SELECT coalesce(sum(bytes_on_disk), 0)
        FROM system.parts
        WHERE active AND database = currentDatabase() AND table IN %(tables)s
        """,
        parameters={"tables": tuple(table_names())},
    ).result_rows[0][0]
    return {"rows": rows, "load_seconds": elapsed, "size_bytes": int(size or 0)}
