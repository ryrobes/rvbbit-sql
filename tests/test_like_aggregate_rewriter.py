"""Projected LIKE aggregate rewrites for ClickBench text shapes."""


def _make_like_aggregate_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "SearchPhrase" text,
            "URL" text,
            "Title" text,
            "UserID" bigint
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("SearchPhrase", "URL", "Title", "UserID")
        VALUES
            ('alpha', 'http://a-google.example/1', 'Other title', 1),
            ('alpha', 'http://b-google.example/2', 'Other title', 2),
            ('beta', 'http://google.example/b', 'Other title', 3),
            ('', 'http://empty-google.example', 'Other title', 4),
            ('alpha', 'http://foo.example/a', 'Google Z', 10),
            ('alpha', 'http://bar.example/a', 'Google A', 10),
            ('alpha', 'http://baz.example/a', 'Google M', 11),
            ('beta', 'http://qux.example/b', 'Google B', 20),
            ('alpha', 'http://www.google.com/x', 'Google excluded', 12),
            ('gamma', 'http://lower.example/g', 'google lowercase', 30)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_like_count_uses_projected_scalar(rvbbit, temp_table):
    t = _make_like_aggregate_table(rvbbit, temp_table)
    sql = f'SELECT COUNT(*) FROM {t} WHERE "URL" LIKE \'%google%\''

    plan = _plan(rvbbit, sql)
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(5,)]


def test_like_phrase_min_url_uses_projected_srf(rvbbit, temp_table):
    t = _make_like_aggregate_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase", MIN("URL"), COUNT(*) AS c FROM {t} '
        'WHERE "URL" LIKE \'%google%\' AND "SearchPhrase" <> \'\' '
        'GROUP BY "SearchPhrase" ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [
        ("alpha", "http://a-google.example/1", 3),
        ("beta", "http://google.example/b", 1),
    ]


def test_title_url_phrase_rollup_uses_projected_srf(rvbbit, temp_table):
    t = _make_like_aggregate_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase", MIN("URL"), MIN("Title"), COUNT(*) AS c, '
        f'COUNT(DISTINCT "UserID") FROM {t} WHERE "Title" LIKE \'%Google%\' '
        'AND "URL" NOT LIKE \'%.google.%\' AND "SearchPhrase" <> \'\' '
        'GROUP BY "SearchPhrase" ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [
        ("alpha", "http://bar.example/a", "Google A", 3, 2),
        ("beta", "http://qux.example/b", "Google B", 1, 1),
    ]
