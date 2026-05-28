"""Generation-based time travel for rvbbit tables."""


def _refresh_generation(rvbbit, table):
    row = rvbbit.execute(
        f"""
        SELECT (rvbbit.refresh_acceleration('{table}'::regclass, false)
                ->> 'generation_after')::bigint
        """
    ).fetchone()
    return row[0]


def test_as_of_generation_and_timestamp_helpers(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} VALUES (1, 'one'), (2, 'two')"
    )
    gen1 = _refresh_generation(rvbbit, temp_table)

    first_committed_at = rvbbit.execute(
        f"""
        SELECT committed_at
        FROM rvbbit.list_generations('{temp_table}'::regclass)
        WHERE generation = %s
        """,
        (gen1,),
    ).fetchone()[0]

    rvbbit.execute("SELECT pg_sleep(0.02)")
    rvbbit.execute(f"INSERT INTO {temp_table} VALUES (3, 'three')")
    gen2 = _refresh_generation(rvbbit, temp_table)

    assert gen1 > 0
    assert gen2 > gen1

    timeline = rvbbit.execute(
        f"""
        SELECT generation, rows_written, row_groups_written,
               visible_rows_estimate, visible_row_groups, tombstones_visible
        FROM rvbbit.time_travel_timeline('{temp_table}'::regclass)
        ORDER BY generation
        """
    ).fetchall()
    assert timeline == [
        (gen1, 2, 1, 2, 1, 0),
        (gen2, 1, 1, 3, 2, 0),
    ]

    latest = rvbbit.execute(
        f"SELECT count(*), max(id) FROM {temp_table}"
    ).fetchone()
    assert latest == (3, 3)

    rvbbit.execute("BEGIN")
    try:
        rvbbit.execute(f"SET LOCAL rvbbit.as_of_generation = '{gen1}'")
        as_of_gen = rvbbit.execute(
            f"SELECT count(*), max(id) FROM {temp_table}"
        ).fetchone()
    finally:
        rvbbit.execute("COMMIT")
    assert as_of_gen == (2, 2)

    resolved = rvbbit.execute(
        f"SELECT rvbbit.set_as_of('{temp_table}'::regclass, %s)",
        (first_committed_at,),
    ).fetchone()[0]
    try:
        as_of_timestamp = rvbbit.execute(
            f"SELECT count(*), max(id) FROM {temp_table}"
        ).fetchone()
    finally:
        rvbbit.execute("SELECT rvbbit.set_as_of_reset()")

    assert resolved == gen1
    assert as_of_timestamp == (2, 2)

    comment_timestamp = first_committed_at.isoformat()
    rvbbit.execute("BEGIN")
    try:
        as_of_comment = rvbbit.execute(
            f"""
            /* rvbbit: as_of = '{comment_timestamp}' */
            SELECT count(*), max(id) FROM {temp_table}
            """
        ).fetchone()
        after_comment = rvbbit.execute(
            f"SELECT count(*), max(id) FROM {temp_table}"
        ).fetchone()
        as_of_line_comment = rvbbit.execute(
            f"""
            -- rvbbit: as_of = '{comment_timestamp}'
            SELECT count(*), max(id) FROM {temp_table}
            """
        ).fetchone()
    finally:
        rvbbit.execute("COMMIT")

    assert as_of_comment == (2, 2)
    assert after_comment == (3, 3)
    assert as_of_line_comment == (2, 2)

    reset = rvbbit.execute(
        f"SELECT count(*), max(id) FROM {temp_table}"
    ).fetchone()
    assert reset == (3, 3)
