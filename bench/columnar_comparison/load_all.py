"""Drive every per-system loader, capture load time + on-disk size,
and print a summary table.

Usage from inside the bench container:
  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.competitors.yml \\
                 exec bench python /bench/columnar_comparison/load_all.py

Set BENCH_SYSTEMS=duckdb,clickhouse,pg_baseline to limit the run.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, "/bench/columnar_comparison")

DATA_DIR = "/data"

# Order matters for the printed table.
ALL_SYSTEMS = [
    "duckdb",
    "clickhouse",
    "pg_baseline",
    "citus",
    "hydra",
    "alloydb",
    "rvbbit",
]


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def run_one(name: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    print(f"\n>>> loading {name} ...")
    try:
        if name == "duckdb":
            from loaders.duckdb_loader import load
            res = load(DATA_DIR)
        elif name == "clickhouse":
            from loaders.clickhouse_loader import load
            res = load(DATA_DIR)
        elif name == "pg_baseline":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
                DATA_DIR,
                post_sql=["VACUUM ANALYZE trips"],
            )
        elif name == "citus":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-citus:5432/bench",
                DATA_DIR,
                using="columnar",
                pre_sql=["CREATE EXTENSION IF NOT EXISTS citus"],
                post_sql=["VACUUM ANALYZE trips"],
            )
        elif name == "hydra":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-hydra:5432/bench",
                DATA_DIR,
                using="columnar",
                post_sql=["VACUUM ANALYZE trips"],
            )
        elif name == "alloydb":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-alloydb:5432/postgres",
                DATA_DIR,
                pre_sql=[
                    "CREATE EXTENSION IF NOT EXISTS google_columnar_engine",
                ],
                post_sql=[
                    "VACUUM ANALYZE trips",
                    "SELECT google_columnar_engine_add('trips')",
                    "SELECT google_columnar_engine_refresh('trips')",
                ],
            )
        elif name == "rvbbit":
            from loaders.postgres_loader import load_pg
            # COPY into the heap catcher, then export_to_parquet to flip the
            # table onto the columnar read path. The reported load_seconds
            # is the COPY phase only; total wall (load_seconds + compact)
            # is in the printed log.
            res = load_pg(
                "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
                DATA_DIR,
                using="rvbbit",
                post_sql=[
                    "SELECT rvbbit.export_to_parquet('trips'::regclass)",
                    "VACUUM ANALYZE trips",
                ],
            )
        else:
            return {"name": name, "status": "unknown system"}
    except Exception as e:
        wall = time.perf_counter() - t0
        return {"name": name, "status": f"FAIL: {e}", "wall_s": wall}
    res["name"] = name
    res["status"] = "ok"
    return res


def main() -> int:
    selected = os.environ.get("BENCH_SYSTEMS", ",".join(ALL_SYSTEMS)).split(",")
    selected = [s.strip() for s in selected if s.strip()]
    results = [run_one(s) for s in selected]

    print("\n\n=== load summary ===")
    print(f"{'system':<14} {'rows':>12} {'load (s)':>10} {'size':>12}   status")
    print("-" * 70)
    for r in results:
        rows = f"{r.get('rows', 0):,}" if r.get("rows") else "-"
        secs = f"{r.get('load_seconds', 0):.1f}" if r.get("load_seconds") else "-"
        size = _human(r.get("size_bytes"))
        print(f"{r['name']:<14} {rows:>12} {secs:>10} {size:>12}   {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
