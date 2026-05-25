"""ClickHouse loader — reads parquet natively via INSERT FROM file().

Connects to bench-clickhouse:9000 over the native protocol via the
HTTP client (clickhouse-connect) — simpler than the binary native
client, fine for bulk loads since the actual data path stays inside
the ClickHouse server reading /data.
"""
from __future__ import annotations

import glob
import os
import time

import clickhouse_connect

import sys
sys.path.insert(0, "/bench/columnar_comparison")
from schema import ddl_clickhouse  # noqa: E402

CH_HOST = os.environ.get("CH_HOST", "bench-clickhouse")
CH_PORT = int(os.environ.get("CH_HTTP_PORT", "8123"))


def _connect():
    return clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT)


def load(data_dir: str) -> dict:
    parquet_glob = os.path.join(data_dir, "yellow_tripdata_*.parquet")
    files = sorted(glob.glob(parquet_glob))
    if not files:
        raise FileNotFoundError(f"no parquet files in {data_dir}")

    client = _connect()
    client.command("DROP TABLE IF EXISTS trips")
    client.command(ddl_clickhouse("trips"))

    # Map parquet column names to our table column order.
    # ClickHouse will read the parquet schema and select-by-name into trips.
    col_rename = {
        "VendorID": "vendor_id",
        "RatecodeID": "ratecode_id",
        "PULocationID": "pu_location_id",
        "DOLocationID": "do_location_id",
    }
    select_cols = []
    parquet_cols = [
        "VendorID", "tpep_pickup_datetime", "tpep_dropoff_datetime",
        "passenger_count", "trip_distance", "RatecodeID",
        "store_and_fwd_flag", "PULocationID", "DOLocationID",
        "payment_type", "fare_amount", "extra", "mta_tax", "tip_amount",
        "tolls_amount", "improvement_surcharge", "total_amount",
        "congestion_surcharge", "airport_fee",
    ]
    for pc in parquet_cols:
        ours = col_rename.get(pc, pc)
        select_cols.append(f"{pc} AS {ours}")
    sel = ", ".join(select_cols)

    t0 = time.perf_counter()
    # ClickHouse only allows file() under user_files_path; our compose
    # mounts /data → /var/lib/clickhouse/user_files/data and file() paths
    # are relative to user_files_path.
    client.command(
        f"INSERT INTO trips SELECT {sel} "
        f"FROM file('data/yellow_tripdata_*.parquet', 'Parquet')"
    )
    elapsed = time.perf_counter() - t0

    n = client.query("SELECT count(*) FROM trips").result_rows[0][0]
    # On-disk size from system.parts.
    size_b = client.query(
        "SELECT sum(bytes_on_disk) FROM system.parts "
        "WHERE table='trips' AND active"
    ).result_rows[0][0]
    return {"rows": int(n), "load_seconds": elapsed, "size_bytes": int(size_b or 0)}
