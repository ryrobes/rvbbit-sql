"""Validators + retry — Loop 16.

A retry plan loops a semantic operator until its output passes a validator:
a SQL boolean expression (``$output`` / ``$inputs`` bound) or a Postgres
function. rvbbit lives inside Postgres, so SQL itself is the validator
language — no sandbox.

Deterministic tests use code-step operators (``trim`` etc.) so the retry
loop, validator evaluation, and audit trail are exercised with zero LLM
dependency. The live test (RUN_LLM_TESTS=1) drives a real retry-fixes-it
loop against a model.
"""

import json
import os
import re
import uuid

import pytest

LIVE = os.environ.get("RUN_LLM_TESTS") == "1"


# ---- helpers -------------------------------------------------------------


def _make_code_op(rvbbit, fn="trim", arg_names=("text",)):
    """Create a deterministic code-step operator; returns its name.

    The single step runs the named code fn over the operator's first arg,
    so output == fn(first arg) with no model call.
    """
    name = f"vr_{uuid.uuid4().hex[:8]}"
    args_sql = "ARRAY[" + ",".join(f"'{a}'" for a in arg_names) + "]"
    step = {
        "name": "c",
        "kind": "code",
        "fn": fn,
        "inputs": {"text": f"{{{{ inputs.{arg_names[0]} }}}}"},
    }
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        f"  op_arg_names => {args_sql}, op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (name, json.dumps([step])),
    )
    return name


def _drop_op(rvbbit, name, n_args=1):
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


# ---- catalog schema ------------------------------------------------------


def test_operators_has_retry_column(rvbbit):
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'operators'"
        ).fetchall()
    }
    assert "retry" in cols


def test_set_operator_retry_roundtrip(rvbbit):
    name = _make_code_op(rvbbit)
    try:
        cfg = {"until": {"sql": "$output <> ''"}, "max_attempts": 2}
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)", (name, json.dumps(cfg))
        )
        row = rvbbit.execute(
            "SELECT retry FROM rvbbit.operators WHERE name = %s", (name,)
        ).fetchone()
        assert row[0]["max_attempts"] == 2
        # NULL clears the plan.
        rvbbit.execute("SELECT rvbbit.set_operator_retry(%s, NULL)", (name,))
        row = rvbbit.execute(
            "SELECT retry FROM rvbbit.operators WHERE name = %s", (name,)
        ).fetchone()
        assert row[0] is None
    finally:
        _drop_op(rvbbit, name)


def test_set_operator_retry_rejects_missing_until(rvbbit):
    name = _make_code_op(rvbbit)
    try:
        with pytest.raises(Exception):
            rvbbit.execute(
                "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
                (name, json.dumps({"max_attempts": 3})),
            )
    finally:
        _drop_op(rvbbit, name)


def test_set_operator_retry_unknown_operator(rvbbit):
    with pytest.raises(Exception):
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (f"nope_{uuid.uuid4().hex}", json.dumps({"until": {"sql": "true"}})),
        )


# ---- retry loop (deterministic, code-step operators) ---------------------


def test_retry_exhausts_when_validator_never_passes(rvbbit):
    """A deterministic op whose output always fails the validator runs
    exactly max_attempts times, and every attempt is audited."""
    name = _make_code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {"until": {"sql": "$output <> 'BADVAL'"}, "max_attempts": 3}
                ),
            ),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  BADVAL  ')").fetchone()
        assert row[0] == "BADVAL"  # last attempt is still returned
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 3  # 1 initial + 2 retries, all in the audit
    finally:
        _drop_op(rvbbit, name)


def test_retry_stops_when_validator_passes(rvbbit):
    """When the first output is valid, no retry happens."""
    name = _make_code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {"until": {"sql": "$output <> 'BADVAL'"}, "max_attempts": 3}
                ),
            ),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  GOODVAL  ')").fetchone()
        assert row[0] == "GOODVAL"
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 1  # validator passed first try
    finally:
        _drop_op(rvbbit, name)


def test_retry_max_attempts_two(rvbbit):
    """max_attempts caps the total attempt count."""
    name = _make_code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (name, json.dumps({"until": {"sql": "false"}, "max_attempts": 2})),
        )
        rvbbit.execute(f"SELECT rvbbit.{name}('  whatever  ')")
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 2
    finally:
        _drop_op(rvbbit, name)


def test_validator_sees_inputs(rvbbit):
    """A SQL validator can reference $inputs — the operator's input jsonb."""
    name = _make_code_op(rvbbit, arg_names=("text", "expected"))
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "until": {"sql": "$output = ($inputs->>'expected')"},
                        "max_attempts": 2,
                    }
                ),
            ),
        )
        # output 'hi' == expected 'hi' -> valid, single attempt
        rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ', 'hi')")
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 1
        # output 'hi' != expected 'bye' -> retries to the cap
        rvbbit.execute(f"SELECT rvbbit.{name}('  hi  ', 'bye')")
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 2
    finally:
        _drop_op(rvbbit, name, n_args=2)


def test_function_validator(rvbbit):
    """A validator can be a Postgres function fn(output text, inputs jsonb)."""
    fn = f"vfn_{uuid.uuid4().hex[:8]}"
    name = _make_code_op(rvbbit)
    try:
        rvbbit.execute(
            f"CREATE FUNCTION rvbbit.{fn}(o text, i jsonb) RETURNS bool "
            f"LANGUAGE sql IMMUTABLE RETURN o <> 'BADVAL'"
        )
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {"until": {"function": f"rvbbit.{fn}"}, "max_attempts": 2}
                ),
            ),
        )
        rvbbit.execute(f"SELECT rvbbit.{name}('  BADVAL  ')")
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 2  # function rejected -> retried to the cap
    finally:
        _drop_op(rvbbit, name)
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{fn}(text, jsonb)")


def test_retry_applies_on_warm_path(rvbbit):
    """apply_retry is shared by the single-row path AND the batched warm
    path — prewarm_operator must produce the same audited retry."""
    name = _make_code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (name, json.dumps({"until": {"sql": "false"}, "max_attempts": 3})),
        )
        rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (name, "SELECT 'BADVAL'::text"),
        )
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls == 3  # warmed via the batched engine, still retried
    finally:
        _drop_op(rvbbit, name)


# ---- live retry (real LLM) ----------------------------------------------


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_live_retry_fixes_format(rvbbit):
    """An operator whose base prompt deliberately yields prose; a strict
    digits-only validator + retry instructions correct it within a few
    attempts."""
    name = f"digits_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => %s, op_user => %s)",
            (
                name,
                "You describe the number mentioned in the text as a "
                "complete English sentence.",
                "Text: {{ text }}",
            ),
        )
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "until": {"sql": "btrim($output) ~ '^[0-9]+$'"},
                        "max_attempts": 4,
                        "instructions": "Return ONLY the digit characters of "
                        "the number — no words, no punctuation, no sentence.",
                    }
                ),
            ),
        )
        marker = uuid.uuid4().hex  # cache-bust each run
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}(%s)",
            (f"(ref {marker}) The sasquatch was seen 42 times that winter.",),
        ).fetchone()
        assert re.match(r"^\d+$", row[0].strip()), f"retry failed to fix: {row[0]!r}"
        _, n_calls = _last_receipt(rvbbit, name)
        assert n_calls >= 2  # the loose base prompt forced at least one retry
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")
