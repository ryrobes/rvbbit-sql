"""Combined semantic flow — pre-ward + takes + retry + post-ward together.

One operator stacks every Loop 16-18 feature. Live (RUN_LLM_TESTS=1): the
base prompt deliberately yields prose, so the flow must run takes, fail the
numeric retry validator, re-run takes with feedback, and finally pass the
post-ward — exercising all four stages in one call.
"""

import json
import os
import re
import uuid

import pytest

LIVE = os.environ.get("RUN_LLM_TESTS") == "1"


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_full_flow_pre_ward_takes_retry_post_ward(rvbbit):
    name = f"flow_{uuid.uuid4().hex[:8]}"
    try:
        # Base prompt intentionally produces a prose sentence, not a number.
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => %s, op_user => %s)",
            (
                name,
                "Describe in one English sentence how positive the text's tone is.",
                "Text: {{ text }}",
            ),
        )
        # pre-ward: reject empty input.
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "pre": [
                            {
                                "validator": {
                                    "sql": "length(btrim($inputs->>'text')) > 0"
                                },
                                "mode": "blocking",
                            }
                        ],
                        "post": [
                            {
                                "validator": {"sql": "length(btrim($output)) <= 3"},
                                "mode": "blocking",
                            }
                        ],
                    }
                ),
            ),
        )
        # takes: 3 attempts, majority vote.
        rvbbit.execute(
            "SELECT rvbbit.set_operator_takes(%s, %s::jsonb)",
            (name, json.dumps({"factor": 3, "reduce": "vote"})),
        )
        # retry: force a bare integer.
        rvbbit.execute(
            "SELECT rvbbit.set_operator_retry(%s, %s::jsonb)",
            (
                name,
                json.dumps(
                    {
                        "until": {"sql": "btrim($output) ~ '^[0-9]+$'"},
                        "max_attempts": 3,
                        "instructions": "Return ONLY a single integer from 0 to 10 "
                        "— no words, no sentence, no punctuation.",
                    }
                ),
            ),
        )

        # pre-ward blocks empty input before anything runs.
        blocked = rvbbit.execute(f"SELECT rvbbit.{name}('')").fetchone()
        assert blocked[0] == ""
        err = rvbbit.execute(
            "SELECT error FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        assert "pre-ward" in (err[0] or "")

        # Full flow: pre-ward passes -> takes x3 (prose) -> retry validator
        # fails -> takes x3 with feedback (numbers) -> post-ward passes.
        marker = uuid.uuid4().hex
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}(%s)",
            (f"(ref {marker}) This is wonderful, I could not be happier!",),
        ).fetchone()
        assert re.match(r"^\d+$", row[0].strip()), f"flow output not numeric: {row[0]!r}"

        n_calls = rvbbit.execute(
            "SELECT jsonb_array_length(sub_calls) FROM rvbbit.receipts "
            "WHERE operator = %s ORDER BY invocation_at DESC LIMIT 1",
            (name,),
        ).fetchone()[0]
        # takes ran at least once (3); the prose base prompt forced a retry,
        # so in practice 6 — assert a safe lower bound.
        assert n_calls >= 4, f"expected takes+retry audit, got {n_calls} sub-calls"
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")
