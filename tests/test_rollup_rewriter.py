"""Projected two-integer-key COUNT/SUM/AVG rollup rewrites."""


def _make_rollup_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "SearchEngineID" smallint,
            "WatchID" bigint,
            "ClientIP" integer,
            "SearchPhrase" text,
            "IsRefresh" smallint,
            "ResolutionWidth" smallint
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("SearchEngineID", "WatchID", "ClientIP", "SearchPhrase", "IsRefresh", "ResolutionWidth")
        VALUES
            (1, 100, 10, 'alpha', 1, 100),
            (1, 100, 10, 'alpha', 0, 200),
            (1, 100, 10, 'alpha', 1, NULL),
            (2, 200, 20, 'beta', 1, 400),
            (2, 200, 20, 'beta', NULL, 600),
            (3, 300, 30, '', 1, 800),
            (3, 300, 30, '', 1, 1000)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_filtered_two_int_rollup_uses_projected_srf(rvbbit, temp_table):
    t = _make_rollup_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchEngineID", "ClientIP", COUNT(*) AS c, SUM("IsRefresh"), '
        f'AVG("ResolutionWidth") FROM {t} WHERE "SearchPhrase" <> \'\' '
        'GROUP BY "SearchEngineID", "ClientIP" ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    normalized = [(r[0], r[1], r[2], r[3], float(r[4])) for r in rows]
    assert normalized == [(1, 10, 3, 2, 150.0), (2, 20, 2, 1, 500.0)]


def test_unfiltered_two_int_rollup_uses_projected_srf(rvbbit, temp_table):
    t = _make_rollup_table(rvbbit, temp_table)
    sql = (
        f'SELECT "WatchID", "ClientIP", COUNT(*) AS c, SUM("IsRefresh"), '
        f'AVG("ResolutionWidth") FROM {t} '
        'GROUP BY "WatchID", "ClientIP" ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    normalized = [(r[0], r[1], r[2], r[3], float(r[4])) for r in rows]
    assert normalized == [(100, 10, 3, 2, 150.0), (300, 30, 2, 2, 900.0)]
