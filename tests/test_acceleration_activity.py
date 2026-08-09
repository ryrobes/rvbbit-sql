import os
import threading
import time

import psycopg


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)


def _seed_scheduled_table(rvbbit, table):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(f"CREATE TABLE {table} (id int PRIMARY KEY, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {table} VALUES (1, 'one'), (2, 'two'), (3, 'three')")
    rvbbit.execute(f"ANALYZE {table}")
    rvbbit.execute(
        f"""
        SELECT rvbbit.set_accel_policy(
            '{table}'::regclass,
            strategy => 'scheduled',
            min_interval_secs => 0
        )
        """
    )


def test_accel_tick_records_append_only_sweep_and_runtime_profile(rvbbit, temp_table):
    _seed_scheduled_table(rvbbit, temp_table)
    before = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.accel_activity_log"
    ).fetchone()[0]

    planned = rvbbit.execute(
        f"""
        SELECT action, status
        FROM rvbbit.accel_tick(1, true)
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert planned == ("delta", "planned")
    assert rvbbit.execute(
        "SELECT count(*) FROM rvbbit.accel_activity_log"
    ).fetchone()[0] == before

    executed = rvbbit.execute(
        f"""
        SELECT action, status, rows_written, error
        FROM rvbbit.accel_tick(1, false)
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert executed == ("delta", "ok", 3, None)

    sweep_id, operation_id, elapsed_ms = rvbbit.execute(
        f"""
        SELECT sweep_id, operation_id, elapsed_ms
        FROM rvbbit.accel_activity_log
        WHERE table_oid = '{temp_table}'::regclass
          AND lane = 'freshness'
          AND event_type = 'table_finished'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert operation_id is not None
    assert elapsed_ms >= 0

    events = rvbbit.execute(
        """
        SELECT event_type, count(*)::int
        FROM rvbbit.accel_activity_log
        WHERE sweep_id = %s
        GROUP BY event_type
        """,
        (sweep_id,),
    ).fetchall()
    assert dict(events) == {
        "sweep_started": 1,
        "table_started": 1,
        "table_finished": 1,
        "sweep_finished": 1,
    }

    history = rvbbit.execute(
        """
        SELECT lane, status, tables_started, tables_succeeded, tables_failed,
               elapsed_ms, details->>'tables_executed'
        FROM rvbbit.accel_sweep_history
        WHERE sweep_id = %s
        """,
        (sweep_id,),
    ).fetchone()
    assert history[0:5] == ("freshness", "ok", 1, 1, 0)
    assert history[5] >= elapsed_ms
    assert history[6] == "1"

    profile = rvbbit.execute(
        f"""
        SELECT runs, failed_runs, avg_elapsed_ms, p50_elapsed_ms,
               p95_elapsed_ms, rows_written
        FROM rvbbit.accel_table_runtime_profile
        WHERE table_oid = '{temp_table}'::regclass
          AND lane = 'freshness'
        """
    ).fetchone()
    assert profile[0:2] == (1, 0)
    assert all(value >= 0 for value in profile[2:5])
    assert profile[5] == 3
    assert rvbbit.execute(
        "SELECT current_setting('application_name') NOT LIKE 'rvbbit/%'"
    ).fetchone()[0]


def test_live_activity_identifies_blocked_table_before_log_commit(rvbbit, temp_table):
    _seed_scheduled_table(rvbbit, temp_table)
    table_oid = rvbbit.execute(
        f"SELECT '{temp_table}'::regclass::oid"
    ).fetchone()[0]
    lock_key = (1380336724 << 32) | table_oid
    rvbbit.execute("SELECT pg_advisory_lock(%s)", (lock_key,))

    worker = {}

    def run_tick():
        try:
            with psycopg.connect(RVBBIT_DSN, autocommit=True) as conn:
                conn.execute("SET rvbbit.compact_vortex_layout = 'off'")
                conn.execute("SET rvbbit.compact_hive_layout = 'off'")
                worker["result"] = conn.execute(
                    f"""
                    SELECT action, status, rows_written, error
                    FROM rvbbit.accel_tick(1, false)
                    WHERE table_oid = '{temp_table}'::regclass
                    """
                ).fetchone()
        except Exception as exc:  # surfaced after releasing the deliberate lock
            worker["error"] = exc

    thread = threading.Thread(target=run_tick, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        live = None
        while time.time() < deadline:
            live = rvbbit.execute(
                """
                SELECT lane, table_oid, table_name, action, state,
                       wait_event_type, wait_event, current_work_elapsed_ms,
                       transaction_locked_table_oids
                FROM rvbbit.accel_live_activity
                WHERE table_oid = %s
                  AND lane = 'freshness'
                """,
                (table_oid,),
            ).fetchone()
            if live is not None and live[5] == "Lock":
                break
            time.sleep(0.05)

        assert live is not None
        assert live[0:4] == ("freshness", table_oid, temp_table, "delta")
        assert live[4] == "active"
        assert live[5:7] == ("Lock", "advisory")
        assert live[7] >= 0

        # table_started exists in the worker transaction but is intentionally
        # invisible until commit; this is why live state does not read the log.
        assert rvbbit.execute(
            """
            SELECT count(*)
            FROM rvbbit.accel_activity_log
            WHERE table_oid = %s AND event_type = 'table_started'
            """,
            (table_oid,),
        ).fetchone()[0] == 0
    finally:
        rvbbit.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        thread.join(timeout=20)

    assert not thread.is_alive()
    assert "error" not in worker, worker.get("error")
    assert worker["result"] == ("delta", "ok", 3, None)
    assert rvbbit.execute(
        """
        SELECT count(*)
        FROM rvbbit.accel_activity_log
        WHERE table_oid = %s AND event_type = 'table_finished'
        """,
        (table_oid,),
    ).fetchone()[0] == 1
