from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid

import psycopg


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)

BENCH_RUNS = int(os.environ.get("RVBBIT_LAG_FOLD_RUNS", "3"))
BENCH_ROWS = int(os.environ.get("RVBBIT_LAG_FOLD_ROWS", "2000"))
CLEAN_BENCH_ROWS = int(os.environ.get("RVBBIT_LAG_FOLD_CLEAN_ROWS", "5000"))


def _table_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _connect(*, autocommit: bool = True):
    return psycopg.connect(RVBBIT_DSN, autocommit=autocommit)


def _drop_table(conn, table: str) -> None:
    try:
        conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    except psycopg.Error:
        pass


def _setup_table(conn, table: str, rows: int) -> None:
    conn.execute("SET rvbbit.compact_vortex_layout = 'off'")
    conn.execute("SET rvbbit.compact_hive_layout = 'off'")
    conn.execute(f"CREATE TABLE {table} (id int PRIMARY KEY, label text) USING rvbbit")
    conn.execute(
        f"""
        INSERT INTO {table}
        SELECT g, 'row ' || g::text
        FROM generate_series(1, %s) AS g
        """,
        (rows,),
    )
    result = conn.execute(
        f"SELECT rvbbit.refresh_acceleration('{table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"


def _run_rebuild(table: str, result_holder: dict, error_holder: dict) -> None:
    try:
        with _connect() as conn:
            conn.execute("SET application_name = 'rvbbit_lagged_fold_bench'")
            result_holder["result"] = conn.execute(
                f"SELECT rvbbit.rebuild_acceleration('{table}'::regclass, false)"
            ).fetchone()[0]
    except Exception as exc:  # pragma: no cover - propagated by caller
        error_holder["error"] = exc


def _wait_for_rebuild_lock_wait(conn, table: str, timeout: float = 15.0) -> float:
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = conn.execute(
            """
            SELECT count(*)::int
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND application_name = 'rvbbit_lagged_fold_bench'
              AND query LIKE %s
              AND query LIKE %s
              AND wait_event_type = 'Lock'
            """,
            ("%rvbbit.rebuild_acceleration%", f"%{table}%"),
        ).fetchone()[0]
        if waiting:
            return (time.perf_counter() - started) * 1000.0
        time.sleep(0.025)
    raise AssertionError(f"rebuild for {table} did not reach final lock wait")


def _start_rebuild(table: str):
    result_holder: dict = {}
    error_holder: dict = {}
    thread = threading.Thread(
        target=_run_rebuild, args=(table, result_holder, error_holder), daemon=True
    )
    started = time.perf_counter()
    thread.start()
    return thread, started, result_holder, error_holder


def _join_rebuild(thread, started: float, result_holder: dict, error_holder: dict):
    thread.join(timeout=30)
    assert not thread.is_alive(), "rebuild did not finish before timeout"
    if error_holder:
        raise error_holder["error"]
    assert "result" in result_holder
    return result_holder["result"], (time.perf_counter() - started) * 1000.0


def _summary(values: list[float]) -> dict:
    return {
        "min_ms": round(min(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "max_ms": round(max(values), 3),
    }


def _table_counts(conn, table: str) -> tuple[int, int, bool, int]:
    return conn.execute(
        f"""
        SELECT
            (SELECT coalesce(sum(n_rows), 0)::int
             FROM rvbbit.row_groups
             WHERE table_oid = '{table}'::regclass),
            rvbbit.tombstone_count('{table}'::regclass)::int,
            (SELECT shadow_heap_dirty
             FROM rvbbit.tables
             WHERE table_oid = '{table}'::regclass),
            (SELECT count(*)::int FROM {table})
        """
    ).fetchone()


def test_lagged_fold_mixed_mutation_catchup_benchmark():
    metrics = []
    with _connect() as admin:
        for run_idx in range(BENCH_RUNS):
            table = _table_name("lag_fold_mix")
            _setup_table(admin, table, BENCH_ROWS)

            writer = _connect(autocommit=False)
            try:
                writer.execute(
                    f"UPDATE {table} SET label = 'updated ' || id::text WHERE id BETWEEN 1 AND 10"
                )
                writer.execute(f"DELETE FROM {table} WHERE id BETWEEN 11 AND 20")
                writer.execute(
                    f"""
                    INSERT INTO {table}
                    SELECT g, 'new ' || g::text
                    FROM generate_series(%s::int, %s::int) AS g
                    """,
                    (BENCH_ROWS + 1, BENCH_ROWS + 25),
                )

                thread, started, result_holder, error_holder = _start_rebuild(table)
                wait_ms = _wait_for_rebuild_lock_wait(admin, table)

                commit_started = time.perf_counter()
                writer.commit()
                commit_ms = (time.perf_counter() - commit_started) * 1000.0

                result, rebuild_ms = _join_rebuild(
                    thread, started, result_holder, error_holder
                )
            finally:
                if not writer.closed:
                    writer.close()

            try:
                assert result["status"] == "ok"
                assert result["baseline_rows"] == BENCH_ROWS
                assert result["catchup_rows"] == 35
                assert result["remapped_tombstones"] == 20
                assert result["rows_written"] == BENCH_ROWS + 35

                parquet_rows, tombstones, dirty, visible_rows = _table_counts(admin, table)
                assert (parquet_rows, tombstones, dirty, visible_rows) == (
                    BENCH_ROWS + 35,
                    20,
                    False,
                    BENCH_ROWS + 15,
                )
                assert (
                    admin.execute(
                        f"""
                        SELECT count(*)::int
                        FROM {table}
                        WHERE id BETWEEN 1 AND 10 AND label LIKE 'updated %'
                        """
                    ).fetchone()[0]
                    == 10
                )
                assert (
                    admin.execute(
                        f"SELECT count(*)::int FROM {table} WHERE id BETWEEN 11 AND 20"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    admin.execute(
                        f"""
                        SELECT count(*)::int
                        FROM {table}
                        WHERE id BETWEEN %s AND %s AND label LIKE 'new %%'
                        """,
                        (BENCH_ROWS + 1, BENCH_ROWS + 25),
                    ).fetchone()[0]
                    == 25
                )

                metrics.append(
                    {
                        "run": run_idx + 1,
                        "rows": BENCH_ROWS,
                        "wait_to_final_lock_ms": round(wait_ms, 3),
                        "writer_commit_ms": round(commit_ms, 3),
                        "rebuild_total_ms": round(rebuild_ms, 3),
                        "catchup_rows": result["catchup_rows"],
                        "remapped_tombstones": result["remapped_tombstones"],
                    }
                )
            finally:
                _drop_table(admin, table)

    print(
        "LAGGED_FOLD_MIXED_CATCHUP",
        json.dumps(
            {
                "runs": metrics,
                "summary": {
                    "rebuild_total": _summary(
                        [m["rebuild_total_ms"] for m in metrics]
                    ),
                    "writer_commit": _summary(
                        [m["writer_commit_ms"] for m in metrics]
                    ),
                },
            },
            sort_keys=True,
        ),
    )


def test_lagged_fold_final_lock_wait_does_not_queue_later_writer():
    table = _table_name("lag_fold_lock")
    with _connect() as admin:
        _setup_table(admin, table, 1000)

        blocker = _connect(autocommit=False)
        try:
            blocker.execute(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE")

            thread, started, result_holder, error_holder = _start_rebuild(table)
            wait_ms = _wait_for_rebuild_lock_wait(admin, table)

            second_writer_started = time.perf_counter()
            with _connect() as second_writer:
                second_writer.execute("SET lock_timeout = '750ms'")
                second_writer.execute("SET statement_timeout = '2s'")
                second_writer.execute(
                    f"UPDATE {table} SET label = 'second writer' WHERE id = 2"
                )
            second_writer_ms = (time.perf_counter() - second_writer_started) * 1000.0

            blocker.commit()
            result, rebuild_ms = _join_rebuild(
                thread, started, result_holder, error_holder
            )

            parquet_rows, tombstones, dirty, visible_rows = _table_counts(admin, table)
            writer_label = admin.execute(
                f"SELECT label FROM {table} WHERE id = 2"
            ).fetchone()[0]
        finally:
            if not blocker.closed:
                try:
                    blocker.rollback()
                except psycopg.Error:
                    pass
                blocker.close()
            _drop_table(admin, table)

    assert result["status"] == "ok"
    assert result["baseline_rows"] == 1000
    assert result["catchup_rows"] == 1
    assert result["remapped_tombstones"] == 1
    assert (parquet_rows, tombstones, dirty, visible_rows, writer_label) == (
        1001,
        1,
        False,
        1000,
        "second writer",
    )

    print(
        "LAGGED_FOLD_FINAL_LOCK_WRITER_PROBE",
        json.dumps(
            {
                "wait_to_final_lock_ms": round(wait_ms, 3),
                "second_writer_ms": round(second_writer_ms, 3),
                "rebuild_total_ms": round(rebuild_ms, 3),
                "catchup_rows": result["catchup_rows"],
                "remapped_tombstones": result["remapped_tombstones"],
            },
            sort_keys=True,
        ),
    )


def test_lagged_fold_clean_rebuild_benchmark():
    table = _table_name("lag_fold_clean")
    metrics = []
    with _connect() as admin:
        _setup_table(admin, table, CLEAN_BENCH_ROWS)
        try:
            for run_idx in range(BENCH_RUNS):
                started = time.perf_counter()
                result = admin.execute(
                    f"SELECT rvbbit.rebuild_acceleration('{table}'::regclass, false)"
                ).fetchone()[0]
                rebuild_ms = (time.perf_counter() - started) * 1000.0
                assert result["status"] == "ok"
                assert result["baseline_rows"] == CLEAN_BENCH_ROWS
                assert result["catchup_rows"] == 0
                assert result["rows_written"] == CLEAN_BENCH_ROWS

                parquet_rows, tombstones, dirty, visible_rows = _table_counts(admin, table)
                assert (parquet_rows, tombstones, dirty, visible_rows) == (
                    CLEAN_BENCH_ROWS,
                    0,
                    False,
                    CLEAN_BENCH_ROWS,
                )
                metrics.append(
                    {
                        "run": run_idx + 1,
                        "rows": CLEAN_BENCH_ROWS,
                        "rebuild_total_ms": round(rebuild_ms, 3),
                    }
                )
        finally:
            _drop_table(admin, table)

    print(
        "LAGGED_FOLD_CLEAN_REBUILD",
        json.dumps(
            {
                "runs": metrics,
                "summary": {
                    "rebuild_total": _summary(
                        [m["rebuild_total_ms"] for m in metrics]
                    )
                },
            },
            sort_keys=True,
        ),
    )
