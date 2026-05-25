"""Projected avg(length(URL)) by CounterID rewrite."""


def _make_url_len_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "CounterID" integer,
            "URL" text
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("CounterID", "URL") VALUES
            (1, 'aaaa'),
            (1, 'aaaaaa'),
            (2, 'bb'),
            (2, 'bbbb'),
            (2, 'bbb'),
            (3, ''),
            (3, NULL),
            (NULL, 'zzzzzzzzzz'),
            (NULL, 'zz')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_counter_url_len_avg_uses_projected_srf(rvbbit, temp_table):
    t = _make_url_len_table(rvbbit, temp_table)
    sql = (
        f'SELECT "CounterID", AVG(length("URL")) AS l, COUNT(*) AS c FROM {t} '
        'WHERE "URL" <> \'\' GROUP BY "CounterID" HAVING COUNT(*) > 1 '
        'ORDER BY l DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    normalized = [(row[0], float(row[1]), row[2]) for row in rows]
    assert normalized == [(None, 6.0, 2), (1, 5.0, 2)]
