"""rvbbit.topics — k-means topic clustering as a single SQL function.

Stub embeddings are deterministic but semantically meaningless, so these
tests assert the CONTRACT of the function (correct shape, k respected,
seed determinism, edge cases) rather than the quality of clusters. With
a real embedder the quality is whatever the embedder provides.
"""
import uuid

import pytest


@pytest.fixture
def stub_embed(rvbbit):
    name = f"stub_topics_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_transport => %s)",
        (name, "stub://128", "stub"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    yield name
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")
    rvbbit.execute(f"SELECT rvbbit.embedding_purge('{name}')")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


@pytest.fixture
def sample_table(rvbbit):
    t = f"topic_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    bodies = [
        "angry customer wants refund",
        "product broke after one week",
        "angry customer demands cancellation",
        "love this product so much",
        "shipping took two weeks",
        "product works great",
        "shipping arrived damaged",
        "love the customer service",
        "product stopped working",
        "shipping was very slow",
    ]
    for i, b in enumerate(bodies):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, b))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    yield t
    rvbbit.execute(f"DROP TABLE {t}")


def test_topics_returns_k_clusters(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT cluster_id, count, exemplar FROM rvbbit.topics("
        "  query_sql => %s, k => 3, specialist => %s) ORDER BY cluster_id",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    assert len(rows) == 3
    cluster_ids = [r[0] for r in rows]
    assert cluster_ids == [0, 1, 2]


def test_topics_counts_sum_to_total_rows(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT count FROM rvbbit.topics(%s, 3, %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    assert sum(r[0] for r in rows) == 10


def test_topics_exemplar_is_actual_row(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT exemplar FROM rvbbit.topics(%s, 3, %s)",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    sources = {
        r[0]
        for r in rvbbit.execute(
            f"SELECT body FROM {sample_table}"
        ).fetchall()
    }
    for (exemplar,) in rows:
        assert exemplar in sources


def test_topics_k_larger_than_rows_clamps(rvbbit, stub_embed):
    t = f"tiny_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {t} VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        rows = rvbbit.execute(
            "SELECT * FROM rvbbit.topics(%s, 10, %s)",
            (f"SELECT body FROM {t}", stub_embed),
        ).fetchall()
        # 3 distinct values, k=10 → at most 3 clusters returned.
        assert len(rows) == 3
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_topics_empty_query_returns_empty(rvbbit, stub_embed):
    rows = rvbbit.execute(
        "SELECT * FROM rvbbit.topics(%s, 5, %s)",
        ("SELECT 'x' WHERE false", stub_embed),
    ).fetchall()
    assert rows == []


def test_topics_invalid_k_returns_empty(rvbbit, stub_embed, sample_table):
    for k in (0, -1):
        rows = rvbbit.execute(
            "SELECT * FROM rvbbit.topics(%s, %s, %s)",
            (f"SELECT body FROM {sample_table}", k, stub_embed),
        ).fetchall()
        assert rows == []


def test_topics_determinism_same_seed(rvbbit, stub_embed, sample_table):
    """Same input + same seed should yield identical clusters."""
    q = f"SELECT body FROM {sample_table}"

    def run(seed):
        return rvbbit.execute(
            "SELECT cluster_id, count, exemplar FROM rvbbit.topics("
            "  query_sql => %s, k => 3, specialist => %s, seed => %s) "
            "ORDER BY cluster_id",
            (q, stub_embed, seed),
        ).fetchall()

    a = run(42)
    b = run(42)
    assert a == b


def test_topics_results_sorted_by_count_descending(rvbbit, stub_embed, sample_table):
    rows = rvbbit.execute(
        "SELECT count FROM rvbbit.topics(%s, 3, %s) ORDER BY cluster_id",
        (f"SELECT body FROM {sample_table}", stub_embed),
    ).fetchall()
    counts = [r[0] for r in rows]
    # cluster_id is renumbered to align with descending count.
    assert counts == sorted(counts, reverse=True)


def test_topics_warms_cache(rvbbit, stub_embed):
    """Side effect of topics() is that all distinct values get embedded
    and cached — so subsequent similarity calls are also fast."""
    t = f"warm_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    bodies = ["alpha", "beta", "gamma", "delta", "epsilon"]
    for i, b in enumerate(bodies):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, b))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        before = rvbbit.execute(
            "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
            (stub_embed,),
        ).fetchone()
        before_n = before[0] if before else 0

        rvbbit.execute(
            "SELECT * FROM rvbbit.topics(%s, 2, %s)",
            (f"SELECT body FROM {t}", stub_embed),
        ).fetchall()

        after_n = rvbbit.execute(
            "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
            (stub_embed,),
        ).fetchone()[0]
        assert after_n - before_n >= 5
    finally:
        rvbbit.execute(f"DROP TABLE {t}")
