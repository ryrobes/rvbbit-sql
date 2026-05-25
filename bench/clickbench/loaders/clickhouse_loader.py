"""ClickHouse loader — native parquet via file().

ClickHouse only allows file() under user_files_path. Our compose
mounts the data dir there.
"""
from __future__ import annotations

import os
import time

import clickhouse_connect

import sys
sys.path.insert(0, "/bench/clickbench")
from schema import ddl_clickhouse  # noqa: E402

CH_HOST = os.environ.get("CH_HOST", "bench-clickhouse")
CH_PORT = int(os.environ.get("CH_HTTP_PORT", "8123"))


def load(data_path: str, limit: int | None = None) -> dict:
    # ClickHouse only resolves file() under user_files_path. Our compose
    # mounts the shared data dir at /var/lib/clickhouse/user_files/data,
    # so file('data/hits.parquet') maps to /data/hits.parquet in the
    # bench container.
    ch_relative = "data/" + os.path.basename(data_path)
    client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT)
    client.command("DROP TABLE IF EXISTS hits")
    client.command(ddl_clickhouse("hits"))
    where_limit = f" LIMIT {limit}" if limit else ""
    t0 = time.perf_counter()
    client.command(
        f"INSERT INTO hits SELECT * FROM file('{ch_relative}', 'Parquet'){where_limit}"
    )
    elapsed = time.perf_counter() - t0
    n = client.query("SELECT count(*) FROM hits").result_rows[0][0]
    size = client.query(
        "SELECT sum(bytes_on_disk) FROM system.parts "
        "WHERE table='hits' AND active"
    ).result_rows[0][0]
    return {"rows": int(n), "load_seconds": elapsed, "size_bytes": int(size or 0)}
