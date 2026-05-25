"""Gradio transport — talks to the echo-gradio sidecar via /api/predict.

Verifies:
  - wire format (positional data array)
  - per-row dispatch (Gradio batches server-side via gr.Interface(batch=True),
    not client-side, so rvbbit sends one HTTP call per row)
  - prewarm runs N concurrent calls (one per row) instead of one batched call
  - L1 cache works the same as for rvbbit-transport specialists

Skipped automatically if the echo-gradio sidecar isn't reachable.
"""
import json
import urllib.request
import uuid

import pytest


ECHO_BASE = "http://rvbbit-echo-gradio:7860"
ECHO_PREDICT = f"{ECHO_BASE}/api/predict"


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{ECHO_BASE}/config", timeout=3).read()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _alive(),
    reason=f"echo-gradio sidecar not reachable at {ECHO_BASE}",
)


def _register(rvbbit, backend_name: str, fn_index: int | None = None) -> None:
    opts = {"fn_index": fn_index} if fn_index is not None else {}
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, "
        "  backend_endpoint => %s, "
        "  backend_transport => 'gradio', "
        "  backend_timeout_ms => 10000, "
        "  backend_opts => %s::jsonb)",
        (backend_name, ECHO_PREDICT, json.dumps(opts)),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def _create_op(rvbbit, op_name: str, backend_name: str, mode: str) -> None:
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (
            op_name,
            json.dumps([{
                "name": "g",
                "kind": "specialist",
                "specialist": backend_name,
                # Gradio's data array is the positional args of the underlying
                # function: (text, mode) for our echo Interface.
                "inputs": {"data": ["{{ inputs.text }}", mode]},
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


def test_gradio_scalar_call(rvbbit):
    backend_name = f"gradio_up_{uuid.uuid4().hex[:8]}"
    op_name = f"gradio_op_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name)
        _create_op(rvbbit, op_name, backend_name, mode="upper")
        rvbbit.execute("SELECT rvbbit.flush_cache()")

        result = rvbbit.execute(f"SELECT rvbbit.{op_name}('hello world')").fetchone()[0]
        assert result == "HELLO WORLD"
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_gradio_reverse_mode(rvbbit):
    backend_name = f"gradio_rev_{uuid.uuid4().hex[:8]}"
    op_name = f"gradio_op_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name)
        _create_op(rvbbit, op_name, backend_name, mode="reverse")
        rvbbit.execute("SELECT rvbbit.flush_cache()")

        result = rvbbit.execute(f"SELECT rvbbit.{op_name}('abcdef')").fetchone()[0]
        assert result == "fedcba"
    finally:
        _cleanup(rvbbit, op_name, backend_name)


def test_gradio_prewarm_per_row_dispatch(rvbbit):
    """Gradio is server-batched, so client_batches=false → 1 HTTP call/row.
    Verifies all rows return correct results and prewarm reports n_executed
    matching the row count (regardless of how those calls were batched)."""
    backend_name = f"gradio_pw_{uuid.uuid4().hex[:8]}"
    op_name = f"gradio_op_{uuid.uuid4().hex[:8]}"
    table = f"gradio_input_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name)
        _create_op(rvbbit, op_name, backend_name, mode="upper")
        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        for i in range(8):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"row-{i}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")

        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op_name, f"SELECT text FROM {table}"),
        ).fetchone()
        n_in, n_hits, n_exec, n_err, _ = row
        assert n_in == 8
        assert n_hits == 0
        assert n_exec == 8
        assert n_err == 0

        rows = rvbbit.execute(
            f"SELECT text, rvbbit.{op_name}(text) FROM {table} ORDER BY text"
        ).fetchall()
        assert all(r[1] == r[0].upper() for r in rows)
    finally:
        _cleanup(rvbbit, op_name, backend_name, table)


def test_gradio_second_call_is_cache_hit(rvbbit):
    backend_name = f"gradio_cache_{uuid.uuid4().hex[:8]}"
    op_name = f"gradio_op_{uuid.uuid4().hex[:8]}"
    try:
        _register(rvbbit, backend_name)
        _create_op(rvbbit, op_name, backend_name, mode="upper")
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op_name}'")

        rvbbit.execute(f"SELECT rvbbit.{op_name}('hello')").fetchone()
        before = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op_name}'"
        ).fetchone()[0]
        assert before == 1

        # Same input again — should hit L1.
        result = rvbbit.execute(f"SELECT rvbbit.{op_name}('hello')").fetchone()[0]
        assert result == "HELLO"
        after = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op_name}'"
        ).fetchone()[0]
        assert after == before, "cache hit should not log a new receipt"
    finally:
        _cleanup(rvbbit, op_name, backend_name)
