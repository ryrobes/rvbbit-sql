"""Projected top-count rewrite for UserID/minute/SearchPhrase groups."""


def _make_minute_topcount_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "UserID" bigint,
            "EventTime" timestamp,
            "SearchPhrase" text
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("UserID", "EventTime", "SearchPhrase")
        VALUES
            (1, '2020-01-01 00:02:03', 'alpha'),
            (1, '2020-01-01 01:02:59', 'alpha'),
            (1, '2020-01-01 02:02:00', 'alpha'),
            (2, '2020-01-01 00:05:00', 'beta'),
            (2, '2020-01-01 00:05:10', 'beta'),
            (3, NULL, 'gamma')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_int_minute_text_topcount_uses_projected_srf(rvbbit, temp_table):
    t = _make_minute_topcount_table(rvbbit, temp_table)
    sql = (
        f'SELECT "UserID", extract(minute FROM "EventTime") AS m, "SearchPhrase", COUNT(*) '
        f'FROM {t} GROUP BY "UserID", m, "SearchPhrase" '
        'ORDER BY COUNT(*) DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    normalized = [(r[0], int(r[1]) if r[1] is not None else None, r[2], r[3]) for r in rows]
    assert normalized == [(1, 2, "alpha", 3), (2, 5, "beta", 2)]
