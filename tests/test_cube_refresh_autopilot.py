import uuid


def _cube_name() -> str:
    return f"cube_{uuid.uuid4().hex[:8]}"


def _drop_cube(rvbbit, cube: str) -> None:
    try:
        rvbbit.execute("SELECT rvbbit.drop_cube(%s)", (cube,))
    except Exception:
        pass
    for sql in (
        "DELETE FROM rvbbit.cube_refresh_policy WHERE cube_name = %s",
        "DELETE FROM rvbbit.cube_control WHERE cube_name = %s",
        "DELETE FROM rvbbit.cube_defs WHERE name = %s",
    ):
        try:
            rvbbit.execute(sql, (cube,))
        except Exception:
            pass


def test_refresh_cube_applies_and_reports_autopilot_policy(rvbbit, temp_table):
    cube = _cube_name()
    category = f"test_{cube}"
    try:
        rvbbit.execute(
            f"""
            CREATE TABLE {temp_table} (
                id int,
                bucket int,
                amount int
            ) USING rvbbit
            """
        )
        rvbbit.execute(
            f"""
            INSERT INTO {temp_table}
            SELECT g, g % 3, g * 10
            FROM generate_series(1, 2000) AS g
            """
        )
        policy = rvbbit.execute(
            """
            SELECT rvbbit.set_cube_refresh_policy(
                %s,
                p_mode => 'auto',
                p_query_threads => 1,
                p_writer_threads => 3,
                p_scan_chunk_rows => 500,
                p_metadata_profile => 'minimal',
                p_refresh_variants => 'deferred',
                p_refresh_interval_seconds => 999,
                p_note => 'pytest'
            )
            """,
            (cube,),
        ).fetchone()[0]
        assert policy["writer_threads"] == 3
        assert policy["scan_chunk_rows"] == 500
        assert policy["metadata_profile"] == "minimal"
        assert policy["refresh_interval_seconds"] == 999

        cube_sql = f"SELECT id, bucket, amount FROM {temp_table}"
        rvbbit.execute(
            "SELECT rvbbit.define_cube(%s, %s, %s, %s, NULL, NULL, %s)",
            (cube, cube_sql, "one row per source row", "test cube", category),
        )

        status = rvbbit.execute(
            """
            SELECT last_rows,
                   refresh_mode,
                   query_threads,
                   writer_threads,
                   scan_chunk_rows,
                   metadata_profile,
                   refresh_variants,
                   refresh_interval_seconds,
                   last_refresh_seconds,
                   last_refresh_policy
            FROM rvbbit.cube_refresh_status
            WHERE name = %s
            """,
            (cube,),
        ).fetchone()
        assert status[0] == 2000
        assert status[1:8] == ("auto", 1, 3, 500, "minimal", "deferred", 999)
        assert float(status[8]) >= 0
        assert status[9]["writer_threads"] == 3

        health = rvbbit.execute("SELECT rvbbit.cube_health(%s)", (cube,)).fetchone()[0]
        assert health["refresh_policy"]["writer_threads"] == 3
        assert health["refresh_policy"]["refresh_interval_seconds"] == 999
        assert health["autopilot"]["refresh_variants"] == "deferred"
    finally:
        _drop_cube(rvbbit, cube)


