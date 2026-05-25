"""Takes — run an operator N times and reduce to one answer (Loop 18).

reduce is 'vote' (majority), 'first_valid' (first to pass the filter), or
'evaluator' (an LLM judge). Deterministic tests use code-step operators:
every take is identical, so they verify the ensemble *ran* (N audited
sub-calls) and that the reducers/filter-fallback don't break. The live
tests exercise a real model pool.
"""

import json
import os
import uuid

import pytest

LIVE = os.environ.get("RUN_LLM_TESTS") == "1"


def _code_op(rvbbit, fn="trim"):
    name = f"tk_{uuid.uuid4().hex[:8]}"
    step = {"name": "c", "kind": "code", "fn": fn,
            "inputs": {"text": "{{ inputs.text }}"}}
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', op_steps => %s::jsonb)",
        (name, json.dumps([step])),
    )
    return name


def _drop(rvbbit, name, n_args=1):
    sig = ", ".join(["text"] * n_args + ["jsonb"])
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}({sig})")


def _last_receipt(rvbbit, op):
    return rvbbit.execute(
        "SELECT output, jsonb_array_length(sub_calls) "
        "FROM rvbbit.receipts WHERE operator = %s "
        "ORDER BY invocation_at DESC LIMIT 1",
        (op,),
    ).fetchone()


def test_operators_has_takes_column(rvbbit):
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'operators'"
        ).fetchall()
    }
    assert "takes" in cols


def test_set_operator_takes_rejects_missing_factor(rvbbit):
    name = _code_op(rvbbit)
    try:
        with pytest.raises(Exception):
            rvbbit.execute(
                "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
                (name, json.dumps({"reduce": "vote"})),
            )
    finally:
        _drop(rvbbit, name)


def test_takes_vote_runs_full_ensemble(rvbbit):
    """factor=3 vote — three audited attempts collapse to one answer."""
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (name, json.dumps({"factor": 3, "reduce": "vote"})),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ')").fetchone()
        assert row[0] == "hi"
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 3  # all three takes audited in the receipt
    finally:
        _drop(rvbbit, name)


def test_takes_first_valid(rvbbit):
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (name, json.dumps({"factor": 3, "reduce": "first_valid"})),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ')").fetchone()
        assert row[0] == "hi"
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 3
    finally:
        _drop(rvbbit, name)


def test_takes_factor_one_short_circuits(rvbbit):
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (name, json.dumps({"factor": 1, "reduce": "vote"})),
        )
        rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ')")
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 1
    finally:
        _drop(rvbbit, name)


def test_takes_filter_fallback_keeps_all(rvbbit):
    """A filter that would drop every take falls back to keeping them all —
    better a flagged answer than none."""
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {"factor": 3, "reduce": "vote", "filter": {"sql": "false"}}
                ),
            ),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ')").fetchone()
        assert row[0] == "hi"  # filter dropped all -> fell back -> still answered
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 3
    finally:
        _drop(rvbbit, name)


def test_takes_applies_on_warm_path(rvbbit):
    """Takes operators warm via the sequential leader path, not the batched
    one — prewarm_operator must still produce the full audited ensemble."""
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (name, json.dumps({"factor": 3, "reduce": "vote"})),
        )
        rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (name, "SELECT 'hi'::text"),
        )
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 3
    finally:
        _drop(rvbbit, name)


# ---- live takes ----------------------------------------------------------


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_live_takes_vote(rvbbit):
    """A real LLM operator run 3x and reduced by majority vote."""
    name = f"tklive_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => %s, op_user => %s)",
            (
                name,
                "Answer with ONLY one lowercase word: yes or no.",
                "Is this text expressing a positive sentiment? {{ text }}",
            ),
        )
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (name, json.dumps({"factor": 3, "reduce": "vote"})),
        )
        marker = uuid.uuid4().hex
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}(%s)",
            (f"(ref {marker}) I absolutely love this, best day ever!",),
        ).fetchone()
        assert "yes" in row[0].lower()
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls >= 3  # three takes ran
    finally:
        _drop(rvbbit, name)


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_live_takes_evaluator(rvbbit):
    """An LLM evaluator picks the best of N takes."""
    name = f"tkeval_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => %s, op_user => %s)",
            (
                name,
                "Write a short, punchy one-line summary of the text.",
                "{{ text }}",
            ),
        )
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "factor": 2,
                        "reduce": "evaluator",
                        "evaluator": {
                            "instructions": "Pick the clearest, most concise summary."
                        },
                    }
                ),
            ),
        )
        marker = uuid.uuid4().hex
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}(%s)",
            (
                f"(ref {marker}) The quarterly report shows revenue up 12 percent, "
                "driven mostly by strong international sales.",
            ),
        ).fetchone()
        assert row[0] and len(row[0]) > 5
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls >= 3  # 2 takes + 1 evaluator call, all audited
    finally:
        _drop(rvbbit, name)
