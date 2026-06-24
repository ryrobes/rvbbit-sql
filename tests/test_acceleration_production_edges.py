import os

import psycopg


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)
PROD_EDGE_ROWS = int(os.environ.get("RVBBIT_PROD_EDGE_ROWS", "15000"))
PROD_EDGE_INSERT_ROWS = int(os.environ.get("RVBBIT_PROD_EDGE_INSERT_ROWS", "750"))
PROD_ATTACH_ROWS = int(os.environ.get("RVBBIT_PROD_ATTACH_ROWS", "5000"))


def _heap_scalar(conn, sql):
    conn.execute("SET rvbbit.force_heap_scan = on")
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.execute("RESET rvbbit.force_heap_scan")


def _assert_routed_matches_heap(conn, sql):
    routed = conn.execute(sql).fetchone()
    heap = _heap_scalar(conn, sql)
    assert routed == heap
    return routed


def _dirty_state(conn, relname):
    return conn.execute(
        f"""
        SELECT shadow_heap_dirty, dirty_has_insert, dirty_has_update,
               dirty_has_delete, dirty_has_truncate
        FROM rvbbit.table_dirty_state
        WHERE table_oid = '{relname}'::regclass
        """
    ).fetchone()


def _marker_count(conn, relname):
    return conn.execute(
        f"""
        SELECT count(*)::int
        FROM rvbbit.table_dirty_markers
        WHERE table_oid = '{relname}'::regclass
        """
    ).fetchone()[0]


