"""Load TPC-H parquet into each benchmark system."""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, "/bench/tpch")
from schema import data_dir_for_scale, table_names  # noqa: E402


ALL_SYSTEMS = [
    "rvbbit",
    "rvbbit_native",
    "rvbbit_native_forced",
    "rvbbit_duck_auto",
    "rvbbit_duck_forced",
    "rvbbit_duck_hive_forced",
    "rvbbit_duck_vortex_forced",
    "rvbbit_datafusion_forced",
    "rvbbit_datafusion_hive_forced",
    "rvbbit_datafusion_vortex_forced",
    "rvbbit_datafusion_mem_forced",
    "rvbbit_pg_heap_forced",
    "duckdb",
    "clickhouse",
    "pg_baseline",
    "citus",
    "hydra",
    "alloydb",
]


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _variant_refresh_mode() -> str:
    raw = os.environ.get("RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD", "")
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "sync"}:
        return "sync"
    if value in {"async", "background", "bg"}:
        return "async"
    return "off"


def _setting_enabled(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off", "disabled"}


def _selected_systems() -> list[str]:
    selected = os.environ.get("BENCH_SYSTEMS", "")
    if not selected.strip():
        selected = ",".join(ALL_SYSTEMS)
    return [system.strip() for system in selected.split(",") if system.strip()]


def _vortex_forced_selected() -> bool:
    return any(
        system in {"rvbbit_datafusion_vortex_forced", "rvbbit_duck_vortex_forced"}
        for system in _selected_systems()
    )


def _vortex_auto_selected() -> bool:
    return "rvbbit" in _selected_systems() and _setting_enabled(
        os.environ.get("RVBBIT_ROUTE_DUCK_VORTEX"),
        default=True,
    )


def _vortex_layout_requested() -> bool:
    return _setting_enabled(
        os.environ.get("RVBBIT_COMPACT_VORTEX_LAYOUT"),
        default=_vortex_forced_selected() or _vortex_auto_selected(),
    )


def _refresh_variants_inline(refresh_mode: str) -> bool:
    return refresh_mode == "sync" or _vortex_layout_requested()


def _hot_load_after_load() -> bool:
    if _env_enabled("RVBBIT_HOT_LOAD_AFTER_LOAD"):
        return True
    selected = os.environ.get("BENCH_SYSTEMS", "")
    return any(
        system.strip() == "rvbbit_datafusion_mem_forced"
        for system in selected.split(",")
    )


def _start_async_variant_refresh(dsn: str, sql: str, log_path: str) -> int | None:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-c", sql],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return proc.pid
    except OSError as exc:
        print(f"    async variant refresh failed to start: {exc}")
        return None


def _rvbbit_compact_settings_sql() -> list[str]:
    settings = {
        "RVBBIT_COMPACT_VARIANTS_SYNC": "rvbbit.compact_variants_sync",
        "RVBBIT_COMPACT_HIVE_LAYOUT": "rvbbit.compact_hive_layout",
        "RVBBIT_COMPACT_HIVE_KEYS": "rvbbit.compact_hive_keys",
        "RVBBIT_COMPACT_HIVE_VARIANTS": "rvbbit.compact_hive_variants",
        "RVBBIT_COMPACT_HIVE_MIN_DISTINCT": "rvbbit.compact_hive_min_distinct",
        "RVBBIT_COMPACT_HIVE_MAX_DISTINCT": "rvbbit.compact_hive_max_distinct",
        "RVBBIT_COMPACT_VORTEX_LAYOUT": "rvbbit.compact_vortex_layout",
    }
    defaults = {
        "RVBBIT_COMPACT_HIVE_LAYOUT": "on",
        "RVBBIT_COMPACT_VORTEX_LAYOUT": "on" if _vortex_layout_requested() else None,
    }
    out: list[str] = []
    for env_name, guc_name in settings.items():
        value = os.environ.get(env_name, defaults.get(env_name))
        if value is not None and value.strip():
            out.append(f"SET {guc_name} = {_sql_literal(value.strip())}")
    return out


def _rvbbit_hot_settings_sql() -> list[str]:
    settings = {
        "RVBBIT_HOT_STORE_BUDGET_MB": "rvbbit.hot_store_budget_mb",
        "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS": "rvbbit.hot_store_route_max_rows",
    }
    out: list[str] = []
    for env_name, guc_name in settings.items():
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            out.append(f"SET {guc_name} = {_sql_literal(value.strip())}")
    return out


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


def _load_system_name(name: str) -> str:
    if name in {
        "rvbbit_native",
        "rvbbit_native_forced",
        "rvbbit_duck_hot",
        "rvbbit_duck_auto",
        "rvbbit_duck_forced",
        "rvbbit_duck_hive_forced",
        "rvbbit_duck_vortex_forced",
        "rvbbit_datafusion_forced",
        "rvbbit_datafusion_hive_forced",
        "rvbbit_datafusion_vortex_forced",
        "rvbbit_datafusion_mem_forced",
        "rvbbit_pg_heap_forced",
        "rvbbit_pg_heap",
        "pg_heap",
    }:
        return "rvbbit"
    return name


def run_one(name: str, data_dir: str, scale: str) -> dict:
    t0 = time.perf_counter()
    print(f"\n>>> loading {name} (TPC-H sf={scale}) ...")
    try:
        if name == "duckdb":
            from loaders.duckdb_loader import load
            res = load(data_dir, scale)
        elif name == "clickhouse":
            from loaders.clickhouse_loader import load
            res = load(data_dir)
        elif name == "pg_baseline":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
                data_dir,
                post_sql=[f"VACUUM ANALYZE {t}" for t in table_names()],
            )
        elif name == "citus":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-citus:5432/bench",
                data_dir,
                using="columnar",
                pre_sql=["CREATE EXTENSION IF NOT EXISTS citus"],
                post_sql=[f"VACUUM ANALYZE {t}" for t in table_names()],
            )
        elif name == "hydra":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-hydra:5432/bench",
                data_dir,
                using="columnar",
                post_sql=[f"VACUUM ANALYZE {t}" for t in table_names()],
            )
        elif name == "alloydb":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-alloydb:5432/postgres",
                data_dir,
                pre_sql=["CREATE EXTENSION IF NOT EXISTS google_columnar_engine"],
                post_sql=[
                    *[f"VACUUM ANALYZE {t}" for t in table_names()],
                    *[f"SELECT google_columnar_engine_add('{t}')" for t in table_names()],
                    *[f"SELECT google_columnar_engine_refresh('{t}')" for t in table_names()],
                ],
            )
        elif name == "rvbbit":
            from loaders.postgres_loader import load_pg
            dsn = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
            refresh_mode = _variant_refresh_mode()
            compact_sql = [
                *[f"ANALYZE {t}" for t in table_names()],
                *_rvbbit_compact_settings_sql(),
                *_rvbbit_hot_settings_sql(),
                *[
                    "SELECT rvbbit.refresh_acceleration("
                    f"'{t}'::regclass, {str(_refresh_variants_inline(refresh_mode)).lower()})"
                    for t in table_names()
                ],
            ]
            if _hot_load_after_load():
                compact_sql.extend(
                    [f"SELECT rvbbit.hot_load('{t}'::regclass)" for t in table_names()]
                )
            res = load_pg(
                dsn,
                data_dir,
                using="rvbbit",
                post_sql=compact_sql,
            )
            if refresh_mode == "async":
                refresh_sql = "; ".join(
                    [
                        *_rvbbit_compact_settings_sql(),
                        *[
                            f"SELECT rvbbit.refresh_layout_variants('{t}'::regclass)"
                            for t in table_names()
                        ],
                    ]
                )
                pid = _start_async_variant_refresh(
                    dsn,
                    refresh_sql,
                    "/bench/tpch/results/refresh_layout_variants.log",
                )
                res["variant_refresh"] = "async" if pid else "failed-to-start"
                if pid:
                    res["variant_refresh_pid"] = pid
            if _hot_load_after_load():
                res["hot_load"] = "on"
        else:
            return {"name": name, "status": "unknown"}
    except Exception as e:
        wall = time.perf_counter() - t0
        return {"name": name, "status": f"FAIL: {str(e)[:100]}", "wall_s": wall}
    wall = time.perf_counter() - t0
    res["name"] = name
    status_parts = ["ok"]
    if res.get("hot_load") == "on":
        status_parts.append("hot loaded")
    if res.get("variant_refresh") == "async":
        status_parts.append(f"variants async pid={res.get('variant_refresh_pid')}")
    elif res.get("variant_refresh"):
        status_parts.append(f"variants {res['variant_refresh']}")
    res["status"] = "; ".join(status_parts)
    res["wall_s"] = wall
    return res


