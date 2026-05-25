"""Multi-step operators (steps JSONB), sub_calls audit, query_id grouping,
provider trait abstraction, and the code-step registry.

Deterministic tests: catalog schema, step validation, code-only operators
(no LLM). Live LLM behavior of multi-step operators lives in
test_operators_live.py.
"""

import uuid

import pytest


# ---- Catalog schema ------------------------------------------------------


def test_operators_has_steps_column(rvbbit):
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'operators'"
        ).fetchall()
    }
    assert "steps" in cols


def test_receipts_has_subcalls_and_query_id(rvbbit):
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'receipts'"
        ).fetchall()
    }
    assert "sub_calls" in cols
    assert "query_id" in cols


def test_backends_catalog_exists(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_tables "
        "WHERE schemaname = 'rvbbit' AND tablename = 'backends'"
    ).fetchone()
    assert row[0] == 1


def test_specialists_transport_check(rvbbit):
    """Only the known transports are allowed."""
    with pytest.raises(Exception):
        rvbbit.execute(
            "INSERT INTO rvbbit.backends "
            "(name, transport, endpoint_url) "
            "VALUES ('bad', 'invalid_transport', 'http://x')"
        )


def test_register_specialist_helper(rvbbit):
    """register_backend is an UPSERT — calling twice updates in place."""
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => 'reg_probe', "
        "  backend_endpoint => 'http://probe:8080/predict', "
        "  backend_batch_size => 16)"
    )
    row = rvbbit.execute(
        "SELECT transport, batch_size FROM rvbbit.backends WHERE name = 'reg_probe'"
    ).fetchone()
    try:
        assert row[0] == "rvbbit"
        assert row[1] == 16

        rvbbit.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => 'reg_probe', "
            "  backend_endpoint => 'http://probe:8080/predict', "
            "  backend_transport => 'gradio', "
            "  backend_batch_size => 64)"
        )
        row2 = rvbbit.execute(
            "SELECT transport, batch_size FROM rvbbit.backends WHERE name = 'reg_probe'"
        ).fetchone()
        assert row2[0] == "gradio"
        assert row2[1] == 64
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = 'reg_probe'")


# ---- safe_classify built-in: schema only ---------------------------------


def test_safe_classify_seeded_with_steps(rvbbit):
    row = rvbbit.execute(
        "SELECT shape, return_type, jsonb_array_length(steps) "
        "FROM rvbbit.operators WHERE name = 'safe_classify'"
    ).fetchone()
    assert row is not None
    assert row[0] == "scalar"
    assert row[1] == "text"
    assert row[2] == 2  # llm + code steps


def test_safe_classify_step_kinds(rvbbit):
    row = rvbbit.execute(
        "SELECT steps->0->>'kind', steps->1->>'kind' "
        "FROM rvbbit.operators WHERE name = 'safe_classify'"
    ).fetchone()
    assert row == ("llm", "code")


def test_safe_classify_wrapper_exists(rvbbit):
    """create_operator generated the typed SQL wrapper even for multi-step."""
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_proc "
        "WHERE pronamespace = 'rvbbit'::regnamespace "
        "  AND proname = 'safe_classify'"
    ).fetchone()
    assert row[0] >= 1  # 2-arg or 3-arg variants


# ---- Code-only operator (no LLM, deterministic end-to-end) ---------------


@pytest.fixture
def code_only_op(rvbbit):
    """Defines a temporary operator whose only step is a code call.
    Runs end-to-end with NO LLM dependency — pure executor + template +
    code-step plumbing test."""
    name = f"trim_op_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (
            name,
            """[
                {"name": "clean", "kind": "code", "fn": "trim",
                 "inputs": {"text": "{{ inputs.text }}"}}
            ]""",
        ),
    )
    yield name
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_code_only_operator_runs_end_to_end(rvbbit, code_only_op):
    row = rvbbit.execute(
        f"SELECT rvbbit.{code_only_op}('   hello world   ')"
    ).fetchone()
    assert row[0] == "hello world"


