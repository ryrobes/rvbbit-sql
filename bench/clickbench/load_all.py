"""Load ClickBench hits.parquet into every system, report sizes + times.

Env:
  BENCH_LIMIT     Cap row count for ALL systems (default: full table)
  BENCH_SYSTEMS   Comma list (default: rvbbit,duckdb,clickhouse,pg_baseline,citus,hydra,alloydb)
  RVBBIT_ACCEL_IDENTITY_MAP
                  primary_key (default), on/all, ctid, or off. Use on/all
                  to build CTID overlay maps for no-PK mutable tables.
  RVBBIT_COMPACT_SCAN_CHUNK_ROWS
                  Canonical row-group size. Lower values create more files and
                  allow writer-thread overlap.
  RVBBIT_COMPACT_WRITER_THREADS
                  Bounded Parquet writer threads for completed canonical chunks.
  RVBBIT_DIRECT_ACCEL_LOAD
                  Load heap via COPY but build canonical accelerator files from
                  source parquet chunks instead of rescanning the heap.
  RVBBIT_DIRECT_ACCEL_CHUNK_ROWS
                  Source parquet chunk size for direct accelerator import.
  RVBBIT_DIRECT_ACCEL_STAGING_MODE
                  single_pass (default) writes one source parquet with row
                  groups; source imports the original parquet directly;
                  offset_chunks preserves the older chunk loop.
  RVBBIT_DIRECT_ACCEL_METADATA_PROFILE
                  rich (default) or minimal. Minimal skips canonical side
                  metadata and Parquet bloom filters for faster bulk import.
  RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD
                  off (default), sync, or async. Async defers Hive/Vortex
                  variant builds until after the canonical accelerator is ready.

Run from inside the bench container.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, "/bench/clickbench")

DATA_PATH = "/data/hits.parquet"

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
    if refresh_mode == "sync":
        return True
    if refresh_mode == "async":
        return False
    return _vortex_layout_requested()


def _hot_load_after_load() -> bool:
    if _env_enabled("RVBBIT_HOT_LOAD_AFTER_LOAD"):
        return True
    selected = os.environ.get("BENCH_SYSTEMS", "")
    return any(
        system.strip() == "rvbbit_datafusion_mem_forced"
        for system in selected.split(",")
    )


def _direct_accel_load() -> bool:
    return _env_enabled("RVBBIT_DIRECT_ACCEL_LOAD")


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
        "RVBBIT_COMPACT_SCAN_CHUNK_ROWS": "rvbbit.compact_scan_chunk_rows",
        "RVBBIT_COMPACT_WRITER_THREADS": "rvbbit.compact_writer_threads",
        "RVBBIT_COMPACT_METADATA_PROFILE": "rvbbit.compact_metadata_profile",
        "RVBBIT_DIRECT_ACCEL_METADATA_PROFILE": "rvbbit.direct_accel_metadata_profile",
        "RVBBIT_COMPACT_TEXT_STATS": "rvbbit.compact_text_stats",
        "RVBBIT_COMPACT_PER_GROUP_STATS": "rvbbit.compact_per_group_stats",
        "RVBBIT_COMPACT_VALUE_BITMAPS": "rvbbit.compact_value_bitmaps",
        "RVBBIT_COMPACT_TEXT_DICTIONARIES": "rvbbit.compact_text_dictionaries",
        "RVBBIT_PARQUET_BLOOM": "rvbbit.parquet_bloom",
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


def _rvbbit_accel_settings_sql() -> list[str]:
    settings = {
        "RVBBIT_ACCEL_IDENTITY_MAP": "rvbbit.accel_identity_map",
        "RVBBIT_ACCEL_IDENTITY_BATCH_ROWS": "rvbbit.accel_identity_batch_rows",
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


def run_one(name: str, limit: int | None) -> dict:
    t0 = time.perf_counter()
    print(f"\n>>> loading {name} (limit={limit}) ...")
    try:
        if name == "duckdb":
            from loaders.duckdb_loader import load
            res = load(DATA_PATH, limit)
        elif name == "clickhouse":
            from loaders.clickhouse_loader import load
            res = load(DATA_PATH, limit)
        elif name == "pg_baseline":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
                DATA_PATH,
                limit,
                post_sql=["VACUUM ANALYZE hits"],
            )
        elif name == "citus":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-citus:5432/bench",
                DATA_PATH,
                limit,
                using="columnar",
                pre_sql=["CREATE EXTENSION IF NOT EXISTS citus"],
                post_sql=["VACUUM ANALYZE hits"],
            )
        elif name == "hydra":
            from loaders.postgres_loader import load_pg
            res = load_pg(
                "postgresql://postgres:bench@bench-hydra:5432/bench",
                DATA_PATH,
                limit,
                using="columnar",
                post_sql=["VACUUM ANALYZE hits"],
            )
        elif name == "alloydb":
            from loaders.postgres_loader import load_pg
            # Engine itself is enabled at startup via compose `command:`
            # (POSTMASTER GUC); the loader just registers the relation +
            # populates the in-memory columnar copy.
            res = load_pg(
                "postgresql://postgres:bench@bench-alloydb:5432/postgres",
                DATA_PATH,
                limit,
                pre_sql=["CREATE EXTENSION IF NOT EXISTS google_columnar_engine"],
                post_sql=[
                    "VACUUM ANALYZE hits",
                    "SELECT google_columnar_engine_add('hits')",
                    # Force-populate so the first benchmark query doesn't
                    # pay the in-memory copy cost.
                    "SELECT google_columnar_engine_refresh('hits')",
                ],
            )
        elif name == "rvbbit":
            from loaders.postgres_loader import load_pg
            dsn = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
            refresh_mode = _variant_refresh_mode()
            direct_accel = _direct_accel_load()
            compact_sql = [
                "ANALYZE hits",
                *_rvbbit_compact_settings_sql(),
                *_rvbbit_hot_settings_sql(),
                *_rvbbit_accel_settings_sql(),
            ]
            if not direct_accel:
                compact_sql.append(
                    "SELECT rvbbit.refresh_acceleration("
                    f"'hits'::regclass, {str(_refresh_variants_inline(refresh_mode)).lower()})"
                )
            final_sql = []
            if _hot_load_after_load():
                final_sql.append("SELECT rvbbit.hot_load('hits'::regclass)")
            res = load_pg(
                dsn,
                DATA_PATH,
                limit,
                using="rvbbit",
                post_sql=compact_sql,
                final_sql=final_sql,
                direct_accel=direct_accel,
                direct_accel_refresh_variants=_refresh_variants_inline(refresh_mode),
            )
            if direct_accel:
                res["direct_accel"] = "on"
            if refresh_mode == "async":
                refresh_sql = "; ".join(
                    [
                        *_rvbbit_compact_settings_sql(),
                        "SELECT rvbbit.refresh_layout_variants('hits'::regclass)",
                    ]
                )
                pid = _start_async_variant_refresh(
                    dsn,
                    refresh_sql,
                    "/bench/clickbench/results/refresh_layout_variants.log",
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
        return {"name": name, "status": f"FAIL: {str(e)[:80]}", "wall_s": wall}
    wall = time.perf_counter() - t0
    res["name"] = name
    status_parts = ["ok"]
    if res.get("hot_load") == "on":
        status_parts.append("hot loaded")
    if res.get("direct_accel") == "on":
        status_parts.append("direct accel")
    if res.get("variant_refresh") == "async":
        status_parts.append(f"variants async pid={res.get('variant_refresh_pid')}")
    elif res.get("variant_refresh"):
        status_parts.append(f"variants {res['variant_refresh']}")
    res["status"] = "; ".join(status_parts)
    res["wall_s"] = wall
    return res


def _print_rvbbit_phase_summary(results: list[dict]) -> None:
    if not any(r.get("name") == "rvbbit" and r.get("status", "").startswith("ok") for r in results):
        return
    try:
        import psycopg
    except Exception:
        return

    dsn = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM rvbbit.acceleration_operations
                    WHERE table_oid = 'hits'::regclass
                      AND operation = 'refresh_acceleration'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if not row:
                    return
                op_id = row[0]
                cur.execute(
                    """
                    SELECT phase,
                           coalesce(layout, ''),
                           status,
                           coalesce(rows_written, 0),
                           coalesce(files_written, 0),
                           coalesce(bytes_written, 0),
                           extract(epoch FROM finished_at - started_at)::float8,
                           details
                    FROM rvbbit.acceleration_operation_phases
                    WHERE operation_id = %s
                    ORDER BY id
                    """,
                    (op_id,),
                )
                phases = cur.fetchall()
    except Exception as exc:
        print(f"\nrvbbit phase summary unavailable: {str(exc)[:120]}")
        return

    if not phases:
        return
    print("\nrvbbit accelerator phase summary")
    print(f"{'phase':<28} {'layout':<24} {'status':<8} {'rows':>12} {'files':>7} {'bytes':>10} {'seconds':>9}")
    print("-" * 106)
    canonical_timing = None
    import_timing = None
    for phase, layout, status, rows, files, bytes_written, seconds, details in phases:
        seconds_s = f"{seconds:.3f}" if seconds is not None else "-"
        print(
            f"{phase:<28} {layout:<24} {status:<8} "
            f"{int(rows):>12,} {int(files):>7} {_human(int(bytes_written)):>10} {seconds_s:>9}"
        )
        if phase == "canonical_delta_export" and isinstance(details, dict):
            canonical_timing = details.get("canonical_timing")
        elif phase == "canonical_delta_import" and isinstance(details, dict):
            import_timing = details.get("import_timing")
    if canonical_timing:
        _print_canonical_timing_summary(canonical_timing)
    if import_timing:
        _print_import_timing_summary(import_timing)


def _print_canonical_timing_summary(timing: dict) -> None:
    rows = [
        ("export total", timing.get("export_total_seconds")),
        ("setup", timing.get("setup_seconds")),
        ("spi select", timing.get("spi_select_seconds")),
        ("arrow row build", timing.get("row_build_seconds")),
        ("finish batches", timing.get("finish_batch_seconds")),
        ("writer wait", timing.get("writer_wait_seconds")),
        ("writer final join", timing.get("writer_join_seconds")),
        ("writer sum", timing.get("writer_seconds_sum")),
        ("writer max", timing.get("writer_seconds_max")),
        ("identity map inserts", timing.get("identity_insert_seconds")),
        ("catalog register", timing.get("register_seconds")),
        ("generation insert", timing.get("generation_insert_seconds")),
        ("sync variants inside export", timing.get("sync_variants_seconds")),
        ("post export hooks", timing.get("post_export_seconds")),
    ]
    print("\nrvbbit canonical export timing")
    print(f"{'step':<28} {'seconds':>9}")
    print("-" * 39)
    for label, value in rows:
        if value is None:
            continue
        print(f"{label:<28} {float(value):>9.3f}")


def _print_import_timing_summary(timing: dict) -> None:
    rows = [
        ("source parquet open", timing.get("source_open_seconds")),
        ("source parquet read", timing.get("source_read_seconds")),
        ("canonicalize batches", timing.get("source_canonicalize_seconds")),
        ("writer wait", timing.get("writer_wait_seconds")),
        ("writer final join", timing.get("writer_join_seconds")),
        ("writer sum", timing.get("writer_seconds_sum")),
        ("writer max", timing.get("writer_seconds_max")),
    ]
    print("\nrvbbit canonical import timing")
    print(f"{'step':<28} {'seconds':>9}")
    print("-" * 39)
    for label, value in rows:
        if value is None:
            continue
        print(f"{label:<28} {float(value):>9.3f}")


def _print_direct_accel_loader_summary(results: list[dict]) -> None:
    rvbbit = next((r for r in results if r.get("name") == "rvbbit" and r.get("direct_accel") == "on"), None)
    if not rvbbit:
        return
    rows = [
        ("metadata profile", (rvbbit.get("direct_accel_doc") or {}).get("metadata_profile")),
        ("source staging mode", rvbbit.get("direct_accel_staging_mode")),
        ("source chunk files", rvbbit.get("direct_accel_source_files")),
        ("source chunk write seconds", rvbbit.get("direct_accel_chunk_seconds")),
        ("extension import seconds", rvbbit.get("direct_accel_import_seconds")),
    ]
    print("\nrvbbit direct accel loader timing")
    print(f"{'step':<28} {'value':>12}")
    print("-" * 43)
    for label, value in rows:
        if value is None:
            continue
        if isinstance(value, float):
            rendered = f"{value:.3f}"
        else:
            rendered = str(value)
        print(f"{label:<28} {rendered:>12}")


def main() -> int:
    if not os.path.exists(DATA_PATH):
        print(f"ERROR: {DATA_PATH} not found. Download via download.py first.")
        return 1
    selected = os.environ.get("BENCH_SYSTEMS", ",".join(ALL_SYSTEMS)).split(",")
    selected = [s.strip() for s in selected if s.strip()]
    selected = [_load_system_name(s) for s in selected]
    selected = list(dict.fromkeys(selected))
    limit_env = os.environ.get("BENCH_LIMIT")
    limit = int(limit_env) if limit_env else None
    results = [run_one(s, limit) for s in selected]

    print("\n\n=== ClickBench load summary ===")
    print(
        f"{'system':<14} {'rows':>14} {'copy (s)':>10} {'post (s)':>10} "
        f"{'load+post (s)':>14} {'wall (s)':>10} {'size':>12}   status"
    )
    print("-" * 114)
    for r in results:
        rows = f"{r.get('rows', 0):,}" if r.get("rows") else "-"
        copy_s = r.get("copy_seconds")
        load_s = r.get("load_seconds")
        post_s = None
        if copy_s is not None and load_s is not None:
            post_s = max(0.0, load_s - copy_s)
        copy = f"{copy_s:.1f}" if copy_s is not None else "-"
        post = f"{post_s:.1f}" if post_s is not None else "-"
        secs = f"{r.get('load_seconds', 0):.1f}" if r.get("load_seconds") else "-"
        wall = f"{r.get('wall_s', 0):.1f}" if r.get("wall_s") else "-"
        size = _human(r.get("size_bytes"))
        print(
            f"{r['name']:<14} {rows:>14} {copy:>10} {post:>10} "
            f"{secs:>14} {wall:>10} {size:>12}   {r['status']}"
        )
    _print_direct_accel_loader_summary(results)
    _print_rvbbit_phase_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
