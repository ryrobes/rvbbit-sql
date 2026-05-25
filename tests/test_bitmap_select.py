"""bitmap_select_int / bitmap_select_text — explicit row-filter via the
cached semantic bitmaps (RYR-300, pragmatic version).

Composes via JOIN: the function returns SETOF (pk) for rows whose
bitmap bit is set; user joins back to the source table.
"""
import uuid


def _make_table_with_bitmap(rvbbit):
    t = f"bsel_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    # 6 "angry" rows + 14 "happy" rows.
    rvbbit.execute(
        f"INSERT INTO {t} SELECT i, 'angry ' || i FROM generate_series(1, 20) g(i) WHERE i % 3 = 0"
    )
    rvbbit.execute(
        f"INSERT INTO {t} SELECT i, 'happy ' || i FROM generate_series(1, 20) g(i) WHERE i % 3 <> 0"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    rvbbit.execute(
        f"SELECT rvbbit.bitmap_populate('{t}'::regclass::oid, 'has_angry', 'v1', "
        "$$ body ILIKE '%angry%' $$)"
    )
    return t


def test_bitmap_select_int_returns_matching_pks(rvbbit):
    t = _make_table_with_bitmap(rvbbit)
    try:
        ids = [r[0] for r in rvbbit.execute(
            f"SELECT pk FROM rvbbit.bitmap_select_int("
            f"  '{t}'::regclass::oid, 'id', 'has_angry', 'v1') ORDER BY pk"
        ).fetchall()]
        assert ids == [3, 6, 9, 12, 15, 18]
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_bitmap_select_int_composes_via_join(rvbbit):
    t = _make_table_with_bitmap(rvbbit)
    try:
        rows = rvbbit.execute(
            f"SELECT t.id, t.body FROM {t} t "
            f"JOIN rvbbit.bitmap_select_int("
            f"  '{t}'::regclass::oid, 'id', 'has_angry', 'v1') AS m(id) "
            f"USING (id) ORDER BY t.id"
        ).fetchall()
        assert len(rows) == 6
        assert all(b.startswith("angry") for (_, b) in rows)
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_bitmap_select_unknown_predicate_returns_empty(rvbbit):
    t = _make_table_with_bitmap(rvbbit)
    try:
        rows = rvbbit.execute(
            f"SELECT * FROM rvbbit.bitmap_select_int("
            f"  '{t}'::regclass::oid, 'id', 'nonexistent_predicate', 'v1')"
        ).fetchall()
        assert rows == []
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_bitmap_select_uncompacted_table_returns_empty(rvbbit):
    t = f"bsel_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    try:
        rows = rvbbit.execute(
            f"SELECT * FROM rvbbit.bitmap_select_int("
            f"  '{t}'::regclass::oid, 'id', 'whatever', 'v1')"
        ).fetchall()
        assert rows == []
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_bitmap_select_text_pk_works(rvbbit):
    """Same shape but for text PKs."""
    t = f"bsel_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (slug text, body text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {t} VALUES "
        f"('a', 'angry one'),('b','happy one'),('c','angry two'),('d','happy two')"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    rvbbit.execute(
        f"SELECT rvbbit.bitmap_populate('{t}'::regclass::oid, 'has_angry', 'v1', "
        "$$ body ILIKE '%angry%' $$)"
    )
    try:
        slugs = sorted(
            r[0] for r in rvbbit.execute(
                f"SELECT pk FROM rvbbit.bitmap_select_text("
                f"  '{t}'::regclass::oid, 'slug', 'has_angry', 'v1')"
            ).fetchall()
        )
        assert slugs == ['a', 'c']
    finally:
        rvbbit.execute(f"DROP TABLE {t}")
