"""Per-group metadata rewrites for simple filtered counts/group-bys."""


def _make_smallint_group_table(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (a smallint) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES "
        "(0::smallint), "
        "(1::smallint), "
        "(1::smallint), "
        "(2::smallint), "
        "(NULL::smallint)"
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def test_count_star_with_int_filter_rewrites_to_result(rvbbit, temp_table):
    t = _make_smallint_group_table(rvbbit, temp_table)

    sql = f"SELECT count(*) FROM {t} WHERE a <> 0"
    plan = "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )
    assert "Result" in plan
    assert "Custom Scan" not in plan
    assert "Seq Scan" not in plan

    row = rvbbit.execute(sql).fetchone()
    assert row == (3,)


def test_groupby_count_with_group_filter_uses_metadata_srf(rvbbit, temp_table):
    t = _make_smallint_group_table(rvbbit, temp_table)

    sql = (
        f"SELECT a, count(*) FROM {t} WHERE a <> 0 "
        "GROUP BY a ORDER BY count(*) DESC, a"
    )
    plan = "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Seq Scan" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(1, 2), (2, 1)]