def main() -> int:
    scale = os.environ.get("TPCH_SCALE", "0.1")
    data_dir = data_dir_for_scale(scale)
    missing = [t for t in table_names() if not os.path.exists(f"{data_dir}/{t}.parquet")]
    if missing:
        print(f"ERROR: missing TPC-H parquet in {data_dir}: {', '.join(missing)}")
        return 1
    selected = os.environ.get("BENCH_SYSTEMS", ",".join(ALL_SYSTEMS)).split(",")
    selected = [s.strip() for s in selected if s.strip()]
    selected = [_load_system_name(s) for s in selected]
    selected = list(dict.fromkeys(selected))
    results = [run_one(s, data_dir, scale) for s in selected]

    print("\n\n=== TPC-H load summary ===")
    print(f"{'system':<14} {'rows':>14} {'load+post (s)':>14} {'wall (s)':>10} {'size':>12}   status")
    print("-" * 94)
    for r in results:
        rows = f"{r.get('rows', 0):,}" if r.get("rows") else "-"
        secs = f"{r.get('load_seconds', 0):.1f}" if r.get("load_seconds") else "-"
        wall = f"{r.get('wall_s', 0):.1f}" if r.get("wall_s") else "-"
        size = _human(r.get("size_bytes"))
        print(f"{r['name']:<14} {rows:>14} {secs:>14} {wall:>10} {size:>12}   {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
