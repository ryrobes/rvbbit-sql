import uuid


def test_storage_maintenance_never_builds_manual_unbuilt_table(rvbbit):
    name = f"storage_manual_unbuilt_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        rvbbit.execute(f"CREATE TABLE {name} (id integer PRIMARY KEY) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {name} SELECT generate_series(1, 128)")
        table_oid = rvbbit.execute(
            f"SELECT '{name}'::regclass::oid"
        ).fetchone()[0]
        # Reproduce the post-reset state seen on an upgraded installation:
        # registry retained, no row groups, and a durable dirty flag left by
        # subsequent source writes.
        rvbbit.execute(
            "UPDATE rvbbit.tables SET shadow_heap_dirty = true, "
            "dirty_has_insert = true, dirty_since = clock_timestamp() "
            "WHERE table_oid = %s",
            (table_oid,),
        )

        assert rvbbit.execute(
            "SELECT shadow_heap_dirty FROM rvbbit.tables WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (True,)
        assert rvbbit.execute(
            "SELECT strategy FROM rvbbit.accel_policy_effective WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == ("manual",)

        rvbbit.execute("SELECT rvbbit.maintain_storage(100, false)")

        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (0,)
        assert rvbbit.execute(f"SELECT count(*) FROM {name}").fetchone() == (128,)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_storage_maintenance_never_refreshes_manual_built_table(rvbbit):
    name = f"storage_manual_built_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        rvbbit.execute(f"CREATE TABLE {name} (id integer PRIMARY KEY) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {name} SELECT generate_series(1, 128)")
        rvbbit.execute(
            f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)"
        )
        rvbbit.execute(f"INSERT INTO {name} SELECT generate_series(129, 160)")
        table_oid = rvbbit.execute(
            f"SELECT '{name}'::regclass::oid"
        ).fetchone()[0]
        groups_before = rvbbit.execute(
            "SELECT count(*), coalesce(sum(n_rows), 0) "
            "FROM rvbbit.row_groups WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()
        variants_before = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.row_group_variants WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()

        assert rvbbit.execute(
            "SELECT shadow_heap_dirty FROM rvbbit.tables WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (True,)
        rvbbit.execute("SELECT rvbbit.maintain_storage(100, true)")

        assert rvbbit.execute(
            "SELECT count(*), coalesce(sum(n_rows), 0) "
            "FROM rvbbit.row_groups WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == groups_before
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.row_group_variants WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == variants_before
        assert rvbbit.execute(
            "SELECT shadow_heap_dirty FROM rvbbit.tables WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (True,)
        assert rvbbit.execute(f"SELECT count(*) FROM {name}").fetchone() == (160,)
        definition = rvbbit.execute(
            "SELECT pg_get_functiondef("
            "'rvbbit.maintain_storage(bigint,boolean)'::regprocedure)"
        ).fetchone()[0]
        assert "Storage housekeeping must never create supply" in definition
        assert "Variant housekeeping respects the same automation boundary" in definition
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
