"""Heterogeneous takes (Loop 21) — a take can be any node.

The takes config gains a `nodes` array: each entry is a node spec (the
same shape as a `steps` node — llm / specialist / python / code). The ensemble runs
every node and reduces to one answer, so an operator can vote or evaluate
across *different engine types*, not just N runs of one model.

Deterministic tests use `code` nodes and the in-process `stub` specialist.
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


def _make_op(rvbbit, return_type="text"):
    name = f"ht_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_arg_names => ARRAY['text'], op_return_type => %s)",
        (name, return_type),
    )
    return name


def _drop(rvbbit, name):
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def _code_node(name, fn):
    return {
        "name": name,
        "kind": "code",
        "fn": fn,
        "inputs": {"text": "{{ inputs.text }}"},
    }


def _last_receipt(rvbbit, op):
    return rvbbit.execute(
        "SELECT output, jsonb_array_length(sub_calls), sub_calls "
        "FROM rvbbit.receipts WHERE operator = %s "
        "ORDER BY invocation_at DESC LIMIT 1",
        (op,),
    ).fetchone()


def _set_takes(rvbbit, op, cfg):
    rvbbit.execute(
        "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)", (op, json.dumps(cfg))
    )


# ---- catalog ------------------------------------------------------------


def test_set_operator_takes_accepts_nodes(rvbbit):
    """A takes config keyed by `nodes` (no `factor`) is accepted."""
    op = _make_op(rvbbit)
    try:
        _set_takes(
            rvbbit,
            op,
            {"nodes": [_code_node("a", "trim")], "reduce": "first_valid"},
        )
        kind = rvbbit.execute(
            "SELECT takes->'nodes'->0->>'kind' FROM rvbbit.operators WHERE name = %s",
            (op,),
        ).fetchone()
        assert kind[0] == "code"
    finally:
        _drop(rvbbit, op)


# ---- heterogeneous ensembles --------------------------------------------


def test_heterogeneous_takes_code_nodes(rvbbit):
    """Two distinct code nodes as takes; first_valid picks node 0."""
    op = _make_op(rvbbit)
    try:
        _set_takes(
            rvbbit,
            op,
            {
                "nodes": [_code_node("t", "trim"), _code_node("l", "lowercase")],
                "reduce": "first_valid",
            },
        )
        result = rvbbit.execute(f"SELECT rvbbit.{op}('  HeLLo  ')").fetchone()[0]
        assert result == "HeLLo"  # first_valid -> node 0 (trim)
        _, n_calls, _ = _last_receipt(rvbbit, op)
        assert n_calls == 2  # both heterogeneous takes are audited
    finally:
        _drop(rvbbit, op)


def test_heterogeneous_takes_vote(rvbbit):
    """Majority vote across heterogeneous takes."""
    op = _make_op(rvbbit)
    try:
        _set_takes(
            rvbbit,
            op,
            {
                "nodes": [
                    _code_node("t1", "trim"),
                    _code_node("t2", "trim"),
                    _code_node("l", "lowercase"),
                ],
                "reduce": "vote",
            },
        )
        # trim,trim -> 'hello' (x2); lowercase -> '  hello  ' (x1)
        result = rvbbit.execute(f"SELECT rvbbit.{op}('  hello  ')").fetchone()[0]
        assert result == "hello"  # the majority output wins
        _, n_calls, _ = _last_receipt(rvbbit, op)
        assert n_calls == 3
    finally:
        _drop(rvbbit, op)


def test_heterogeneous_takes_mixed_kinds(rvbbit):
    """An ensemble mixing node kinds — a code node and a specialist node."""
    spec = _register_stub(rvbbit)
    op = _make_op(rvbbit)
    try:
        _set_takes(
            rvbbit,
            op,
            {
                "nodes": [
                    _code_node("c", "trim"),
                    {
                        "name": "s",
                        "kind": "specialist",
                        "specialist": spec,
                        "inputs": {"text": "{{ inputs.text }}"},
                    },
                ],
                "reduce": "first_valid",
            },
        )
        result = rvbbit.execute(f"SELECT rvbbit.{op}('  mixed  ')").fetchone()[0]
        assert result == "mixed"  # node 0 (code/trim) wins first_valid
        _, n_calls, sub = _last_receipt(rvbbit, op)
        assert n_calls == 2
        assert [s["kind"] for s in sub] == ["code", "specialist"]
    finally:
        _drop(rvbbit, op)
        rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{spec}'")
