"""ClickHouse TPC-DS loader."""
from __future__ import annotations

import os
import sys
import time

import clickhouse_connect

sys.path.insert(0, "/bench/tpcds")
from schema import ddl_clickhouse, table_names  # noqa: E402


CH_HOST = os.environ.get("CH_HOST", "bench-clickhouse")
CH_PORT = int(os.environ.get("CH_PORT", "8123"))
# Dedicated database: TPC-H and TPC-DS both define `customer` (with different
# columns), so the shared default database would let one suite clobber the
# other. The query runner (runners.py CH_DATABASE) connects to the same one.
CH_DATABASE = "tpcds"


def _client():
    bootstrap = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT)
    bootstrap.command(f"CREATE DATABASE IF NOT EXISTS {CH_DATABASE}")
    bootstrap.close()
    return clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT, database=CH_DATABASE)


def _ch_file_path(data_dir: str, table: str) -> str:
    rel = data_dir.removeprefix("/data/").strip("/")
    return f"data/{rel}/{table}.parquet"


def load(data_dir: str) -> dict:
    client = _client()
    t0 = time.perf_counter()
    rows = 0
    names = table_names(data_dir)
    for table in reversed(names):
        client.command(f"DROP TABLE IF EXISTS {table}")
    for table in names:
        client.command(ddl_clickhouse(data_dir, table))
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
        parameters={"tables": tuple(names)},
    ).result_rows[0][0]
    return {"rows": rows, "load_seconds": elapsed, "size_bytes": int(size or 0)}

