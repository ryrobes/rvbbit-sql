"""JIT embeddings + content-addressed cache — RYR-289 capstone.

Uses the in-process 'stub' transport (deterministic hash-based vectors)
so tests don't need network or an embedding model. Real-model behavior
is covered by manual scale checks; correctness of cache + UDF shape
lives here.
"""
import json
import uuid

import pytest


@pytest.fixture
def stub_embed(rvbbit):
    """Register a stub embedder and return its name. Auto-cleanup."""
    name = f"stub_embed_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_transport => %s)",
        (name, "stub://384", "stub"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    yield name
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")
    rvbbit.execute(f"SELECT rvbbit.embedding_purge('{name}')")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_embed_returns_384_dim_vector(rvbbit, stub_embed):
    row = rvbbit.execute(
        "SELECT rvbbit.embed('hello world', %s)", (stub_embed,)
    ).fetchone()
    vec = row[0]
    assert isinstance(vec, list)
    assert len(vec) == 384


def test_embed_is_deterministic_same_text(rvbbit, stub_embed):
    a = rvbbit.execute(
        "SELECT rvbbit.embed('repeatable input', %s)", (stub_embed,)
    ).fetchone()[0]
    b = rvbbit.execute(
        "SELECT rvbbit.embed('repeatable input', %s)", (stub_embed,)
    ).fetchone()[0]
    assert a == b


def test_similarity_of_identical_text_is_one(rvbbit, stub_embed):
    sim = rvbbit.execute(
        "SELECT rvbbit.similarity('hello world', 'hello world', %s)",
        (stub_embed,),
    ).fetchone()[0]
    assert abs(sim - 1.0) < 1e-6


def test_similarity_of_different_text_is_not_one(rvbbit, stub_embed):
    sim = rvbbit.execute(
        "SELECT rvbbit.similarity('the quick brown fox', "
        "'something completely unrelated', %s)",
        (stub_embed,),
    ).fetchone()[0]
    # Stub vectors are hash-based — different inputs should be near
    # orthogonal, definitely not identical.
    assert abs(sim - 1.0) > 0.5


def test_embed_distance_complements_similarity(rvbbit, stub_embed):
    a = rvbbit.execute(
        "SELECT rvbbit.similarity('foo bar', 'baz qux', %s)", (stub_embed,)
    ).fetchone()[0]
    b = rvbbit.execute(
        "SELECT rvbbit.embed_distance('foo bar', 'baz qux', %s)", (stub_embed,)
    ).fetchone()[0]
    assert abs((a + b) - 1.0) < 1e-9


def test_cosine_vec_on_materialized_vectors(rvbbit, stub_embed):
    # Manually compute cosine on two pre-fetched vectors and check it
    # matches rvbbit.cosine_vec.
    sim_text = rvbbit.execute(
        "SELECT rvbbit.similarity('foo', 'bar', %s)", (stub_embed,)
    ).fetchone()[0]
    sim_vec = rvbbit.execute(
        "SELECT rvbbit.cosine_vec(rvbbit.embed('foo', %s), rvbbit.embed('bar', %s))",
        (stub_embed, stub_embed),
    ).fetchone()[0]
    assert abs(sim_text - sim_vec) < 1e-9


def test_cache_populates_on_first_call(rvbbit, stub_embed):
    before = rvbbit.execute(
        "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
        (stub_embed,),
    ).fetchone()
    before_n = before[0] if before else 0

    rvbbit.execute(
        "SELECT rvbbit.embed('a brand new piece of text', %s)", (stub_embed,)
    )

    after = rvbbit.execute(
        "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
        (stub_embed,),
    ).fetchone()
    after_n = after[0]
    assert after_n == before_n + 1


def test_materialize_embeddings_pre_warms(rvbbit, stub_embed):
    t = f"emb_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    bodies = [
        "alpha first body",
        "beta second body",
        "gamma third body",
        "alpha first body",   # duplicate — should only embed once
    ]
    for i, b in enumerate(bodies):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, b))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        # First materialize call — embeds 3 distinct values.
        produced = rvbbit.execute(
            "SELECT rvbbit.materialize_embeddings(%s::regclass::oid, 'body', %s)",
            (t, stub_embed),
        ).fetchone()[0]
        assert produced == 3, f"expected 3 distinct embeddings, got {produced}"

        # Second call — all cached, no new work.
        produced_again = rvbbit.execute(
            "SELECT rvbbit.materialize_embeddings(%s::regclass::oid, 'body', %s)",
            (t, stub_embed),
        ).fetchone()[0]
        assert produced_again == 0
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_purge_clears_cache_for_specialist(rvbbit, stub_embed):
    rvbbit.execute(
        "SELECT rvbbit.embed('to be purged', %s)", (stub_embed,)
    )
    n_before = rvbbit.execute(
        "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
        (stub_embed,),
    ).fetchone()[0]
    assert n_before > 0

    purged = rvbbit.execute(
        "SELECT rvbbit.embedding_purge(%s)", (stub_embed,)
    ).fetchone()[0]
    assert purged == n_before

    n_after_row = rvbbit.execute(
        "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
        (stub_embed,),
    ).fetchone()
    # After purge there should be no row for this specialist.
    assert n_after_row is None


