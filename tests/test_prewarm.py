"""Cross-row pre-warming via rvbbit.prewarm_operator.

Verifies the stats contract (n_inputs / n_cache_hits / n_executed / n_errors /
wall_ms), that a second prewarm pass hits cache for every row, and that the
user's subsequent SELECT becomes a full cache hit.

Uses a code-only operator (no LLM) so the test is deterministic and free.
"""

import uuid


def _create_uppercase_op(rvbbit, name: str) -> None:
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


def test_prewarm_function_exists(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_proc WHERE proname = 'prewarm_operator' "
        "AND pronamespace = 'rvbbit'::regnamespace"
    ).fetchone()
    assert row[0] == 1


def test_prewarm_stats_shape(rvbbit):
    name = f"prewarm_probe_{uuid.uuid4().hex[:8]}"
    table = f"prewarm_input_{uuid.uuid4().hex[:8]}"
    try:
        _create_uppercase_op(rvbbit, name)
        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        marker = uuid.uuid4().hex
        for i in range(5):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"{marker}-{i}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{name}'")

        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s, %s)",
            (name, f"SELECT text FROM {table} ORDER BY text", 4),
        ).fetchone()
        n_in, n_hits, n_exec, n_err, wall_ms = row
        assert n_in == 5
        assert n_hits == 0
        assert n_exec == 5
        assert n_err == 0
        assert wall_ms >= 0
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_prewarm_second_pass_is_all_hits(rvbbit):
    name = f"prewarm_probe_{uuid.uuid4().hex[:8]}"
    table = f"prewarm_input_{uuid.uuid4().hex[:8]}"
    try:
        _create_uppercase_op(rvbbit, name)
        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        marker = uuid.uuid4().hex
        for i in range(3):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"{marker}-{i}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{name}'")

        # First pass: all misses.
        rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (name, f"SELECT text FROM {table}"),
        ).fetchone()

        # Second pass: all hits (no new receipts should be written).
        before = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name}'"
        ).fetchone()[0]
        row = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (name, f"SELECT text FROM {table}"),
        ).fetchone()
        n_in, n_hits, n_exec, n_err, _ = row
        assert n_in == 3
        assert n_hits == 3
        assert n_exec == 0
        assert n_err == 0
        after = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name}'"
        ).fetchone()[0]
        assert after == before, "second-pass cache hits should not log new receipts"
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")


def test_prewarm_then_select_is_cache_hits(rvbbit):
    """End-to-end demo: prewarm populates cache so the user's actual query
    runs against the operator with zero further compute. Verifies receipts
    count doesn't grow across the post-prewarm SELECT."""
    name = f"prewarm_probe_{uuid.uuid4().hex[:8]}"
    table = f"prewarm_input_{uuid.uuid4().hex[:8]}"
    try:
        _create_uppercase_op(rvbbit, name)
        rvbbit.execute(f"CREATE TABLE {table} (text text)")
        marker = uuid.uuid4().hex
        for i in range(6):
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s)", (f"{marker}-{i}",))

        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute(f"DELETE FROM rvbbit.receipts WHERE operator = '{name}'")

        rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (name, f"SELECT text FROM {table}"),
        ).fetchone()

        before = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name}'"
        ).fetchone()[0]
        assert before == 6

        rows = rvbbit.execute(
            f"SELECT rvbbit.{name}(text) FROM {table} ORDER BY text"
        ).fetchall()
        assert len(rows) == 6
        assert all(r[0] == r[0].upper() for r in rows)

        after = rvbbit.execute(
            f"SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name}'"
        ).fetchone()[0]
        assert after == before, "post-prewarm SELECT should be 100% cache hits"
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}(text, jsonb)")
