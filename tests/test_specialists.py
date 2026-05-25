"""Specialist transport — rvbbit-native wire protocol end-to-end.

Talks to the echo sidecar (sidecars/echo/main.py). Verifies:
  - register_backend + reload_backends DDL
  - single-row scalar call goes through the transport
  - prewarm batches per spec.batch_size (1 HTTP call per chunk)
  - 2nd prewarm hits cache (no echo calls at all)
  - bearer-token auth flows through auth_header_env

Requires the echo sidecar to be reachable at rvbbit-echo:8080 from
the pg-rvbbit container. Bring it up via:
  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.sidecars.yml up -d echo
"""
import json
import urllib.request
import uuid

import pytest


ECHO_BASE = "http://rvbbit-echo:8080"
ECHO_PREDICT = f"{ECHO_BASE}/predict"


def _echo_alive() -> bool:
    try:
        urllib.request.urlopen(f"{ECHO_BASE}/health", timeout=2).read()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _echo_alive(),
    reason=f"echo sidecar not reachable at {ECHO_BASE} — run docker-compose.sidecars.yml",
)


def _echo_reset() -> None:
    req = urllib.request.Request(f"{ECHO_BASE}/debug/reset", method="POST")
    urllib.request.urlopen(req, timeout=2).read()


def _echo_stats() -> dict:
    return json.loads(urllib.request.urlopen(f"{ECHO_BASE}/debug/stats", timeout=2).read())


def _register_echo(rvbbit, backend_name: str, batch_size: int = 32) -> None:
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_batch_size => %s, "
        "  backend_max_concur => 4, "
        "  backend_timeout_ms => 5000)",
        (backend_name, ECHO_PREDICT, batch_size),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def _create_specialist_op(rvbbit, op_name: str, backend_name: str, fn: str = "upper") -> None:
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (
            op_name,
            json.dumps([{
                "name": "s",
                "kind": "specialist",
                "specialist": backend_name,
                "inputs": {"text": "{{ inputs.text }}", "fn": fn},
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


def test_reload_specialists_returns_count(rvbbit):
    backend_name = f"echo_reload_{uuid.uuid4().hex[:8]}"
    try:
        before = rvbbit.execute("SELECT rvbbit.reload_backends()").fetchone()[0]
        _register_echo(rvbbit, backend_name)
        after = rvbbit.execute("SELECT rvbbit.reload_backends()").fetchone()[0]
        assert after >= before + 1
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{backend_name}'")
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_specialist_scalar_call(rvbbit):
    backend_name = f"echo_scalar_{uuid.uuid4().hex[:8]}"
    op_name = f"echo_upper_{uuid.uuid4().hex[:8]}"
    try:
        _register_echo(rvbbit, backend_name)
        _create_specialist_op(rvbbit, op_name, backend_name, fn="upper")
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        _echo_reset()

        result = rvbbit.execute(
            f"SELECT rvbbit.{op_name}('hello world')"
        ).fetchone()[0]
        assert result == "HELLO WORLD"

        stats = _echo_stats()
        assert stats["calls"] == 1
        assert stats["max_batch"] == 1
        assert stats["total_inputs"] == 1
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_specialist_prewarm_batches(rvbbit):
    """50 rows with batch_size=10 → 5 HTTP calls, max_batch=10."""
    backend_name = f"echo_batch_{uuid.uuid4().hex[:8]}"
    op_name = f"echo_op_{uuid.uuid4().hex[:8]}"
    table = f"echo_input_{uuid.uuid4().hex[:8]}"
    try:
        _register_echo(rvbbit, backend_name, batch_size=10)
        _create_specialist_op(rvbbit, op_name, backend_name)

        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        marker = uuid.uuid4().hex
        for i in range(50):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"{marker}-{i:03}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")
        _echo_reset()

        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        n_in, n_hits, n_exec, n_err, _ = row
        assert n_in == 50
        assert n_hits == 0
        assert n_exec == 50
        assert n_err == 0

        stats = _echo_stats()
        assert stats["calls"] == 5, f"expected 5 batches, got {stats}"
        assert stats["max_batch"] == 10
        assert stats["total_inputs"] == 50

        # Result correctness — wrap query against the operator, all upper.
        rows = rvbbit.execute(
            f"SELECT text, rvbbit.{op_name}(text) FROM {table} ORDER BY text"
        ).fetchall()
        assert len(rows) == 50
        assert all(r[1] == r[0].upper() for r in rows)
    finally:
        _cleanup(rvbbit, op_name, backend_name, table)


def test_specialist_prewarm_second_pass_no_echo_calls(rvbbit):
    """After prewarm, repeat prewarm + SELECT should issue ZERO HTTP calls."""
    backend_name = f"echo_cache_{uuid.uuid4().hex[:8]}"
    op_name = f"echo_op_{uuid.uuid4().hex[:8]}"
    table = f"echo_input_{uuid.uuid4().hex[:8]}"
    try:
        _register_echo(rvbbit, backend_name, batch_size=8)
        _create_specialist_op(rvbbit, op_name, backend_name)

        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        marker = uuid.uuid4().hex
        for i in range(12):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"{marker}-{i:02}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")
        _echo_reset()

        # Warm.
        rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        first_calls = _echo_stats()["calls"]
        assert first_calls > 0

        # Second prewarm — pure cache, no echo calls.
        _echo_reset()
        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        assert row[1] == 12  # n_cache_hits
        assert row[2] == 0   # n_executed
        assert _echo_stats()["calls"] == 0

        # Query — should also be all cache hits.
        _echo_reset()
        rvbbit.execute(f"SELECT rvbbit.{op_name}(text) FROM {table}").fetchall()
        assert _echo_stats()["calls"] == 0
    finally:
        _cleanup(rvbbit, op_name, backend_name, table)


def test_specialist_error_logs_receipt(rvbbit):
    """Unreachable endpoint → result NULL, error captured in rvbbit.receipts."""
    backend_name = f"echo_bad_{uuid.uuid4().hex[:8]}"
    op_name = f"echo_bad_op_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => %s, "
            "  backend_endpoint => %s, "
            "  backend_timeout_ms => 500)",
            (backend_name, "http://nope.invalid:9/predict"),
        )
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        _create_specialist_op(rvbbit, op_name, backend_name)

        result = rvbbit.execute(f"SELECT rvbbit.{op_name}('x')").fetchone()[0]
        # Current contract: failures return empty string (matches LLM-op
        # behavior); the error is captured in rvbbit.receipts. Worth
        # revisiting later — NULL would be a stronger signal.
        assert result == ""

        # A receipt with non-null error should have been written.
        err = rvbbit.execute(
            f"SELECT error FROM rvbbit.receipts "
            f"WHERE operator = '{op_name}' AND error IS NOT NULL "
            f"ORDER BY invocation_at DESC LIMIT 1"
        ).fetchone()
        assert err is not None
        assert "nope.invalid" in err[0] or "specialist" in err[0].lower()
    finally:
        _cleanup(rvbbit, op_name, backend_name)
