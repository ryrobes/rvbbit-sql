"""Semantic predicate bitmap cache (RYR-288, Phase A).

Verifies the storage primitive: a boolean SQL predicate is evaluated
once per parquet row, results are compressed into per-row-group
roaring bitmaps keyed by (table_oid, rg_id, predicate_name,
model_version), and a second populate call short-circuits.

Auto-routing of queries through the bitmap (planner integration)
is a follow-up — these tests cover the population + invalidation
contract only.
"""
import uuid

import pytest


def _make_compacted(rvbbit, n_rows: int = 50):
    """Small USING rvbbit table with one parquet row group containing
    deterministic, easy-to-filter rows."""
    t = f"bm_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rows = [(i, f"body-{i}-{'angry' if i % 7 == 0 else 'calm'}") for i in range(n_rows)]
    for r in rows:
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", r)
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    return t


def test_populate_writes_one_bitmap_per_group(rvbbit):
    t = _make_compacted(rvbbit)
    try:
        # `id % 7 = 0` matches rows 0,7,14,21,28,35,42,49 — 8 of 50.
        n = rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        ).fetchone()[0]
        assert n == 1, f"expected 1 row group populated, got {n}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_populate_is_idempotent(rvbbit):
    t = _make_compacted(rvbbit)
    try:
        n1 = rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        ).fetchone()[0]
        # Second call with same key should short-circuit.
        n2 = rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        ).fetchone()[0]
        assert n1 == 1
        assert n2 == 0, "second populate should be cache hit (0 bitmaps written)"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_decoded_bitmap_matches_predicate(rvbbit):
    t = _make_compacted(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        )
        rg_id = rvbbit.execute(
            "SELECT min(rg_id) FROM rvbbit.row_groups WHERE table_oid = %s::regclass::oid",
            (t,),
        ).fetchone()[0]
        decoded = rvbbit.execute(
            "SELECT rvbbit.bitmap_test_decode(%s::regclass::oid, %s, %s, %s)",
            (t, rg_id, "mod7", "test-v1"),
        ).fetchone()[0]
        # Rows emitted from a parquet row group are indexed 0..n_rows-1.
        # `id % 7 = 0` → rows 0,7,14,21,28,35,42,49 in id order.
        # Custom scan emits in (rg_id ASC, row_index ASC) order, which
        # matches insertion order here (single group, no sort).
        assert decoded == [0, 7, 14, 21, 28, 35, 42, 49], f"got {decoded}"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_stats_reports_selectivity(rvbbit):
    t = _make_compacted(rvbbit, n_rows=50)
    try:
        rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        )
        row = rvbbit.execute(
            "SELECT predicate_name, model_version, n_groups, rows_set, rows_total, selectivity "
            "FROM rvbbit.bitmap_stats(%s::regclass::oid)",
            (t,),
        ).fetchone()
        assert row is not None
        name, mv, n_groups, rows_set, rows_total, sel = row
        assert name == "mod7"
        assert mv == "test-v1"
        assert n_groups == 1
        assert rows_set == 8
        assert rows_total == 50
        assert abs(sel - 8 / 50) < 1e-9
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_model_version_bump_misses_cache(rvbbit):
    t = _make_compacted(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        )
        # Same predicate name, different model_version → different hash → must repopulate.
        n2 = rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v2", "id % 7 = 0"),
        ).fetchone()[0]
        assert n2 == 1, "model_version change should bust the cache"
        # And we should now have two bitmap rows in the catalog for this table.
        count = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.semantic_bitmaps "
            "WHERE table_oid = %s::regclass::oid",
            (t,),
        ).fetchone()[0]
        assert count == 2
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_drop_clears_bitmaps(rvbbit):
    t = _make_compacted(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        )
        dropped = rvbbit.execute(
            "SELECT rvbbit.bitmap_drop(%s::regclass::oid, %s, %s)",
            (t, "mod7", "test-v1"),
        ).fetchone()[0]
        assert dropped == 1
        # After drop, repopulate should succeed (cache empty).
        n2 = rvbbit.execute(
            "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
            (t, "mod7", "test-v1", "id % 7 = 0"),
        ).fetchone()[0]
        assert n2 == 1
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_populate_errors_on_uncompacted_table(rvbbit):
    t = f"bm_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    try:
        # No export_to_parquet → no row groups → must error explicitly.
        with pytest.raises(Exception) as exc:
            rvbbit.execute(
                "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
                (t, "mod7", "test-v1", "id % 7 = 0"),
            ).fetchone()
        assert "no row groups" in str(exc.value).lower()
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_dropping_rvbbit_table_cascades_bitmaps(rvbbit):
    t = _make_compacted(rvbbit)
    rvbbit.execute(
        "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
        (t, "mod7", "test-v1", "id % 7 = 0"),
    )
    # Capture the oid before drop (since it disappears after).
    oid = rvbbit.execute(f"SELECT '{t}'::regclass::oid").fetchone()[0]
    rvbbit.execute(f"DROP TABLE {t}")
    remaining = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.semantic_bitmaps WHERE table_oid = %s",
        (oid,),
    ).fetchone()[0]
    assert remaining == 0, "bitmaps should cascade-delete with the source table"