def test_different_specialists_keep_separate_entries(rvbbit, stub_embed):
    # Register a second stub specialist with a different dim so we can tell
    # which one served a call.
    other = f"stub_other_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_transport => %s)",
        (other, "stub://128", "stub"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    try:
        v_default = rvbbit.execute(
            "SELECT rvbbit.embed('same text both specialists', %s)",
            (stub_embed,),
        ).fetchone()[0]
        v_other = rvbbit.execute(
            "SELECT rvbbit.embed('same text both specialists', %s)",
            (other,),
        ).fetchone()[0]
        assert len(v_default) == 384
        assert len(v_other) == 128

        # Both should now appear in stats.
        rows = rvbbit.execute(
            "SELECT specialist, dim FROM rvbbit.embedding_cache_stats() "
            "WHERE specialist IN (%s, %s) ORDER BY specialist",
            (stub_embed, other),
        ).fetchall()
        assert len(rows) == 2
    finally:
        rvbbit.execute(f"SELECT rvbbit.embedding_purge('{other}')")
        rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{other}'")
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_knn_text_returns_topk_by_cosine(rvbbit, stub_embed):
    t = f"knn_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rows = [
        (1, "the cat sat on the mat"),
        (2, "completely unrelated phrase here"),
        (3, "the cat sat on the mat"),  # duplicate of #1
        (4, "another distinct sentence"),
        (5, "yet another sentence"),
    ]
    for r in rows:
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", r)
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        # Query exactly matches one row → cosine 1.0 for that row.
        out = rvbbit.execute(
            "SELECT value, score FROM rvbbit.knn_text("
            "%s::regclass::oid, 'body', 'the cat sat on the mat', 3, %s)",
            (t, stub_embed),
        ).fetchall()
        assert len(out) == 3
        # Top row should be the exact match with score 1.0.
        assert out[0][0] == "the cat sat on the mat"
        assert abs(out[0][1] - 1.0) < 1e-6
        # Results are sorted descending by score.
        assert all(out[i][1] >= out[i + 1][1] for i in range(len(out) - 1))
        # Distinct only — no duplicate of row 1 should appear.
        seen_values = [r[0] for r in out]
        assert len(set(seen_values)) == len(seen_values)
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_knn_text_respects_k(rvbbit, stub_embed):
    t = f"knn_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    for i in range(10):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, f"body number {i}"))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        for k in (1, 3, 5):
            out = rvbbit.execute(
                "SELECT * FROM rvbbit.knn_text(%s::regclass::oid, 'body', 'query', %s, %s)",
                (t, k, stub_embed),
            ).fetchall()
            assert len(out) == k
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_knn_text_handles_empty_table(rvbbit, stub_embed):
    t = f"knn_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        out = rvbbit.execute(
            "SELECT * FROM rvbbit.knn_text(%s::regclass::oid, 'body', 'anything', 5, %s)",
            (t, stub_embed),
        ).fetchall()
        assert out == []
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_knn_text_warms_cache_for_distinct_values(rvbbit, stub_embed):
    t = f"knn_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {t} VALUES (1, 'alpha'), (2, 'beta'), (3, 'gamma')")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        before = rvbbit.execute(
            "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
            (stub_embed,),
        ).fetchone()
        before_n = before[0] if before else 0

        rvbbit.execute(
            "SELECT * FROM rvbbit.knn_text(%s::regclass::oid, 'body', 'query', 2, %s)",
            (t, stub_embed),
        ).fetchall()

        after = rvbbit.execute(
            "SELECT n_entries FROM rvbbit.embedding_cache_stats() WHERE specialist = %s",
            (stub_embed,),
        ).fetchone()
        # 3 distinct rows + 1 query = 4 new entries (or only 3 if query happens to match a row).
        assert after[0] - before_n >= 3
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_knn_text_k_zero_or_negative_returns_empty(rvbbit, stub_embed):
    t = f"knn_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    rvbbit.execute(f"INSERT INTO {t} VALUES (1, 'one'), (2, 'two')")
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    try:
        for k in (0, -1):
            out = rvbbit.execute(
                "SELECT * FROM rvbbit.knn_text(%s::regclass::oid, 'body', 'q', %s, %s)",
                (t, k, stub_embed),
            ).fetchall()
            assert out == []
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_implicit_default_specialist_named_embed(rvbbit):
    """When the user omits the specialist arg, rvbbit falls back to
    a specialist literally named 'embed'. Register one + verify."""
    original = rvbbit.execute(
        "SELECT transport, endpoint_url, batch_size, max_concurrent, "
        "timeout_ms, auth_header_env, transport_opts, description "
        "FROM rvbbit.backends WHERE name = 'embed'"
    ).fetchone()
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => 'embed', "
        "  backend_endpoint => 'stub://384', "
        "  backend_transport => 'stub')"
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    try:
        vec = rvbbit.execute(
            "SELECT rvbbit.embed('default-specialist text')"
        ).fetchone()[0]
        assert len(vec) == 384
    finally:
        rvbbit.execute("SELECT rvbbit.embedding_purge('embed')")
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = 'embed'")
        if original:
            rvbbit.execute(
                "INSERT INTO rvbbit.backends "
                "(name, transport, endpoint_url, batch_size, max_concurrent, "
                " timeout_ms, auth_header_env, transport_opts, description) "
                "VALUES ('embed', %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                (*original[:6], json.dumps(original[6]), original[7]),
            )
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_default_embed_backend_is_local_but_replaceable(rvbbit):
    original = rvbbit.execute(
        "SELECT transport, endpoint_url, batch_size, max_concurrent, "
        "timeout_ms, auth_header_env, transport_opts, description "
        "FROM rvbbit.backends WHERE name = 'embed'"
    ).fetchone()
    row = rvbbit.execute(
        "SELECT transport, endpoint_url, transport_opts->>'model' "
        "FROM rvbbit.backends WHERE name = 'embed'"
    ).fetchone()
    assert row is not None
    assert row[0] == "local_embed"
    assert row[1] == "local://embed"
    assert row[2] == "bge-small-en-v1.5"

    try:
        rvbbit.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => 'embed', "
            "  backend_endpoint => 'stub://384', "
            "  backend_transport => 'stub')"
        )
        replaced = rvbbit.execute(
            "SELECT transport, endpoint_url FROM rvbbit.backends WHERE name = 'embed'"
        ).fetchone()
        assert replaced == ("stub", "stub://384")
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = 'embed'")
        if original:
            rvbbit.execute(
                "INSERT INTO rvbbit.backends "
                "(name, transport, endpoint_url, batch_size, max_concurrent, "
                " timeout_ms, auth_header_env, transport_opts, description) "
                "VALUES ('embed', %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                (*original[:6], json.dumps(original[6]), original[7]),
            )
        rvbbit.execute("SELECT rvbbit.reload_backends()")