def test_large_table_mixed_churn_refresh_and_rebuild_matches_heap(rvbbit, temp_table):
    base_rows = PROD_EDGE_ROWS
    inserted_rows = PROD_EDGE_INSERT_ROWS
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            id int PRIMARY KEY,
            tenant_id int NOT NULL,
            bucket int NOT NULL,
            amount bigint NOT NULL,
            payload text NOT NULL,
            created_at timestamptz NOT NULL
        ) USING rvbbit
        """
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g,
               g % 32,
               g % 97,
               ((g * 13) % 100000)::bigint,
               'payload-' || (g % 251)::text,
               timestamptz '2026-01-01 00:00:00+00' + (g || ' seconds')::interval
        FROM generate_series(1, {base_rows}) AS g
        """
    )

    initial = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert initial["status"] == "ok"
    assert initial["rows_written"] == base_rows
    assert _dirty_state(rvbbit, temp_table) == (False, False, False, False, False)

    aggregate_sql = f"""
        SELECT count(*)::bigint,
               coalesce(sum(amount), 0)::bigint,
               count(*) FILTER (WHERE payload LIKE '%:u%')::bigint,
               min(id),
               max(id),
               count(DISTINCT tenant_id)::bigint
        FROM {temp_table}
    """
    _assert_routed_matches_heap(rvbbit, aggregate_sql)

    rvbbit.execute(
        f"""
        UPDATE {temp_table}
           SET amount = amount + 7,
               payload = payload || ':u'
         WHERE id % 10 = 0
        """
    )
    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id % 17 = 0")
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g,
               g % 32,
               g % 97,
               ((g * 13) % 100000)::bigint,
               'payload-new-' || (g % 251)::text,
               timestamptz '2026-02-01 00:00:00+00' + (g || ' seconds')::interval
        FROM generate_series({base_rows + 1}, {base_rows + inserted_rows}) AS g
        """
    )

    dirty = _dirty_state(rvbbit, temp_table)
    assert dirty[0] is True
    assert dirty[1:4] == (True, True, True)
    _assert_routed_matches_heap(rvbbit, aggregate_sql)

    refreshed = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert refreshed["status"] == "ok"
    assert refreshed["rows_written"] > 0
    assert _dirty_state(rvbbit, temp_table) == (False, False, False, False, False)
    assert _marker_count(rvbbit, temp_table) == 0
    _assert_routed_matches_heap(rvbbit, aggregate_sql)

    rebuilt = rvbbit.execute(
        f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert rebuilt["status"] == "ok"
    assert _dirty_state(rvbbit, temp_table) == (False, False, False, False, False)
    assert _marker_count(rvbbit, temp_table) == 0
    assert rvbbit.execute(
        f"SELECT rvbbit.tombstone_count('{temp_table}'::regclass)"
    ).fetchone()[0] == 0
    _assert_routed_matches_heap(rvbbit, aggregate_sql)


def test_rolled_back_mutation_leaves_no_dirty_markers_or_tombstones(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    with psycopg.connect(RVBBIT_DSN, autocommit=False) as writer:
        writer.execute(f"UPDATE {temp_table} SET label = 'two_v2' WHERE id = 2")
        writer.execute(f"DELETE FROM {temp_table} WHERE id = 3")
        writer.execute(f"INSERT INTO {temp_table} VALUES (4, 'four')")
        writer.rollback()

    assert _dirty_state(rvbbit, temp_table) == (False, False, False, False, False)
    assert _marker_count(rvbbit, temp_table) == 0
    assert rvbbit.execute(
        f"SELECT rvbbit.tombstone_count('{temp_table}'::regclass)"
    ).fetchone()[0] == 0
    assert rvbbit.execute(
        f"SELECT count(*)::int, string_agg(label, ',' ORDER BY id) FROM {temp_table}"
    ).fetchone() == (3, "one,two,three")


def test_partition_parent_dml_marks_accelerated_child_partitions(rvbbit, temp_table):
    p0 = f"{temp_table}_p0"
    p1 = f"{temp_table}_p1"
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            id int NOT NULL,
            bucket int NOT NULL,
            label text NOT NULL,
            PRIMARY KEY (id, bucket)
        ) PARTITION BY RANGE (bucket)
        """
    )
    rvbbit.execute(
        f"CREATE TABLE {p0} PARTITION OF {temp_table} FOR VALUES FROM (0) TO (10) USING rvbbit"
    )
    rvbbit.execute(
        f"CREATE TABLE {p1} PARTITION OF {temp_table} FOR VALUES FROM (10) TO (20) USING rvbbit"
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} VALUES
            (1, 1, 'one'),
            (2, 2, 'two'),
            (3, 11, 'three'),
            (4, 12, 'four')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{p0}'::regclass, false)")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{p1}'::regclass, false)")

    parent_sql = f"""
        SELECT count(*)::int, string_agg(id::text || ':' || bucket::text || ':' || label, ',' ORDER BY id)
        FROM {temp_table}
    """
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (
        4,
        "1:1:one,2:2:two,3:11:three,4:12:four",
    )

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'three_u' WHERE id = 3")
    rvbbit.execute(f"UPDATE {temp_table} SET bucket = 13, label = 'one_moved' WHERE id = 1")
    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 2")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (5, 15, 'five')")

    assert _dirty_state(rvbbit, p0)[0] is True
    assert _dirty_state(rvbbit, p1)[0] is True
    assert rvbbit.execute(f"SELECT rvbbit.tombstone_count('{p0}'::regclass)").fetchone()[0] >= 2
    assert rvbbit.execute(f"SELECT rvbbit.tombstone_count('{p1}'::regclass)").fetchone()[0] >= 1
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (
        4,
        "1:13:one_moved,3:11:three_u,4:12:four,5:15:five",
    )

    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{p0}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{p1}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"

    assert _dirty_state(rvbbit, p0) == (False, False, False, False, False)
    assert _dirty_state(rvbbit, p1) == (False, False, False, False, False)
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (
        4,
        "1:13:one_moved,3:11:three_u,4:12:four,5:15:five",
    )

    rvbbit.execute(f"TRUNCATE {temp_table}")
    assert _dirty_state(rvbbit, p0)[4] is True
    assert _dirty_state(rvbbit, p1)[4] is True
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (0, None)

    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{p0}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{p1}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert _dirty_state(rvbbit, p0) == (False, False, False, False, False)
    assert _dirty_state(rvbbit, p1) == (False, False, False, False, False)
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (0, None)


