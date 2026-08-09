import json

import pytest


def _observer_installed(conn):
    return conn.execute(
        "SELECT to_regprocedure('rvbbit.accel_autopilot_observe(text)') IS NOT NULL"
    ).fetchone()[0]


def test_activity_observer_is_durable_and_never_accelerates(rvbbit, temp_table):
    if not _observer_installed(rvbbit):
        pytest.skip("run rvbbit.migrate() to install the workload observer")
    if not rvbbit.execute(
        "SELECT to_regclass('rvbbit.mcp_activity') IS NOT NULL"
    ).fetchone()[0]:
        pytest.skip("warehouse activity table is not installed")

    rvbbit.execute("BEGIN")
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int, payload text)")
        rvbbit.execute(
            f"INSERT INTO {temp_table} SELECT g, repeat(md5(g::text), 2) FROM generate_series(1, 100) g"
        )
        rvbbit.execute(f"ANALYZE {temp_table}")
        rvbbit.execute(
            """
            UPDATE rvbbit.accel_autopilot_config
               SET min_calls = 2,
                   min_active_hours = 2,
                   min_attributed_ms = 1000,
                   min_table_bytes = 0,
                   max_write_ratio = 1
             WHERE singleton
            """
        )

        sql_text = f"SELECT count(*) FROM {temp_table}"
        args = json.dumps({"sql": sql_text})
        rvbbit.execute(
            """
            INSERT INTO rvbbit.mcp_activity
                (ts, caller, subject, tool, args, ok, elapsed_ms)
            SELECT clock_timestamp() - make_interval(hours => g),
                   'observer-test-' || g || '@example.test',
                   'observer-test-' || g || '@example.test',
                   'dashboard_query', %s::jsonb, true, 1200
              FROM generate_series(1, 2) AS g
            """,
            (args,),
        )

        # ETL statements must not consume the observer's bounded query-shape
        # budget or become acceleration evidence.  Keep this statement longer
        # than PostgreSQL's btree index-entry limit as a regression for the old
        # sql_text PRIMARY KEY, which disabled the entire activity source when
        # a large dashboard/COPY shape was encountered.
        copy_args = json.dumps(
            {"sql": f"COPY {temp_table} TO STDOUT /* {'etl-padding-' * 400} */"}
        )
        rvbbit.execute(
            """
            INSERT INTO rvbbit.mcp_activity
                (ts, caller, subject, tool, args, ok, elapsed_ms)
            SELECT clock_timestamp(),
                   'etl@example.test', 'etl@example.test',
                   'dashboard_query', %s::jsonb, true, 60000
              FROM generate_series(1, 25)
            """,
            (copy_args,),
        )

        before = rvbbit.execute(
            f"""
            SELECT EXISTS (SELECT 1 FROM rvbbit.tables WHERE table_oid = '{temp_table}'::regclass),
                   (SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = '{temp_table}'::regclass),
                   (SELECT count(*) FROM rvbbit.accel_policy WHERE table_oid = '{temp_table}'::regclass)
            """
        ).fetchone()
        assert before == (False, 0, 0)

        result = rvbbit.execute(
            "SELECT rvbbit.accel_autopilot_observe('pytest')"
        ).fetchone()[0]
        assert result["status"] == "ok"
        assert result["mutations"] == 0
        assert result["query_shapes_resolved"] >= 1
        assert result["activity_source"] is True

        candidate = rvbbit.execute(
            f"""
            SELECT status, hot, eligible, query_calls, users, active_hours,
                   round(attributed_ms)::bigint, observation_count
              FROM rvbbit.accel_autopilot_candidates
             WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()
        assert candidate == ("ready", True, True, 2, 2, 2, 2400, 1)

        # A second pass appends history and updates the durable current row; it
        # still cannot register/build/refresh the table.
        result = rvbbit.execute(
            "SELECT rvbbit.accel_autopilot_observe('pytest')"
        ).fetchone()[0]
        assert result["mutations"] == 0
        assert rvbbit.execute(
            f"""
            SELECT observation_count
              FROM rvbbit.accel_autopilot_candidates
             WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()[0] == 2
        assert rvbbit.execute(
            f"""
            SELECT count(*)
              FROM rvbbit.accel_observer_observations
             WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()[0] == 2

        after = rvbbit.execute(
            f"""
            SELECT EXISTS (SELECT 1 FROM rvbbit.tables WHERE table_oid = '{temp_table}'::regclass),
                   (SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = '{temp_table}'::regclass),
                   (SELECT count(*) FROM rvbbit.accel_policy WHERE table_oid = '{temp_table}'::regclass)
            """
        ).fetchone()
        assert after == before
    finally:
        rvbbit.execute("ROLLBACK")


def test_observer_holds_row_security_tables(rvbbit, temp_table):
    if not _observer_installed(rvbbit):
        pytest.skip("run rvbbit.migrate() to install the workload observer")
    if not rvbbit.execute(
        "SELECT to_regclass('rvbbit.mcp_activity') IS NOT NULL"
    ).fetchone()[0]:
        pytest.skip("warehouse activity table is not installed")

    rvbbit.execute("BEGIN")
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int)")
        rvbbit.execute(f"INSERT INTO {temp_table} SELECT generate_series(1, 10)")
        rvbbit.execute(f"ALTER TABLE {temp_table} ENABLE ROW LEVEL SECURITY")
        rvbbit.execute(f"ANALYZE {temp_table}")
        rvbbit.execute(
            """
            UPDATE rvbbit.accel_autopilot_config
               SET min_calls = 1, min_active_hours = 1,
                   min_attributed_ms = 0, min_table_bytes = 0,
                   max_write_ratio = 1
             WHERE singleton
            """
        )
        rvbbit.execute(
            """
            INSERT INTO rvbbit.mcp_activity
                (ts, caller, tool, args, ok, elapsed_ms)
            VALUES (
                clock_timestamp(),
                'observer-test@example.test', 'run_sql', %s::jsonb, true, 50
            )
            """,
            (json.dumps({"sql": f"SELECT * FROM {temp_table}"}),),
        )

        rvbbit.execute("SELECT rvbbit.accel_autopilot_observe('pytest')")
        status, structurally_eligible, reasons = rvbbit.execute(
            f"""
            SELECT status, structurally_eligible, reasons
              FROM rvbbit.accel_autopilot_candidates
             WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()
        assert status == "held"
        assert structurally_eligible is False
        assert any("row-level-security" in reason for reason in reasons)
    finally:
        rvbbit.execute("ROLLBACK")


def test_observer_off_mode_is_a_recorded_noop(rvbbit):
    if not _observer_installed(rvbbit):
        pytest.skip("run rvbbit.migrate() to install the workload observer")

    rvbbit.execute("BEGIN")
    try:
        before = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.accel_observer_observations"
        ).fetchone()[0]
        rvbbit.execute(
            "UPDATE rvbbit.accel_autopilot_config SET mode = 'off' WHERE singleton"
        )
        result = rvbbit.execute(
            "SELECT rvbbit.accel_autopilot_observe('pytest-off')"
        ).fetchone()[0]
        assert result["status"] == "skipped"
        assert result["mode"] == "off"
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.accel_observer_observations"
        ).fetchone()[0] == before
        assert rvbbit.execute(
            """
            SELECT status, message
              FROM rvbbit.accel_observer_runs
             WHERE source = 'pytest-off'
             ORDER BY run_id DESC LIMIT 1
            """
        ).fetchone() == ("skipped", "observer mode is off")
    finally:
        rvbbit.execute("ROLLBACK")