def test_refresh_all_cubes_selectively_refreshes_source_changes(rvbbit, temp_table):
    cube = _cube_name()
    category = f"test_{cube}"
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int, amount int) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 10), (2, 20)")
        rvbbit.execute(
            """
            SELECT rvbbit.set_cube_refresh_policy(
                %s,
                p_mode => 'auto',
                p_refresh_interval_seconds => 999999,
                p_refresh_variants => 'deferred'
            )
            """,
            (cube,),
        )
        rvbbit.execute(
            "SELECT rvbbit.define_cube(%s, %s, %s, %s, NULL, NULL, %s)",
            (cube, f"SELECT id, amount FROM {temp_table}", "one row per source row", "auto cube", category),
        )

        status = rvbbit.execute(
            """
            SELECT source_accel_dirty, source_dirty, tracked_source_count, source_count, recommended_action
            FROM rvbbit.cube_refresh_status
            WHERE name = %s
            """,
            (cube,),
        ).fetchone()
        assert status == (False, False, 1, 1, "maintain_storage")

        rvbbit.execute(
            "UPDATE rvbbit.cube_control SET last_refresh_seconds = -42 WHERE cube_name = %s",
            (cube,),
        )
        rvbbit.execute("CALL rvbbit.refresh_all_cubes(p_category => %s, p_sleep_seconds => 0)", (category,))
        assert rvbbit.execute(
            "SELECT last_refresh_seconds FROM rvbbit.cube_refresh_status WHERE name = %s",
            (cube,),
        ).fetchone()[0] == -42

        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (3, 30)")
        assert rvbbit.execute(
            "SELECT source_dirty, recommended_action FROM rvbbit.cube_refresh_status WHERE name = %s",
            (cube,),
        ).fetchone() == (True, "refresh_cube")

        rvbbit.execute("CALL rvbbit.refresh_all_cubes(p_category => %s, p_sleep_seconds => 0)", (category,))
        status = rvbbit.execute(
            """
            SELECT last_rows, source_dirty, recommended_action, last_refresh_seconds >= 0
            FROM rvbbit.cube_refresh_status
            WHERE name = %s
            """,
            (cube,),
        ).fetchone()
        assert status == (3, False, "maintain_storage", True)
    finally:
        _drop_cube(rvbbit, cube)


def test_refresh_all_cubes_skips_manual_policy_but_direct_refresh_runs(rvbbit, temp_table):
    cube = _cube_name()
    category = f"test_{cube}"
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int, amount int) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 10), (2, 20)")
        rvbbit.execute(
            """
            SELECT rvbbit.set_cube_refresh_policy(
                %s,
                p_mode => 'manual',
                p_writer_threads => 2,
                p_refresh_variants => 'deferred'
            )
            """,
            (cube,),
        )
        rvbbit.execute(
            "SELECT rvbbit.define_cube(%s, %s, %s, %s, NULL, NULL, %s)",
            (cube, f"SELECT id, amount FROM {temp_table}", "one row per source row", "manual cube", category),
        )
        assert rvbbit.execute(
            "SELECT last_rows FROM rvbbit.cube_refresh_status WHERE name = %s",
            (cube,),
        ).fetchone()[0] == 2

        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (3, 30)")
        rvbbit.execute("CALL rvbbit.refresh_all_cubes(p_category => %s, p_sleep_seconds => 0)", (category,))
        assert rvbbit.execute(
            "SELECT last_rows, recommended_action FROM rvbbit.cube_refresh_status WHERE name = %s",
            (cube,),
        ).fetchone() == (2, "manual")

        assert rvbbit.execute("SELECT rvbbit.refresh_cube(%s)", (cube,)).fetchone()[0] == 3
        assert rvbbit.execute(
            "SELECT last_rows FROM rvbbit.cube_refresh_status WHERE name = %s",
            (cube,),
        ).fetchone()[0] == 3
    finally:
        _drop_cube(rvbbit, cube)


