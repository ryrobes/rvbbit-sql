"""compact() + rewriter integration: from heap insert all the way
to transparent parquet reads, rewritten JSON ops, and count(*) const."""

import pytest


@pytest.fixture
def llm_table(rvbbit, temp_table):
    """LLM-shaped table with 200 rows, compacted to parquet.

    Schema is FIXED to what rvbbit.export_to_parquet expects (Phase 2a's
    hardcoded LLM_events shape — generalization is Phase 2b deferred).
    """
    rvbbit.execute(f"""
        CREATE TABLE {temp_table} (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts          timestamptz NOT NULL,
            user_id     bigint NOT NULL,
            model       text NOT NULL,
            tokens_in   int NOT NULL,
            tokens_out  int NOT NULL,
            latency_ms  int NOT NULL,
            status      text NOT NULL,
            prompt      text NOT NULL,
            response    jsonb NOT NULL,
            metadata    jsonb NOT NULL
        ) USING rvbbit
    """)
    rvbbit.execute(f"""
        INSERT INTO {temp_table}
            (ts, user_id, model, tokens_in, tokens_out, latency_ms, status, prompt, response, metadata)
        SELECT
            now() - (g * interval '1 second'),
            (g * 7) % 1000,
            (ARRAY['opus-4-6','sonnet-4-6','haiku-4-5'])[1 + (g % 3)],
            g,
            g * 2,
            (g * 13) % 30000,
            (ARRAY['ok','error','timeout'])[1 + (g % 3)],
            'sample prompt ' || g,
            jsonb_build_object(
                'stop_reason', (ARRAY['end_turn','max_tokens','stop_sequence'])[1 + (g % 3)],
                'model', (ARRAY['opus-4-6','sonnet-4-6'])[1 + (g % 2)],
                'usage', jsonb_build_object('input_tokens', g, 'output_tokens', g * 2)
            ),
            jsonb_build_object('region', (ARRAY['us-east-1','eu-west-1'])[1 + (g % 2)])
        FROM generate_series(1, 200) g
    """)
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)").fetchone()
    yield temp_table


# ---- Compact ----------------------------------------------------------------


def test_compact_retains_heap_gold_source(rvbbit, llm_table):
    # The heap remains the gold-source fallback after acceleration. Rvbbit
    # reads accelerated files when safe, but pg_dump/restore and rebuilds still
    # have ordinary heap state available.
    row = rvbbit.execute(
        f"SELECT pg_relation_size('{llm_table}'::regclass)"
    ).fetchone()
    assert row[0] > 0

    visible = rvbbit.execute(f"SELECT count(*) FROM {llm_table}").fetchone()
    assert visible[0] == 200


def test_row_groups_registered(rvbbit, llm_table):
    row = rvbbit.execute(
        f"SELECT count(*), sum(n_rows) FROM rvbbit.row_groups "
        f"WHERE table_oid = '{llm_table}'::regclass"
    ).fetchone()
    assert row[0] >= 1
    assert row[1] == 200


def test_no_physical_shred_columns_required(rvbbit, llm_table):
    cols = {
        r[0]
        for r in rvbbit.execute(
            f"SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{llm_table}' AND column_name LIKE 'x\\_%' ESCAPE '\\'"
        ).fetchall()
    }
    assert cols == set()

    rows = rvbbit.execute(
        f"SELECT response->>'stop_reason' AS r, count(*) FROM {llm_table} "
        f"GROUP BY 1 ORDER BY r"
    ).fetchall()
    assert rows == [("end_turn", 66), ("max_tokens", 67), ("stop_sequence", 67)]


# ---- Rewriter: count(*) → metadata constant ---------------------------------


def test_count_star_uses_metadata(rvbbit, llm_table):
    plan = rvbbit.execute(
        f"EXPLAIN (FORMAT TEXT) SELECT count(*) FROM {llm_table}"
    ).fetchall()
    plan_text = "\n".join(r[0] for r in plan)
    # The R4 rewrite reduces the whole thing to a Result returning a Const.
    assert "Result" in plan_text
    assert "Custom Scan" not in plan_text
    assert "Seq Scan" not in plan_text

    # And the answer matches reality:
    row = rvbbit.execute(f"SELECT count(*) FROM {llm_table}").fetchone()
    assert row[0] == 200


def test_count_star_includes_post_compact_heap_delta(rvbbit, llm_table):
    rvbbit.execute(f"""
        INSERT INTO {llm_table}
            (ts, user_id, model, tokens_in, tokens_out, latency_ms, status, prompt, response, metadata)
        VALUES
            (now(), 2001, 'haiku-4-5', 1, 2, 3, 'ok', 'delta prompt 1',
             '{{"stop_reason":"end_turn"}}'::jsonb, '{{"region":"us-east-1"}}'::jsonb),
            (now(), 2002, 'haiku-4-5', 1, 2, 3, 'ok', 'delta prompt 2',
             '{{"stop_reason":"end_turn"}}'::jsonb, '{{"region":"us-east-1"}}'::jsonb)
    """)

    row = rvbbit.execute(f"SELECT count(*) FROM {llm_table}").fetchone()
    assert row[0] == 202


def test_count_star_with_where_falls_through(rvbbit, llm_table):
    # WHERE prevents the rewrite; plan should use Custom Scan.
    plan = rvbbit.execute(
        f"EXPLAIN (FORMAT TEXT) SELECT count(*) FROM {llm_table} WHERE status = 'error'"
    ).fetchall()
    plan_text = "\n".join(r[0] for r in plan)
    assert "Custom Scan" in plan_text


# ---- JSON expression execution ----------------------------------------------


def test_json_expression_groupby(rvbbit, llm_table):
    rows = rvbbit.execute(
        f"SELECT response->>'stop_reason' AS r, count(*) FROM {llm_table} "
        f"GROUP BY 1 ORDER BY r"
    ).fetchall()
    assert rows == [("end_turn", 66), ("max_tokens", 67), ("stop_sequence", 67)]


def test_json_expression_nested_cast(rvbbit, llm_table):
    # (response->'usage'->>'input_tokens')::int  — the Q7 case
    row = rvbbit.execute(
        f"SELECT sum((response->'usage'->>'input_tokens')::int) FROM {llm_table}"
    ).fetchone()
    # 1+2+...+200 = 20100
    assert row[0] == 20100


# ---- The CRITICAL ExecQual bug: regression test -----------------------------


def test_where_filter_actually_applies(rvbbit, llm_table):
    """ExecQual must be called in our CustomScan. Without it, every
    WHERE on rvbbit tables silently returned all rows."""
    row_all = rvbbit.execute(f"SELECT count(*) FROM {llm_table}").fetchone()
    row_filt = rvbbit.execute(
        f"SELECT count(*) FROM {llm_table} WHERE status = 'error'"
    ).fetchone()
    assert row_all[0] == 200
    # Out of 200 with status cycling ok/error/timeout, 'error' is ~67.
    assert 0 < row_filt[0] < row_all[0]


def test_where_via_json_expression_path(rvbbit, llm_table):
    """JSON-expression quals must still apply correctly after acceleration."""
    row = rvbbit.execute(
        f"SELECT count(*) FROM {llm_table} WHERE response->>'stop_reason' = 'max_tokens'"
    ).fetchone()
    # ~67 rows with stop_reason='max_tokens'
    assert row[0] == 67
