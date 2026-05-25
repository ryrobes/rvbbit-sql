"""Wards — pre/post validator gates (Loop 17).

A ward places a validator before (pre) or after (post) an operator runs.
mode 'blocking' fails the call on a failed check; 'advisory' just warns.
Deterministic tests use code-step operators — no LLM.
"""

import json
import uuid


def _code_op(rvbbit, fn="trim"):
    name = f"wd_{uuid.uuid4().hex[:8]}"
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


def _drop(rvbbit, name):
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def _last_error(rvbbit, op):
    row = rvbbit.execute(
        "SELECT error FROM rvbbit.receipts WHERE operator = %s "
        "ORDER BY invocation_at DESC LIMIT 1",
        (op,),
    ).fetchone()
    return row[0] if row else None


def test_operators_has_wards_column(rvbbit):
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'operators'"
        ).fetchall()
    }
    assert "wards" in cols


def test_set_operator_wards_roundtrip(rvbbit):
    name = _code_op(rvbbit)
    try:
        cfg = {"post": [{"validator": {"sql": "$output <> ''"}, "mode": "advisory"}]}
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)", (name, json.dumps(cfg))
        )
        row = rvbbit.execute(
            "SELECT wards FROM rvbbit.operators WHERE name = %s", (name,)
        ).fetchone()
        assert row[0]["post"][0]["mode"] == "advisory"
        rvbbit.execute("SELECT rvbbit.set_operator_wards(%s, NULL)", (name,))
        row = rvbbit.execute(
            "SELECT wards FROM rvbbit.operators WHERE name = %s", (name,)
        ).fetchone()
        assert row[0] is None
    finally:
        _drop(rvbbit, name)


def test_pre_ward_blocking_rejects_input(rvbbit):
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "pre": [
                            {
                                "validator": {
                                    "sql": "length(btrim($inputs->>'text')) >= 3"
                                },
                                "mode": "blocking",
                            }
                        ]
                    }
                ),
            ),
        )
        # 'ab' is too short -> blocked -> empty result + pre-ward error.
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  ab  ')").fetchone()
        assert row[0] == ""
        assert "pre-ward" in (_last_error(rvbbit, name) or "")
        # 'abcd' passes the gate and runs.
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  abcd  ')").fetchone()
        assert row[0] == "abcd"
    finally:
        _drop(rvbbit, name)


def test_pre_ward_advisory_allows_input(rvbbit):
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "pre": [
                            {
                                "validator": {
                                    "sql": "length(btrim($inputs->>'text')) >= 3"
                                },
                                "mode": "advisory",
                            }
                        ]
                    }
                ),
            ),
        )
        # 'ab' fails the check but advisory mode lets it run anyway.
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  ab  ')").fetchone()
        assert row[0] == "ab"
        assert _last_error(rvbbit, name) is None
    finally:
        _drop(rvbbit, name)


def test_post_ward_blocking_rejects_output(rvbbit):
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "post": [
                            {
                                "validator": {"sql": "$output <> 'BADVAL'"},
                                "mode": "blocking",
                            }
                        ]
                    }
                ),
            ),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  BADVAL  ')").fetchone()
        assert row[0] == ""
        assert "post-ward" in (_last_error(rvbbit, name) or "")
        # A non-BADVAL output passes the gate.
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  GOODVAL  ')").fetchone()
        assert row[0] == "GOODVAL"
    finally:
        _drop(rvbbit, name)


def test_post_ward_advisory_keeps_output(rvbbit):
    name = _code_op(rvbbit)
    try:
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "post": [
                            {
                                "validator": {"sql": "$output <> 'BADVAL'"},
                                "mode": "advisory",
                            }
                        ]
                    }
                ),
            ),
        )
        # advisory: the output fails the check but is still returned.
        row = rvbbit.execute(f"SELECT rvbbit.{name}('  BADVAL  ')").fetchone()
        assert row[0] == "BADVAL"
        assert _last_error(rvbbit, name) is None
    finally:
        _drop(rvbbit, name)
