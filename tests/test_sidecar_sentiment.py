"""Smoke test for the sentiment built-in sidecar.

Validates the end-to-end pipe with a REAL model: rvbbit → HTTP →
DistilBERT-SST-2 → jsonb back to SQL. We don't assert exact scores,
just label correctness on unambiguous phrases.

Skipped if the sentiment sidecar isn't reachable. Requires the
`models` profile:

  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.sidecars.yml \\
                 --profile models up -d sentiment
"""
import json
import urllib.request
import uuid

import pytest


SIDECAR_BASE = "http://rvbbit-sentiment:8080"
PREDICT_URL = f"{SIDECAR_BASE}/predict"


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/health", timeout=3).read()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _alive(),
    reason=f"sentiment sidecar not reachable at {SIDECAR_BASE}",
)


def _setup(rvbbit):
    backend_name = f"sent_{uuid.uuid4().hex[:8]}"
    op_name = f"sentiment_op_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_batch_size => 32, "
        "  backend_timeout_ms => 60000)",
        (backend_name, PREDICT_URL),
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
                "name": "s",
                "kind": "specialist",
                "specialist": backend_name,
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


def test_sentiment_scalar(rvbbit):
    backend_name, op_name = _setup(rvbbit)
    try:
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        pos = rvbbit.execute(f"SELECT rvbbit.{op_name}('I absolutely love this!')").fetchone()[0]
        neg = rvbbit.execute(f"SELECT rvbbit.{op_name}('This is utterly terrible.')").fetchone()[0]
        assert pos["label"] == "POSITIVE"
        assert pos["score"] > 0.9
        assert neg["label"] == "NEGATIVE"
        assert neg["score"] > 0.9
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_sentiment_prewarm_batched(rvbbit):
    """8 inputs / batch_size 32 → 1 HTTP call, all positive labels."""
    backend_name, op_name = _setup(rvbbit)
    table = f"sent_input_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        positives = [
            "I love this!", "Wonderful experience.", "Amazing product.",
            "Highly recommend!", "Fantastic value.", "Perfect for me.",
            "Truly excellent.", "Made my day."
        ]
        for t in positives:
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (t,))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")

        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        n_in, _n_hits, n_exec, n_err, _ = row
        assert n_in == 8
        assert n_exec == 8
        assert n_err == 0

        rows = rvbbit.execute(
            f"SELECT text, rvbbit.{op_name}(text) FROM {table}"
        ).fetchall()
        assert all(r[1]["label"] == "POSITIVE" for r in rows), [r[1] for r in rows]
    finally:
        _cleanup(rvbbit, op_name, backend_name, table)
