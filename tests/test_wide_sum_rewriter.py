"""Wide SUM expression rewrite over projected parquet columns."""


def _make_smallint_table(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (w smallint) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} SELECT 30000::smallint FROM generate_series(1, 10000)"
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _make_bigint_table(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (v bigint) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES "
        "(2522247420139142823), "
        "(2522247420139142824)"
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def test_agg_sum_scans_exact_smallint_without_stats_overflow(rvbbit, temp_table):
    t = _make_smallint_table(rvbbit, temp_table)

    row = rvbbit.execute(f"SELECT rvbbit.agg_sum('{t}'::regclass, 'w')").fetchone()
    assert row == (300000000.0,)


def test_wide_sum_int2_plus_const_rewrites_to_result(rvbbit, temp_table):
    t = _make_smallint_table(rvbbit, temp_table)

    sql = f"SELECT SUM(w + 0), SUM(w + 1), SUM(w + 89) FROM {t}"
    plan = "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )
    assert "Result" in plan
    assert "Custom Scan" not in plan
    assert "Seq Scan" not in plan

    row = rvbbit.execute(sql).fetchone()
    assert row == (300000000, 300010000, 300890000)


def test_simple_sum_count_avg_rewrites_to_result(rvbbit, temp_table):
    t = _make_smallint_table(rvbbit, temp_table)

    sql = f"SELECT SUM(w), COUNT(*), AVG(w) FROM {t}"
    plan = "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )
    assert "Result" in plan
    assert "Custom Scan" not in plan
    assert "Seq Scan" not in plan

    row = rvbbit.execute(sql).fetchone()
    assert row[0] == 300000000
    assert row[1] == 10000
    assert str(row[2]) == "30000.000000000000"


def test_bigint_avg_rewrites_with_postgres_numeric_rounding(rvbbit, temp_table):
    t = _make_bigint_table(rvbbit, temp_table)

    sql = f"SELECT AVG(v) FROM {t}"
    plan = "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )
    assert "Result" in plan
    assert "Custom Scan" not in plan
    assert "Seq Scan" not in plan

    row = rvbbit.execute(sql).fetchone()
    assert str(row[0]) == "2522247420139142824"
