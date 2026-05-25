"""HLL approx_distinct over per-group sketches (RYR-291).

Validates:
- Exact match when N_distinct ≤ HLL's exact-mode threshold.
- ±5% accuracy on larger collections.
- Cross-group union (multiple row groups give correct totals).
- NULL on numeric columns (HLL only tracked for text).
- NULL on tables with no compacted row groups.
"""
import uuid


def test_exact_small_distinct(rvbbit):
    t = f"hll_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {t} SELECT i, 'item_' || (i % 100) FROM generate_series(1, 5000) g(i)"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        approx = rvbbit.execute(
            f"SELECT rvbbit.approx_distinct('{t}'::regclass::oid, 'body')"
        ).fetchone()[0]
        # 100 distinct values is well below HLL's exact-mode threshold,
        # so the answer should be exact.
        assert approx == 100, f"got {approx}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_larger_distinct_within_5_percent(rvbbit):
    t = f"hll_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {t} "
        f"SELECT i, 'distinct_value_' || i FROM generate_series(1, 10000) g(i)"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        approx = rvbbit.execute(
            f"SELECT rvbbit.approx_distinct('{t}'::regclass::oid, 'body')"
        ).fetchone()[0]
        # All 10000 distinct. Allow ±5% (HLL precision 12 ≈ ±1.6% but
        # CI variance gives more headroom).
        assert 9500 <= approx <= 10500, f"got {approx}; expected 10000±5%"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_returns_null_for_numeric(rvbbit):
    t = f"hll_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, n int) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {t} SELECT i, i*2 FROM generate_series(1, 100) g(i)")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        row = rvbbit.execute(
            f"SELECT rvbbit.approx_distinct('{t}'::regclass::oid, 'n')"
        ).fetchone()
        assert row[0] is None
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_returns_null_for_uncompacted_table(rvbbit):
    t = f"hll_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    # No export_to_parquet → no row groups → no sketches.
    try:
        row = rvbbit.execute(
            f"SELECT rvbbit.approx_distinct('{t}'::regclass::oid, 'body')"
        ).fetchone()
        assert row[0] is None
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_returns_null_for_missing_column(rvbbit):
    t = f"hll_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {t} VALUES (1, 'hello'), (2, 'world')")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        row = rvbbit.execute(
            f"SELECT rvbbit.approx_distinct('{t}'::regclass::oid, 'nonexistent_col')"
        ).fetchone()
        assert row[0] is None
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_two_row_groups_union(rvbbit):
    """Cross-group union should give correct totals (not double-counted)."""
    t = f"hll_t_{uuid.uuid4().hex[:8]}"
    t2 = f"hll_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(f"CREATE TABLE {t2} (id int, body text) USING rvbbit")
    # First batch: items 0..99
    rvbbit.execute(
        f"INSERT INTO {t} SELECT i, 'item_' || (i % 100) FROM generate_series(1, 200) g(i)"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    # Second sketch: items 50..149 (50 overlap with batch 1, 50 new).
    # Copy the row-group metadata under t's oid so this test exercises
    # HLL union in rvbbit.approx_distinct without depending on append
    # compaction semantics.
    rvbbit.execute(
        f"INSERT INTO {t2} SELECT i, 'item_' || (50 + (i % 100)) FROM generate_series(1, 200) g(i)"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t2}'::regclass)")
    rvbbit.execute(
        f"""
        INSERT INTO rvbbit.row_groups
            (table_oid, rg_id, path, n_rows, n_bytes, stats, per_group_stats)
        SELECT '{t}'::regclass::oid, 1, path, n_rows, n_bytes, stats, per_group_stats
        FROM rvbbit.row_groups
        WHERE table_oid = '{t2}'::regclass::oid
        """
    )
    try:
        n_groups = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = '{t}'::regclass::oid"
        ).fetchone()[0]
        assert n_groups == 2

        approx = rvbbit.execute(
            f"SELECT rvbbit.approx_distinct('{t}'::regclass::oid, 'body')"
        ).fetchone()[0]
        # Union of {item_0..99} and {item_50..149} = {item_0..149} = 150.
        # NOT 200 (which would be the sum of per-group counts).
        assert approx == 150, f"cross-group union should give 150, got {approx}"
    finally:
        rvbbit.execute(
            f"DELETE FROM rvbbit.row_groups "
            f"WHERE table_oid IN ('{t}'::regclass::oid, '{t2}'::regclass::oid)"
        )
        rvbbit.execute(f"DROP TABLE {t}")
        rvbbit.execute(f"DROP TABLE {t2}")
