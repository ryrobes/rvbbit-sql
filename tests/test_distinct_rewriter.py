"""Projected COUNT(DISTINCT int) rewrites."""


def _make_distinct_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "RegionID" integer,
            "MobilePhone" smallint,
            "MobilePhoneModel" text,
            "SearchPhrase" text,
            "UserID" bigint
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("RegionID", "MobilePhone", "MobilePhoneModel", "SearchPhrase", "UserID")
        VALUES
            (1, 1, 'ios', 'alpha', 10),
            (1, 1, 'ios', 'alpha', 10),
            (1, 1, 'ios', 'alpha', 11),
            (1, 1, 'ios', 'alpha', 12),
            (1, 1, 'ios', 'alpha', 13),
            (2, 2, 'android', 'beta', 20),
            (2, 2, 'android', 'beta', 21),
            (2, 2, 'android', 'beta', 22),
            (3, 3, '', 'gamma', 30),
            (3, 3, '', 'gamma', 31),
            (4, 4, 'other', '', 40),
            (4, 4, 'other', '', 41),
            (NULL, NULL, 'null-model', 'null-phrase', 50),
            (NULL, NULL, 'null-model', 'null-phrase', 51)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_ungrouped_count_distinct_int_uses_projected_function(rvbbit, temp_table):
    t = _make_distinct_table(rvbbit, temp_table)
    sql = f'SELECT COUNT(DISTINCT "UserID") FROM {t}'

    plan = _plan(rvbbit, sql)
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(13,)]


def test_int_group_count_distinct_uses_projected_srf(rvbbit, temp_table):
    t = _make_distinct_table(rvbbit, temp_table)
    sql = (
        f'SELECT "RegionID", COUNT(DISTINCT "UserID") AS u FROM {t} '
        'GROUP BY "RegionID" ORDER BY u DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(1, 4), (2, 3)]


def test_text_group_count_distinct_filter_uses_projected_srf(rvbbit, temp_table):
    t = _make_distinct_table(rvbbit, temp_table)
    sql = (
        f'SELECT "SearchPhrase", COUNT(DISTINCT "UserID") AS u FROM {t} '
        'WHERE "SearchPhrase" <> \'\' GROUP BY "SearchPhrase" '
        'ORDER BY u DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [("alpha", 4), ("beta", 3)]


def test_int_text_group_count_distinct_filter_uses_projected_srf(rvbbit, temp_table):
    t = _make_distinct_table(rvbbit, temp_table)
    sql = (
        f'SELECT "MobilePhone", "MobilePhoneModel", COUNT(DISTINCT "UserID") AS u '
        f'FROM {t} WHERE "MobilePhoneModel" <> \'\' '
        'GROUP BY "MobilePhone", "MobilePhoneModel" ORDER BY u DESC LIMIT 2'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    assert rows == [(1, "ios", 4), (2, "android", 3)]
