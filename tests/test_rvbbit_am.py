"""RVBBIT registry mode and USING rvbbit compatibility alias."""

import uuid

import psycopg
from psycopg.conninfo import make_conninfo

from conftest import RVBBIT_DSN


def test_create_table_using_rvbbit_registers_in_catalog(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int) USING rvbbit")
    row = rvbbit.execute(
        f"""
        SELECT a.amname,
               rvbbit.is_rvbbit_table('{temp_table}'::regclass),
               count(t.*)
        FROM pg_class c
        JOIN pg_am a ON a.oid = c.relam
        LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
        WHERE c.oid = '{temp_table}'::regclass
        GROUP BY a.amname
        """
    ).fetchone()
    assert row == ("heap", True, 1)


def test_create_table_with_heap_doesnt_register(rvbbit):
    rvbbit.execute("DROP TABLE IF EXISTS not_rvbbit_tbl")
    rvbbit.execute("CREATE TABLE not_rvbbit_tbl (id int)")  # default heap
    row = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.tables WHERE table_oid::regclass::text = 'not_rvbbit_tbl'"
    ).fetchone()
    rvbbit.execute("DROP TABLE not_rvbbit_tbl")
    assert row[0] == 0


def test_heap_table_can_be_enabled_and_disabled(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int)")
    assert (
        rvbbit.execute(
            f"SELECT rvbbit.is_rvbbit_table('{temp_table}'::regclass)"
        ).fetchone()[0]
        is False
    )

    enabled = rvbbit.execute(
        f"SELECT rvbbit.enable_table('{temp_table}'::regclass)"
    ).fetchone()[0]
    assert enabled["status"] == "enabled"
    assert enabled["registered_before"] is False
    assert (
        rvbbit.execute(
            f"SELECT rvbbit.is_rvbbit_table('{temp_table}'::regclass)"
        ).fetchone()[0]
        is True
    )

    disabled = rvbbit.execute(
        f"SELECT rvbbit.disable_table('{temp_table}'::regclass)"
    ).fetchone()[0]
    assert disabled["status"] == "disabled"
    assert disabled["enabled_before"] is True
    row = rvbbit.execute(
        f"""
        SELECT rvbbit.is_rvbbit_table('{temp_table}'::regclass),
               acceleration_enabled
        FROM rvbbit.tables
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchone()
    assert row == (False, False)


def test_drop_rvbbit_table_cleans_catalog(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int) USING rvbbit")
    row = rvbbit.execute(
        f"SELECT count(*) FROM rvbbit.tables WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()
    assert row[0] == 1
    rvbbit.execute(f"DROP TABLE {temp_table}")
    # Lookup by name now (oid no longer resolvable):
    row = rvbbit.execute(
        f"SELECT count(*) FROM rvbbit.tables t "
        f"WHERE NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = t.table_oid) "
        f"  AND table_oid::text = '{temp_table}'"
    ).fetchone()
    assert row[0] == 0


def test_insert_select_through_rvbbit_am(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, val text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} SELECT g, 'v' || g FROM generate_series(1, 50) g"
    )
    row = rvbbit.execute(f"SELECT count(*), max(id) FROM {temp_table}").fetchone()
    assert row == (50, 50)


def test_using_rvbbit_alias_survives_extension_uninstall():
    dbname = f"rvbbit_detach_{uuid.uuid4().hex[:8]}"
    admin_dsn = make_conninfo(RVBBIT_DSN, dbname="postgres")
    test_dsn = make_conninfo(RVBBIT_DSN, dbname=dbname)

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {dbname}")
        admin.execute(f"CREATE DATABASE {dbname}")

    try:
        with psycopg.connect(test_dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION pg_rvbbit")
            conn.execute("SELECT rvbbit.migrate()")
            conn.execute("CREATE TABLE public.alias_probe(id int) USING rvbbit")
            conn.execute("INSERT INTO public.alias_probe VALUES (1)")
            am_name = conn.execute(
                """
                SELECT a.amname
                FROM pg_class c
                JOIN pg_am a ON a.oid = c.relam
                WHERE c.oid = 'public.alias_probe'::regclass
                """
            ).fetchone()[0]
            assert am_name == "heap"

            conn.execute("DROP EXTENSION pg_rvbbit CASCADE")
            assert conn.execute("SELECT count(*) FROM public.alias_probe").fetchone()[0] == 1
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {dbname}")
