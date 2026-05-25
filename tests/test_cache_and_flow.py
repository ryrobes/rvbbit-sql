"""In-memory cache + flow control infrastructure.

The L1 in-memory cache layered on top of rvbbit.receipts is the biggest
single-call latency win — drops cache-hit cost from ~1-3ms SPI roundtrip
to ~5μs hashmap lookup. Tests verify the layer wires up correctly and
the SQL surface (flush_cache, cache_size, cache_capacity) is present.
"""

import uuid


def test_cache_functions_exist(rvbbit):
    rows = rvbbit.execute(
        "SELECT proname FROM pg_proc "
        "WHERE pronamespace = 'rvbbit'::regnamespace "
        "  AND proname IN ('flush_cache', 'cache_size', 'cache_capacity') "
        "ORDER BY proname"
    ).fetchall()
    names = [r[0] for r in rows]
    assert names == ["cache_capacity", "cache_size", "flush_cache"]


def test_cache_starts_empty(rvbbit):
    rvbbit.execute("SELECT rvbbit.flush_cache()")
    row = rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()
    assert row[0] == 0


def test_cache_capacity_set(rvbbit):
    """Default 10k entries per the docker-compose env var (RVBBIT_CACHE_SIZE)."""
    row = rvbbit.execute("SELECT rvbbit.cache_capacity()").fetchone()
    # Either the docker-compose default (10000) or whatever you set explicitly.
    assert row[0] >= 1


def test_cache_populated_via_code_only_operator(rvbbit):
    """A pure-code operator deterministic enough to test cache plumbing
    without burning LLM credits."""
    name = f"cache_probe_{uuid.uuid4().hex[:8]}"
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
                    {"name": "x", "kind": "code", "fn": "uppercase",
                     "inputs": {"text": "{{ inputs.text }}"}}
                ]""",
            ),
        )
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        before = rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()[0]

        marker = f"cache-test-{uuid.uuid4().hex}"
        rvbbit.execute(f"SELECT rvbbit.{name}(%s)", (marker,))
        rvbbit.execute(f"SELECT rvbbit.{name}(%s)", (marker,))  # second call: pure cache hit

        after = rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()[0]
        assert after >= before + 1
        # Only ONE receipt logged for the two calls (second is cache hit).
        row = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name}'"
        ).fetchone()
        assert row[0] == 1
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_flush_cache_drops_in_memory(rvbbit):
    name = f"flush_probe_{uuid.uuid4().hex[:8]}"
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
                    {"name": "x", "kind": "code", "fn": "trim",
                     "inputs": {"text": "{{ inputs.text }}"}}
                ]""",
            ),
        )
        rvbbit.execute(f"SELECT rvbbit.{name}('  hello  ')")
        assert rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()[0] >= 1
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        assert rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()[0] == 0
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_l2_backfills_l1(rvbbit):
    """Cache hit from rvbbit.receipts (L2) should populate L1 so the next
    call is the µs path. We simulate by inserting directly into receipts,
    flushing L1, then calling the operator."""
    name = f"l2_probe_{uuid.uuid4().hex[:8]}"
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
                    {"name": "x", "kind": "code", "fn": "uppercase",
                     "inputs": {"text": "{{ inputs.text }}"}}
                ]""",
            ),
        )
        # First call - populates L1 + receipts (L2).
        marker = f"l2-{uuid.uuid4().hex}"
        rvbbit.execute(f"SELECT rvbbit.{name}(%s)", (marker,))
        # Now flush L1 only — receipts (L2) still there.
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        before_l1 = rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()[0]
        assert before_l1 == 0
        # Call again — should hit L2, backfill L1, and NOT log another receipt.
        rvbbit.execute(f"SELECT rvbbit.{name}(%s)", (marker,))
        after_l1 = rvbbit.execute("SELECT rvbbit.cache_size()").fetchone()[0]
        assert after_l1 == 1, "L2 hit should backfill L1"
        n_receipts = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name}'"
        ).fetchone()[0]
        assert n_receipts == 1, "should not have logged a second receipt"
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")
