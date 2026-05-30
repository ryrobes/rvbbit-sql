"""Adaptive route profile control-plane tests."""

from __future__ import annotations

import uuid


def _profile(active: bool = False) -> str:
    return (
        '{"version":1,'
        '"name":"pytest",'
        f'"active":{str(active).lower()},'
        '"entries":{},'
        '"profile_points":[]}'
    )


def test_route_profile_override_helpers(rvbbit):
    old_active = rvbbit.execute(
        "SELECT name FROM rvbbit.route_profiles WHERE active LIMIT 1"
    ).fetchone()
    active_name = f"pytest_active_{uuid.uuid4().hex[:8]}"
    alt_name = f"pytest_alt_{uuid.uuid4().hex[:8]}"

    try:
        rvbbit.execute(
            "SELECT rvbbit.route_set_profile(%s, %s::jsonb, true)",
            (active_name, _profile(True)),
        )
        rvbbit.execute(
            "SELECT rvbbit.route_set_profile(%s, %s::jsonb, false)",
            (alt_name, _profile(False)),
        )

        current = rvbbit.execute("SELECT rvbbit.route_current_profile()").fetchone()[0]
        assert current["profile_name"] == active_name
        assert current["profile_source"] == "active"
        assert current["requested_profile"] is None

        selected = rvbbit.execute(
            "SELECT rvbbit.route_use_profile(%s, false)", (alt_name,)
        ).fetchone()[0]
        assert selected["profile_name"] == alt_name
        assert selected["profile_source"] == "guc"
        assert selected["requested_profile"] == alt_name

        explained = rvbbit.execute(
            "SELECT rvbbit.route_explain('SELECT 1')"
        ).fetchone()[0]
        assert explained["profile_name"] == alt_name
        assert explained["profile_source"] == "guc"

        cleared = rvbbit.execute(
            "SELECT rvbbit.route_clear_profile(false)"
        ).fetchone()[0]
        assert cleared["profile_name"] == active_name
        assert cleared["profile_source"] == "active"
        assert cleared["requested_profile"] is None
    finally:
        rvbbit.execute("SELECT rvbbit.route_clear_profile(false)")
        rvbbit.execute(
            "UPDATE rvbbit.route_profiles SET active = false WHERE name IN (%s, %s)",
            (active_name, alt_name),
        )
        rvbbit.execute(
            "DELETE FROM rvbbit.route_profiles WHERE name IN (%s, %s)",
            (active_name, alt_name),
        )
        if old_active is not None:
            rvbbit.execute(
                "UPDATE rvbbit.route_profiles SET active = true WHERE name = %s",
                (old_active[0],),
            )


def test_route_profiles_and_status_helpers(rvbbit):
    profile_name = f"pytest_status_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.route_set_profile(%s, %s::jsonb, true)",
            (profile_name, _profile(True)),
        )

        profiles = rvbbit.execute("SELECT rvbbit.route_profiles()").fetchone()[0]
        profile = next((item for item in profiles if item["name"] == profile_name), None)
        assert profile is not None
        assert profile["active"] is True
        assert profile["entries"] == 0
        assert profile["points"] == 0

        status = rvbbit.execute("SELECT rvbbit.route_status()").fetchone()[0]
        assert status["current_profile"]["profile_name"] == profile_name
        assert status["runtime"]["duck_backend_fail_open"] is True
        assert {
            item["candidate"] for item in status["candidate_gates"]
        } == {
            "rvbbit_native",
            "datafusion_mem",
            "datafusion_hive",
            "datafusion_vortex",
            "datafusion_vector",
            "duck_hive",
            "duck_vortex",
            "duck_vector",
            "pg_rowstore",
        }
    finally:
        rvbbit.execute("SELECT rvbbit.route_clear_profile(false)")
        rvbbit.execute(
            "UPDATE rvbbit.route_profiles SET active = false WHERE name = %s",
            (profile_name,),
        )
        rvbbit.execute(
            "DELETE FROM rvbbit.route_profiles WHERE name = %s",
            (profile_name,),
        )


def test_route_telemetry_profile_columns_exist(rvbbit):
    cols = {
        row[0]
        for row in rvbbit.execute(
            """
            SELECT table_name || '.' || column_name
            FROM information_schema.columns
            WHERE table_schema = 'rvbbit'
              AND table_name IN ('route_decisions', 'route_executions')
              AND column_name IN ('profile_name', 'profile_source')
            """
        ).fetchall()
    }
    assert cols == {
        "route_decisions.profile_name",
        "route_decisions.profile_source",
        "route_executions.profile_name",
        "route_executions.profile_source",
    }


