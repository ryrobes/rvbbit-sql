"""OpenAI-embeddings transport — talks to the echo-openai-embed sidecar.

Verifies:
  - wire format (POST /v1/embeddings with {input, model} body)
  - response parsing (data[].embedding, sorted by index)
  - return type is jsonb (operator return_type='jsonb' for embedding arrays)
  - prewarm batches client-side: N inputs at batch_size B → ceil(N/B) HTTP calls
  - L1 cache key includes input text (different text → different vector)

Skipped automatically if the echo-openai-embed sidecar isn't reachable.
"""
import json
import urllib.request
import uuid

import pytest


SIDECAR_BASE = "http://rvbbit-echo-openai-embed:8080"
EMBEDDINGS_URL = f"{SIDECAR_BASE}/v1/embeddings"


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/health", timeout=2).read()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _alive(),
    reason=f"echo-openai-embed sidecar not reachable at {SIDECAR_BASE}",
)


def _reset_stats() -> None:
    req = urllib.request.Request(f"{SIDECAR_BASE}/debug/reset", method="POST")
    urllib.request.urlopen(req, timeout=2).read()


def _stats() -> dict:
    return json.loads(urllib.request.urlopen(f"{SIDECAR_BASE}/debug/stats", timeout=2).read())


def _register(rvbbit, backend_name: str, batch_size: int = 16, model: str = "mock-embed") -> None:
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_transport => 'openai', "
        "  backend_batch_size => %s, "
        "  backend_timeout_ms => 5000, "
        "  backend_opts => %s::jsonb)",
        (backend_name, EMBEDDINGS_URL, batch_size, json.dumps({"model": model})),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def _create_embed_op(rvbbit, op_name: str, backend_name: str) -> None:
    """Operator returns jsonb so the embedding array survives round-tripping."""
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'jsonb', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (
            op_name,
            json.dumps([{
                "name": "e",
                "kind": "specialist",
                "specialist": backend_name,
                "inputs": {"text": "{{ inputs.text }}"},
            }]),
        ),
    )


def _cleanup(rvbbit, op_name: str, backend_name: str, table: str | None = None) -> None:
    if table:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op_name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op_name}(text, jsonb)")
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{backend_name}'")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_openai_scalar_embedding(rvbbit):
    backend_name = f"oai_em_{uuid.uuid4().hex[:8]}"
    op_name = f"embed_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name)
        _create_embed_op(rvbbit, op_name, backend_name)
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        _reset_stats()

        row = rvbbit.execute(f"SELECT rvbbit.{op_name}('hello')").fetchone()
        assert row is not None
        vec = row[0]
        # Mock sidecar produces 8-dim vectors of floats in [-1,1].
        assert isinstance(vec, list)
        assert len(vec) == 8
        assert all(-1.0 <= float(v) <= 1.0 for v in vec)

        s = _stats()
        assert s["calls"] == 1
        assert s["max_batch"] == 1
        assert s["total_inputs"] == 1
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_openai_prewarm_client_batches(rvbbit):
    """50 rows with batch_size=10 → 5 HTTP calls, max_batch=10."""
    backend_name = f"oai_batch_{uuid.uuid4().hex[:8]}"
    op_name = f"embed_{uuid.uuid4().hex[:8]}"
    table = f"embed_input_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name, batch_size=10)
        _create_embed_op(rvbbit, op_name, backend_name)

        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        marker = uuid.uuid4().hex
        for i in range(50):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"{marker}-{i:03}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")
        _reset_stats()

        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        n_in, n_hits, n_exec, n_err, _ = row
        assert n_in == 50
        assert n_hits == 0
        assert n_exec == 50
        assert n_err == 0

        s = _stats()
        assert s["calls"] == 5, f"expected 5 batches, got {s}"
        assert s["max_batch"] == 10
        assert s["total_inputs"] == 50
    finally:
        _cleanup(rvbbit, op_name, backend_name, table)


def test_openai_distinct_inputs_distinct_vectors(rvbbit):
    """Cache key must include input text — different inputs return
    different vectors even after one is cached."""
    backend_name = f"oai_distinct_{uuid.uuid4().hex[:8]}"
    op_name = f"embed_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name)
        _create_embed_op(rvbbit, op_name, backend_name)
        rvbbit.execute("SELECT rvbbit.flush_cache()")

        v1 = rvbbit.execute(f"SELECT rvbbit.{op_name}('apple')").fetchone()[0]
        v2 = rvbbit.execute(f"SELECT rvbbit.{op_name}('banana')").fetchone()[0]
        v1_again = rvbbit.execute(f"SELECT rvbbit.{op_name}('apple')").fetchone()[0]

        assert v1 != v2, "different inputs should produce different vectors"
        assert v1 == v1_again, "same input should hit cache (identical vector)"
    finally:
        _cleanup(rvbbit, op_name, backend_name)