def test_code_only_operator_logs_receipt(rvbbit, code_only_op):
    rvbbit.execute(f"SELECT rvbbit.{code_only_op}('  fresh-{uuid.uuid4().hex} ')")
    row = rvbbit.execute(
        f"SELECT n_tokens_in, n_tokens_out, sub_calls "
        f"FROM rvbbit.receipts WHERE operator = '{code_only_op}' "
        f"ORDER BY invocation_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    tokens_in, tokens_out, sub_calls = row
    assert tokens_in == 0  # code steps don't consume tokens
    assert tokens_out == 0
    assert sub_calls is not None
    assert len(sub_calls) == 1
    assert sub_calls[0]["kind"] == "code"
    assert sub_calls[0]["model"] == "trim"  # we stash code fn name in model col
    # error is serialized with skip-if-none, so absent == success
    assert sub_calls[0].get("error") is None


def test_query_id_present_on_receipts(rvbbit, code_only_op):
    rvbbit.execute(f"SELECT rvbbit.{code_only_op}(' unique-{uuid.uuid4().hex} ')")
    row = rvbbit.execute(
        f"SELECT query_id FROM rvbbit.receipts WHERE operator = '{code_only_op}' "
        f"ORDER BY invocation_at DESC LIMIT 1"
    ).fetchone()
    assert row[0] is not None  # query_id is a uuid


# ---- Multi-step code-only chaining (tests templating across steps) -------


def test_chained_code_steps(rvbbit):
    """Two code steps where step 2 references step 1's output via
    {{ steps.X.output }}. Validates the inter-step templating contract."""
    name = f"chained_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => 'unused', op_user => 'unused', "
            "  op_steps => %s::jsonb)",
            (
                name,
                """[
                    {"name": "step1", "kind": "code", "fn": "trim",
                     "inputs": {"text": "{{ inputs.text }}"}},
                    {"name": "step2", "kind": "code", "fn": "uppercase",
                     "inputs": {"text": "{{ steps.step1.output }}"}}
                ]""",
            ),
        )
        row = rvbbit.execute(f"SELECT rvbbit.{name}('   hello   ')").fetchone()
        assert row[0] == "HELLO"
        # Receipt should show TWO sub-calls.
        row = rvbbit.execute(
            f"SELECT jsonb_array_length(sub_calls) FROM rvbbit.receipts "
            f"WHERE operator = '{name}' ORDER BY invocation_at DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 2
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_validate_one_of_with_comma_string(rvbbit):
    """code_steps::validate_one_of accepts comma-separated 'allowed'."""
    name = f"validator_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text', 'choices'], op_return_type => 'text', "
            "  op_system => 'unused', op_user => 'unused', "
            "  op_steps => %s::jsonb)",
            (
                name,
                """[
                    {"name": "v", "kind": "code", "fn": "validate_one_of",
                     "inputs": {"value": "{{ inputs.text }}",
                                "allowed": "{{ inputs.choices }}",
                                "default": "unknown"}}
                ]""",
            ),
        )
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}('apple', 'apple,banana,cherry')"
        ).fetchone()
        assert row[0] == "apple"
        # Case-insensitive match, canonical casing returned
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}('APPLE', 'apple,banana,cherry')"
        ).fetchone()
        assert row[0] == "apple"
        # No match -> default
        row = rvbbit.execute(
            f"SELECT rvbbit.{name}('durian', 'apple,banana,cherry')"
        ).fetchone()
        assert row[0] == "unknown"
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, text, jsonb)")


def test_specialist_step_errors_cleanly(rvbbit):
    """Step kind='specialist' is accepted in catalog but executor errors
    clearly (no working specialist sidecar this session)."""
    name = f"specialist_stub_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => 'unused', op_user => 'unused', "
            "  op_steps => %s::jsonb)",
            (
                name,
                """[
                    {"name": "e", "kind": "specialist", "specialist": "bge-m3",
                     "inputs": {"text": "{{ inputs.text }}"}}
                ]""",
            ),
        )
        # Call should return empty (default for text) and log error receipt.
        row = rvbbit.execute(f"SELECT rvbbit.{name}('hi')").fetchone()
        assert row[0] == ""
        row = rvbbit.execute(
            f"SELECT error FROM rvbbit.receipts WHERE operator = '{name}' "
            f"ORDER BY invocation_at DESC LIMIT 1"
        ).fetchone()
        assert row[0] is not None
        assert "specialist" in row[0].lower()
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_unknown_code_fn_errors_cleanly(rvbbit):
    """Referencing a non-existent code fn errors via receipts, not panic."""
    name = f"bad_code_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => 'unused', op_user => 'unused', "
            "  op_steps => %s::jsonb)",
            (
                name,
                """[
                    {"name": "bad", "kind": "code", "fn": "definitely_not_a_real_fn",
                     "inputs": {"text": "{{ inputs.text }}"}}
                ]""",
            ),
        )
        rvbbit.execute(f"SELECT rvbbit.{name}('test')")
        row = rvbbit.execute(
            f"SELECT error FROM rvbbit.receipts WHERE operator = '{name}' "
            f"ORDER BY invocation_at DESC LIMIT 1"
        ).fetchone()
        assert row[0] is not None
        assert "definitely_not_a_real_fn" in row[0]
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")
