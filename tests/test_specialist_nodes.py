"""Specialist endpoints as operator node primitives (Loop 20).

A specialist endpoint is a registered model backend (a row in
rvbbit.backends). It is NOT a callable in its own right — it is a node
*primitive*, `kind: specialist`, a peer of the `llm` and `code` node kinds,
used inside an operator's `steps`. The operator is the only callable thing.

These tests use the in-process `stub` transport (deterministic hash-based
"embeddings") so they need no network, sidecar, or model files.
"""

import json
import uuid


def _register_stub(rvbbit, dim=8):
    name = f"stub_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, backend_transport => 'stub')",
        (name, f"stub://{dim}"),
    )
    return name


def _drop_specialist(rvbbit, name):
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")


def _drop_op(rvbbit, name, n_args=1):
    sig = ", ".join(["text"] * n_args + ["jsonb"])
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}({sig})")


def _specialist_op(rvbbit, op, spec, return_type="jsonb"):
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_arg_names => ARRAY['text'], "
        "  op_return_type => %s, op_steps => %s::jsonb)",
        (
            op,
            return_type,
            json.dumps(
                [
                    {
                        "name": "e",
                        "kind": "specialist",
                        "specialist": spec,
                        "inputs": {"text": "{{ inputs.text }}"},
                    }
                ]
            ),
        ),
    )


# ---- create_operator no longer demands prompts ---------------------------


def test_create_operator_without_prompts(rvbbit):
    """A steps-only operator is created with no op_system / op_user — the
    prompts default to '' and are simply unused."""
    name = f"np_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_arg_names => ARRAY['text'], "
            "  op_return_type => 'text', op_steps => %s::jsonb)",
            (
                name,
                json.dumps(
                    [
                        {
                            "name": "c",
                            "kind": "code",
                            "fn": "trim",
                            "inputs": {"text": "{{ inputs.text }}"},
                        }
                    ]
                ),
            ),
        )
        row = rvbbit.execute(
            "SELECT system_prompt, user_prompt FROM rvbbit.operators WHERE name = %s",
            (name,),
        ).fetchone()
        assert row == ("", "")
        assert rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ')").fetchone()[0] == "hi"
    finally:
        _drop_op(rvbbit, name)


# ---- a specialist endpoint, wrapped as an operator -----------------------


def test_specialist_node_operator(rvbbit):
    """Register a backend; wrap it in a plain operator via a specialist
    node. The operator is the callable; the specialist is the primitive."""
    spec = _register_stub(rvbbit, dim=8)
    op = f"vec_{uuid.uuid4().hex[:8]}"
    try:
        _specialist_op(rvbbit, op, spec)
        row = rvbbit.execute(f"SELECT rvbbit.{op}('hello')").fetchone()
        assert isinstance(row[0], list)
        assert len(row[0]) == 8  # the stub backend's dim
    finally:
        _drop_op(rvbbit, op)
        _drop_specialist(rvbbit, spec)


def test_specialist_node_deterministic(rvbbit):
    spec = _register_stub(rvbbit, dim=8)
    op = f"vec_{uuid.uuid4().hex[:8]}"
    try:
        _specialist_op(rvbbit, op, spec)
        a = rvbbit.execute(f"SELECT rvbbit.{op}('same text')").fetchone()[0]
        b = rvbbit.execute(f"SELECT rvbbit.{op}('same text')").fetchone()[0]
        assert a == b
    finally:
        _drop_op(rvbbit, op)
        _drop_specialist(rvbbit, spec)


def test_specialist_node_audited_in_receipt(rvbbit):
    """A specialist node shows up in the receipt's sub_calls audit."""
    spec = _register_stub(rvbbit)
    op = f"vec_{uuid.uuid4().hex[:8]}"
    try:
        _specialist_op(rvbbit, op, spec)
        rvbbit.execute(f"SELECT rvbbit.{op}('audit-{uuid.uuid4().hex}')")
        kind = rvbbit.execute(
            "SELECT sub_calls->0->>'kind' FROM rvbbit.receipts "
            "WHERE operator = %s ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()
        assert kind[0] == "specialist"
    finally:
        _drop_op(rvbbit, op)
        _drop_specialist(rvbbit, spec)


# ---- chained, heterogeneous node pipelines -------------------------------


def test_chained_code_then_specialist(rvbbit):
    """A pipeline mixing node kinds — code node feeds a specialist node.
    This is the operator 'graph': nodes of any kind, composed."""
    spec = _register_stub(rvbbit, dim=8)
    op = f"chain_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_arg_names => ARRAY['text'], "
            "  op_return_type => 'jsonb', op_steps => %s::jsonb)",
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "up",
                            "kind": "code",
                            "fn": "uppercase",
                            "inputs": {"text": "{{ inputs.text }}"},
                        },
                        {
                            "name": "e",
                            "kind": "specialist",
                            "specialist": spec,
                            "inputs": {"text": "{{ steps.up.output }}"},
                        },
                    ]
                ),
            ),
        )
        out = rvbbit.execute(f"SELECT rvbbit.{op}('hello')").fetchone()[0]
        assert isinstance(out, list) and len(out) == 8
        kinds = rvbbit.execute(
            "SELECT sub_calls->0->>'kind', sub_calls->1->>'kind' "
            "FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()
        assert kinds == ("code", "specialist")
    finally:
        _drop_op(rvbbit, op)
        _drop_specialist(rvbbit, spec)


def test_specialist_node_composes_with_flow(rvbbit):
    """Flow control wraps an operator regardless of its node kind — a
    blocking post-ward gates a specialist-node operator's output."""
    spec = _register_stub(rvbbit, dim=8)
    op = f"warded_{uuid.uuid4().hex[:8]}"
    try:
        _specialist_op(rvbbit, op, spec)
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                op,
                json.dumps(
                    {
                        "post": [
                            {
                                "validator": {
                                    "sql": "jsonb_array_length($output::jsonb) = 8"
                                },
                                "mode": "blocking",
                            }
                        ]
                    }
                ),
            ),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{op}('warded')").fetchone()
        assert isinstance(row[0], list) and len(row[0]) == 8  # post-ward passed
    finally:
        _drop_op(rvbbit, op)
        _drop_specialist(rvbbit, spec)
