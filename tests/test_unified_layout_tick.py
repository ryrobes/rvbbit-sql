import threading
import time
import uuid

import psycopg
import pytest

from conftest import RVBBIT_DSN


MAINTENANCE_GATE_CLASS = 1381187156
HEAVY_LANE_KEY = 8


def _make_layout_candidate(conn, name: str, accepted=()) -> int:
    conn.execute("SET rvbbit.compact_vortex_layout = 'on'")
    conn.execute(
        f"""
        CREATE TABLE {name} (
            id integer PRIMARY KEY,
            region text NOT NULL,
            event_date date NOT NULL,
            amount integer NOT NULL
        ) USING rvbbit
        """
    )
    conn.execute(
        f"""
        INSERT INTO {name}
        SELECT g,
               'r' || (g % 4),
               DATE '2026-01-01' + (g % 30),
               g % 100
          FROM generate_series(1, 256) AS g
        """
    )
    conn.execute(f"ANALYZE {name}")
    # Build only canonical Parquet. Its row-group changes create the automatic
    # Vortex target consumed by the unified layout fleet.
    conn.execute(f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)")
    if "cluster" in accepted:
        conn.execute(
            f"SELECT rvbbit.accept_workload_layout("
            f"'{name}'::regclass, 'cluster', 'event_date')"
        )
    if "hive" in accepted:
        conn.execute(
            f"SELECT rvbbit.accept_workload_layout("
            f"'{name}'::regclass, 'hive', 'region')"
        )
    table_oid = conn.execute(f"SELECT '{name}'::regclass::oid").fetchone()[0]
    # Keep the test independent of whether a future canonical implementation
    # stages Vortex inline before committing its new Parquet generation.
    conn.execute(
        "SELECT rvbbit.enqueue_variant_build(%s::regclass, 'unified_test', 10)",
        (table_oid,),
    )
    return table_oid


