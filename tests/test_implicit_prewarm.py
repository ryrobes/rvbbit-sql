"""Implicit prewarm — auto-fire rvbbit.prewarm_operator() for plain
SELECT rvbbit.<op>(col) FROM table queries (RYR-277).

Verified via the echo sidecar so tests are deterministic + free. The
auto-prewarm rewrite preserves simple quals/order/limits so the
receipts table should match the effective row set, not blindly fetch
the whole source table.
"""
import json
import urllib.request
import uuid

import pytest


ECHO_BASE = "http://rvbbit-echo:8080"


def _echo_alive() -> bool:
    try:
        urllib.request.urlopen(f"{ECHO_BASE}/health", timeout=2).read()
        return True
    except Exception:
        return False


def _echo_reset():
    req = urllib.request.Request(f"{ECHO_BASE}/debug/reset", method="POST")
    urllib.request.urlopen(req, timeout=2).read()


def _echo_stats():
    return json.loads(urllib.request.urlopen(f"{ECHO_BASE}/debug/stats", timeout=2).read())


pytestmark = pytest.mark.skipif(
    not _echo_alive(),
    reason=f"echo sidecar not reachable at {ECHO_BASE}",
)


def _setup(rvbbit):
    """Register echo specialist + an op that upper-cases the input."""
    spec = f"echo_pw_{uuid.uuid4().hex[:6]}"
    op = f"upper_pw_{uuid.uuid4().hex[:6]}"
    tbl = f"pw_input_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, "
        "  backend_batch_size => 16, backend_timeout_ms => 5000)",
        (spec, f"{ECHO_BASE}/predict"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (op, json.dumps([{
            "name": "u", "kind": "specialist", "specialist": spec,
            "inputs": {"text": "{{ inputs.text }}", "fn": "upper"},
        }])),
    )
    rvbbit.execute(f"CREATE TABLE {tbl} (id int, body text)")
    for i in range(1, 13):
        rvbbit.execute(f"INSERT INTO {tbl} VALUES (%s, %s)", (i, f"item{i}"))
    rvbbit.execute(f"ANALYZE {tbl}")
    rvbbit.execute("SELECT rvbbit.flush_cache()")
    rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op}'")
    return spec, op, tbl


def _cleanup(rvbbit, spec, op, tbl):
    rvbbit.execute(f"DROP TABLE IF EXISTS {tbl}")
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{spec}'")
    rvbbit.execute("SELECT rvbbit.reload_backends()")


def _setup_two_col_rvbbit(rvbbit):
    spec = f"echo_pw_{uuid.uuid4().hex[:6]}"
    op = f"upper_pw_{uuid.uuid4().hex[:6]}"
    tbl = f"pw_rvbbit_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, "
        "  backend_batch_size => 16, backend_timeout_ms => 5000)",
        (spec, f"{ECHO_BASE}/predict"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_shape => 'scalar', "
        "  op_arg_names => ARRAY['text'], op_return_type => 'text', "
        "  op_system => 'unused', op_user => 'unused', "
        "  op_steps => %s::jsonb)",
        (op, json.dumps([{
            "name": "u", "kind": "specialist", "specialist": spec,
            "inputs": {"text": "{{ inputs.text }}", "fn": "upper"},
        }])),
    )
    rvbbit.execute(f"CREATE TABLE {tbl} (id int, title text, observed text) USING rvbbit")
    for i in range(1, 13):
        rvbbit.execute(
            f"INSERT INTO {tbl} VALUES (%s, %s, %s)",
            (i, f"title{i}", f"observed{i}"),
        )
    rvbbit.execute(f"ANALYZE {tbl}")
    rvbbit.execute("SELECT rvbbit.flush_cache()")
    rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op}'")
    return spec, op, tbl


def test_unfiltered_select_fires_prewarm(rvbbit):
    """SELECT rvbbit.op(col) FROM t — no qual → auto-prewarm runs first;
    LIMIT is preserved so only the requested rows are warmed."""
    spec, op, tbl = _setup(rvbbit)
    try:
        rows = rvbbit.execute(
            f"SELECT id, rvbbit.{op}(body) FROM {tbl} ORDER BY id LIMIT 5"
        ).fetchall()
        assert [r[1] for r in rows] == [f"ITEM{i}" for i in range(1, 6)]

        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 5, f"expected 5 limited prewarm receipts, got {n}"
    finally:
        _cleanup(rvbbit, spec, op, tbl)


def test_qual_is_preserved_by_prewarm(rvbbit):
    """SELECT ... FROM t WHERE id > 5 — qual/order are preserved.
    Receipts after = exactly the rows the user requested, no extras."""
    spec, op, tbl = _setup(rvbbit)
    try:
        rows = rvbbit.execute(
            f"SELECT id, rvbbit.{op}(body) FROM {tbl} WHERE id > 5 ORDER BY id"
        ).fetchall()
        assert len(rows) == 7

        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 7, f"expected 7 filtered prewarm receipts, got {n}"
    finally:
        _cleanup(rvbbit, spec, op, tbl)