def test_missing_route_profile_guc_does_not_fall_back_to_active(rvbbit):
    missing_name = f"pytest_missing_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT set_config('rvbbit.route_profile', %s, false)", (missing_name,)
        )
        current = rvbbit.execute("SELECT rvbbit.route_current_profile()").fetchone()[0]
        assert current["requested_profile"] == missing_name
        assert current["profile_name"] is None
        assert current["profile_source"] == "guc-missing"
        assert missing_name in current["profile_warning"]

        explained = rvbbit.execute(
            "SELECT rvbbit.route_explain('SELECT 1')"
        ).fetchone()[0]
        assert explained["requested_profile"] == missing_name
        assert explained["profile_name"] is None
        assert explained["profile_source"] == "guc-missing"
    finally:
        rvbbit.execute("SELECT rvbbit.route_clear_profile(false)")


def test_no_active_profile_uses_conservative_native_route(rvbbit, temp_table):
    old_active = rvbbit.execute(
        "SELECT name FROM rvbbit.route_profiles WHERE active LIMIT 1"
    ).fetchone()
    try:
        rvbbit.execute(
            f"CREATE TABLE {temp_table} (id int, val text) USING rvbbit"
        )
        rvbbit.execute(
            f"INSERT INTO {temp_table} SELECT g, 'v' || g FROM generate_series(1, 20) g"
        )
        rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)").fetchone()
        rvbbit.execute("UPDATE rvbbit.route_profiles SET active = false WHERE active")

        explained = rvbbit.execute(
            f"SELECT rvbbit.route_explain('SELECT sum(id) FROM {temp_table}')"
        ).fetchone()[0]
        assert explained["profile_name"] is None
        assert explained["profile_source"] == "none"
        assert explained["chosen_candidate"] == "rvbbit_native"
        assert explained["route_source"] == "hard-rule"
        assert "native simple aggregate metadata" in explained["reason"]
    finally:
        if old_active is not None:
            rvbbit.execute(
                "UPDATE rvbbit.route_profiles SET active = true WHERE name = %s",
                (old_active[0],),
            )


def test_sql_route_training_records_candidates(rvbbit, temp_table):
    old_active = rvbbit.execute(
        "SELECT name FROM rvbbit.route_profiles WHERE active LIMIT 1"
    ).fetchone()
    profile_name = f"pytest_sql_train_{uuid.uuid4().hex[:8]}"
    query = (
        f"SELECT grp, count(DISTINCT id) AS c "
        f"FROM {temp_table} GROUP BY grp ORDER BY grp"
    )
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int, grp int) USING rvbbit")
        rvbbit.execute(
            f"INSERT INTO {temp_table} SELECT g, g % 5 FROM generate_series(1, 1000) g"
        )
        rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)").fetchone()
        rvbbit.execute("UPDATE rvbbit.route_profiles SET active = false WHERE active")

        trained = rvbbit.execute(
            "SELECT rvbbit.route_train_query(%s, %s, 1, 0.0, false, %s, %s)",
            (
                profile_name,
                query,
                "rvbbit_native,datafusion_vector",
                "pytest sql training",
            ),
        ).fetchone()[0]
        assert trained["profile"] == profile_name
        assert trained["run_id"] > 0
        training_query_id = trained["training_query_id"]

        rows = rvbbit.execute(
            """
            SELECT candidate, ok_runs, error_runs, last_validation_status
            FROM rvbbit.route_training_summary
            WHERE profile_name = %s AND training_query_id = %s
            """,
            (profile_name, training_query_id),
        ).fetchall()
        by_candidate = {row[0]: row for row in rows}
        assert by_candidate["rvbbit_native"][1] == 1
        assert by_candidate["rvbbit_native"][3] == "baseline"
        assert (
            by_candidate["datafusion_vector"][1]
            + by_candidate["datafusion_vector"][2]
        ) == 1

        deleted = rvbbit.execute(
            "SELECT rvbbit.route_training_delete_query(%s, %s, true)",
            (profile_name, training_query_id),
        ).fetchone()[0]
        assert deleted["deleted"] == 1
    finally:
        rvbbit.execute("SELECT rvbbit.route_clear_profile(false)")
        rvbbit.execute("DELETE FROM rvbbit.route_profiles WHERE name = %s", (profile_name,))
        if old_active is not None:
            rvbbit.execute(
                "UPDATE rvbbit.route_profiles SET active = true WHERE name = %s",
                (old_active[0],),
            )


def test_duck_backend_fail_open_uses_native_fallback(rvbbit):
    rows = rvbbit.execute(
        "SELECT rvbbit.duck_query_json(%s, %s::jsonb, 10)",
        ("SELECT current_setting('server_version') AS x", '["x"]'),
    ).fetchone()[0]
    assert len(rows) == 1
    assert rows[0]["x"]


def test_route_summary_views_include_profile_dimensions(rvbbit):
    views = {
        row[0]
        for row in rvbbit.execute(
            """
            SELECT table_name || '.' || column_name
            FROM information_schema.columns
            WHERE table_schema = 'rvbbit'
              AND table_name IN ('route_decision_summary', 'route_runtime_summary')
              AND column_name IN ('profile_name', 'profile_source')
            """
        ).fetchall()
    }
    assert views == {
        "route_decision_summary.profile_name",
        "route_decision_summary.profile_source",
        "route_runtime_summary.profile_name",
        "route_runtime_summary.profile_source",
    }
