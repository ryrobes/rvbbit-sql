"""Operator-shape catalog tests.

The shape column distinguishes scalar (per-row), aggregate (one call per
group), and dimension (collection -> per-input assignment). Today only
scalar executes; aggregate/dimension are catalog-level only.
These tests guard the shape contract.
"""

import uuid

import pytest


def test_shape_column_present(rvbbit):
    cols = {
        r[0]
        for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'operators'"
        ).fetchall()
    }
    assert "shape" in cols
    assert "tests" in cols


def test_builtins_all_scalar(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, shape FROM rvbbit.operators "
        "WHERE name IN ('means', 'about', 'summarize') ORDER BY name"
    ).fetchall()
    for name, shape in rows:
        assert shape == "scalar", f"{name} has shape={shape}, expected scalar"


def test_classify_collection_aggregate_in_catalog(rvbbit):
    row = rvbbit.execute(
        "SELECT shape, return_type FROM rvbbit.operators WHERE name = 'classify_collection'"
    ).fetchone()
    assert row is not None
    assert row[0] == "aggregate"
    assert row[1] == "text"


def test_shape_check_constraint(rvbbit):
    """Only scalar/aggregate/dimension allowed."""
    with pytest.raises(Exception):
        rvbbit.execute(
            "INSERT INTO rvbbit.operators "
            "(name, shape, arg_names, arg_types, return_type, model, "
            " system_prompt, user_prompt, parser) "
            "VALUES ('bad_shape', 'invalid_shape', ARRAY['x'], ARRAY['text'], 'text', "
            "        'm', 'sys', 'usr', 'strip')"
        )


def test_infix_symbol_only_with_scalar(rvbbit):
    """The catalog CHECK forbids infix on aggregate/dimension shapes."""
    with pytest.raises(Exception):
        rvbbit.execute(
            "INSERT INTO rvbbit.operators "
            "(name, shape, arg_names, arg_types, return_type, model, "
            " system_prompt, user_prompt, parser, infix_symbol) "
            "VALUES ('bad_infix', 'aggregate', ARRAY['x','y'], ARRAY['text','text'], "
            "        'text', 'm', 'sys', 'usr', 'strip', '@~')"
        )


def test_create_aggregate_via_helper_registers_aggregate(rvbbit):
    """create_operator with shape='aggregate' inserts the catalog row
    AND registers a real PG aggregate (RYR-285 made this work)."""
    name = f"test_agg_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'aggregate', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => 'sys', op_user => '{{ text }}', "
            "  op_steps => '[{\"name\":\"x\",\"kind\":\"code\",\"fn\":\"uppercase\","
            "                 \"inputs\":{\"text\":\"{{ inputs.collection }}\"}}]'::jsonb)",
            (name,),
        )
        # Catalog row exists with shape=aggregate
        row = rvbbit.execute(
            "SELECT shape FROM rvbbit.operators WHERE name = %s", (name,)
        ).fetchone()
        assert row[0] == "aggregate"
        # And a PG AGGREGATE is now registered (no-opts + with-opts variants).
        row = rvbbit.execute(
            "SELECT count(*) FROM pg_aggregate "
            f"WHERE aggfnoid::regprocedure::text LIKE 'rvbbit.{name}%'"
        ).fetchone()
        assert row[0] >= 1
    finally:
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{name}(text)")
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{name}(text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{name}_sfunc(jsonb, text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{name}_sfunc_no_opts(jsonb, text)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{name}_ffunc(jsonb)")
        rvbbit.execute("DELETE FROM rvbbit.operators WHERE name = %s", (name,))


# ---- Embedded tests + runner -------------------------------------------------


def test_builtins_have_tests(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, jsonb_array_length(tests) FROM rvbbit.operators "
        "WHERE name IN ('means', 'about', 'summarize') "
        "  AND tests IS NOT NULL ORDER BY name"
    ).fetchall()
    by_name = {r[0]: r[1] for r in rows}
    assert by_name.get("means", 0) >= 2
    assert by_name.get("about", 0) >= 2
    assert by_name.get("summarize", 0) >= 1


def test_run_tests_function_exists(rvbbit):
    """The runner is a stable API surface — assert its signature."""
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_proc "
        "WHERE pronamespace = 'rvbbit'::regnamespace "
        "  AND proname IN ('run_tests', 'run_all_tests')"
    ).fetchone()
    assert row[0] == 2


def test_run_tests_with_stub_operator(rvbbit):
    """Validates the test runner end-to-end using a stub operator whose
    test cases call only built-in SQL (no LLM). We register an operator
    whose tests assert against constants, then run them and check that
    each expect.type works."""
    name = f"test_runner_op_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_shape => 'scalar', "
            "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "  op_system => 'sys', op_user => '{{ text }}', "
            "  op_tests => %s::jsonb)",
            (
                name,
                """[
                    {"name": "exact_pass", "sql": "SELECT 'hello'",
                     "expect": {"type": "exact", "value": "hello"}},
                    {"name": "exact_fail", "sql": "SELECT 'goodbye'",
                     "expect": {"type": "exact", "value": "hello"}},
                    {"name": "contains_pass", "sql": "SELECT 'hello world'",
                     "expect": {"type": "contains", "value": "world"}},
                    {"name": "regex_pass", "sql": "SELECT 'abc123'",
                     "expect": {"type": "regex", "pattern": "^[a-z]+[0-9]+$"}},
                    {"name": "min_pass", "sql": "SELECT 0.85::text",
                     "expect": {"type": "min", "value": "0.5"}},
                    {"name": "max_fail", "sql": "SELECT 0.95::text",
                     "expect": {"type": "max", "value": "0.5"}},
                    {"name": "not_empty_pass", "sql": "SELECT 'x'",
                     "expect": {"type": "not_empty"}},
                    {"name": "sql_error", "sql": "SELECT (1/0)::text",
                     "expect": {"type": "exact", "value": "anything"}}
                ]""",
            ),
        )
        rows = rvbbit.execute(
            f"SELECT test_name, passed, actual, error FROM rvbbit.run_tests('{name}') ORDER BY test_name"
        ).fetchall()
        result = {r[0]: (r[1], r[2], r[3]) for r in rows}
        # Pass/fail per expect.type:
        assert result["exact_pass"][0] is True
        assert result["exact_fail"][0] is False
        assert result["contains_pass"][0] is True
        assert result["regex_pass"][0] is True
        assert result["min_pass"][0] is True
        assert result["max_fail"][0] is False
        assert result["not_empty_pass"][0] is True
        # SQL errors are captured, not raised:
        assert result["sql_error"][0] is False
        assert result["sql_error"][2] is not None  # error string set
    finally:
        rvbbit.execute("DELETE FROM rvbbit.operators WHERE name = %s", (name,))
