import os
import threading
import time
import uuid

import psycopg
from psycopg.conninfo import make_conninfo


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)


def test_preloaded_router_is_inert_without_extension_catalog():
    """Sibling databases may host pg_cron without installing pg_rvbbit."""
    dbname = f"rvbbit_catalogless_{uuid.uuid4().hex[:8]}"
    admin_dsn = make_conninfo(RVBBIT_DSN, dbname="postgres")
    test_dsn = make_conninfo(RVBBIT_DSN, dbname=dbname)

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {dbname}")

    try:
        with psycopg.connect(test_dsn, autocommit=True) as conn:
            assert conn.execute(
                "SELECT to_regclass('rvbbit.tables')"
            ).fetchone() == (None,)
            conn.execute("CREATE TABLE public.scheduler_probe (id integer)")
            assert conn.execute(
                "SELECT count(*) FROM public.scheduler_probe"
            ).fetchone() == (0,)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {dbname}")


def test_unrelated_accelerated_lock_does_not_block_routing(rvbbit):
    """A lock queue on relation A must never enter relation B's route path."""
    suffix = uuid.uuid4().hex[:8]
    locked_table = f"route_locked_{suffix}"
    query_table = f"route_free_{suffix}"
    try:
        rvbbit.execute(
            f"CREATE TABLE {locked_table} (id int PRIMARY KEY) USING rvbbit"
        )
        rvbbit.execute(
            f"CREATE TABLE {query_table} (id int PRIMARY KEY) USING rvbbit"
        )
        rvbbit.execute(f"INSERT INTO {locked_table} VALUES (1)")
        rvbbit.execute(f"INSERT INTO {query_table} VALUES (1), (2), (3)")
        rvbbit.execute(
            f"SELECT rvbbit.rebuild_acceleration('{locked_table}'::regclass, false)"
        )
        rvbbit.execute(
            f"SELECT rvbbit.rebuild_acceleration('{query_table}'::regclass, false)"
        )

        locked = threading.Event()
        release = threading.Event()
        blocker_errors = []

        def hold_unrelated_lock():
            try:
                with psycopg.connect(RVBBIT_DSN, autocommit=False) as blocker:
                    blocker.execute(
                        f"LOCK TABLE {locked_table} IN ACCESS EXCLUSIVE MODE"
                    )
                    locked.set()
                    release.wait(timeout=4)
                    blocker.rollback()
            except Exception as exc:  # surfaced in the assertion below
                blocker_errors.append(exc)
                locked.set()

        blocker_thread = threading.Thread(target=hold_unrelated_lock, daemon=True)
        blocker_thread.start()
        assert locked.wait(timeout=2)
        try:
            with psycopg.connect(RVBBIT_DSN, autocommit=True) as reader:
                reader.execute("SET statement_timeout = '6s'")
                reader.execute("SET rvbbit.route_force_candidate = 'duck_vector'")
                started = time.monotonic()
                row = reader.execute(
                    f"SELECT count(*)::bigint FROM {query_table}"
                ).fetchone()
                elapsed = time.monotonic() - started
        finally:
            release.set()
            blocker_thread.join(timeout=2)

        assert not blocker_errors
        assert row == (3,)
        assert elapsed < 1.5
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {query_table} CASCADE")
        rvbbit.execute(f"DROP TABLE IF EXISTS {locked_table} CASCADE")


def test_catalog_change_cursor_is_transactional(rvbbit):
    before = rvbbit.execute(
        "SELECT coalesce(max(id), 0) FROM rvbbit.accel_catalog_changes"
    ).fetchone()[0]
    table = f"route_cursor_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(RVBBIT_DSN, autocommit=False) as writer:
            writer.execute(f"CREATE TABLE {table} (id int) USING rvbbit")
            writer.execute(f"INSERT INTO {table} VALUES (1)")
            invisible = rvbbit.execute(
                "SELECT coalesce(max(id), 0) FROM rvbbit.accel_catalog_changes"
            ).fetchone()[0]
            assert invisible == before
            writer.commit()

        after = rvbbit.execute(
            "SELECT coalesce(max(id), 0) FROM rvbbit.accel_catalog_changes"
        ).fetchone()[0]
        assert after > before

        rvbbit.execute(f"ALTER TABLE {table} ADD COLUMN label text")
        after_ddl = rvbbit.execute(
            "SELECT coalesce(max(id), 0) FROM rvbbit.accel_catalog_changes"
        ).fetchone()[0]
        assert after_ddl > after
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def test_legacy_tick_budget_commits_one_table_per_heartbeat(rvbbit):
    """Old accel_tick(4, false) cron text must no longer batch table locks."""
    suffix = uuid.uuid4().hex[:8]
    tables = [f"tick_isolated_a_{suffix}", f"tick_isolated_b_{suffix}"]
    try:
        for table in tables:
            rvbbit.execute(
                f"CREATE TABLE {table} (id int PRIMARY KEY, label text) USING rvbbit"
            )
            rvbbit.execute(
                f"INSERT INTO {table} VALUES (1, 'one'), (2, 'two'), (3, 'three')"
            )
            rvbbit.execute(f"ANALYZE {table}")
            rvbbit.execute(
                f"""
                SELECT rvbbit.set_accel_policy(
                    '{table}'::regclass,
                    strategy => 'scheduled',
                    min_interval_secs => 0
                )
                """
            )

        oid_list = ", ".join(f"'{table}'::regclass" for table in tables)
        planned = rvbbit.execute(
            f"""
            SELECT count(*)
            FROM rvbbit.accel_tick(4, true)
            WHERE table_oid IN ({oid_list})
              AND status = 'planned'
            """
        ).fetchone()[0]
        assert planned == 2

        rows = rvbbit.execute(
            f"""
            SELECT table_oid, executed, status
            FROM rvbbit.accel_tick(4, false)
            WHERE table_oid IN ({oid_list})
            """
        ).fetchall()
        # Execution mode now stops after the single permitted action instead
        # of manufacturing a "tick budget reached" row for every remaining
        # candidate in the same registry scan.
        assert len(rows) == 1
        assert rows[0][1:] == (True, "ok")
    finally:
        for table in reversed(tables):
            rvbbit.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
