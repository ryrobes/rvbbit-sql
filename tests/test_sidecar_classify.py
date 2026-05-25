"""Smoke test for the classify built-in sidecar (cross-encoder NLI).

Real zero-shot classification end-to-end through rvbbit — verifies the
two-arg operator pattern (text + candidate_labels) and that the top
label is the expected one for clearly-categorical inputs.

Requires the `models` profile:
  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.sidecars.yml \\
                 --profile models up -d classify
"""
import json
import urllib.request
import uuid

import pytest


SIDECAR_BASE = "http://rvbbit-classify:8080"
PREDICT_URL = f"{SIDECAR_BASE}/predict"


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/health", timeout=3).read()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _alive(),
    reason=f"classify sidecar not reachable at {SIDECAR_BASE}",
)


def _setup(rvbbit):
    backend_name = f"cls_{uuid.uuid4().hex[:8]}"
    op_name = f"classify_op_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, "
        "  backend_batch_size => 16, backend_timeout_ms => 60000)",
        (backend_name, PREDICT_URL),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text','candidate_labels'], "
        "  op_return_type => 'jsonb', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (
            op_name,
            json.dumps([{
                "name": "c", "kind": "specialist", "specialist": backend_name,
                "inputs": {
                    "text": "{{ inputs.text }}",
                    "candidate_labels": "{{ inputs.candidate_labels }}",
                },
            }]),
        ),
    )
    return backend_name, op_name


def _cleanup(rvbbit, op_name, backend_name):
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op_name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op_name}(text, text, jsonb)")
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{backend_name}'")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_classify_picks_top_label(rvbbit):
    backend_name, op_name = _setup(rvbbit)
    try:
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        labels = "sports,politics,technology,cooking"
        cases = [
            ("The Lakers beat the Celtics by 12 last night.", "sports"),
            ("The senator proposed new infrastructure legislation.", "politics"),
            ("Add salt and simmer for 20 minutes.", "cooking"),
        ]
        for text, expected in cases:
            result = rvbbit.execute(
                f"SELECT rvbbit.{op_name}(%s, %s)", (text, labels)
            ).fetchone()[0]
            assert result["label"] == expected, (
                f"'{text}' → expected {expected}, got {result}"
            )
            assert 0.0 <= result["score"] <= 1.0
            assert set(result["all"].keys()) == set(labels.split(","))
    finally:
        _cleanup(rvbbit, op_name, backend_name)