def test_attach_detach_preserves_accelerated_partition_dirty_tracking(rvbbit, temp_table):
    child = f"{temp_table}_attached"
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            id int NOT NULL,
            bucket int NOT NULL,
            label text NOT NULL,
            PRIMARY KEY (id, bucket)
        ) PARTITION BY RANGE (bucket)
        """
    )
    rvbbit.execute(
        f"""
        CREATE TABLE {child} (
            id int NOT NULL,
            bucket int NOT NULL,
            label text NOT NULL,
            PRIMARY KEY (id, bucket),
            CHECK (bucket >= 20 AND bucket < 30)
        ) USING rvbbit
        """
    )
    rvbbit.execute(
        f"""
        INSERT INTO {child} VALUES
            (20, 20, 'twenty'),
            (21, 21, 'twentyone'),
            (22, 22, 'twentytwo')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{child}'::regclass, false)")

    standalone_triggers = {
        row[0]
        for row in rvbbit.execute(
            f"""
            SELECT tgname
            FROM pg_trigger
            WHERE tgrelid = '{child}'::regclass
              AND NOT tgisinternal
            """
        ).fetchall()
    }
    assert "rvbbit_shadow_heap_dirty_update" in standalone_triggers
    assert "rvbbit_shadow_heap_dirty_row_update" not in standalone_triggers

    rvbbit.execute(
        f"ALTER TABLE {temp_table} ATTACH PARTITION {child} FOR VALUES FROM (20) TO (30)"
    )
    attached_triggers = {
        row[0]
        for row in rvbbit.execute(
            f"""
            SELECT tgname
            FROM pg_trigger
            WHERE tgrelid = '{child}'::regclass
              AND NOT tgisinternal
            """
        ).fetchall()
    }
    assert "rvbbit_shadow_heap_dirty_update" not in attached_triggers
    assert "rvbbit_shadow_heap_dirty_row_update" in attached_triggers

    parent_sql = f"""
        SELECT count(*)::int, string_agg(id::text || ':' || bucket::text || ':' || label, ',' ORDER BY id)
        FROM {temp_table}
    """
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (
        3,
        "20:20:twenty,21:21:twentyone,22:22:twentytwo",
    )

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'twenty_u' WHERE id = 20")
    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 21")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (23, 23, 'twentythree')")

    assert _dirty_state(rvbbit, child)[0:4] == (True, True, True, True)
    assert rvbbit.execute(f"SELECT rvbbit.tombstone_count('{child}'::regclass)").fetchone()[0] >= 2
    assert _assert_routed_matches_heap(rvbbit, parent_sql) == (
        3,
        "20:20:twenty_u,22:22:twentytwo,23:23:twentythree",
    )

    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{child}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert _dirty_state(rvbbit, child) == (False, False, False, False, False)

    rvbbit.execute(f"ALTER TABLE {temp_table} DETACH PARTITION {child}")
    assert rvbbit.execute(
        f"SELECT relispartition FROM pg_class WHERE oid = '{child}'::regclass"
    ).fetchone()[0] is False

    rvbbit.execute(f"UPDATE {child} SET label = 'twentytwo_u' WHERE id = 22")
    rvbbit.execute(f"DELETE FROM {child} WHERE id = 23")
    rvbbit.execute(f"INSERT INTO {child} VALUES (24, 24, 'twentyfour')")

    assert _dirty_state(rvbbit, child)[0:4] == (True, True, True, True)
    child_sql = f"""
        SELECT count(*)::int, string_agg(id::text || ':' || bucket::text || ':' || label, ',' ORDER BY id)
        FROM {child}
    """
    assert _assert_routed_matches_heap(rvbbit, child_sql) == (
        3,
        "20:20:twenty_u,22:22:twentytwo_u,24:24:twentyfour",
    )
    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{child}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert _dirty_state(rvbbit, child) == (False, False, False, False, False)
    assert _assert_routed_matches_heap(rvbbit, child_sql) == (
        3,
        "20:20:twenty_u,22:22:twentytwo_u,24:24:twentyfour",
    )