def test_unified_worker_builds_automatic_vortex_target(rvbbit):
    name = f"layout_auto_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        table_oid = _make_layout_candidate(rvbbit, name)

        candidate = rvbbit.execute(
            """
            SELECT vortex_pending, workload_pending, target_layouts
              FROM rvbbit.layout_tick_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert candidate == (True, False, ["vortex_scan"])

        built = rvbbit.execute(
            """
            SELECT action, executed, status, rows_written
              FROM rvbbit.layout_tick_worker(3, 3, false)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert built[0:3] == ("build_vortex", True, "ok")
        assert built[3] == 256

        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.variant_build_queue WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (0,)
        assert rvbbit.execute(
            """
            SELECT status, expected_rows, actual_rows
              FROM rvbbit.layout_variant_status
             WHERE table_oid = %s AND layout = 'vortex_scan'
            """,
            (table_oid,),
        ).fetchone() == ("ready", 256, 256)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_unified_worker_builds_vortex_and_accepted_layouts_together(rvbbit):
    name = f"layout_both_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        table_oid = _make_layout_candidate(
            rvbbit,
            name,
            accepted=("cluster", "hive"),
        )

        candidate = rvbbit.execute(
            """
            SELECT vortex_pending, workload_pending, target_layouts
              FROM rvbbit.layout_tick_ready_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert candidate == (
            True,
            True,
            ["vortex_scan", "cluster:event_date", "hive:region"],
        )

        built = rvbbit.execute(
            """
            SELECT action, status, rows_written, details
              FROM rvbbit.layout_tick_worker(2, 3, false)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert built[0] == "build_vortex_and_accepted_layouts"
        assert built[1] == "ok"
        assert built[2] >= 768
        assert built[3]["vortex_result"]["status"] == "ok"
        assert built[3]["workload_pending_after"] is False

        status = rvbbit.execute(
            """
            SELECT layout, status, actual_rows
              FROM rvbbit.layout_variant_status
             WHERE table_oid = %s
             ORDER BY layout
            """,
            (table_oid,),
        ).fetchall()
        assert status == [
            ("cluster:event_date", "ready", 256),
            ("hive:region", "ready", 256),
            ("vortex_scan", "ready", 256),
        ]
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.layout_tick_candidates WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (0,)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_disabled_vortex_does_not_block_an_accepted_layout(rvbbit):
    name = f"layout_denied_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        table_oid = _make_layout_candidate(rvbbit, name, accepted=("hive",))
        rvbbit.execute(
            "SELECT rvbbit.set_table_engine(%s::regclass, 'vortex', false)",
            (table_oid,),
        )

        built = rvbbit.execute(
            """
            SELECT status, details
              FROM rvbbit.layout_tick_worker(1, 1, false)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert built[0] == "ok"
        assert built[1]["vortex_result"]["status"] == "skipped"
        assert built[1]["workload_pending_after"] is False
        assert rvbbit.execute(
            """
            SELECT status
              FROM rvbbit.layout_variant_status
             WHERE table_oid = %s AND layout = 'hive:region'
            """,
            (table_oid,),
        ).fetchone() == ("ready",)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_vortex_run_preserves_accepted_layout_retry_window(rvbbit):
    name = f"layout_retry_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        table_oid = _make_layout_candidate(rvbbit, name, accepted=("hive",))
        candidate = rvbbit.execute(
            """
            SELECT table_name, target_generation
              FROM rvbbit.layout_tick_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        rvbbit.execute(
            """
            INSERT INTO rvbbit.layout_tick_runs (
                table_oid, table_name, target_generation, target_layouts,
                attempt, action, status, retry_at, error, details
            ) VALUES (
                %s, %s, %s, ARRAY['hive:region'],
                2, 'build_accepted_layouts', 'failed',
                clock_timestamp() + interval '1 hour', 'synthetic failure',
                jsonb_build_object(
                    'workload_attempted', true,
                    'accepted_targets', jsonb_build_array('hive:region')
                )
            )
            """,
            (table_oid, candidate[0], candidate[1]),
        )

        ready = rvbbit.execute(
            """
            SELECT vortex_ready, workload_ready
              FROM rvbbit.layout_tick_ready_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert ready == (True, False)

        built = rvbbit.execute(
            """
            SELECT action, status, details
              FROM rvbbit.layout_tick_worker(1, 1, false)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert built[0:2] == ("build_vortex", "ok")
        assert built[2]["workload_attempted"] is False

        assert rvbbit.execute(
            """
            SELECT count(*)
              FROM rvbbit.layout_tick_ready_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone() == (0,)
        assert rvbbit.execute(
            """
            SELECT attempt, status, retry_at > clock_timestamp()
              FROM rvbbit.layout_tick_runs
             WHERE table_oid = %s
               AND coalesce(
                       (details ->> 'workload_attempted')::boolean,
                       false
                   )
             ORDER BY id DESC
             LIMIT 1
            """,
            (table_oid,),
        ).fetchone() == (2, "failed", True)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_three_unified_workers_build_distinct_tables(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    names = [f"layout_parallel_{letter}_{suffix}" for letter in ("a", "b", "c")]
    original_heavy_slots = rvbbit.execute(
        "SELECT rvbbit.accel_maintenance_heavy_slots()"
    ).fetchone()[0]
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        rvbbit.execute("SELECT rvbbit.set_accel_maintenance_heavy_slots(3)")
        target_oids = {_make_layout_candidate(rvbbit, name) for name in names}
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def run_worker(slot: int):
            try:
                with psycopg.connect(RVBBIT_DSN, autocommit=True) as conn:
                    conn.execute("SET statement_timeout = '20s'")
                    barrier.wait(timeout=3)
                    rows = conn.execute(
                        """
                        SELECT table_oid::oid, executed, status
                          FROM rvbbit.layout_tick_worker(%s, 3, false)
                         WHERE table_oid = ANY(%s::oid[])
                        """,
                        (slot, list(target_oids)),
                    ).fetchall()
                    results.extend(rows)
            except Exception as exc:  # surfaced below with all worker results
                errors.append(exc)

        threads = [
            threading.Thread(target=run_worker, args=(slot,), daemon=True)
            for slot in (1, 2, 3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        executed_oids = {
            table_oid
            for table_oid, executed, status in results
            if executed and status != "failed"
        }
        assert executed_oids == target_oids
    finally:
        rvbbit.execute(
            "SELECT rvbbit.set_accel_maintenance_heavy_slots(%s)",
            (original_heavy_slots,),
        )
        for name in reversed(names):
            rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_unified_worker_pass_commits_four_tables(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    names = [f"layout_pass_{i}_{suffix}" for i in range(5)]
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        target_oids = [_make_layout_candidate(rvbbit, name) for name in names]

        with psycopg.connect(RVBBIT_DSN, autocommit=True) as caller:
            caller.execute("CALL rvbbit.layout_tick_worker_pass(1, 1, 4)")

        remaining = rvbbit.execute(
            """
            SELECT count(*)
              FROM rvbbit.variant_build_queue
             WHERE table_oid = ANY(%s::oid[])
            """,
            (target_oids,),
        ).fetchone()[0]
        assert remaining == 1

        runs = rvbbit.execute(
            """
            SELECT count(*), count(DISTINCT table_oid)
              FROM rvbbit.layout_tick_runs
             WHERE table_oid = ANY(%s::oid[])
            """,
            (target_oids,),
        ).fetchone()
        assert runs == (4, 4)
    finally:
        for name in reversed(names):
            rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_unified_layout_lane_is_nonblocking_and_scheduler_retires_old_jobs(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    with psycopg.connect(RVBBIT_DSN, autocommit=False) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (MAINTENANCE_GATE_CLASS, HEAVY_LANE_KEY),
        )
        started = time.monotonic()
        rows = rvbbit.execute(
            "SELECT * FROM rvbbit.layout_tick_worker(8, 8, false)"
        ).fetchall()
        elapsed = time.monotonic() - started
        holder.rollback()
    assert rows == []
    assert elapsed < 1.0

    with pytest.raises(psycopg.errors.RaiseException, match="between 1 and 8"):
        rvbbit.execute("SELECT * FROM rvbbit.layout_tick_worker(1, 9, true)")

    scheduler_def = rvbbit.execute(
        """
        SELECT pg_get_functiondef(
            'rvbbit.schedule_layout_tick_worker_passes(text,integer,integer)'
                ::regprocedure
        )
        """
    ).fetchone()[0]
    assert "rvbbit_variant_tick" in scheduler_def
    assert "rvbbit_workload_layout_tick_worker_" in scheduler_def
    assert "CALL rvbbit.layout_tick_worker_pass" in scheduler_def