def test_maintain_builds_cube_layouts_and_refreshes_due_snapshot(rvbbit, temp_table):
    cube = _cube_name()
    category = f"test_{cube}"
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int, amount int) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 10), (2, 20)")
        rvbbit.execute(
            """
            SELECT rvbbit.set_cube_refresh_policy(
                %s,
                p_mode => 'auto',
                p_refresh_interval_seconds => 999999,
                p_refresh_variants => 'deferred'
            )
            """,
            (cube,),
        )
        rvbbit.execute(
            "SELECT rvbbit.define_cube(%s, %s, %s, %s, NULL, NULL, %s)",
            (cube, f"SELECT id, amount FROM {temp_table}", "one row per source row", "maintained cube", category),
        )

        status = rvbbit.execute(
            """
            SELECT lifecycle_state, maintenance_action, needs_maintenance
            FROM rvbbit.maintenance_status
            WHERE target_kind = 'cube' AND target_name = %s
            """,
            (cube,),
        ).fetchone()
        assert status == ("layouts_pending", "build_layouts", True)

        planned = rvbbit.execute(
            "SELECT maintenance_action, executed, status FROM rvbbit.maintain('cube', %s::text, true)",
            (cube,),
        ).fetchone()
        assert planned == ("build_layouts", False, "planned")

        executed = rvbbit.execute(
            "SELECT maintenance_action, executed, status, rows_written > 0 FROM rvbbit.maintain('cube', %s::text)",
            (cube,),
        ).fetchone()
        assert executed == ("build_layouts", True, "ok", True)
        assert rvbbit.execute(
            """
            SELECT lifecycle_state, maintenance_action, needs_maintenance
            FROM rvbbit.maintenance_status
            WHERE target_kind = 'cube' AND target_name = %s
            """,
            (cube,),
        ).fetchone() == ("current", "none", False)

        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (3, 30)")
        assert rvbbit.execute(
            """
            SELECT lifecycle_state, maintenance_action, needs_maintenance
            FROM rvbbit.maintenance_status
            WHERE target_kind = 'cube' AND target_name = %s
            """,
            (cube,),
        ).fetchone() == ("refresh_due", "refresh_snapshot", True)

        executed = rvbbit.execute(
            "SELECT maintenance_action, executed, status, rows_written FROM rvbbit.maintain('cube', %s::text)",
            (cube,),
        ).fetchone()
        assert executed == ("refresh_snapshot", True, "ok", 3)
        assert rvbbit.execute(
            """
            SELECT current_rows, lifecycle_state, needs_maintenance
            FROM rvbbit.maintenance_status
            WHERE target_kind = 'cube' AND target_name = %s
            """,
            (cube,),
        ).fetchone() == (3, "current", False)
    finally:
        _drop_cube(rvbbit, cube)


def test_cube_refresh_hidden_snapshot_tombstones_do_not_block_current_routes(rvbbit, temp_table):
    cube = _cube_name()
    category = f"test_{cube}"
    cube_rel = f"cubes.{cube}"
    try:
        rvbbit.execute(f"CREATE TABLE {temp_table} (id int, amount int) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 10), (2, 20), (3, 30)")
        rvbbit.execute(
            """
            SELECT rvbbit.set_cube_refresh_policy(
                %s,
                p_mode => 'auto',
                p_refresh_interval_seconds => 999999,
                p_refresh_variants => 'deferred'
            )
            """,
            (cube,),
        )
        rvbbit.execute(
            "SELECT rvbbit.define_cube(%s, %s, %s, %s, NULL, NULL, %s)",
            (cube, f"SELECT id, amount FROM {temp_table}", "one row per source row", "snapshot route cube", category),
        )

        assert rvbbit.execute("SELECT rvbbit.refresh_cube(%s)", (cube,)).fetchone()[0] == 3

        raw_tombstones, visible_tombstones, all_row_groups, visible_row_groups = rvbbit.execute(
            f"""
            SELECT
                rvbbit.tombstone_count('{cube_rel}'::regclass),
                rvbbit.visible_tombstone_count('{cube_rel}'::regclass),
                (SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = '{cube_rel}'::regclass),
                (SELECT count(*) FROM rvbbit.row_groups_visible WHERE table_oid = '{cube_rel}'::regclass)
            """
        ).fetchone()
        assert raw_tombstones >= 3
        assert visible_tombstones == 0
        assert all_row_groups >= 2
        assert visible_row_groups == 1

        freshness = rvbbit.execute(
            """
            SELECT parquet_rows, row_groups, tombstones
            FROM rvbbit.accel_freshness
            WHERE table_name = %s
            """,
            (cube_rel,),
        ).fetchone()
        assert freshness == (3, 1, 0)

        explained = rvbbit.execute(
            "SELECT rvbbit.route_explain(%s)",
            (f"SELECT count(*) FROM {cube_rel}",),
        ).fetchone()[0]
        assert explained["rvbbit_tables"][0]["delete_count"] == 0
        assert not any(
            "delete log" in str(candidate.get("reason", "")).lower()
            for candidate in explained["candidates"]
        )
    finally:
        _drop_cube(rvbbit, cube)
