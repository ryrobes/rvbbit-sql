import os
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)


def _labels(rvbbit, table):
    return rvbbit.execute(
        f"SELECT count(*) AS n, string_agg(label, ',' ORDER BY label) AS labels FROM {table}"
    ).fetchone()


def _dirty_flags(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT shadow_heap_dirty, dirty_has_insert, dirty_has_update,
               dirty_has_delete, dirty_has_truncate
        FROM rvbbit.tables
        WHERE table_oid = '{table}'::regclass
        """
    ).fetchone()


def _remove_one_identity_map_entry(rvbbit, table):
    rvbbit.execute(
        f"""
        DELETE FROM rvbbit.row_identity_map
        WHERE ctid IN (
            SELECT ctid
            FROM rvbbit.row_identity_map
            WHERE table_oid = '{table}'::regclass
            LIMIT 1
        )
        """
    )


def test_dirty_marker_fallback_does_not_wait_on_catalog_row(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'old')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    locker = psycopg.connect(RVBBIT_DSN, autocommit=False)
    writer = psycopg.connect(RVBBIT_DSN, autocommit=True)
    try:
        locker.execute(
            f"""
            SELECT 1
            FROM rvbbit.tables
            WHERE table_oid = '{temp_table}'::regclass
            FOR UPDATE
            """
        )

        writer.execute("SET lock_timeout = '250ms'")
        writer.execute(f"UPDATE {temp_table} SET label = 'new' WHERE id = 1")

        raw_dirty, effective_dirty, effective_update, markers = rvbbit.execute(
            f"""
            SELECT t.shadow_heap_dirty,
                   ds.shadow_heap_dirty,
                   ds.dirty_has_update,
                   (SELECT count(*)
                    FROM rvbbit.table_dirty_markers m
                    WHERE m.table_oid = '{temp_table}'::regclass
                      AND m.dirty_op = 'U')::int
            FROM rvbbit.tables t
            JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
            WHERE t.table_oid = '{temp_table}'::regclass
            """
        ).fetchone()

        assert raw_dirty is False
        assert effective_dirty is True
        assert effective_update is True
        assert markers >= 1
    finally:
        locker.rollback()
        locker.close()
        writer.close()

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert _labels(rvbbit, temp_table) == (1, "new")
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert (
        rvbbit.execute(
            f"""
            SELECT count(*)::int
            FROM rvbbit.table_dirty_markers
            WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()[0]
        == 0
    )


def test_dirty_marker_fallback_allows_parallel_same_table_writers(rvbbit, temp_table):
    writer_count = 8
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit")
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g, 'old'
        FROM generate_series(1, {writer_count}) AS g
        """
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    locker = psycopg.connect(RVBBIT_DSN, autocommit=False)
    start = threading.Barrier(writer_count)

    def write_one(row_id):
        with psycopg.connect(RVBBIT_DSN, autocommit=True) as writer:
            writer.execute("SET lock_timeout = '250ms'")
            start.wait(timeout=5)
            writer.execute(
                f"UPDATE {temp_table} SET label = %s WHERE id = %s",
                (f"new {row_id}", row_id),
            )

    try:
        locker.execute(
            f"""
            SELECT 1
            FROM rvbbit.tables
            WHERE table_oid = '{temp_table}'::regclass
            FOR UPDATE
            """
        )

        with ThreadPoolExecutor(max_workers=writer_count) as pool:
            futures = [pool.submit(write_one, i) for i in range(1, writer_count + 1)]
            for future in futures:
                future.result(timeout=10)

        raw_dirty, effective_dirty, effective_update, markers = rvbbit.execute(
            f"""
            SELECT t.shadow_heap_dirty,
                   ds.shadow_heap_dirty,
                   ds.dirty_has_update,
                   (SELECT count(*)
                    FROM rvbbit.table_dirty_markers m
                    WHERE m.table_oid = '{temp_table}'::regclass
                      AND m.dirty_op = 'U')::int
            FROM rvbbit.tables t
            JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
            WHERE t.table_oid = '{temp_table}'::regclass
            """
        ).fetchone()
        routed_updated_rows = rvbbit.execute(
            f"SELECT count(*)::int FROM {temp_table} WHERE label LIKE 'new %'"
        ).fetchone()[0]
        rvbbit.execute("SET rvbbit.force_heap_scan = on")
        try:
            heap_updated_rows = rvbbit.execute(
                f"SELECT count(*)::int FROM {temp_table} WHERE label LIKE 'new %'"
            ).fetchone()[0]
        finally:
            rvbbit.execute("RESET rvbbit.force_heap_scan")

        assert raw_dirty is False
        assert effective_dirty is True
        assert effective_update is True
        assert markers >= 1
        assert routed_updated_rows == writer_count
        assert heap_updated_rows == writer_count
    finally:
        locker.rollback()
        locker.close()

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
    assert (
        rvbbit.execute(
            f"""
            SELECT count(*)::int
            FROM rvbbit.table_dirty_markers
            WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()[0]
        == 0
    )


def test_update_dirty_episode_requires_rebuild_when_overlay_incomplete(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'old')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")
    _remove_one_identity_map_entry(rvbbit, temp_table)

    rvbbit.execute(f"UPDATE {temp_table} SET label = 'new' WHERE id = 1")
    assert _labels(rvbbit, temp_table) == (1, "new")
    assert _dirty_flags(rvbbit, temp_table) == (True, False, True, False, False)

    with pytest.raises(Exception) as exc:
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        ).fetchone()
    assert "UPDATE/DELETE/TRUNCATE" in str(exc.value)

    assert _labels(rvbbit, temp_table) == (1, "new")

    rvbbit.execute(f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)")
    assert _labels(rvbbit, temp_table) == (1, "new")
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)


def test_delete_dirty_episode_requires_rebuild_when_overlay_incomplete(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'keep'), (2, 'delete_me')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")
    _remove_one_identity_map_entry(rvbbit, temp_table)

    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 2")
    assert _labels(rvbbit, temp_table) == (1, "keep")
    assert _dirty_flags(rvbbit, temp_table) == (True, False, False, True, False)

    with pytest.raises(Exception) as exc:
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        ).fetchone()
    assert "UPDATE/DELETE/TRUNCATE" in str(exc.value)

    assert _labels(rvbbit, temp_table) == (1, "keep")

    rvbbit.execute(f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)")
    assert _labels(rvbbit, temp_table) == (1, "keep")
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)


def test_insert_only_dirty_episode_still_delta_refreshes(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (1, 'one')")
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (2, 'two')")
    assert _labels(rvbbit, temp_table) == (2, "one,two")
    assert _dirty_flags(rvbbit, temp_table) == (True, True, False, False, False)

    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 1

    assert _labels(rvbbit, temp_table) == (2, "one,two")
    assert _dirty_flags(rvbbit, temp_table) == (False, False, False, False, False)
