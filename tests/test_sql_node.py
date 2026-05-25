"""SQL node — a parameterized SELECT as a workflow node primitive (Loop 22).

A `kind: sql` node runs a parameterized query against the database. `$1..$N`
are filled from the rendered `params` templates (bound as quoted literals —
injection-safe). The output is the first row as a {column: value} jsonb
object, addressable downstream as `{{ steps.<name>.output.<column> }}`.

SQL is deterministic, so these tests need no LLM.
"""

import json
import uuid

import pytest


@pytest.fixture
def lookup_table(rvbbit):
    name = f"sqltest_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {name} (id int PRIMARY KEY, name text, tier text)")
    rvbbit.execute(f"INSERT INTO {name} VALUES (1,'Alice','gold'), (2,'Bob','silver')")
    yield name
    rvbbit.execute(f"DROP TABLE IF EXISTS {name}")


def _make_op(rvbbit, steps, arg_names=("id",), return_type="jsonb"):
    name = f"sqlop_{uuid.uuid4().hex[:8]}"
    args_sql = "ARRAY[" + ",".join(f"'{a}'" for a in arg_names) + "]"
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        f"  op_name => %s, op_arg_names => {args_sql}, "
        "  op_return_type => %s, op_steps => %s::jsonb)",
        (name, return_type, json.dumps(steps)),
    )
    return name


def _drop_op(rvbbit, name, n_args=1):
    sig = ", ".join(["text"] * n_args + ["jsonb"])
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}({sig})")


def test_sql_node_lookup(rvbbit, lookup_table):
    """A sql node returns the first row as a {column: value} object."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "row",
                "kind": "sql",
                "sql": f"SELECT name, tier FROM {lookup_table} WHERE id = $1",
                "params": ["{{ inputs.id }}"],
            }
        ],
    )
    try:
        out = rvbbit.execute(f"SELECT rvbbit.{op}('1')").fetchone()[0]
        assert out == {"name": "Alice", "tier": "gold"}
    finally:
        _drop_op(rvbbit, op)


def test_sql_node_two_params(rvbbit, lookup_table):
    """$1 and $2 are filled positionally from the params list."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "row",
                "kind": "sql",
                "sql": f"SELECT name FROM {lookup_table} WHERE id = $1 AND tier = $2",
                "params": ["{{ inputs.id }}", "{{ inputs.tier }}"],
            }
        ],
        arg_names=("id", "tier"),
    )
    try:
        assert rvbbit.execute(f"SELECT rvbbit.{op}('1','gold')").fetchone()[0] == {
            "name": "Alice"
        }
        # id=1 is gold, not silver -> no row
        assert rvbbit.execute(f"SELECT rvbbit.{op}('1','silver')").fetchone()[0] is None
    finally:
        _drop_op(rvbbit, op, n_args=2)


def test_sql_node_zero_rows(rvbbit, lookup_table):
    """No matching row -> null output."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "row",
                "kind": "sql",
                "sql": f"SELECT name FROM {lookup_table} WHERE id = $1",
                "params": ["{{ inputs.id }}"],
            }
        ],
    )
    try:
        assert rvbbit.execute(f"SELECT rvbbit.{op}('999')").fetchone()[0] is None
    finally:
        _drop_op(rvbbit, op)


def test_sql_node_feeds_downstream(rvbbit, lookup_table):
    """A sql node's row feeds a later node via {{ steps.x.output.col }}."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "row",
                "kind": "sql",
                "sql": f"SELECT name, tier FROM {lookup_table} WHERE id = $1",
                "params": ["{{ inputs.id }}"],
            },
            {
                "name": "up",
                "kind": "code",
                "fn": "uppercase",
                "inputs": {"text": "{{ steps.row.output.name }}"},
            },
        ],
        return_type="text",
    )
    try:
        assert rvbbit.execute(f"SELECT rvbbit.{op}('2')").fetchone()[0] == "BOB"
        kinds = rvbbit.execute(
            "SELECT sub_calls->0->>'kind', sub_calls->1->>'kind' "
            "FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()
        assert kinds == ("sql", "code")
    finally:
        _drop_op(rvbbit, op)


def test_sql_node_injection_safe(rvbbit, lookup_table):
    """A param containing SQL is bound as a literal, never executed."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "row",
                "kind": "sql",
                "sql": f"SELECT name FROM {lookup_table} WHERE name = $1",
                "params": ["{{ inputs.id }}"],
            }
        ],
    )
    try:
        evil = f"x'; DROP TABLE {lookup_table}; --"
        assert rvbbit.execute(f"SELECT rvbbit.{op}(%s)", (evil,)).fetchone()[0] is None
        # the table is untouched — the param never escaped its literal
        n = rvbbit.execute(f"SELECT count(*) FROM {lookup_table}").fetchone()[0]
        assert n == 2
    finally:
        _drop_op(rvbbit, op)


def test_sql_node_warm_path(rvbbit, lookup_table):
    """prewarm over a sql-node operator runs the leader path (no crash)."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "row",
                "kind": "sql",
                "sql": f"SELECT tier FROM {lookup_table} WHERE id = $1",
                "params": ["{{ inputs.id }}"],
            }
        ],
    )
    try:
        res = rvbbit.execute(
            "SELECT n_inputs, n_executed FROM rvbbit.prewarm_operator(%s, %s)",
            (op, f"SELECT id::text FROM {lookup_table} ORDER BY id"),
        ).fetchone()
        assert res == (2, 2)
    finally:
        _drop_op(rvbbit, op)
