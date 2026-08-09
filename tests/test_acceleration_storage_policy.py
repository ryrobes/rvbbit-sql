"""Generation semantics and current-vs-retained accelerator history."""

import psycopg
import pytest


def _snapshot_load(rvbbit, table, rows, *, current=False):
    values = ", ".join(
        f"({int(row_id)}, '{label.replace(chr(39), chr(39) * 2)}')"
        for row_id, label in rows
    )
    fn = "snapshot_load_current" if current else "snapshot_load"
    source_query = f"SELECT * FROM (VALUES {values}) AS src(id, label)"
    return rvbbit.execute(
        f"""
        SELECT generation, rows_loaded, action
        FROM rvbbit.{fn}(
            '{table}'::regclass,
            %s
        )
        """,
        (source_query,),
    ).fetchone()


def _catalog_counts(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT
            (SELECT count(*)::int FROM rvbbit.generations
              WHERE table_oid = '{table}'::regclass),
            (SELECT count(*)::int FROM rvbbit.row_groups
              WHERE table_oid = '{table}'::regclass),
            (SELECT count(*)::int FROM rvbbit.delete_log
              WHERE table_oid = '{table}'::regclass)
        """
    ).fetchone()


def _set_history_policy(rvbbit, table, policy):
    return rvbbit.execute(
        f"""
        SELECT rvbbit.set_acceleration_storage_policy(
            '{table}'::regclass,
            history_policy => %s
        )
        """,
        (policy,),
    ).fetchone()[0]


def test_new_tables_default_to_current_history(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int) USING rvbbit")
    assert rvbbit.execute(
        f"SELECT history_policy FROM rvbbit.tables "
        f"WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()[0] == "current"


def test_retained_snapshot_skips_internal_truncate_tombstones_and_keeps_asof(
    rvbbit, temp_table
):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    _set_history_policy(rvbbit, temp_table, "retained")

    gen1, rows1, action1 = _snapshot_load(
        rvbbit, temp_table, [(1, "old one"), (2, "old two")]
    )
    gen2, rows2, action2 = _snapshot_load(
        rvbbit, temp_table, [(10, "new ten"), (20, "new twenty")]
    )

    assert gen2 > gen1 > 0
    assert (rows1, action1) == (2, "snapshot")
    assert (rows2, action2) == (2, "snapshot")
    # The second load executes TRUNCATE with dirty triggers installed. Its old
    # complete snapshot must remain history, not become millions of tombstones.
    assert _catalog_counts(rvbbit, temp_table) == (2, 2, 0)
    assert rvbbit.execute(
        f"""
        SELECT generation_semantics, history_policy, retained_generations
        FROM rvbbit.acceleration_storage_policy
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone() == ("snapshot", "retained", 2)
    assert rvbbit.execute(
        f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
    ).fetchone()[0] == [10, 20]

    rvbbit.execute("BEGIN")
    try:
        rvbbit.execute(f"SET LOCAL rvbbit.as_of_generation = '{gen1}'")
        old_rows = rvbbit.execute(
            f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
        ).fetchone()[0]
    finally:
        rvbbit.execute("COMMIT")
    assert old_rows == [1, 2]


def test_current_snapshot_keeps_one_generation_and_queues_old_files(
    rvbbit, temp_table
):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")

    gen1, _, _ = _snapshot_load(
        rvbbit, temp_table, [(1, "old one"), (2, "old two")], current=True
    )
    old_paths = rvbbit.execute(
        f"""
        SELECT array_agg(path ORDER BY path)
        FROM (
            SELECT path FROM rvbbit.row_groups
             WHERE table_oid = '{temp_table}'::regclass
            UNION ALL
            SELECT path FROM rvbbit.row_group_variants
             WHERE table_oid = '{temp_table}'::regclass
            UNION ALL
            SELECT path FROM rvbbit.text_dictionaries
             WHERE table_oid = '{temp_table}'::regclass
        ) files
        """
    ).fetchone()[0]

    gen2, rows2, action2 = _snapshot_load(
        rvbbit, temp_table, [(30, "new thirty")], current=True
    )
    assert gen2 > gen1
    assert (rows2, action2) == (1, "snapshot")
    assert _catalog_counts(rvbbit, temp_table) == (1, 1, 0)
    assert rvbbit.execute(
        f"""
        SELECT generation_semantics, history_policy, min_visible_generation
        FROM rvbbit.tables WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone() == ("snapshot", "current", gen2)
    assert rvbbit.execute(
        f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
    ).fetchone()[0] == [30]

    queued = rvbbit.execute(
        f"""
        SELECT array_agg(path ORDER BY path)
        FROM rvbbit.orphaned_files
        WHERE table_oid = '{temp_table}'::regclass
          AND reason = 'current_snapshot_history_prune'
        """
    ).fetchone()[0]
    assert queued == old_paths

    rvbbit.execute("BEGIN")
    try:
        rvbbit.execute(f"SET LOCAL rvbbit.as_of_generation = '{gen1}'")
        with pytest.raises(psycopg.Error, match="history_policy=current"):
            rvbbit.execute(f"SELECT array_agg(id ORDER BY id) FROM {temp_table}")
    finally:
        rvbbit.execute("ROLLBACK")


def test_switching_retained_snapshot_to_current_prunes_existing_history(
    rvbbit, temp_table
):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    _set_history_policy(rvbbit, temp_table, "retained")
    gen1, _, _ = _snapshot_load(rvbbit, temp_table, [(1, "one")])
    gen2, _, _ = _snapshot_load(rvbbit, temp_table, [(2, "two")])
    assert _catalog_counts(rvbbit, temp_table) == (2, 2, 0)

    # Simulate a legacy snapshot_load installation that produced per-row
    # tombstones for its superseded complete snapshot. Opting into current-only
    # must retire that backlog along with the old row group.
    rvbbit.execute(
        f"""
        INSERT INTO rvbbit.delete_log
            (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
        SELECT '{temp_table}'::regclass, rg_id, 0, pg_current_xact_id(), {gen2}
        FROM rvbbit.row_groups
        WHERE table_oid = '{temp_table}'::regclass
          AND generation = {gen1}
        """
    )
    assert _catalog_counts(rvbbit, temp_table) == (2, 2, 1)

    policy = rvbbit.execute(
        f"""
        SELECT rvbbit.set_acceleration_storage_policy(
            '{temp_table}'::regclass,
            history_policy => 'current'
        )
        """
    ).fetchone()[0]
    assert policy["generation_semantics"] == "snapshot"
    assert policy["history_policy"] == "current"
    assert policy["prune"]["generations_pruned"] == 1
    assert policy["prune"]["tombstones_pruned"] == 1
    assert _catalog_counts(rvbbit, temp_table) == (1, 1, 0)
    assert rvbbit.execute(
        f"SELECT min_visible_generation FROM rvbbit.tables "
        f"WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()[0] == gen2


def test_current_cumulative_keeps_correctness_tombstone_until_full_fold(
    rvbbit, temp_table
):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
    )
    gen1 = rvbbit.execute(
        f"""
        SELECT (rvbbit.refresh_acceleration('{temp_table}'::regclass, false)
                ->> 'generation_after')::bigint
        """
    ).fetchone()[0]
    rvbbit.execute(
        f"""
        SELECT rvbbit.set_acceleration_storage_policy(
            '{temp_table}'::regclass,
            history_policy => 'current'
        )
        """
    )

    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 2")
    rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    )
    # Current-only is not permission to resurrect immutable deleted rows. The
    # overlay remains until the major fold materializes the current heap.
    assert rvbbit.execute(
        f"SELECT rvbbit.tombstone_count('{temp_table}'::regclass)"
    ).fetchone()[0] == 1
    policy = rvbbit.execute(
        f"""
        SELECT rvbbit.set_acceleration_storage_policy(
            '{temp_table}'::regclass,
            history_policy => 'current'
        )
        """
    ).fetchone()[0]
    assert policy["fold_required"] is True
    assert rvbbit.execute(
        f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
    ).fetchone()[0] == [1, 3]

    rvbbit.execute("BEGIN")
    try:
        rvbbit.execute(f"SET LOCAL rvbbit.as_of_generation = '{gen1}'")
        with pytest.raises(psycopg.Error, match="history_policy=current"):
            rvbbit.execute(
                f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
            )
    finally:
        rvbbit.execute("ROLLBACK")

    rebuilt = rvbbit.execute(
        f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert rebuilt["status"] == "ok"
    assert rvbbit.execute(
        f"SELECT count(*) FROM rvbbit.delete_log "
        f"WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()[0] == 0
    assert rvbbit.execute(
        f"SELECT history_policy FROM rvbbit.tables "
        f"WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()[0] == "current"
    assert _catalog_counts(rvbbit, temp_table) == (1, 1, 0)
    assert rvbbit.execute(
        f"SELECT (rvbbit.set_acceleration_storage_policy("
        f"'{temp_table}'::regclass, history_policy => 'current')->>'fold_required')::boolean"
    ).fetchone()[0] is False
    assert rvbbit.execute(
        f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
    ).fetchone()[0] == [1, 3]


def test_plain_truncate_retained_history_writes_correctness_tombstones(
    rvbbit, temp_table
):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int PRIMARY KEY) USING rvbbit")
    _set_history_policy(rvbbit, temp_table, "retained")
    rvbbit.execute(f"INSERT INTO {temp_table} SELECT generate_series(1, 3)")
    rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    )

    rvbbit.execute(f"TRUNCATE TABLE {temp_table}")
    assert rvbbit.execute(
        f"SELECT count(*) FROM rvbbit.delete_log "
        f"WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()[0] == 3


def test_plain_truncate_current_history_uses_heap_until_scheduled_full_rebuild(
    rvbbit, temp_table
):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'old one'), (2, 'old two'), (3, 'old three')"
    )
    rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    )

    # This is deliberately ordinary PostgreSQL ETL syntax. The TRUNCATE is one
    # replacement marker, not one delete_log row per prior accelerated row.
    rvbbit.execute(f"TRUNCATE TABLE {temp_table}")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (10, 'new ten'), (20, 'new twenty')"
    )

    assert rvbbit.execute(
        f"SELECT count(*) FROM rvbbit.delete_log "
        f"WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()[0] == 0
    assert rvbbit.execute(
        f"""
        SELECT rvbbit.current_replacement_pending('{temp_table}'::regclass),
               rvbbit.accel_overlay_ready('{temp_table}'::regclass)
        """
    ).fetchone() == (True, False)
    pending_policy = _set_history_policy(rvbbit, temp_table, "current")
    assert pending_policy["fold_required"] is True
    assert "heap-authoritative" in pending_policy["note"]

    plan = "\n".join(
        row[0]
        for row in rvbbit.execute(
            f"EXPLAIN (FORMAT TEXT) SELECT * FROM {temp_table} ORDER BY id"
        ).fetchall()
    )
    assert "RvbbitParquetScan" not in plan
    assert rvbbit.execute(
        f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
    ).fetchone()[0] == [10, 20]

    # A delta refresh must not append the replacement to the obsolete baseline,
    # even when the primary-key identity map is otherwise complete.
    with pytest.raises(psycopg.Error, match="UPDATE/DELETE/TRUNCATE"):
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        )

    rvbbit.execute(
        f"""
        SELECT rvbbit.set_accel_policy(
            '{temp_table}'::regclass,
            strategy => 'scheduled',
            min_interval_secs => 0
        )
        """
    )
    planned = rvbbit.execute(
        f"""
        SELECT tick.action, tick.reason, tick.executed, tick.status
        FROM rvbbit.accel_tick(NULL, true, 1) AS tick
        WHERE tick.table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert planned == ("full", "current replacement pending", False, "planned")

    rebuilt = rvbbit.execute(
        f"""
        SELECT tick.action, tick.reason, tick.executed, tick.status
        FROM rvbbit.accel_tick(1, false, 1) AS tick
        WHERE tick.table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert rebuilt == ("full", "current replacement pending", True, "ok")
    assert rvbbit.execute(
        f"""
        SELECT rvbbit.current_replacement_pending('{temp_table}'::regclass),
               shadow_heap_dirty,
               (SELECT count(*) FROM rvbbit.delete_log
                 WHERE table_oid = '{temp_table}'::regclass)
        FROM rvbbit.tables
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone() == (False, False, 0)
    assert rvbbit.execute(
        f"SELECT array_agg(id ORDER BY id) FROM {temp_table}"
    ).fetchone()[0] == [10, 20]


def test_system_column_queries_stay_on_heap(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int PRIMARY KEY) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} SELECT generate_series(1, 3)")
    rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    )

    # Populate the backend's positive rvbbit-table cache first, then prove a
    # later xmin query is still kept off the user-column-only file scan.
    assert rvbbit.execute(f"SELECT count(*) FROM {temp_table}").fetchone()[0] == 3
    plan = rvbbit.execute(
        f"EXPLAIN (FORMAT JSON) SELECT xmin::text FROM {temp_table}"
    ).fetchone()[0]
    assert "RvbbitParquetScan" not in str(plan)
    assert rvbbit.execute(
        f"SELECT count(*) FROM {temp_table} WHERE rvbbit.xid_to_fxid(xmin) > 0"
    ).fetchone()[0] == 3
