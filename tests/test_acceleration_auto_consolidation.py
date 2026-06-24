import os
import threading
import time

import psycopg


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)


def _row_group_state(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT count(*)::int, coalesce(sum(n_rows), 0)::int
        FROM rvbbit.row_groups
        WHERE table_oid = '{table}'::regclass
        """
    ).fetchone()


def _latest_operation(rvbbit, table):
    return rvbbit.execute(
        f"""
        SELECT operation, status, rows_written, settings
        FROM rvbbit.acceleration_operations
        WHERE table_oid = '{table}'::regclass
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def _set_consolidation_policy(
    rvbbit, table, *, max_row_groups=None, max_tombstones=None
):
    rvbbit.execute(
        f"""
        SELECT rvbbit.set_accel_policy(
            '{table}'::regclass,
            strategy => 'scheduled',
            min_interval_secs => 0,
            full_rebuild_drift_ratio => 1000,
            max_row_groups_before_rebuild => %s,
            max_tombstones_before_rebuild => %s
        )
        """,
        (max_row_groups, max_tombstones),
    )


def _wait_for_rebuild_lock_wait(rvbbit, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        waiting = rvbbit.execute(
            """
            SELECT count(*)::int
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND query LIKE '%rvbbit.rebuild_acceleration%'
              AND wait_event_type = 'Lock'
            """
        ).fetchone()[0]
        if waiting > 0:
            return
        time.sleep(0.05)
    raise AssertionError("rebuild did not reach the final lock wait")


def test_accel_tick_consolidates_clean_row_group_fanout(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )

    for i in range(1, 4):
        rvbbit.execute(f"INSERT INTO {temp_table} VALUES ({i}, 'row {i}')")
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        )

    assert _row_group_state(rvbbit, temp_table) == (3, 3)
    _set_consolidation_policy(rvbbit, temp_table, max_row_groups=3)

    planned = rvbbit.execute(
        f"""
        SELECT action, status, executed, reason
        FROM rvbbit.accel_tick(NULL, true)
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert planned[0:3] == ("full", "planned", False)
    assert planned[3].startswith("row_group_fanout 3 >= 3")

    executed = rvbbit.execute(
        f"""
        SELECT action, status, executed, rows_written, error
        FROM rvbbit.accel_tick(NULL, false)
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert executed == ("full", "ok", True, 3, None)

    assert _row_group_state(rvbbit, temp_table) == (1, 3)
    assert rvbbit.execute(f"SELECT count(*) FROM {temp_table}").fetchone()[0] == 3

    operation, status, rows_written, settings = _latest_operation(rvbbit, temp_table)
    assert (operation, status, rows_written) == ("rebuild_acceleration", "ok", 3)
    assert settings["dropped_row_groups"] == 3


def test_accel_tick_consolidates_clean_tombstone_pressure(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    rvbbit.execute(f"DELETE FROM {temp_table} WHERE id = 2")
    result = rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["rows_written"] == 0
    assert _row_group_state(rvbbit, temp_table) == (1, 3)
    assert rvbbit.execute(
        f"SELECT rvbbit.tombstone_count('{temp_table}'::regclass)"
    ).fetchone()[0] == 1

    _set_consolidation_policy(rvbbit, temp_table, max_tombstones=1)

    planned = rvbbit.execute(
        f"""
        SELECT action, status, executed, reason
        FROM rvbbit.accel_tick(NULL, true)
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert planned[0:3] == ("full", "planned", False)
    assert planned[3].startswith("tombstone_count 1 >= 1")

    executed = rvbbit.execute(
        f"""
        SELECT action, status, executed, rows_written, error
        FROM rvbbit.accel_tick(NULL, false)
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert executed == ("full", "ok", True, 2, None)

    assert _row_group_state(rvbbit, temp_table) == (1, 2)
    assert rvbbit.execute(
        f"SELECT rvbbit.tombstone_count('{temp_table}'::regclass)"
    ).fetchone()[0] == 0
    assert rvbbit.execute(f"SELECT count(*) FROM {temp_table}").fetchone()[0] == 2

    operation, status, rows_written, settings = _latest_operation(rvbbit, temp_table)
    assert (operation, status, rows_written) == ("rebuild_acceleration", "ok", 2)
    assert settings["dropped_row_groups"] == 1


def test_rebuild_stages_fold_and_defers_old_file_reap(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )

    for i in range(1, 4):
        rvbbit.execute(f"INSERT INTO {temp_table} VALUES ({i}, 'row {i}')")
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)"
        )

    old_max_rg_id, old_paths = rvbbit.execute(
        f"""
        SELECT max(rg_id)::int, array_agg(path ORDER BY rg_id)
        FROM rvbbit.row_groups
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert old_max_rg_id == 2
    assert len(old_paths) == 3
    old_file_paths = rvbbit.execute(
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

    result = rvbbit.execute(
        f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)"
    ).fetchone()[0]
    assert result["status"] == "ok"
    assert result["operation"] == "rebuild_acceleration"
    assert result["dropped_row_groups"] == 3
    assert result["queued_orphan_files"] == len(set(old_file_paths))

    new_min_rg_id, row_groups, rows = rvbbit.execute(
        f"""
        SELECT min(rg_id), count(*)::int, sum(n_rows)::int
        FROM rvbbit.row_groups
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert new_min_rg_id > old_max_rg_id
    assert (row_groups, rows) == (1, 3)

    queued_paths = rvbbit.execute(
        f"""
        SELECT array_agg(path ORDER BY path)
        FROM rvbbit.orphaned_files
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()[0]
    assert queued_paths == sorted(set(old_file_paths))
    assert rvbbit.execute(f"SELECT count(*) FROM {temp_table}").fetchone()[0] == 3

    dequeued, unlinked = rvbbit.execute(
        "SELECT files_dequeued, files_unlinked "
        "FROM rvbbit.reap_orphaned_files(interval '0 seconds', 100)"
    ).fetchone()
    assert dequeued >= len(set(old_file_paths))
    assert unlinked >= len(set(old_file_paths))
    assert (
        rvbbit.execute(
            "SELECT count(*) FROM rvbbit.orphaned_files WHERE table_oid = %s::regclass",
            (temp_table,),
        ).fetchone()[0]
        == 0
    )


def test_rebuild_catches_up_update_committed_during_lagged_fold(rvbbit, temp_table):
    rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
    rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
    rvbbit.execute(
        f"CREATE TABLE {temp_table} (id int PRIMARY KEY, label text) USING rvbbit"
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g, 'row ' || g::text
        FROM generate_series(1, 1000) AS g
        """
    )
    rvbbit.execute(f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, false)")

    result_holder = {}
    error_holder = {}

    def run_rebuild():
        try:
            with psycopg.connect(RVBBIT_DSN, autocommit=True) as conn:
                result_holder["result"] = conn.execute(
                    f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)"
                ).fetchone()[0]
        except Exception as exc:  # pragma: no cover - propagated below
            error_holder["error"] = exc

    with psycopg.connect(RVBBIT_DSN) as writer:
        writer.execute(f"UPDATE {temp_table} SET label = 'updated' WHERE id = 1")
        thread = threading.Thread(target=run_rebuild)
        thread.start()
        _wait_for_rebuild_lock_wait(rvbbit)
        writer.commit()
        thread.join(timeout=10)

    assert not thread.is_alive()
    if error_holder:
        raise error_holder["error"]

    result = result_holder["result"]
    assert result["status"] == "ok"
    assert result["catchup_rows"] == 1
    assert result["remapped_tombstones"] == 1
    assert result["rows_written"] == 1001

    rows, tombstones, dirty = rvbbit.execute(
        f"""
        SELECT
            (SELECT coalesce(sum(n_rows), 0)::int
             FROM rvbbit.row_groups
             WHERE table_oid = '{temp_table}'::regclass),
            rvbbit.tombstone_count('{temp_table}'::regclass)::int,
            (SELECT shadow_heap_dirty
             FROM rvbbit.tables
             WHERE table_oid = '{temp_table}'::regclass)
        """
    ).fetchone()
    assert (rows, tombstones, dirty) == (1001, 1, False)
    assert rvbbit.execute(f"SELECT count(*) FROM {temp_table}").fetchone()[0] == 1000
    assert (
        rvbbit.execute(f"SELECT label FROM {temp_table} WHERE id = 1").fetchone()[0]
        == "updated"
    )
