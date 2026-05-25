"""Token-count + tokenize UDFs — RYR-289 slice 1 (tiktoken-rs bindings).

Verifies the cheapest tier of the local model stack: deterministic
BPE token counts using OpenAI's bundled encodings. No model files,
no network. Used by EXPLAIN SEMANTIC (RYR-290) for cost preview.
"""
import pytest


def test_token_count_default_encoding(rvbbit):
    # cl100k_base: "hello world" -> [hello, " world"] -> 2 tokens
    n = rvbbit.execute("SELECT rvbbit.token_count('hello world')").fetchone()[0]
    assert n == 2


def test_token_count_explicit_encoding(rvbbit):
    # o200k_base is the GPT-4o tokenizer — same trivial phrase still 2 tokens
    n = rvbbit.execute(
        "SELECT rvbbit.token_count('hello world', 'o200k_base')"
    ).fetchone()[0]
    assert n == 2


def test_token_count_empty_string(rvbbit):
    n = rvbbit.execute("SELECT rvbbit.token_count('')").fetchone()[0]
    assert n == 0


def test_token_count_unknown_encoding_errors(rvbbit):
    with pytest.raises(Exception) as exc:
        rvbbit.execute("SELECT rvbbit.token_count('hi', 'bogus_encoding')").fetchone()
    assert "unknown encoding" in str(exc.value).lower()


def test_tokenize_returns_array(rvbbit):
    toks = rvbbit.execute("SELECT rvbbit.tokenize('hello world')").fetchone()[0]
    assert isinstance(toks, list)
    assert len(toks) == 2
    # Every token ID should be a non-negative int that fits in a 32-bit signed.
    assert all(isinstance(t, int) and 0 <= t < (1 << 31) for t in toks)


def test_tokenize_count_matches_token_count(rvbbit):
    sentence = "Postgres is an extensible database engine."
    n_from_count = rvbbit.execute(
        "SELECT rvbbit.token_count(%s)", (sentence,)
    ).fetchone()[0]
    n_from_array = rvbbit.execute(
        "SELECT array_length(rvbbit.tokenize(%s), 1)", (sentence,)
    ).fetchone()[0]
    assert n_from_count == n_from_array


def test_token_encodings_lists_supported(rvbbit):
    encs = rvbbit.execute("SELECT rvbbit.token_encodings()").fetchone()[0]
    assert set(encs) == {"cl100k_base", "o200k_base", "p50k_base", "r50k_base"}


def test_token_count_is_immutable(rvbbit):
    """IMMUTABLE STRICT PARALLEL SAFE — same input always gives same output,
    so PG can cache + parallelize. Hammer it to confirm no state drift."""
    n1 = rvbbit.execute("SELECT rvbbit.token_count('repeatability check')").fetchone()[0]
    for _ in range(5):
        n = rvbbit.execute(
            "SELECT rvbbit.token_count('repeatability check')"
        ).fetchone()[0]
        assert n == n1
