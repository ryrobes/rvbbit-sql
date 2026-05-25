"""Date columns round-trip through parquet with PostgreSQL date semantics."""


def test_date_round_trips_after_compact(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (d date) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES "
        "('2013-07-15'::date), "
        "('2013-07-16'::date)"
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")

    rows = rvbbit.execute(f"SELECT d::text FROM {temp_table} ORDER BY d").fetchall()
    assert rows == [("2013-07-15",), ("2013-07-16",)]

    count = rvbbit.execute(
        f"SELECT count(*) FROM {temp_table} "
        "WHERE d >= '2013-07-01'::date AND d <= '2013-07-31'::date"
    ).fetchone()[0]
    assert count == 2


def test_min_max_date_rewrites_from_row_group_stats(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (d date) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES "
        "('2013-07-15'::date), "
        "('2013-07-16'::date), "
        "('2013-07-14'::date)"
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")

    sql = f"SELECT min(d), max(d) FROM {temp_table}"
    plan = "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )
    assert "Result" in plan
    assert "Custom Scan" not in plan
    assert "Seq Scan" not in plan

    row = rvbbit.execute(f"SELECT min(d)::text, max(d)::text FROM {temp_table}").fetchone()
    assert row == ("2013-07-14", "2013-07-16")
