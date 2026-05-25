"""Non-scalar operator shapes — RYR-285 (aggregate) + RYR-286 (dimension).

scalar shape is covered extensively elsewhere; these tests just verify
the new dimension + aggregate paths:
  - CREATE OPERATOR generates the right wrappers
  - dimension yields multiple rows per call
  - aggregate's SFUNC collects, FFUNC runs once per group
  - both write receipts
  - LATERAL composition works for dimension
"""
import json
import uuid

import pytest


# ---- Dimension --------------------------------------------------------------


def test_dimension_setof_wrapper_generated(rvbbit):
    op = f"dim_probe_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'dimension', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "uppercase",
                              "inputs": {"text": "{{ inputs.text }}"}}])),
        )
        # Verify wrapper exists and returns SETOF text
        row = rvbbit.execute(
            "SELECT pg_catalog.pg_get_function_result(oid) "
            "FROM pg_proc WHERE proname = %s AND pronamespace = 'rvbbit'::regnamespace",
            (op,),
        ).fetchone()
        assert row is not None
        assert "SETOF text" in row[0], f"expected SETOF text, got {row[0]!r}"
    finally:
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")


def test_dimension_newline_split(rvbbit):
    """A code op that uppercases — fed a multi-line input, the dim wrapper
    splits the result on newlines into multiple rows."""
    op = f"dim_lines_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'dimension', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "uppercase",
                              "inputs": {"text": "{{ inputs.text }}"}}])),
        )
        rows = rvbbit.execute(
            f"SELECT rvbbit.{op}(%s) AS v", ("line one\nline two\nline three",),
        ).fetchall()
        assert [r[0] for r in rows] == ["LINE ONE", "LINE TWO", "LINE THREE"]
    finally:
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")


def test_dimension_json_array_split(rvbbit):
    """If the final step's output is a JSON array string, each element
    becomes a row. Uses json_parse + identity through a synthetic op."""
    op = f"dim_json_{uuid.uuid4().hex[:8]}"
    try:
        # Use a code step that just passes through (uppercase keeps strings).
        # Feed it a JSON array string; the dim wrapper sees the output
        # starts with '[' and splits accordingly.
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'dimension', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "uppercase",
                              "inputs": {"text": "{{ inputs.text }}"}}])),
        )
        rows = rvbbit.execute(
            f'SELECT rvbbit.{op}(%s) AS v', ('["alpha","beta","gamma"]',),
        ).fetchall()
        assert [r[0] for r in rows] == ["ALPHA", "BETA", "GAMMA"]
    finally:
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")


def test_dimension_lateral_composition(rvbbit):
    """Dimension ops compose with LATERAL — 1 input row → N output rows
    in a join."""
    op = f"dim_lat_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'dimension', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "uppercase",
                              "inputs": {"text": "{{ inputs.text }}"}}])),
        )
        rows = rvbbit.execute(
            f"WITH t(id, payload) AS (VALUES (1, %s), (2, %s)) "
            f"SELECT t.id, parts.value FROM t, LATERAL rvbbit.{op}(t.payload) AS parts(value) "
            f"ORDER BY t.id, parts.value",
            ("aaa\nbbb", "one\ntwo\nthree"),
        ).fetchall()
        assert rows == [
            (1, "AAA"), (1, "BBB"),
            (2, "ONE"), (2, "THREE"), (2, "TWO"),
        ]
    finally:
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")


# ---- Aggregate --------------------------------------------------------------


def test_aggregate_create_registers_aggregate(rvbbit):
    op = f"agg_probe_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'aggregate', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "uppercase",
                              "inputs": {"text": "wrap: {{ inputs.collection }}"}}])),
        )
        # Both with-opts and no-opts aggregates should exist
        rows = rvbbit.execute(
            "SELECT aggfnoid::regprocedure::text FROM pg_aggregate "
            f"WHERE aggfnoid::regprocedure::text LIKE 'rvbbit.{op}%' ORDER BY 1"
        ).fetchall()
        sigs = [r[0] for r in rows]
        assert any("(text)" in s for s in sigs), f"missing no-opts variant in {sigs}"
        assert any("text,jsonb" in s.replace(" ", "") for s in sigs), \
            f"missing with-opts in {sigs}"
    finally:
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{op}(text)")
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_sfunc(jsonb, text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_sfunc_no_opts(jsonb, text)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_ffunc(jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")


def test_aggregate_runs_once_per_group(rvbbit):
    """SFUNC collects rows per group; FFUNC runs the operator pipeline
    once per group on the accumulated collection. Receipts table has
    exactly one row per group (not one per source row)."""
    op = f"agg_run_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'aggregate', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "char_count",
                              "inputs": {"text": "collection: {{ inputs.collection }}"}}])),
        )
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op}'")
        rows = rvbbit.execute(
            f"WITH t(g, w) AS (VALUES ('a','one'), ('a','two'), ('a','three'), "
            f"                       ('b','x'), ('b','yy')) "
            f"SELECT g, rvbbit.{op}(w) FROM t GROUP BY g ORDER BY g"
        ).fetchall()
        assert len(rows) == 2  # 2 groups
        assert rows[0][0] == "a"
        assert rows[1][0] == "b"
        # Group 'a' collected 3 items, 'b' collected 2 — char_count of
        # the JSON-rendered collection is larger for 'a'.
        assert int(rows[0][1]) > int(rows[1][1])

        # One receipt per group, NOT per source row.
        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 2, f"expected 2 receipts (one per group), got {n}"
    finally:
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{op}(text)")
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_sfunc(jsonb, text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_sfunc_no_opts(jsonb, text)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_ffunc(jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")


def test_aggregate_with_opts_form(rvbbit):
    """The opts variant should also work — explicit empty jsonb passed in."""
    op = f"agg_opts_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.create_operator(op_name => %s, op_shape => 'aggregate', "
            "op_arg_names => ARRAY['text'], op_return_type => 'text', "
            "op_system => 'unused', op_user => 'unused', "
            "op_steps => %s::jsonb)",
            (op, json.dumps([{"name": "x", "kind": "code", "fn": "uppercase",
                              "inputs": {"text": "wrap: {{ inputs.collection }}"}}])),
        )
        row = rvbbit.execute(
            f"WITH t(w) AS (VALUES ('hello'), ('world')) "
            f"SELECT rvbbit.{op}(w, '{{}}'::jsonb) FROM t"
        ).fetchone()
        assert row is not None
        # Should be the uppercase of the wrapped collection
        assert "HELLO" in row[0].upper() or "HELLO" in row[0]
    finally:
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{op}(text)")
        rvbbit.execute(f"DROP AGGREGATE IF EXISTS rvbbit.{op}(text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_sfunc(jsonb, text, jsonb)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_sfunc_no_opts(jsonb, text)")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit._agg_{op}_ffunc(jsonb)")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
