import uuid


def _cube_name() -> str:
    return f"cube_{uuid.uuid4().hex[:8]}"


def _drop_cube(rvbbit, cube: str) -> None:
    try:
        rvbbit.execute("SELECT rvbbit.drop_cube(%s)", (cube,))
    except Exception:
        pass
    rvbbit.execute("DELETE FROM rvbbit.cube_refresh_policy WHERE cube_name = %s", (cube,))
    rvbbit.execute("DELETE FROM rvbbit.cube_control WHERE cube_name = %s", (cube,))
    rvbbit.execute("DELETE FROM rvbbit.cube_defs WHERE name = %s", (cube,))


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
