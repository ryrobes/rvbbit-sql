"""Benchmark runner.

Compares query timings across the heap baseline and rvbbit.

Usage from the bench container:

    docker compose exec bench python run.py info
    docker compose exec bench python run.py smoke
    docker compose exec bench python run.py load llm --rows 100000
    docker compose exec bench python run.py query llm
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from contextlib import contextmanager
from typing import Iterable

import click
import psycopg
from psycopg import sql as pgsql
from tabulate import tabulate

sys.path.insert(0, os.path.dirname(__file__))
from data import gen_llm  # type: ignore[import-not-found]  # noqa: E402

HEAP = os.environ["HEAP_DSN"]
RVBBIT = os.environ["RVBBIT_DSN"]
TARGETS = (("heap", HEAP), ("rvbbit", RVBBIT))


@contextmanager
def conn(dsn: str):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


def _server_version(c: psycopg.Connection) -> str:
    row = c.execute("SELECT version()").fetchone()
    assert row is not None
    return row[0]


def _rvbbit_version(c: psycopg.Connection) -> str | None:
    try:
        row = c.execute("SELECT rvbbit.rvbbit_build_info()").fetchone()
        return row[0] if row else None
    except psycopg.Error:
        return None


def _table_size(c: psycopg.Connection, table: str) -> int:
    row = c.execute(
        pgsql.SQL("SELECT pg_total_relation_size({}::regclass)").format(
            pgsql.Literal(table)
        )
    ).fetchone()
    return row[0] if row else 0


def _time_query(c: psycopg.Connection, sql: str, runs: int = 3) -> tuple[float, float]:
    """Run sql `runs` times, return (median_ms, min_ms)."""
    times = []
    # queries are trusted constants from LLM_QUERIES
    stmt = pgsql.SQL(sql)  # type: ignore[arg-type]
    for _ in range(runs):
        t0 = time.perf_counter()
        c.execute(stmt).fetchall()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), min(times)


@click.group()
def cli() -> None:
    pass


@cli.command()
def info() -> None:
    """Print server + extension versions for both targets."""
    rows = []
    for name, dsn in TARGETS:
        with conn(dsn) as c:
            rows.append([name, _server_version(c), _rvbbit_version(c) or "—"])
    print(tabulate(rows, headers=["target", "server", "extension"]))


@cli.command()
def smoke() -> None:
    """End-to-end Phase 1a smoke test: CREATE TABLE USING rvbbit, INSERT, SELECT."""
    with conn(RVBBIT) as c:
        c.execute("DROP TABLE IF EXISTS smoke_rvbbit")
        c.execute("CREATE TABLE smoke_rvbbit (id int, j jsonb) USING rvbbit")
        c.execute(
            "INSERT INTO smoke_rvbbit SELECT g, jsonb_build_object('n', g) "
            "FROM generate_series(1, 100) g"
        )
        row = c.execute(
            "SELECT count(*), max((j->>'n')::int) FROM smoke_rvbbit"
        ).fetchone()
        assert row == (100, 100), f"unexpected: {row}"
        am = c.execute(
            "SELECT a.amname FROM pg_class c JOIN pg_am a ON c.relam = a.oid "
            "WHERE c.oid = 'smoke_rvbbit'::regclass"
        ).fetchone()
        assert am is not None and am[0] == "heap", f"wrong AM: {am}"
        registered = c.execute(
            "SELECT count(*) FROM rvbbit.tables "
            "WHERE table_oid = 'smoke_rvbbit'::regclass "
            "AND acceleration_enabled"
        ).fetchone()
        assert registered == (1,), f"not in rvbbit.tables: {registered}"
        c.execute("DROP TABLE smoke_rvbbit")
        gone = c.execute(
            "SELECT count(*) FROM rvbbit.tables "
            "WHERE table_oid::regclass::text = 'smoke_rvbbit'"
        ).fetchone()
        assert gone == (0,), f"DROP didn't clean catalog: {gone}"
    click.secho("smoke OK", fg="green")


def _copy_rows(c: psycopg.Connection, table: str, rows: Iterable[tuple]) -> None:
    cols = pgsql.SQL(", ").join(map(pgsql.Identifier, gen_llm.COPY_COLUMNS))
    stmt = pgsql.SQL("COPY {tbl} ({cols}) FROM STDIN").format(
        tbl=pgsql.Identifier(table), cols=cols,
    )
    with c.cursor().copy(stmt) as cp:
        for r in rows:
            cp.write_row(r)


@cli.command()
@click.argument("workload", type=click.Choice(["llm", "clickbench"]))
@click.option("--rows", default=100_000, help="Rows to generate (llm only).")
@click.option("--seed", default=42)
def load(workload: str, rows: int, seed: int) -> None:
    """Generate + load a workload into both targets."""
    if workload != "llm":
        click.echo(f"TODO: {workload} loader — wired up later.")
        return
    cfg = gen_llm.GenConfig(rows=rows, seed=seed)
    for name, dsn in TARGETS:
        ddl = gen_llm.CREATE_DDL_RVBBIT if name == "rvbbit" else gen_llm.CREATE_DDL_HEAP
        click.echo(f"[{name}] DDL + load {rows:,} rows…")
        t0 = time.perf_counter()
        with conn(dsn) as c:
            c.execute(pgsql.SQL(ddl))
            _copy_rows(c, "llm_events", gen_llm.rows_iter(cfg))
            c.execute(pgsql.SQL("ANALYZE llm_events"))
        elapsed = time.perf_counter() - t0
        with conn(dsn) as c:
            size = _table_size(c, "llm_events")
        click.echo(
            f"[{name}] loaded in {elapsed:.1f}s, "
            f"total relation size {size / 1024 / 1024:.1f} MiB "
            f"({size / max(rows, 1):.0f} B/row)"
        )


# Pair-wise queries.
#   heap_sql      — runs against heap baseline
#   rvbbit_sql    — runs against rvbbit container, which post-compact routes
#                   through the CustomScan reading parquet — SAME SQL as heap
#                   (transparent reads).
#   rvbbit_fast   — equivalent expressed via rvbbit.rg_* projection-pushed
#                   functions; shows ceiling speedup when projection works.
LLM_QUERY_PAIRS: list[tuple[str, str, str, str]] = [
    (
        "Q1 count(*)",
        "SELECT count(*) FROM llm_events",
        "SELECT count(*) FROM llm_events",  # same SQL, transparent
        "SELECT rvbbit.rg_count_projected('llm_events'::regclass, 0, 'id')",
    ),
    (
        "Q2 sum(tokens_in)",
        "SELECT sum(tokens_in::bigint) FROM llm_events",
        "SELECT sum(tokens_in::bigint) FROM llm_events",
        "SELECT rvbbit.rg_sum_int('llm_events'::regclass, 0, 'tokens_in')",
    ),
    (
        "Q3 GROUP BY model",
        "SELECT model, count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT model, count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT * FROM rvbbit.rg_count_by_string('llm_events'::regclass, 0, 'model')",
    ),
    (
        "Q4 GROUP BY status",
        "SELECT status, count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT status, count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT * FROM rvbbit.rg_count_by_string('llm_events'::regclass, 0, 'status')",
    ),
    (
        "Q5 JSON stop_reason",
        "SELECT response->>'stop_reason', count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT response->>'stop_reason', count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT * FROM rvbbit.rg_count_by_string('llm_events'::regclass, 0, 'x_response_stop_reason')",
    ),
    (
        "Q6 JSON region",
        "SELECT metadata->>'region', count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT metadata->>'region', count(*) FROM llm_events GROUP BY 1 ORDER BY 2 DESC",
        "SELECT * FROM rvbbit.rg_count_by_string('llm_events'::regclass, 0, 'x_metadata_region')",
    ),
    (
        "Q7 JSON input_tokens sum",
        "SELECT sum((response->'usage'->>'input_tokens')::int) FROM llm_events",
        "SELECT sum((response->'usage'->>'input_tokens')::int) FROM llm_events",
        "SELECT rvbbit.rg_sum_int('llm_events'::regclass, 0, 'x_response_input_tokens')",
    ),
]


@cli.command()
def compact() -> None:
    """Run rvbbit.compact() on the loaded llm_events table."""
    with conn(RVBBIT) as c:
        row = c.execute(
            pgsql.SQL("SELECT * FROM rvbbit.compact('llm_events'::regclass)")
        ).fetchone()
    if row is None:
        click.secho("compact returned no rows", fg="red")
        return
    rg_id, n_rows, n_bytes, freed = row
    click.secho(
        f"compacted rg_id={rg_id}: {n_rows:,} rows -> "
        f"{n_bytes / 1024 / 1024:.1f} MiB parquet "
        f"(heap freed: {freed / 1024 / 1024:.1f} MiB)",
        fg="green",
    )


@cli.command()
@click.argument("workload", type=click.Choice(["llm", "clickbench"]))
@click.option("--runs", default=3, help="Per-query timing samples.")
def query(workload: str, runs: int) -> None:
    """Pair-wise compare heap vs rvbbit for each query in the workload."""
    if workload != "llm":
        click.echo(f"TODO: {workload} queries — wired up later.")
        return

    table_rows = []
    for label, heap_sql, rvbbit_native_sql, rvbbit_fast_sql in LLM_QUERY_PAIRS:
        with conn(HEAP) as c:
            h_med, _ = _time_query(c, heap_sql, runs=runs)
        with conn(RVBBIT) as c:
            an_med, _ = _time_query(c, rvbbit_native_sql, runs=runs)
            af_med, _ = _time_query(c, rvbbit_fast_sql, runs=runs)
        sp_native = h_med / an_med if an_med > 0 else float("inf")
        sp_fast = h_med / af_med if af_med > 0 else float("inf")
        table_rows.append([
            label,
            f"{h_med:8.1f}",
            f"{an_med:8.1f}",
            f"{sp_native:5.1f}x",
            f"{af_med:8.1f}",
            f"{sp_fast:5.1f}x",
        ])
    print(tabulate(
        table_rows,
        headers=[
            "query",
            "heap (ms)",
            "rvbbit native (ms)",
            "vs heap",
            "rvbbit rg_* (ms)",
            "vs heap",
        ],
        tablefmt="github",
    ))


if __name__ == "__main__":
    cli()