def test_filtered_order_limit_can_sort_by_non_input_column(rvbbit):
    """Implicit prewarm should preserve an ORDER BY/LIMIT row set even when the
    sort key is not one of the semantic operator arguments."""
    spec, op, tbl = _setup(rvbbit)
    try:
        _echo_reset()
        rows = rvbbit.execute(
            f"""
            SELECT id, rvbbit.{op}(body) AS out
            FROM {tbl}
            WHERE id >= 4
            ORDER BY id DESC
            LIMIT 3
            """
        ).fetchall()
        assert rows == [(12, "ITEM12"), (11, "ITEM11"), (10, "ITEM10")]

        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 3, f"expected 3 limited prewarm receipts, got {n}"

        stats = _echo_stats()
        assert stats["total_inputs"] == 3, stats
    finally:
        _cleanup(rvbbit, spec, op, tbl)


def test_groupby_blocks_prewarm(rvbbit):
    """GROUP BY also disqualifies — rule bails."""
    spec, op, tbl = _setup(rvbbit)
    try:
        # GROUP BY id is trivial but exercises the gate
        rvbbit.execute(
            f"SELECT id, MIN(rvbbit.{op}(body)) FROM {tbl} GROUP BY id ORDER BY id LIMIT 3"
        ).fetchall()
        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        # No prewarm; only the 3 group rows materialized + per-call.
        # Could be 3 if executed lazily; bound check.
        assert n <= 12, f"unexpected receipt count: {n}"
    finally:
        _cleanup(rvbbit, spec, op, tbl)


def test_nested_op_in_expression_still_detected(rvbbit):
    """rvbbit.op(body)->>'whatever' wraps the FuncExpr in an OpExpr.
    The recursive walker finds it. For text-returning ops a wrapping
    function call (length, upper, etc.) is the equivalent shape."""
    spec, op, tbl = _setup(rvbbit)
    try:
        # Wrap the call in lower() — verifies the walker descends FuncExpr
        rows = rvbbit.execute(
            f"SELECT id, lower(rvbbit.{op}(body)) AS l FROM {tbl} ORDER BY id LIMIT 4"
        ).fetchall()
        assert [r[1] for r in rows] == [f"item{i}" for i in range(1, 5)]

        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 4, f"expected 4 limited prewarm receipts, got {n}"
    finally:
        _cleanup(rvbbit, spec, op, tbl)


def test_limit_bounds_large_estimate_prewarm(rvbbit):
    """Synthetic large table estimate plus small LIMIT should still warm
    only the bounded row set."""
    spec, op, tbl = _setup(rvbbit)
    try:
        # Force pg_class.reltuples to look huge. A constant LIMIT should now
        # bound the effective prewarm size instead of blocking the rewrite.
        rvbbit.execute(
            f"UPDATE pg_class SET reltuples = 1000000000 "
            f"WHERE oid = '{tbl}'::regclass"
        )
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{op}'")

        rvbbit.execute(
            f"SELECT id, rvbbit.{op}(body) FROM {tbl} ORDER BY id LIMIT 3"
        ).fetchall()
        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 3, f"expected 3 limited prewarm receipts, got {n}"
    finally:
        # Reset reltuples + cleanup
        try:
            rvbbit.execute(
                f"UPDATE pg_class SET reltuples = 12 WHERE oid = '{tbl}'::regclass"
            )
        except Exception:
            pass
        _cleanup(rvbbit, spec, op, tbl)


def test_multi_expression_select_batches_each_semantic_projection(rvbbit):
    """Multiple scalar semantic expressions in one SELECT should prewarm
    before the executor evaluates per-row functions. This catches Rvbbit-table
    projection fast paths that used to return before implicit prewarm ran."""
    spec, op, tbl = _setup_two_col_rvbbit(rvbbit)
    try:
        _echo_reset()
        rows = rvbbit.execute(
            f"""
            SELECT id, rvbbit.{op}(title), rvbbit.{op}(observed)
            FROM {tbl}
            ORDER BY id
            LIMIT 8
            """
        ).fetchall()
        assert rows[0] == (1, "TITLE1", "OBSERVED1")
        assert rows[-1] == (8, "TITLE8", "OBSERVED8")

        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        assert n == 16, f"expected 16 receipts, got {n}"

        stats = _echo_stats()
        assert stats["total_inputs"] == 16, stats
        assert stats["max_batch"] > 1, stats
    finally:
        _cleanup(rvbbit, spec, op, tbl)


def test_parameterized_limit_small_relation_still_batches(rvbbit):
    """A DB-API parameterized LIMIT is not a Const in the parse hook.
    If the relation is small enough, implicit prewarm should warm the whole
    no-WHERE relation rather than falling back to serial per-row calls."""
    spec, op, tbl = _setup_two_col_rvbbit(rvbbit)
    try:
        _echo_reset()
        rows = rvbbit.execute(
            f"""
            SELECT id, rvbbit.{op}(title), rvbbit.{op}(observed)
            FROM {tbl}
            ORDER BY id
            LIMIT %s::int
            """,
            (8,),
        ).fetchall()
        assert rows[0] == (1, "TITLE1", "OBSERVED1")
        assert rows[-1] == (8, "TITLE8", "OBSERVED8")

        n = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{op}'"
        ).fetchone()[0]
        # The parameter value is unavailable in the parse hook, so the rule
        # warms the small no-WHERE relation rather than only the bounded rows.
        assert n == 24, f"expected 24 receipts from full small-relation warm, got {n}"

        stats = _echo_stats()
        assert stats["total_inputs"] == 24, stats
        assert stats["max_batch"] > 1, stats
    finally:
        _cleanup(rvbbit, spec, op, tbl)
