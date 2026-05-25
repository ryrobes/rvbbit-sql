"""Projected top-N rewrites for SearchPhrase ORDER BY LIMIT queries."""


def _make_searchphrase_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "SearchPhrase" text,
            "EventTime" timestamp
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("SearchPhrase", "EventTime") VALUES
            ('zulu', '2024-01-01 00:00:05'),
            ('', '2024-01-01 00:00:00'),
            ('beta', '2024-01-01 00:00:03'),
            ('alpha', '2024-01-01 00:00:02'),
            ('omega', '2024-01-01 00:00:01'),
            (NULL, '2024-01-01 00:00:00'),
            ('delta', '2024-01-01 00:00:04')
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_searchphrase_order_by_eventtime_uses_topn_srf(rvbbit, temp_table):
    t = _make_searchphrase_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase" FROM {t} WHERE "SearchPhrase" <> \'\' '
        'ORDER BY "EventTime" LIMIT 3'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [("omega",), ("alpha",), ("beta",)]


def test_searchphrase_order_by_phrase_uses_topn_srf(rvbbit, temp_table):
    t = _make_searchphrase_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase" FROM {t} WHERE "SearchPhrase" <> \'\' '
        'ORDER BY "SearchPhrase" LIMIT 3'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [("alpha",), ("beta",), ("delta",)]


def test_searchphrase_order_by_eventtime_phrase_uses_topn_srf(rvbbit, temp_table):
    t = _make_searchphrase_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase" FROM {t} WHERE "SearchPhrase" <> \'\' '
        'ORDER BY "EventTime", "SearchPhrase" LIMIT 4'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [("omega",), ("alpha",), ("beta",), ("delta",)]
