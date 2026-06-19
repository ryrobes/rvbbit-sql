-- 0070_agent_loop.sql
-- The agent step kind (v0): a bounded, tool-calling loop that runs as a SQL
-- operator. The Rust driver (unit_of_work::run_step_agent) dispatches a
-- `{"kind":"agent"}` step: the model is given a system prompt + task + tool
-- specs, then drives itself — calling the built-in read-only `query` tool (or
-- allow-listed MCP tools), feeding each result back, until it answers with no
-- tool call or a cap trips (max_iters / token / cost / wall budget). Every turn
-- is appended here, to rvbbit.agent_messages, for token/cost debugging.
--
-- This migration adds the transcript table + a worked example operator,
-- rvbbit.pg_health(focus) — a read-only Postgres + rvbbit health analyst.

-- ---------------------------------------------------------------------------
-- Transcript audit. One row per turn (system/user/assistant/tool/error),
-- keyed by a generated run_id that run_step_agent also returns in the step
-- output (steps.<name>.agent_run_id). v0 writes are in-transaction (visible on
-- commit); out-of-band durability on abort is a v0.1 refinement.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rvbbit.agent_messages (
    run_id        text        NOT NULL,
    turn_idx      int         NOT NULL,
    operator      text,                   -- the operator whose agent step this run belongs to
    model         text,                   -- the model driving the loop (constant per run in v0)
    role          text        NOT NULL,   -- system | user | assistant | tool | error
    content       text,
    tool_name     text,                   -- set on tool turns
    tool_calls    jsonb,                  -- the assistant turn's chosen tool_calls (raw)
    finish_reason text,
    tokens_in     int         NOT NULL DEFAULT 0,
    tokens_out    int         NOT NULL DEFAULT 0,
    cost_usd      numeric(12, 6),
    latency_ms    int         NOT NULL DEFAULT 0,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, turn_idx)
);

CREATE INDEX IF NOT EXISTS agent_messages_created_at_idx
    ON rvbbit.agent_messages (created_at DESC);

CREATE INDEX IF NOT EXISTS agent_messages_operator_idx
    ON rvbbit.agent_messages (operator, created_at DESC);

COMMENT ON TABLE rvbbit.agent_messages IS
    'Per-turn transcript of agent-step (kind:"agent") operator runs. run_id is returned in the step output. Use for token/cost debugging: SELECT * FROM rvbbit.agent_messages WHERE run_id = ''…'' ORDER BY turn_idx.';

-- ---------------------------------------------------------------------------
-- Worked example: rvbbit.pg_health(focus text) -> text (markdown report).
--
-- A read-only health analyst. It explores with the `query` tool, then writes a
-- markdown report. The system prompt grounds it in rvbbit so it does not treat
-- rvbbit-internal behaviors as vanilla-PG problems, and tells it to DISCOVER the
-- telemetry schema itself (information_schema) rather than assuming table names.
-- ---------------------------------------------------------------------------
SELECT rvbbit.create_operator(
    op_name        => 'pg_health',
    op_arg_names   => ARRAY['focus'],
    op_return_type => 'text',
    op_shape       => 'scalar',
    op_model       => 'openai/gpt-5.4-mini',
    op_max_tokens  => 4096,
    op_description => 'Agentic Postgres + rvbbit health report: a bounded read-only query loop that returns a markdown report. Pass an optional focus area (e.g. ''connections'', ''bloat'', ''rvbbit routing'').',
    op_steps       => $steps$
[
  {
    "name": "report",
    "kind": "agent",
    "model": "openai/gpt-5.4-mini",
    "system": "You are rvbbit's Postgres health analyst. You operate INSIDE a Postgres database that runs the `rvbbit` extension. You have ONE tool: `query`, which runs a single read-only SQL SELECT/WITH and returns rows as JSON (200-row cap). Writes and DDL are rejected — you can only observe.\n\nHOW TO WORK:\n1. Discover before you assume. Telemetry table names vary by version — find them with `SELECT table_name FROM information_schema.tables WHERE table_schema='rvbbit' ORDER BY table_name`, and inspect columns the same way before querying a table you have not seen.\n2. Start with the Postgres standard views: pg_stat_activity (connections/idle-in-transaction/long runners), pg_stat_database (commits, rollbacks, deadlocks, cache hit ratio), pg_stat_user_tables (seq scans, dead tuples / bloat, last autovacuum), pg_locks (blocking), and pg_stat_statements if present (top queries). Use now()/age() freely — they are allowed.\n3. Then look at rvbbit's own telemetry (whatever you found in step 1) — typically routing decisions, operator receipts, LLM cost events, MCP invocations, brain/catalog sync runs, accel/compaction ticks, alert state.\n4. Keep each query small and targeted. If a result is truncated, narrow it (add WHERE / aggregate / LIMIT) rather than re-fetching everything.\n\nKNOW WHAT IS NORMAL FOR RVBBIT (do not flag these as problems):\n- High CPU during a cube refresh is EXPECTED — it runs a DataFusion aggregation, not a runaway query.\n- The FIRST execution of a novel query shape can cost a few seconds — the router benchmarks candidate engines before pinning a route; subsequent runs are fast. force_heap_scan bypasses this.\n- Compaction / columnar acceleration ticks are single-core by design and are harmless background work.\n- The Document Brain degrades to lexical-only search if the embedder is down — that is graceful degradation, not a crash.\nWeigh these against genuine issues: connection saturation, idle-in-transaction holding locks, deadlocks, runaway bloat / stalled autovacuum, low cache-hit ratio, replication lag, repeated operator/MCP errors, or runaway LLM cost.\n\nWHEN DONE, stop calling tools and reply with ONLY a markdown report:\n# Postgres + rvbbit Health — <focus or 'overview'>\n**Status:** 🟢 healthy | 🟡 watch | 🔴 act\nThen short sections (Connections & activity, Storage & autovacuum, Cache & I/O, rvbbit subsystems, and Recommendations). Lead with what matters. If everything is fine, say so plainly and keep it brief — do not invent problems. Cite the concrete numbers you observed.",
    "task": "Produce a Postgres + rvbbit health report for the current moment. Investigate with the query tool first (discover the rvbbit telemetry tables, then sample the standard pg_stat views and rvbbit's own), then write the markdown report. Focus area (optional): {{ inputs.focus }}",
    "tools": [{ "builtin": "query" }],
    "max_iters": 10,
    "budget": { "cost_usd": 0.50, "wall_ms": 120000 },
    "tool_result_max_chars": 8000
  }
]
$steps$
);

-- Agent operators MUST bypass the result cache: a memoized agent would replay a
-- frozen transcript instead of re-inspecting live state. (cache_policy is honored
-- on the invoke path; create_operator leaves it at the 'memoize' default.)
UPDATE rvbbit.operators SET cache_policy = 'never' WHERE name = 'pg_health';
