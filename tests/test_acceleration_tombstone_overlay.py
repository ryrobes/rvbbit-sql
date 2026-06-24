import pytest


def _rows(rvbbit, table):
    return rvbbit.execute(f"SELECT id, label FROM {table} ORDER BY id").fetchall()


def _count_with_scan_barrier(rvbbit, table):
    return rvbbit.execute(
        f"SELECT count(*) FROM (SELECT id FROM {table} OFFSET 0) s"
    ).fetchone()[0]


def _dirty_flags(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT shadow_heap_dirty, dirty_has_insert, dirty_has_update,
               dirty_has_delete, dirty_has_truncate
        FROM rvbbit.tables
        WHERE table_oid = '{table}'::regclass
        """
    ).fetchone()


def _overlay_state(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT rvbbit.accel_overlay_ready('{table}'::regclass),
               (SELECT count(*) FROM rvbbit.row_identity_map
                WHERE table_oid = '{table}'::regclass),
               rvbbit.tombstone_count('{table}'::regclass),
               (SELECT count(*) FROM rvbbit.row_groups
                WHERE table_oid = '{table}'::regclass)
        """
    ).fetchone()


def _identity_mode(rvbbit, table):
    return rvbbit.execute(
        f"SELECT rvbbit.accel_identity_mode('{table}'::regclass)"
    ).fetchone()[0]


def _visible_columns(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = '{table}'::regclass
          AND attnum > 0
          AND NOT attisdropped
        ORDER BY attnum
        """
    ).fetchall()


def _heap_ctids(rvbbit, table):
    rvbbit.execute("SET rvbbit.force_heap_scan = on")
    try:
        return dict(
            rvbbit.execute(f"SELECT id, ctid::text FROM {table} ORDER BY id").fetchall()
        )
    finally:
        rvbbit.execute("RESET rvbbit.force_heap_scan")


def _ctid_identity_state(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT rvbbit.accel_ctid_identity_valid('{table}'::regclass),
               t.ctid_identity_relfilenode,
               pg_relation_filenode('{table}'::regclass)
        FROM rvbbit.tables t
        WHERE t.table_oid = '{table}'::regclass
        """
    ).fetchone()


def test_pk_update_delta_refresh_uses_tombstone_overlay(rvbbit, temp_table):
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    assert _overlay_state(rvbbit, temp_table) == (True, 3, 0, 1)

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'two_v2' WHERE id = 2")
    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2"), (3, "three")]
    assert _dirty_flags(rvbbit, temp_table) == (True, False, True, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 1, 1)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 1
    assert result["row_groups_written"] == 1

    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2"), (3, "three")]
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 4, 1, 2)


def test_pk_delete_delta_refresh_uses_tombstone_overlay(rvbbit, temp_table):
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'keep'), (2, 'delete_me'), (3, 'keep2')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 2")
    assert _rows(rvbbit, temp_table) == [(1, "keep"), (3, "keep2")]
    assert _dirty_flags(rvbbit, temp_table) == (True, False, False, True, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 1, 1)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 0
    assert result["row_groups_written"] == 0

    assert _rows(rvbbit, temp_table) == [(1, "keep"), (3, "keep2")]
    assert _count_with_scan_barrier(rvbbit, temp_table) == 2
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 1, 1)


def test_pk_truncate_delta_refresh_tombstones_all_rows(rvbbit, temp_table):
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    rvbbit.execute(f"TRUNCATE {temp_table}")
    assert _count_with_scan_barrier(rvbbit, temp_table) == 0
    assert _dirty_flags(rvbbit, temp_table) == (True, False, False, False, True)
    assert _overlay_state(rvbbit, temp_table) == (True, 2, 2, 1)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 0
    assert result["row_groups_written"] == 0

    assert _count_with_scan_barrier(rvbbit, temp_table) == 0
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 2, 2, 1)


def test_no_pk_update_delta_refresh_uses_ctid_overlay(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.accel_identity_map = 'on'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    assert _identity_mode(rvbbit, temp_table) == "ctid"
    assert _visible_columns(rvbbit, temp_table) == [("id",), ("label",)]
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 0, 1)

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'two_v2' WHERE id = 2")
    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2"), (3, "three")]
    assert _dirty_flags(rvbbit, temp_table) == (True, False, True, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 1, 1)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 1
    assert result["row_groups_written"] == 1

    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2"), (3, "three")]
    assert _count_with_scan_barrier(rvbbit, temp_table) == 3
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 4, 1, 2)


