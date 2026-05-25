"""LIKE / NOT LIKE / ILIKE pushdown — RYR-284.

Verifies correctness of the predicate-pushdown path on text columns
using a real USING rvbbit table compacted to parquet.
"""
import uuid


def _make_table(rvbbit):
    """A small USING rvbbit table with text data the LIKE queries can hit."""
    t = f"like_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rows = [
        (1, "the quick brown fox"),
        (2, "Google is a search engine"),
        (3, "PostgreSQL is great"),
        (4, "google.com homepage"),
        (5, "I love DuckDB"),
        (6, "GOOGLE shouting"),
        (7, ""),
    ]
    for r in rows:
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", r)
    # Compact to parquet so queries run through the custom scan
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    return t


def test_like_contains(rvbbit):
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body LIKE '%google%'"
        ).fetchall())
        # case-sensitive 'google' matches only rows 4 ('google.com homepage')
        assert ids == [4], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_ilike_contains(rvbbit):
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body ILIKE '%google%'"
        ).fetchall())
        # case-insensitive 'google' matches rows 2 (Google), 4 (google.com), 6 (GOOGLE)
        assert ids == [2, 4, 6], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_like_startswith(rvbbit):
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body LIKE 'the%'"
        ).fetchall())
        assert ids == [1], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_like_endswith(rvbbit):
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body LIKE '%homepage'"
        ).fetchall())
        assert ids == [4], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_not_like(rvbbit):
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body NOT LIKE '%google%' AND body <> ''"
        ).fetchall())
        # Everyone except row 4 (the lowercase 'google.com'), minus empty
        assert ids == [1, 2, 3, 5, 6], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_like_underscore_wildcard(rvbbit):
    """The single-char wildcard `_` falls to the general matcher (not the
    fast path). Verify it still works correctly."""
    t = _make_table(rvbbit)
    try:
        # 'p_st%' should match 'PostgreSQL'? — no, that's 'Po' not 'p_'
        # 'g_ogle%' should match 'google.com homepage'
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body LIKE 'g_ogle%'"
        ).fetchall())
        assert ids == [4], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_like_exact_no_wildcard(rvbbit):
    """A LIKE with no wildcards falls into the equality fast path."""
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body LIKE 'PostgreSQL is great'"
        ).fetchall())
        assert ids == [3], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_like_combined_with_numeric_predicate(rvbbit):
    """LIKE composes with the existing numeric pushdown."""
    t = _make_table(rvbbit)
    try:
        ids = sorted(r[0] for r in rvbbit.execute(
            f"SELECT id FROM {t} WHERE body LIKE '%a%' AND id > 2"
        ).fetchall())
        # rows with 'a' in body AND id > 2: row 3 (great), row 5 (DuckDB→no a? has 'a' in 'love'? no)
        # let me think: row 3 'PostgreSQL is great' has 'a' in 'great' ✓; row 4 'google.com homepage' has 'a' in 'homepage' ✓
        # row 5 'I love DuckDB' — no 'a'; row 6 'GOOGLE shouting' — no lowercase 'a'
        assert ids == [3, 4], f"got {ids}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")
