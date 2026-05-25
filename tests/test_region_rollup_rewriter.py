"""Projected one-key COUNT/SUM/AVG/COUNT(DISTINCT) rollup rewrites."""


def _make_region_rollup_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "RegionID" integer,
            "AdvEngineID" smallint,
            "ResolutionWidth" smallint,
            "UserID" bigint
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("RegionID", "AdvEngineID", "ResolutionWidth", "UserID")
        VALUES
            (1, NULL, 100, 10),
            (1, NULL, 200, 10),
            (1, NULL, NULL, 11),
            (2, 5, 400, 20),
            (2, 7, NULL, 21),
            (3, 9, 900, 30)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_region_rollup_uses_projected_srf(rvbbit, temp_table):
    t = _make_region_rollup_table(rvbbit, temp_table)
    sql = (
        f'SELECT "RegionID", SUM("AdvEngineID"), COUNT(*) AS c, '
        f'AVG("ResolutionWidth"), COUNT(DISTINCT "UserID") FROM {t} '
        'GROUP BY "RegionID" ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    normalized = [(r[0], r[1], r[2], float(r[3]), r[4]) for r in rows]
    assert normalized == [(1, None, 3, 150.0, 2), (2, 12, 2, 400.0, 2)]
