# rvbbit

[![CI](https://github.com/ryrobes/rvbbit-sql/actions/workflows/ci.yml/badge.svg)](https://github.com/ryrobes/rvbbit-sql/actions/workflows/ci.yml)

**The AI primitives Snowflake Cortex and Databricks AI Functions
didn't ship. In your Postgres.**

`AI_COMPLETE`, `AI_CLASSIFY`, `AI_FILTER`. One-shot. No retry. No
ensemble. No validators. No composition. The cache is bolted on. The
receipts aren't queryable. You can't `pg_dump` your judgments.

Rvbbit's bet is that the unit of AI work in a database is the
**operator** — a user-definable, planner-visible SQL function with
multi-step pipelines, retry policies, ensembles, validators, and
audit trails — not a function-per-task built into the vendor's stack.

```sql
-- 1. Define once: a real operator. Three step kinds in one pipeline
--    (specialist → LLM → MCP tool), 3-way ensemble, blocking validator.
--    None of these compose in Cortex or Databricks AI Functions.
SELECT rvbbit.create_operator(
    op_name        => 'triage_ticket',
    op_arg_names   => ARRAY['body'],
    op_return_type => 'text',
    op_steps => '[
      {"name": "cheap", "kind": "specialist", "specialist": "classify",
       "inputs": {"text":   "{{ inputs.body }}",
                  "labels": "billing,bug,how-to,outage"}},
      {"name": "judge", "kind": "llm", "model": "claude-haiku-4-5",
       "system": "You categorize support tickets. Output one label.",
       "user":   "Cheap classifier said: {{ steps.cheap.output }}. Ticket: {{ inputs.body }}"},
      {"name": "enrich", "kind": "mcp", "server": "crm", "tool": "get_customer",
       "inputs": {"ticket_text": "{{ inputs.body }}"}}
    ]'::jsonb
);

-- Decorate: 3-way ensemble + reject anything outside the allowed labels.
SELECT rvbbit.set_operator_takes('triage_ticket',
    '{"factor": 3, "reduce": "vote"}'::jsonb);
SELECT rvbbit.set_operator_wards('triage_ticket', jsonb_build_object(
    'post', jsonb_build_array(jsonb_build_object(
        'validator', jsonb_build_object(
            'sql', '$output IN (''billing'',''bug'',''how-to'',''outage'')'),
        'mode', 'blocking'))));

-- 2. Use it like a function. Joins, WHERE, ORDER BY — the planner sees it.
SELECT body, rvbbit.triage_ticket(body) AS category
FROM tickets WHERE created_at > now() - interval '1 day';

-- 3. Audit it.
SELECT op_name, n_invocations, n_unique_inputs, total_cost_usd, total_latency_ms
FROM rvbbit.judgment_stats('triage_ticket');
-- op_name        n_invocations  n_unique_inputs  total_cost_usd  total_latency_ms
-- triage_ticket  1247           284              0.42            47180
```

One SQL function. Three step kinds inside it, ensemble + validator
wrapping it. Editable, planner-visible, content-hash-cached,
`pg_dump`-able. That's the wedge.

## What's actually different

Four orthogonal axes that compose. Most systems give you one at a time:

| Axis | What | Why it matters |
|---|---|---|
| **`steps`** | A pipeline of nodes: LLM, specialist (BERT / GLiNER / embed / rerank), Python, code, SQL, MCP-tool — any order, each reading the previous | Real workflows, not one-shot functions |
| **`takes`** | Run the pipeline N times, reduce via vote / median / evaluator / first-valid | Ensembles without orchestrator code |
| **`retry`** | Re-execute until a SQL predicate holds, with feedback in the prompt | Bounded self-healing inside the function |
| **`wards`** | Pre/post validators, blocking or advisory | Type/shape contracts at the function boundary |

Every operator is one row in `rvbbit.operators`. Edit the prompt →
cache invalidates by content hash. `EXPLAIN (SEMANTIC ON) SELECT …`
previews the dollar cost before you pay. Receipts live in
`rvbbit.receipts`. Embeddings in `rvbbit.embedding_cache`. All
queryable. All in your backup.

## Quick start

The full stack — Postgres 18 + rvbbit, the [Data Rabbit SQL
Desktop](https://rvbbit.ai/docs/data-rabbit), and the warren capability
agent — in one line (the script is short; read it first if that's your
style):

```bash
curl -fsSL https://rvbbit.ai/install.sh | bash
```

Just the database, no UI:

```bash
docker run -d --name rvbbit \
    -p 55433:5432 \
    -e POSTGRES_PASSWORD=rvbbit \
    -e POSTGRES_DB=demo \
    ghcr.io/ryrobes/rvbbit-postgres:latest

psql postgresql://postgres:rvbbit@localhost:55433/demo \
    -c 'SELECT rvbbit.rvbbit_version();'
```

Full walkthrough: [rvbbit.ai/docs/quickstart](https://rvbbit.ai/docs/quickstart).
Tarball + bare-metal install paths are in [PACKAGING.md](./PACKAGING.md).

## Rvbbit tables: the storage layer that backs all of this

The same engine that hosts the operators also rewrites scans against
`USING rvbbit` tables through a learned router — picking between
native PG, DuckDB, DataFusion, Vortex layouts, and (when the hardware
exists) an NVIDIA GPU engine, per query, transparently. The numbers,
all six systems on one desktop (8-core i7-11700K, RTX 3090 Ti,
median of 3 runs):

**ClickBench, 5M rows, 43 queries:**

| System  | geomean | sum of medians | wins (best of 43) |
|---|---:|---:|---:|
| **rvbbit** | **46ms** | **3.9s** | **22** |
| ClickHouse | 53ms | 5.0s | 12 |
| AlloyDB | 161ms | 37.4s | 9 |
| Hydra | 293ms | 46.3s | 0 |
| Citus | 672ms | 67.0s | 0 |
| Postgres 18 (heap) | 1.06s | 62.6s | 0 |

Yes — faster than ClickHouse on its own benchmark, from inside
Postgres. The router's picks for those 43 queries: GPU 16,
Duck/Vortex 12, native scan 12, DataFusion 3.

**TPC-H scale 1, 22 queries:**

| System | geomean | sum of medians | wins | failures |
|---|---:|---:|---:|---:|
| ClickHouse | 156ms | 8.8s | 8 | 0 |
| AlloyDB | 160ms | 9.9s | 6 | 1 |
| **rvbbit** | **165ms** | **9.0s** | **7** | **0** |
| Hydra | 306ms | 13.9s | 0 | 1 |
| Postgres 18 (heap) | 339ms | 15.5s | 1 | 0 |
| Citus | 776ms | 22.1s | 0 | 0 |

A statistical three-way tie with ClickHouse and AlloyDB at the top —
except rvbbit runs all 22 (Q22 kills AlloyDB and Hydra) and remains a
plain Postgres the whole time. The router even sent two TPC-H queries
to the ordinary Postgres rowstore, because that *was* the fastest
engine for them. At 50M rows on a Blackwell GPU box the ClickBench gap
widens to ~15× over AlloyDB.

[Full benchmark output →](./bench/clickbench/README.md)

## Bigfoot demo

If "ticket triage" reads as boring-money, run
[`examples/bigfoot/run_all.sh`](./examples/bigfoot/) — 5,000 BFRO
sasquatch encounter reports (the CSV auto-downloads), every semantic
primitive exercised on real data, no faked outputs. Topic clustering,
semantic diff between Texas and Washington sightings,
k-nearest-neighbor over witness narratives, the whole operator stack.
Annotated walkthrough:
[the Bigfoot Field Notebook](https://rvbbit.ai/docs/examples/bigfoot-field-notebook).
GPU-sidecar variant: [BIGFOOT-DEMO.md](./docs/BIGFOOT-DEMO.md).

## Documentation

The README is the elevator pitch. The full guide lives at
**[rvbbit.ai/docs](https://rvbbit.ai/docs)** — quickstart, semantic SQL,
acceleration, routing, GPU/GQE, MCP, receipts, all of it. Deeper
engineering references are in [`docs/`](./docs/):

- **[OPERATORS.md](./docs/OPERATORS.md)** — every flow primitive
  (steps, takes, retry, wards), every templating rule, the full
  reference
- **[COSTS_AND_RECEIPTS.md](./docs/COSTS_AND_RECEIPTS.md)** — how the
  judgment cache is keyed, how EXPLAIN SEMANTIC prices a query, what
  rows you can join receipts against
- **[EMBEDDINGS.md](./docs/EMBEDDINGS.md)** + [LOCAL_EMBEDDINGS.md](./docs/LOCAL_EMBEDDINGS.md) — the embedding cache, knn_text,
  topics, the local-CPU vs GPU sidecar story
- **[CAPABILITIES.md](./docs/CAPABILITIES.md)** — HuggingFace-backed
  specialist sidecars (DeBERTa NLI, GLiNER NER, BGE rerank, etc.) as
  registerable backends
- **[MCP.md](./docs/MCP.md)** — MCP tools as first-class steps inside
  operators
- **[KNOWLEDGE_GRAPH.md](./docs/KNOWLEDGE_GRAPH.md)** — entity
  extraction + traversal in SQL
- **[RVBBIT_ROUTING_PRODUCTION_GOAL.md](./docs/RVBBIT_ROUTING_PRODUCTION_GOAL.md)**
  — how the storage-layer router learns and decides
- **[PACKAGING.md](./PACKAGING.md)** — Docker image, release tarball,
  build-from-source
- **[TUNING.md](./docs/TUNING.md)** — Postgres + DataFusion + Parquet
  knobs the image bumps over vanilla defaults, and what to set when
  running outside Docker

## Status

PostgreSQL 18. Apache-2.0. Active development. The operator surface,
storage routing, receipts, embeddings, and MCP integration are real
and exercised by a 31-step end-to-end acceptance harness
([`make e2e-realworld`](./docs/ACCEPTANCE_HARNESS.md)). PG17 backport
is feasible but not shipped — see [PACKAGING.md](./PACKAGING.md).

---

Built on [pgrx](https://github.com/pgcentralfoundation/pgrx). Storage
layer uses [Apache Arrow](https://arrow.apache.org/) + Parquet for
columnar reads, [DuckDB](https://duckdb.org/) and
[DataFusion](https://datafusion.apache.org/) as alternate execution
engines, [fastembed](https://github.com/Anush008/fastembed-rs) for
local CPU embeddings, and [ONNX Runtime](https://onnxruntime.ai/) for
specialist models.
