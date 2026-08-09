import uuid

import psycopg

from conftest import RVBBIT_DSN


TABLE_LOCK_CLASS = 1380336724


def _table_lock_key(table_oid: int) -> int:
    return (TABLE_LOCK_CLASS << 32) | table_oid


def test_retire_acceleration_preserves_heap_registry_and_evidence(rvbbit):
    name = f"retire_accel_{uuid.uuid4().hex[:8]}"
    observer_run_id = None
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        rvbbit.execute(
            f"CREATE TABLE {name} (id integer PRIMARY KEY, payload text) USING rvbbit"
        )
        rvbbit.execute(
            f"INSERT INTO {name} "
            "SELECT value, repeat(md5(value::text), 2) "
            "FROM generate_series(1, 512) AS value"
        )
        rvbbit.execute(
            f"""
            SELECT rvbbit.set_accel_policy(
                '{name}'::regclass,
                strategy => 'scheduled',
                min_interval_secs => 0
            )
            """
        )
        rvbbit.execute(
            f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)"
        )
        table_oid = rvbbit.execute(
            f"SELECT '{name}'::regclass::oid"
        ).fetchone()[0]
        old_paths = {
            row[0]
            for row in rvbbit.execute(
                """
                SELECT path FROM rvbbit.row_groups WHERE table_oid = %s
                UNION
                SELECT path FROM rvbbit.row_group_variants WHERE table_oid = %s
                UNION
                SELECT path FROM rvbbit.text_dictionaries WHERE table_oid = %s
                """,
                (table_oid, table_oid, table_oid),
            ).fetchall()
        }
        assert old_paths

        # Preserve observer evidence when materialized supply is retired.
        observer_run_id = rvbbit.execute(
            """
            INSERT INTO rvbbit.accel_observer_runs (
                status, source, finished_at, tables_observed
            ) VALUES ('ok', 'retire-acceleration-test', clock_timestamp(), 1)
            RETURNING run_id
            """
        ).fetchone()[0]
        rvbbit.execute(
            """
            INSERT INTO rvbbit.accel_observer_observations (
                run_id, table_oid, table_name, observed_at,
                relkind, relpersistence, access_method, status, score,
                structurally_eligible, eligible, hot, query_shapes, query_calls,
                users, active_hours, inclusive_ms, attributed_ms,
                p95_ms, seq_scans_total, seq_scans_delta,
                seq_rows_total, seq_rows_delta, writes_total, writes_delta,
                write_ratio, row_estimate, table_bytes, registered,
                acceleration_enabled, row_groups, baseline_missing, reasons
            )
            VALUES (
                %s, %s, %s, clock_timestamp(),
                'r', 'p', 'rvbbit', 'ready', 42.0,
                true, true, true, 1, 3, 1, 1, 120, 120, 120,
                1, 1, 512, 512, 0, 0, 0, 512, 4096,
                true, true, 1, false, ARRAY['test evidence']::text[]
            )
            """,
            (observer_run_id, table_oid, name),
        )
        evidence_before = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.accel_observer_observations WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()[0]

        result = rvbbit.execute(
            "SELECT rvbbit.retire_acceleration(%s::regclass)",
            (table_oid,),
        ).fetchone()[0]
        assert result["status"] == "retired"
        assert result["registry_preserved"] is True
        assert result["heap_preserved"] is True
        assert result["row_groups_retired"] > 0
        assert result["files_queued"] == len(old_paths)

        assert rvbbit.execute(f"SELECT count(*) FROM {name}").fetchone() == (512,)
        assert rvbbit.execute(
            "SELECT acceleration_enabled, shadow_heap_retained, "
            "       min_visible_generation, shadow_heap_dirty "
            "FROM rvbbit.tables WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (True, True, 0, False)
        assert rvbbit.execute(
            "SELECT strategy FROM rvbbit.accel_policy WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == ("manual",)
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (0,)
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.generations WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (0,)
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.orphaned_files "
            "WHERE table_oid = %s AND reason = 'operator_retire_acceleration'",
            (table_oid,),
        ).fetchone() == (len(old_paths),)
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.accel_observer_observations WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (evidence_before,)

        again = rvbbit.execute(
            "SELECT rvbbit.retire_acceleration(%s::regclass)",
            (table_oid,),
        ).fetchone()[0]
        assert again["status"] == "already_unbuilt"
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        if observer_run_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.accel_observer_runs WHERE run_id = %s",
                (observer_run_id,),
            )


def test_retire_acceleration_returns_busy_without_waiting(rvbbit):
    name = f"retire_busy_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        rvbbit.execute(f"CREATE TABLE {name} (id integer) USING rvbbit")
        rvbbit.execute(f"INSERT INTO {name} SELECT generate_series(1, 64)")
        rvbbit.execute(
            f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)"
        )
        table_oid = rvbbit.execute(
            f"SELECT '{name}'::regclass::oid"
        ).fetchone()[0]

        with psycopg.connect(RVBBIT_DSN, autocommit=False) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_table_lock_key(table_oid),),
            )
            result = rvbbit.execute(
                "SELECT rvbbit.retire_acceleration(%s::regclass)",
                (table_oid,),
            ).fetchone()[0]
            holder.rollback()

        assert result["status"] == "busy"
        assert rvbbit.execute(
            "SELECT count(*) > 0 FROM rvbbit.row_groups WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (True,)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
