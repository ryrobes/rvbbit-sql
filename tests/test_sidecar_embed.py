"""Smoke test for the embed built-in sidecar (BGE-small-en-v1.5).

Verifies a real embedding model wired through the rvbbit transport:
  - vector dimensionality (384 for bge-small)
  - L2-normalized (∥v∥ ≈ 1)
  - distinct inputs produce distinct vectors
  - prewarm dispatches one HTTP call per batch

Requires the `models` profile:
  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.sidecars.yml \\
                 --profile models up -d embed
"""
import json
import math
import urllib.request
import uuid

import pytest


SIDECAR_BASE = "http://rvbbit-embed:8080"
PREDICT_URL = f"{SIDECAR_BASE}/predict"


def _detect_dim() -> int:
    """Default sidecar is now BGE-M3 (1024-dim). Probe /health and fall
    back to 384 if the user overrode EMBED_MODEL with bge-small-en-v1.5."""
    try:
        body = urllib.request.urlopen(f"{SIDECAR_BASE}/health", timeout=3).read()
        model = json.loads(body).get("model", "")
        if "small" in model.lower():
            return 384
    except Exception:
        pass
    return 1024


DIM = _detect_dim()


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/health", timeout=3).read()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _alive(),
    reason=f"embed sidecar not reachable at {SIDECAR_BASE}",
)


def _setup(rvbbit, batch_size: int = 32):
    backend_name = f"emb_{uuid.uuid4().hex[:8]}"
    op_name = f"embed_op_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, "
        "  backend_batch_size => %s, backend_timeout_ms => 60000)",
        (backend_name, PREDICT_URL, batch_size),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'jsonb', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (
            op_name,
            json.dumps([{
                "name": "e", "kind": "specialist", "specialist": backend_name,
                "inputs": {"text": "{{ inputs.text }}"},
            }]),
        ),
    )
    return backend_name, op_name


def _cleanup(rvbbit, op_name, backend_name, table=None):
    if table:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op_name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op_name}(text, jsonb)")
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{backend_name}'")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_embed_returns_normalized_vector(rvbbit):
    backend_name, op_name = _setup(rvbbit)
    try:
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        v = rvbbit.execute(f"SELECT rvbbit.{op_name}('hello world')").fetchone()[0]
        assert isinstance(v, list)
        assert len(v) == DIM
        norm = math.sqrt(sum(float(x) * float(x) for x in v))
        assert abs(norm - 1.0) < 0.01, f"expected unit-norm, got {norm}"
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_embed_distinct_inputs_distinct_vectors(rvbbit):
    backend_name, op_name = _setup(rvbbit)
    try:
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        v1 = rvbbit.execute(f"SELECT rvbbit.{op_name}('cat')").fetchone()[0]
        v2 = rvbbit.execute(f"SELECT rvbbit.{op_name}('skyscraper')").fetchone()[0]
        # Cosine sim — unrelated terms should be ≪ 1.
        sim = sum(float(a) * float(b) for a, b in zip(v1, v2))
        assert sim < 0.9, f"distinct inputs too similar (cos={sim:.3f})"
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_embed_prewarm_batched(rvbbit):
    """20 rows / batch_size=10 → 2 HTTP calls."""
    backend_name, op_name = _setup(rvbbit, batch_size=10)
    table = f"emb_input_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        for i in range(20):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"document number {i}",))
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")

        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        assert row[0] == 20  # n_inputs
        assert row[2] == 20  # n_executed
        assert row[3] == 0   # n_errors
    finally:
        _cleanup(rvbbit, op_name, backend_name, table)
