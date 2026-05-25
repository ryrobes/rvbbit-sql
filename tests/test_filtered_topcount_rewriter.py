"""Generic filtered top-count vector rewrite."""

from datetime import date


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_filtered_text_group_topcount_uses_vector_srf(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "URL" text,
            "CounterID" integer,
            "EventDate" date,
            "DontCountHits" smallint,
            "IsRefresh" smallint
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("URL", "CounterID", "EventDate", "DontCountHits", "IsRefresh")
        VALUES
            ('/a', 62, '2013-07-02', 0, 0),
            ('/a', 62, '2013-07-03', 0, 0),
            ('/b', 62, '2013-07-04', 0, 0),
            ('/c', 62, '2013-08-01', 0, 0),
            ('/d', 10, '2013-07-04', 0, 0),
            ('',   62, '2013-07-04', 0, 0),
            ('/e', 62, '2013-07-04', 1, 0),
            ('/f', 62, '2013-07-04', 0, 1)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")

    sql = (
        f'SELECT "URL", COUNT(*) AS PageViews FROM {temp_table} '
        'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
        'AND "EventDate" <= \'2013-07-31\' AND "DontCountHits" = 0 '
        'AND "IsRefresh" = 0 AND "URL" <> \'\' GROUP BY "URL" '
        'ORDER BY PageViews DESC LIMIT 2'
    )
    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    assert rvbbit.execute(sql).fetchall() == [("/a", 2), ("/b", 1)]


def test_filtered_two_key_topcount_with_in_and_offset(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "URLHash" bigint,
            "EventDate" date,
            "CounterID" integer,
            "IsRefresh" smallint,
            "TraficSourceID" smallint,
            "RefererHash" bigint
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
            ("URLHash", "EventDate", "CounterID", "IsRefresh", "TraficSourceID", "RefererHash")
        VALUES
            (10, '2013-07-02', 62, 0, -1, 99),
            (10, '2013-07-02', 62, 0, 6,  99),
            (10, '2013-07-02', 62, 0, 6,  99),
            (20, '2013-07-03', 62, 0, -1, 99),
            (20, '2013-07-03', 62, 0, 6,  99),
            (30, '2013-07-04', 62, 0, 6,  99),
            (40, '2013-07-04', 62, 1, 6,  99),
            (50, '2013-07-04', 62, 0, 5,  99),
            (60, '2013-07-04', 62, 0, 6,  12)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")

    sql = (
        f'SELECT "URLHash", "EventDate", COUNT(*) AS PageViews FROM {temp_table} '
        'WHERE "CounterID" = 62 AND "EventDate" >= \'2013-07-01\' '
        'AND "EventDate" <= \'2013-07-31\' AND "IsRefresh" = 0 '
        'AND "TraficSourceID" IN (-1, 6) AND "RefererHash" = 99 '
        'GROUP BY "URLHash", "EventDate" ORDER BY PageViews DESC LIMIT 1 OFFSET 1'
    )
    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    assert rvbbit.execute(sql).fetchall() == [(20, date(2013, 7, 3), 2)]
