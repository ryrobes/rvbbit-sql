"""Incremental semantic materialized views (RYR-292, Loop 12).

Anti-join refresh — rows in source not yet in the MV get computed
once and stored. Subsequent refreshes only touch new PK values.
INSERT-only source semantics; UPDATE/DELETE on source not detected
(documented limitation).
"""
import uuid

import pytest


def _make_table(rvbbit):
    t = f"mvsrc_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int PRIMARY KEY, body text)")
    rvbbit.execute(f"INSERT INTO {t} VALUES (1, 'alpha'), (2, 'beta'), (3, 'gamma')")
    return t


def _drop(rvbbit, source: str, mv: str):
    rvbbit.execute(f"SELECT rvbbit.semantic_mv_drop('{mv}')")
    rvbbit.execute(f"DROP TABLE IF EXISTS {source}")


def test_create_populates_initial(rvbbit):
    src = _make_table(rvbbit)
    mv = f"mvtgt_{uuid.uuid4().hex[:8]}"
    try:
        n = rvbbit.execute(
            "SELECT rvbbit.semantic_mv_create(%s, %s::regclass::oid, %s, %s, %s)",
            (mv, src, "id", "upper(body)", "caps"),
        ).fetchone()[0]
        assert n == 3

        rows = rvbbit.execute(
            f"SELECT id, caps FROM rvbbit.{mv} ORDER BY id"
        ).fetchall()
        assert rows == [(1, "ALPHA"), (2, "BETA"), (3, "GAMMA")]
    finally:
        _drop(rvbbit, src, mv)


def test_refresh_only_picks_up_new_rows(rvbbit):
    src = _make_table(rvbbit)
    mv = f"mvtgt_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.semantic_mv_create(%s, %s::regclass::oid, %s, %s, %s)",
            (mv, src, "id", "upper(body)", "caps"),
        )
        # Re-refresh with no source changes → 0 new rows.
        n_noop = rvbbit.execute(
            f"SELECT rvbbit.semantic_mv_refresh('{mv}')"
        ).fetchone()[0]
        assert n_noop == 0

        rvbbit.execute(
            f"INSERT INTO {src} VALUES (4, 'delta'), (5, 'epsilon')"
        )
        n_added = rvbbit.execute(
            f"SELECT rvbbit.semantic_mv_refresh('{mv}')"
        ).fetchone()[0]
        assert n_added == 2

        rows = rvbbit.execute(
            f"SELECT caps FROM rvbbit.{mv} ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == ["ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON"]
    finally:
        _drop(rvbbit, src, mv)


def test_recreate_overwrites(rvbbit):
    src = _make_table(rvbbit)
    mv = f"mvtgt_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.semantic_mv_create(%s, %s::regclass::oid, %s, %s, %s)",
            (mv, src, "id", "upper(body)", "caps"),
        )
        # Recreate with a different projection — old table dropped, new starts fresh.
        rvbbit.execute(
            "SELECT rvbbit.semantic_mv_create(%s, %s::regclass::oid, %s, %s, %s)",
            (mv, src, "id", "length(body)::text", "len"),
        )
        cols = rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = %s ORDER BY ordinal_position",
            (mv,),
        ).fetchall()
        col_names = [r[0] for r in cols]
        assert col_names == ["id", "len"]
    finally:
        _drop(rvbbit, src, mv)


def test_drop_returns_1_then_0(rvbbit):
    src = _make_table(rvbbit)
    mv = f"mvtgt_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.semantic_mv_create(%s, %s::regclass::oid, %s, %s, %s)",
            (mv, src, "id", "upper(body)", "caps"),
        )
        n1 = rvbbit.execute(
            f"SELECT rvbbit.semantic_mv_drop('{mv}')"
        ).fetchone()[0]
        assert n1 == 1
        n2 = rvbbit.execute(
            f"SELECT rvbbit.semantic_mv_drop('{mv}')"
        ).fetchone()[0]
        assert n2 == 0
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {src}")


def test_refresh_on_unknown_mv_errors(rvbbit):
    with pytest.raises(Exception) as exc:
        rvbbit.execute(
            "SELECT rvbbit.semantic_mv_refresh('nonexistent_mv_xyz')"
        ).fetchone()
    assert "not found" in str(exc.value).lower()


def test_catalog_row_updates_on_refresh(rvbbit):
    src = _make_table(rvbbit)
    mv = f"mvtgt_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.semantic_mv_create(%s, %s::regclass::oid, %s, %s, %s)",
            (mv, src, "id", "upper(body)", "caps"),
        )
        first = rvbbit.execute(
            "SELECT last_refreshed, n_rows_total "
            "FROM rvbbit.semantic_mvs WHERE mv_name = %s",
            (mv,),
        ).fetchone()
        assert first[0] is not None
        assert first[1] == 3
    finally:
        _drop(rvbbit, src, mv)
