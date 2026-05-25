"""CREATE TABLE USING rvbbit: AM registration + DDL trigger."""


def test_create_table_using_rvbbit_registers_in_catalog(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int) USING rvbbit")
    row = rvbbit.execute(
        f"SELECT count(*) FROM rvbbit.tables WHERE table_oid = '{temp_table}'::regclass"
    ).fetchone()
    assert row[0] == 1


def test_create_table_with_heap_doesnt_register(rvbbit):
    rvbbit.execute("DROP TABLE IF EXISTS not_rvbbit_tbl")
    rvbbit.execute("CREATE TABLE not_rvbbit_tbl (id int)")  # default heap
    row = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.tables WHERE table_oid::regclass::text = 'not_rvbbit_tbl'"
    ).fetchone()
    rvbbit.execute("DROP TABLE not_rvbbit_tbl")
    assert row[0] == 0


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
