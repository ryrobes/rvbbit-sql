"""Operator catalog + DDL helper tests.

These DO NOT make live LLM calls — they verify:
  - The 3 built-in operators are seeded
  - Wrapper functions (typed) are generated
  - CREATE OPERATOR ran for infix-symbol operators
  - rvbbit.create_operator creates new operators on the fly

Live-LLM tests live in test_operators_live.py (opt-in via
RUN_LLM_TESTS=1 env var) since each call costs money and is slow.
"""

import uuid

import pytest


# ---- Built-ins -------------------------------------------------------------


def test_builtins_seeded(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, return_type, infix_symbol, infix_word "
        "FROM rvbbit.operators ORDER BY name"
    ).fetchall()
    by_name = {r[0]: r for r in rows}
    for name in ("means", "about", "summarize"):
        assert name in by_name, f"built-in {name} missing"
    assert by_name["means"][1] == "bool"
    assert by_name["about"][1] == "float8"
    assert by_name["summarize"][1] == "text"
    assert by_name["means"][2] == "~~?"
    assert by_name["about"][2] == "~~%"
    # summarize is unary -> no infix symbol
    assert by_name["summarize"][2] is None


def test_wrapper_functions_generated(rvbbit):
    rows = rvbbit.execute(
        "SELECT proname, pronargs FROM pg_proc "
        "WHERE pronamespace = 'rvbbit'::regnamespace "
        "  AND proname IN ('means', 'about', 'summarize', '_op_means', '_op_about') "
        "ORDER BY proname, pronargs"
    ).fetchall()
    arities = {(name, n) for (name, n) in rows}
    # Public wrappers: always include opts. n_args + 1.
    assert ("means", 3) in arities
    assert ("about", 3) in arities
    assert ("summarize", 2) in arities
    # Internal 2-arg variants for binary ops — bound to the infix operator.
    assert ("_op_means", 2) in arities
    assert ("_op_about", 2) in arities


def test_infix_operators_registered(rvbbit):
    rows = rvbbit.execute(
        "SELECT op.oprname, l.typname, r.typname, res.typname "
        "FROM pg_operator op "
        "JOIN pg_type l ON op.oprleft = l.oid "
        "JOIN pg_type r ON op.oprright = r.oid "
        "JOIN pg_type res ON op.oprresult = res.oid "
        "WHERE op.oprnamespace = 'rvbbit'::regnamespace "
        "ORDER BY op.oprname"
    ).fetchall()
    by_op = {r[0]: r for r in rows}
    assert "~~?" in by_op
    assert by_op["~~?"][1:] == ("text", "text", "bool")
    assert "~~%" in by_op
    assert by_op["~~%"][1:] == ("text", "text", "float8")


# ---- DDL helper: create / overwrite / edit prompt --------------------------


@pytest.fixture
def temp_operator(rvbbit):
    """Yields a unique operator name; cleans up afterwards."""
    name = f"test_op_{uuid.uuid4().hex[:8]}"
    yield name
    # Drop catalog row and the auto-generated wrapper functions.
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    for sig in [
        f"rvbbit.{name}(text)",
        f"rvbbit.{name}(text, jsonb)",
        f"rvbbit.{name}(text, text)",
        f"rvbbit.{name}(text, text, jsonb)",
        f"rvbbit._op_{name}(text, text)",
    ]:
        try:
            rvbbit.execute(f"DROP FUNCTION IF EXISTS {sig}")
        except Exception:
            pass


def test_create_operator_basic(rvbbit, temp_operator):
    rvbbit.execute(f"""
        SELECT rvbbit.create_operator(
            op_name => '{temp_operator}',
            op_arg_names => ARRAY['text'],
            op_return_type => 'text',
            op_system => 'sys',
            op_user => 'usr: {{{{ text }}}}'
        )
    """)
    # Catalog row exists with auto-defaulted parser:
    row = rvbbit.execute(
        f"SELECT return_type, parser FROM rvbbit.operators WHERE name = '{temp_operator}'"
    ).fetchone()
    assert row == ("text", "strip")
    # Wrapper functions exist.
    row = rvbbit.execute(
        f"SELECT count(*) FROM pg_proc "
        f"WHERE pronamespace = 'rvbbit'::regnamespace "
        f"  AND proname = '{temp_operator}'"
    ).fetchone()
    assert row[0] >= 1


def test_create_operator_with_infix_symbol(rvbbit, temp_operator):
    # Use a symbol unlikely to collide.
    rvbbit.execute(f"""
        SELECT rvbbit.create_operator(
            op_name => '{temp_operator}',
            op_arg_names => ARRAY['text', 'rhs'],
            op_return_type => 'bool',
            op_system => 'sys',
            op_user => 'a={{{{ text }}}} b={{{{ rhs }}}}',
            op_infix_symbol => '#?'
        )
    """)
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_operator op "
        "WHERE op.oprnamespace = 'rvbbit'::regnamespace "
        "  AND op.oprname = '#?'"
    ).fetchone()
    assert row[0] == 1
    # cleanup
    rvbbit.execute("DROP OPERATOR IF EXISTS rvbbit.#? (text, text)")


def test_update_operator_prompt(rvbbit):
    """A SQL UPDATE on rvbbit.operators is sufficient to change prompt
    behavior on the next call — no DDL/recompile needed."""
    # Snapshot the original.
    original = rvbbit.execute(
        "SELECT system_prompt FROM rvbbit.operators WHERE name = 'means'"
    ).fetchone()[0]
    try:
        rvbbit.execute(
            "UPDATE rvbbit.operators SET system_prompt = 'MARKER' WHERE name = 'means'"
        )
        row = rvbbit.execute(
            "SELECT system_prompt FROM rvbbit.operators WHERE name = 'means'"
        ).fetchone()
        assert row[0] == "MARKER"
        # updated_at must bump too.
        row = rvbbit.execute(
            "SELECT (updated_at > created_at) FROM rvbbit.operators WHERE name = 'means'"
        ).fetchone()
        assert row[0] is True
    finally:
        rvbbit.execute(
            "UPDATE rvbbit.operators SET system_prompt = %s WHERE name = 'means'",
            (original,),
        )


# ---- Receipts table validation --------------------------------------------


def test_receipts_table_has_expected_columns(rvbbit):
    """The receipts table needs all the columns the executor logs."""
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'receipts'"
        ).fetchall()
    }
    expected = {
        "receipt_id", "operator", "inputs_hash", "model", "inputs",
        "output", "n_tokens_in", "n_tokens_out", "latency_ms", "error",
        "invocation_at",
    }
    missing = expected - cols
    assert not missing, f"receipts columns missing: {missing}"