def test_identity_map_primary_key_policy_keeps_pk_overlay(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.accel_identity_map = 'primary_key'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    assert _identity_mode(rvbbit, temp_table) == "primary_key"
    assert _overlay_state(rvbbit, temp_table) == (True, 2, 0, 1)


def test_identity_map_primary_key_policy_skips_ctid_overlay(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    assert _identity_mode(rvbbit, temp_table) == "ctid"
    assert _overlay_state(rvbbit, temp_table) == (False, 0, 0, 1)

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'two_v2' WHERE id = 2")
    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2")]
    with pytest.raises(Exception) as exc:
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        ).fetchone()
    assert "UPDATE/DELETE/TRUNCATE" in str(exc.value)


def test_no_pk_delete_delta_refresh_uses_ctid_overlay(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.accel_identity_map = 'on'")
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'keep'), (2, 'delete_me'), (3, 'keep2')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 2")
    assert _rows(rvbbit, temp_table) == [(1, "keep"), (3, "keep2")]
    assert _dirty_flags(rvbbit, temp_table) == (True, False, False, True, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 1, 1)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 0
    assert result["row_groups_written"] == 0

    assert _rows(rvbbit, temp_table) == [(1, "keep"), (3, "keep2")]
    assert _count_with_scan_barrier(rvbbit, temp_table) == 2
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 1, 1)


def test_no_pk_positional_insert_still_works_with_ctid_overlay(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (3, 'three')")
    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two"), (3, "three")]
    assert _dirty_flags(rvbbit, temp_table) == (True, True, False, False, False)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 1
    assert result["row_groups_written"] == 1

    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two"), (3, "three")]
    assert _visible_columns(rvbbit, temp_table) == [("id",), ("label",)]


def test_no_pk_cluster_rewrite_invalidates_ctid_overlay(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.accel_identity_map = 'on'")
    idx = f"{temp_table}_id_desc_idx"
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    valid, recorded_node, current_node = _ctid_identity_state(rvbbit, temp_table)
    assert valid is True
    assert recorded_node == current_node
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 0, 1)

    before_ctids = _heap_ctids(rvbbit, temp_table)
    rvbbit.execute(f"CREATE INDEX {idx} ON {temp_table} (id DESC)")
    rvbbit.execute(f"CLUSTER {temp_table} USING {idx}")
    after_ctids = _heap_ctids(rvbbit, temp_table)
    assert before_ctids[1] != after_ctids[1]

    valid, recorded_node, current_node = _ctid_identity_state(rvbbit, temp_table)
    assert valid is False
    assert recorded_node != current_node
    assert _overlay_state(rvbbit, temp_table) == (False, 3, 0, 1)

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'two_v2' WHERE id = 2")
    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2"), (3, "three")]
    assert _dirty_flags(rvbbit, temp_table) == (True, False, True, False, False)
    assert _overlay_state(rvbbit, temp_table) == (False, 3, 0, 1)

    with pytest.raises(Exception) as exc:
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        ).fetchone()
    assert "UPDATE/DELETE/TRUNCATE" in str(exc.value)

    result = rvbbit.execute(
        f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 3

    valid, recorded_node, current_node = _ctid_identity_state(rvbbit, temp_table)
    assert valid is True
    assert recorded_node == current_node
    assert _rows(rvbbit, temp_table) == [(1, "one"), (2, "two_v2"), (3, "three")]
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)


def test_no_pk_truncate_after_rewrite_tombstones_all_row_groups(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.accel_identity_map = 'on'")
    idx = f"{temp_table}_id_desc_idx"
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")
    rvbbit.execute(f"CREATE INDEX {idx} ON {temp_table} (id DESC)")
    rvbbit.execute(f"CLUSTER {temp_table} USING {idx}")

    assert _overlay_state(rvbbit, temp_table) == (False, 3, 0, 1)

    rvbbit.execute(f"TRUNCATE {temp_table}")
    assert _count_with_scan_barrier(rvbbit, temp_table) == 0
    assert _dirty_flags(rvbbit, temp_table) == (True, False, False, False, True)
    assert _overlay_state(rvbbit, temp_table) == (True, 3, 3, 1)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 0
    assert result["row_groups_written"] == 0

    assert _count_with_scan_barrier(rvbbit, temp_table) == 0
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
