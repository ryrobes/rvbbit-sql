import threading
import time
import uuid

import psycopg
import pytest

from conftest import RVBBIT_DSN


FRESHNESS_GATE_CLASS = 1381187156
FRESHNESS_GATE_KEY = 7
WORKER_SLOT_KEY_BASE = 70
TABLE_LOCK_CLASS = 1380336724


def _table_lock_key(table_oid: int) -> int:
    return (TABLE_LOCK_CLASS << 32) | table_oid


def _make_scheduled_table(conn, name: str) -> int:
    conn.execute(
        f"CREATE TABLE {name} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    conn.execute(
        f"INSERT INTO {name} "
        "SELECT i, repeat(md5(i::text), 2) FROM generate_series(1, 2000) AS i"
    )
    conn.execute(f"ANALYZE {name}")
    conn.execute(
        f"""
        SELECT rvbbit.set_accel_policy(
            '{name}'::regclass,
            strategy => 'scheduled',
            min_interval_secs => 0,
            full_rebuild_drift_ratio => 2.0
        )
        """
    )
    return conn.execute(f"SELECT '{name}'::regclass::oid").fetchone()[0]


def test_parallel_worker_contract_supports_eight_slots(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    assert rvbbit.execute(
        "SELECT to_regprocedure("
        "'rvbbit.accel_tick_worker(integer,integer,boolean,integer)') IS NOT NULL"
    ).fetchone() == (True,)
    assert rvbbit.execute(
        "SELECT to_regprocedure("
        "'rvbbit.accel_tick_worker_pass(integer,integer,integer,integer)') "
        "IS NOT NULL"
    ).fetchone() == (True,)

    rvbbit.execute("SELECT * FROM rvbbit.accel_tick_worker(8, 8, true, 1)")

    with pytest.raises(psycopg.errors.RaiseException, match="between 1 and 8"):
        rvbbit.execute("SELECT * FROM rvbbit.accel_tick_worker(1, 9, true, 1)")

    with pytest.raises(psycopg.errors.RaiseException, match="worker_slot"):
        rvbbit.execute("SELECT * FROM rvbbit.accel_tick_worker(0, 2, true, 1)")


def test_busy_worker_slot_returns_without_waiting(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    with psycopg.connect(RVBBIT_DSN, autocommit=False) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (FRESHNESS_GATE_CLASS, WORKER_SLOT_KEY_BASE + 8),
        )

        with psycopg.connect(RVBBIT_DSN, autocommit=True) as caller:
            caller.execute("SET statement_timeout = '2s'")
            started = time.monotonic()
            rows = caller.execute(
                "SELECT * FROM rvbbit.accel_tick_worker(8, 8, false, 1)"
            ).fetchall()
            elapsed = time.monotonic() - started

        assert rows == []
        assert elapsed < 1.0
        holder.rollback()


def test_shared_worker_gate_excludes_fold_lane():
    with psycopg.connect(RVBBIT_DSN, autocommit=False) as worker_one:
        with psycopg.connect(RVBBIT_DSN, autocommit=False) as worker_two:
            assert worker_one.execute(
                "SELECT pg_try_advisory_xact_lock_shared(%s, %s)",
                (FRESHNESS_GATE_CLASS, FRESHNESS_GATE_KEY),
            ).fetchone() == (True,)
            assert worker_two.execute(
                "SELECT pg_try_advisory_xact_lock_shared(%s, %s)",
                (FRESHNESS_GATE_CLASS, FRESHNESS_GATE_KEY),
            ).fetchone() == (True,)

            with psycopg.connect(RVBBIT_DSN, autocommit=True) as fold_probe:
                assert fold_probe.execute(
                    "SELECT pg_try_advisory_xact_lock(%s, %s)",
                    (FRESHNESS_GATE_CLASS, FRESHNESS_GATE_KEY),
                ).fetchone() == (False,)

            worker_two.rollback()
        worker_one.rollback()


def test_busy_table_claim_is_skipped_without_waiting(rvbbit):
    name = f"tick_claimed_{uuid.uuid4().hex[:8]}"
    try:
        table_oid = _make_scheduled_table(rvbbit, name)
        planned = rvbbit.execute(
            f"""
            SELECT status
              FROM rvbbit.accel_tick_worker(1, 2, true, 1)
             WHERE table_oid = '{name}'::regclass
            """
        ).fetchone()
        assert planned == ("planned",)

        with psycopg.connect(RVBBIT_DSN, autocommit=False) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_table_lock_key(table_oid),),
            )
            started = time.monotonic()
            rows = rvbbit.execute(
                f"""
                SELECT executed, status, reason
                  FROM rvbbit.accel_tick_worker(1, 2, false, 1)
                 WHERE table_oid = '{name}'::regclass
                """
            ).fetchall()
            elapsed = time.monotonic() - started
            holder.rollback()

        assert rows == [
            (False, "deferred", "table maintenance claimed by another worker")
        ]
        assert elapsed < 1.0
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_three_workers_refresh_three_distinct_tables(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    names = [f"tick_parallel_{letter}_{suffix}" for letter in ("a", "b", "c")]
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        target_oids = {_make_scheduled_table(rvbbit, name) for name in names}
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
                          FROM rvbbit.accel_tick_worker(%s, 3, false, 1)
                         WHERE table_oid = ANY(%s::oid[])
                        """,
                        (slot, list(target_oids)),
                    ).fetchall()
                    results.extend(rows)
            except Exception as exc:  # surfaced below with both worker results
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
        for name in reversed(names):
            rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_worker_pass_processes_four_tables_with_transaction_boundaries(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    names = [f"tick_pass_{i}_{suffix}" for i in range(5)]
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        target_oids = [_make_scheduled_table(rvbbit, name) for name in names]

        # CALL must be top-level so the procedure can commit after each table.
        with psycopg.connect(RVBBIT_DSN, autocommit=True) as caller:
            caller.execute("CALL rvbbit.accel_tick_worker_pass(1, 1, 4, 1)")

        built = rvbbit.execute(
            """
            SELECT count(*) FILTER (WHERE groups > 0),
                   count(*) FILTER (WHERE groups = 0)
              FROM (
                    SELECT target.table_oid,
                           count(rg.rg_id) AS groups
                      FROM unnest(%s::oid[]) AS target(table_oid)
                      LEFT JOIN rvbbit.row_groups rg
                        ON rg.table_oid = target.table_oid
                     GROUP BY target.table_oid
                   ) status
            """,
            (target_oids,),
        ).fetchone()
        assert built == (4, 1)

        # Four independent worker invocations produce four distinct sweeps.
        sweeps = rvbbit.execute(
            """
            SELECT count(DISTINCT sweep_id)
              FROM rvbbit.accel_activity_log
             WHERE lane = 'freshness'
               AND event_type = 'table_finished'
               AND table_oid = ANY(%s::oid[])
            """,
            (target_oids,),
        ).fetchone()[0]
        assert sweeps == 4
    finally:
        for name in reversed(names):
            rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
