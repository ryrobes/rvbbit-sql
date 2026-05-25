"""Projected top-count rewrites for integer/text grouped pairs."""


def _make_int_text_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "SearchEngineID" smallint,
            "UserID" bigint,
            "SearchPhrase" text
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("SearchEngineID", "UserID", "SearchPhrase") VALUES
            (1, 10, 'alpha'),
            (1, 10, 'alpha'),
            (1, 10, 'alpha'),
            (2, 20, 'beta'),
            (2, 20, 'beta'),
            (3, 30, 'gamma'),
            (9, 90, ''),
            (9, 90, ''),
            (9, 90, ''),
            (9, 90, ''),
            (NULL, 40, 'null-engine'),
            (NULL, 40, 'null-engine'),
            (NULL, 40, 'null-engine'),
            (NULL, 40, 'null-engine'),
            (NULL, 40, 'null-engine')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_smallint_text_pair_count_filter_uses_top_count_srf(rvbbit, temp_table):
    t = _make_int_text_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchEngineID", "SearchPhrase", COUNT(*) AS c FROM {t} '
        'WHERE "SearchPhrase" <> \'\' GROUP BY "SearchEngineID", "SearchPhrase" '
        'ORDER BY c DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(None, "null-engine", 5), (1, "alpha", 3)]


def test_bigint_text_pair_count_uses_top_count_srf(rvbbit, temp_table):
    t = _make_int_text_table(rvbbit, temp_table)
    sql = (
        f'SELECT "UserID", "SearchPhrase", COUNT(*) FROM {t} '
        'GROUP BY "UserID", "SearchPhrase" ORDER BY COUNT(*) DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(40, "null-engine", 5), (90, "", 4)]


def test_bigint_text_pair_count_without_order_uses_any_count_srf(rvbbit, temp_table):
    t = _make_int_text_table(rvbbit, temp_table)
    sql = (
        f'SELECT "UserID", "SearchPhrase", COUNT(*) FROM {t} '
        'GROUP BY "UserID", "SearchPhrase" LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(10, "alpha", 3), (20, "beta", 2)]
