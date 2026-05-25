"""Projected one-column top-count GROUP BY rewrites."""


def _make_top_count_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "SearchPhrase" text,
            "UserID" bigint,
            "URL" text
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("SearchPhrase", "UserID", "URL") VALUES
            ('alpha', 10, '/a'),
            ('alpha', 10, '/a'),
            ('alpha', 10, '/a'),
            ('beta', 20, '/b'),
            ('beta', 20, '/b'),
            ('gamma', 30, '/c'),
            ('', 40, '/d'),
            (NULL, 50, '/e')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _make_high_cardinality_user_table(rvbbit, temp_table):
    rvbbit.execute(f'CREATE TABLE {temp_table} ("UserID" bigint) USING rvbbit')
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("UserID")
        SELECT g::bigint FROM generate_series(1, 300) g
        UNION ALL SELECT 10
        UNION ALL SELECT 10
        UNION ALL SELECT 20
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _make_clientip_table(rvbbit, temp_table):
    rvbbit.execute(f'CREATE TABLE {temp_table} ("ClientIP" integer) USING rvbbit')
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("ClientIP") VALUES
            (10), (10), (10),
            (20), (20),
            (30),
            (NULL)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_text_group_count_filter_uses_top_count_srf(rvbbit, temp_table):
    t = _make_top_count_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase", COUNT(*) AS c FROM {t} '
        'WHERE "SearchPhrase" <> \'\' GROUP BY "SearchPhrase" '
        'ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [("alpha", 3), ("beta", 2)]


def test_bigint_group_count_uses_top_count_srf(rvbbit, temp_table):
    t = _make_high_cardinality_user_table(rvbbit, temp_table)
    sql = (
        f'SELECT "UserID", COUNT(*) FROM {t} '
        'GROUP BY "UserID" ORDER BY COUNT(*) DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(10, 3), (20, 2)]


def test_literal_plus_text_group_count_uses_top_count_srf(rvbbit, temp_table):
    t = _make_top_count_table(rvbbit, temp_table)
    sql = (
        f'SELECT 1, "URL", COUNT(*) AS c FROM {t} '
        'GROUP BY 1, "URL" ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(1, "/a", 3), (1, "/b", 2)]


def test_integer_derived_group_count_uses_top_count_srf(rvbbit, temp_table):
    t = _make_clientip_table(rvbbit, temp_table)
    sql = (
        f'SELECT "ClientIP", "ClientIP" - 1, "ClientIP" - 2, "ClientIP" - 3, '
        f'COUNT(*) AS c FROM {t} GROUP BY "ClientIP", "ClientIP" - 1, '
        '"ClientIP" - 2, "ClientIP" - 3 ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(10, 9, 8, 7, 3), (20, 19, 18, 17, 2)]