def test_large_attach_detach_partition_matches_heap(rvbbit, temp_table):
    child = f"{temp_table}_attached_big"
    attach_rows = PROD_ATTACH_ROWS
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            id int NOT NULL,
            bucket int NOT NULL,
            amount bigint NOT NULL,
            label text NOT NULL,
            PRIMARY KEY (id, bucket)
        ) PARTITION BY RANGE (bucket)
        """
    )
    rvbbit.execute(
        f"""
        CREATE TABLE {child} (
            id int NOT NULL,
            bucket int NOT NULL,
            amount bigint NOT NULL,
            label text NOT NULL,
            PRIMARY KEY (id, bucket),
            CHECK (bucket >= 100 AND bucket < 200)
        ) USING rvbbit
        """
    )
    rvbbit.execute(
        f"""
        INSERT INTO {child}
        SELECT g,
               100 + (g % 100),
               (g * 19)::bigint,
               'row-' || g::text
        FROM generate_series(1, {attach_rows}) AS g
        """
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{child}'::regclass, false)")
    rvbbit.execute(
        f"ALTER TABLE {temp_table} ATTACH PARTITION {child} FOR VALUES FROM (100) TO (200)"
    )

    aggregate_sql = f"""
        SELECT count(*)::bigint,
               coalesce(sum(amount), 0)::bigint,
               count(*) FILTER (WHERE label LIKE '%:u%')::bigint,
               min(id),
               max(id)
        FROM {temp_table}
    """
    assert _assert_routed_matches_heap(rvbbit, aggregate_sql)[0] == attach_rows

    rvbbit.execute(
        f"""
        UPDATE {temp_table}
           SET amount = amount + 11,
               label = label || ':u'
         WHERE id % 11 = 0
        """
    )
    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id % 13 = 0")
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g,
               100 + (g % 100),
               (g * 19)::bigint,
               'row-new-' || g::text
        FROM generate_series({attach_rows + 1}, {attach_rows + 250}) AS g
        """
    )

    assert _dirty_state(rvbbit, child)[0:4] == (True, True, True, True)
    _assert_routed_matches_heap(rvbbit, aggregate_sql)
    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{child}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert _dirty_state(rvbbit, child) == (False, False, False, False, False)
    _assert_routed_matches_heap(rvbbit, aggregate_sql)

    rvbbit.execute(f"ALTER TABLE {temp_table} DETACH PARTITION {child}")
    detached_sql = f"""
        SELECT count(*)::bigint,
               coalesce(sum(amount), 0)::bigint,
               count(*) FILTER (WHERE label LIKE '%:d%')::bigint,
               min(id),
               max(id)
        FROM {child}
    """
    rvbbit.execute(
        f"""
        UPDATE {child}
           SET amount = amount + 5,
               label = label || ':d'
         WHERE id % 17 = 0
        """
    )
    rvbbit.execute(f"DELETE FROM {child} WHERE id % 19 = 0")
    rvbbit.execute(
        f"""
        INSERT INTO {child}
        SELECT g,
               100 + (g % 100),
               (g * 19)::bigint,
               'row-detached-' || g::text
        FROM generate_series({attach_rows + 251}, {attach_rows + 500}) AS g
        """
    )

    assert _dirty_state(rvbbit, child)[0:4] == (True, True, True, True)
    _assert_routed_matches_heap(rvbbit, detached_sql)
    assert rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{child}'::regclass, false)"
    ).fetchone()[0]["status"] == "ok"
    assert _dirty_state(rvbbit, child) == (False, False, False, False, False)
    _assert_routed_matches_heap(rvbbit, detached_sql)
