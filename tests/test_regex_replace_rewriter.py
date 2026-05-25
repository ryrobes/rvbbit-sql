"""Projected regexp_replace(text) group rollup rewrite."""


def _make_referer_table(rvbbit, temp_table):
    rvbbit.execute(
        f'''
        CREATE TABLE {temp_table} (
            "Referer" text
        ) USING rvbbit
        '''
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table} ("Referer") VALUES
            ('https://www.alpha.example/search?q=1'),
            ('https://alpha.example/long/path'),
            ('http://beta.example/x'),
            ('http://www.beta.example/longer/path'),
            ('notaurl'),
            (''),
            (NULL)
        """
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan(rvbbit, sql):
    return "\n".join(
        r[0] for r in rvbbit.execute(f"EXPLAIN (FORMAT TEXT) {sql}").fetchall()
    )


def test_regex_replace_url_host_avg_len_uses_projected_srf(rvbbit, temp_table):
    t = _make_referer_table(rvbbit, temp_table)
    sql = (
        f"SELECT REGEXP_REPLACE(\"Referer\", '^https?://(?:www\\.)?([^/]+)/.*$', '\\1') AS k, "
        f'AVG(length("Referer")) AS l, COUNT(*) AS c, MIN("Referer") FROM {t} '
        f'WHERE "Referer" <> \'\' GROUP BY k HAVING COUNT(*) > 1 '
        f'ORDER BY l DESC LIMIT 25'
    )

    plan = _plan(rvbbit, sql)
    assert "Function Scan" in plan
    assert "Custom Scan" not in plan
    assert "Aggregate" not in plan
    assert "Sort" not in plan

    rows = rvbbit.execute(sql).fetchall()
    normalized = [(row[0], float(row[1]), row[2], row[3]) for row in rows]
    assert normalized == [
        ("alpha.example", 33.5, 2, "https://alpha.example/long/path"),
        ("beta.example", 28.0, 2, "http://beta.example/x"),
    ]
