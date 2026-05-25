"""Tier B composite semantic operators (RYR-303): outliers,
dedupe_groups, semantic_case.

Stub embeddings are deterministic but not semantically meaningful, so
these tests assert the CONTRACT (shape, monotonicity, sort order,
edge cases) rather than the QUALITY of the picks. With a real embedder
behind 'embed', the same calls produce semantically useful output.
"""
import uuid

import pytest


@pytest.fixture
def stub_embed(rvbbit):
    name = f"stub_comp_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, backend_transport => %s)",
        (name, "stub://128", "stub"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    yield name
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")
    rvbbit.execute(f"SELECT rvbbit.embedding_purge('{name}')")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


@pytest.fixture
def sample_table(rvbbit):
    t = f"comp_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    bodies = [
        "angry customer wants refund",
        "product broke after week",
        "angry customer demands cancellation",
        "love the product",
        "shipping was slow",
        "product works great",
        "angry customer wants refund",  # dup
        "angry customer wants refund",  # dup
        "wildly unrelated unicycle clown",
    ]
    for i, b in enumerate(bodies):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, b))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    yield t
    rvbbit.execute(f"DROP TABLE {t}")


# ---- outliers ------------------------------------------------------


def test_outliers_returns_n_rows(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT text, score FROM rvbbit.outliers(%s, 3, '', %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    assert len(rows) == 3


def test_outliers_sorted_descending_by_score(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT score FROM rvbbit.outliers(%s, 5, '', %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    scores = [r[0] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_outliers_with_criterion_changes_ranking(rvbbit, stub_embed, sample_table):
    base = rvbbit.execute(
        "SELECT text FROM rvbbit.outliers(%s, 5, '', %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    with_crit = rvbbit.execute(
        "SELECT text FROM rvbbit.outliers(%s, 5, 'angry customer complaint', %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    # Two different scoring modes → ranking should generally differ.
    assert base != with_crit


def test_outliers_n_zero_returns_empty(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT * FROM rvbbit.outliers(%s, 0, '', %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    assert rows == []


def test_outliers_empty_input_returns_empty(rvbbit, stub_embed):
    rows = rvbbit.execute(
        "SELECT * FROM rvbbit.outliers(%s, 5, '', %s)",
        ("SELECT 'x' WHERE false", stub_embed),
    ).fetchall()
    assert rows == []


# ---- dedupe_groups -------------------------------------------------


def test_dedupe_groups_collapses_duplicates(rvbbit, stub_embed, sample_table):
    """The three duplicate 'angry customer wants refund' rows should
    end up in a single group regardless of similarity threshold (they
    have IDENTICAL embeddings)."""
    rows = rvbbit.execute(
        "SELECT representative, size FROM rvbbit.dedupe_groups(%s, 0.99, %s) "
        "ORDER BY size DESC, representative",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    # 'angry customer wants refund' should be in a group of size 3.
    # Other rows are singletons at threshold 0.99 with stub vectors.
    refund_group = [r for r in rows if r[0] == "angry customer wants refund"]
    assert refund_group[0][1] == 3


def test_dedupe_groups_returns_sorted_by_size_desc(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT size FROM rvbbit.dedupe_groups(%s, 0.99, %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    sizes = [r[0] for r in rows]
    assert sizes == sorted(sizes, reverse=True)


def test_dedupe_groups_members_is_array(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT members FROM rvbbit.dedupe_groups(%s, 0.99, %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    for (members,) in rows:
        assert isinstance(members, list)
        assert all(isinstance(m, str) for m in members)


def test_dedupe_groups_low_threshold_merges_more(rvbbit, stub_embed, sample_table):
    """At threshold=0.0 every pair has cosine >= 0 by accident often, so
    we may end up with very few groups. At threshold=0.99 (near-identical),
    only true duplicates merge."""
    high = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.dedupe_groups(%s, 0.99, %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchone()[0]
    low = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.dedupe_groups(%s, -0.99, %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchone()[0]
    # Lower threshold should produce <= groups than higher.
    assert low <= high


def test_dedupe_groups_representative_is_longest(rvbbit, stub_embed):
    """When a group has multiple distinct texts (similarity >= threshold),
    the longest one wins. Force this with two near-identical embeddings."""
    t = f"dr_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    # Stub vectors are deterministic per-text — these will be different,
    # but at threshold = -1.0 everything merges into one group.
    bodies = ["x", "longer text here", "medium"]
    for i, b in enumerate(bodies):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, b))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        rep = rvbbit.execute(
            "SELECT representative FROM rvbbit.dedupe_groups(%s, -1.0, %s)",
            (f"SELECT body FROM {t}", stub_embed),
        ).fetchone()[0]
        assert rep == "longer text here"
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


# ---- diff (semantic set difference / novelty) ----------------------


@pytest.fixture
def two_tables(rvbbit):
    a = f"diff_a_{uuid.uuid4().hex[:8]}"
    b = f"diff_b_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {a} (id int, body text) USING rvbbit")
    rvbbit.execute(f"CREATE TABLE {b} (id int, body text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {b} VALUES (1,'shipping was slow'),(2,'love product')")
    rvbbit.execute(
        f"INSERT INTO {a} VALUES "
        f"(1,'shipping was slow'),"
        f"(2,'completely brand new cybersecurity topic'),"
        f"(3,'another novel topic about quantum computing')"
    )
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{a}'::regclass)")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{b}'::regclass)")
    yield a, b
    rvbbit.execute(f"DROP TABLE {a}")
    rvbbit.execute(f"DROP TABLE {b}")


def test_diff_identical_row_has_zero_novelty(rvbbit, stub_embed, two_tables):
    a, b = two_tables
    rows = rvbbit.execute(
        "SELECT text, novelty FROM rvbbit.diff(%s, %s, 5, %s)",
        (f"SELECT body FROM {a}", f"SELECT body FROM {b}", stub_embed),
    ).fetchall()
    novelty_by_text = {r[0]: r[1] for r in rows}
    # The shared text appears in both — novelty must be 0.
    assert abs(novelty_by_text["shipping was slow"]) < 1e-9


def test_diff_sorted_descending_by_novelty(rvbbit, stub_embed, two_tables):
    a, b = two_tables
    rows = rvbbit.execute(
        "SELECT novelty FROM rvbbit.diff(%s, %s, 5, %s)",
        (f"SELECT body FROM {a}", f"SELECT body FROM {b}", stub_embed),
    ).fetchall()
    novelties = [r[0] for r in rows]
    assert novelties == sorted(novelties, reverse=True)


def test_diff_empty_b_returns_max_novelty(rvbbit, stub_embed, two_tables):
    a, _ = two_tables
    rows = rvbbit.execute(
        "SELECT text, novelty FROM rvbbit.diff(%s, %s, 5, %s)",
        (f"SELECT body FROM {a}", "SELECT 'x' WHERE false", stub_embed),
    ).fetchall()
    # Empty B → everything is maximally novel (1.0).
    for _, n in rows:
        assert abs(n - 1.0) < 1e-9


def test_diff_empty_a_returns_empty(rvbbit, stub_embed, two_tables):
    _, b = two_tables
    rows = rvbbit.execute(
        "SELECT * FROM rvbbit.diff(%s, %s, 5, %s)",
        ("SELECT 'x' WHERE false", f"SELECT body FROM {b}", stub_embed),
    ).fetchall()
    assert rows == []


def test_diff_k_respected(rvbbit, stub_embed, two_tables):
    a, b = two_tables
    rows = rvbbit.execute(
        "SELECT * FROM rvbbit.diff(%s, %s, 2, %s)",
        (f"SELECT body FROM {a}", f"SELECT body FROM {b}", stub_embed),
    ).fetchall()
    assert len(rows) == 2


# ---- semantic_case -------------------------------------------------


def test_semantic_case_returns_argmax_result(rvbbit, stub_embed):
    """Identity case: an exact-match condition should win every time."""
    out = rvbbit.execute(
        "SELECT rvbbit.semantic_case("
        "  'the product is broken', "
        "  ARRAY['the product is broken', 'unrelated phrase', 'random phrase'], "
        "  ARRAY['exact', 'other', 'other2'], "
        "  'fallback', 0.0, %s)",
        (stub_embed,),
    ).fetchone()[0]
    assert out == "exact"


def test_semantic_case_falls_back_below_min_score(rvbbit, stub_embed):
    out = rvbbit.execute(
        "SELECT rvbbit.semantic_case("
        "  'totally unrelated text', "
        "  ARRAY['something else entirely', 'another disjoint text'], "
        "  ARRAY['x', 'y'], "
        "  'unknown', 0.99, %s)",
        (stub_embed,),
    ).fetchone()[0]
    # min_score 0.99 with stub vectors that don't match -> default.
    assert out == "unknown"


def test_semantic_case_empty_text_returns_default(rvbbit, stub_embed):
    out = rvbbit.execute(
        "SELECT rvbbit.semantic_case("
        "  '', ARRAY['a','b'], ARRAY['x','y'], 'def', 0.0, %s)",
        (stub_embed,),
    ).fetchone()[0]
    assert out == "def"


def test_semantic_case_length_mismatch_errors(rvbbit, stub_embed):
    with pytest.raises(Exception) as exc:
        rvbbit.execute(
            "SELECT rvbbit.semantic_case("
            "  'x', ARRAY['a','b'], ARRAY['x'], 'def', 0.0, %s)",
            (stub_embed,),
        ).fetchone()
    assert "length" in str(exc.value).lower()


def test_semantic_case_caches_inputs(rvbbit, stub_embed):
    """First call embeds text + conditions; second call should hit cache."""
    before = rvbbit.execute(
        "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
        (stub_embed,),
    ).fetchone()
    before_n = before[0] if before else 0

    rvbbit.execute(
        "SELECT rvbbit.semantic_case("
        "  'cache test input', "
        "  ARRAY['condition alpha', 'condition beta'], "
        "  ARRAY['A', 'B'], 'fallback', 0.0, %s)",
        (stub_embed,),
    )

    after_n = rvbbit.execute(
        "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
        (stub_embed,),
    ).fetchone()[0]
    # 1 text + 2 conditions = 3 new cache entries.
    assert after_n - before_n >= 3
