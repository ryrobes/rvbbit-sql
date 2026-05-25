# rvbbit

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
| **`steps`** | A pipeline of nodes: LLM, specialist (BERT / GLiNER / embed / rerank), code, SQL, MCP-tool — any order, each reading the previous | Real workflows, not one-shot functions |
| **`takes`** | Run the pipeline N times, reduce via vote / median / evaluator / first-valid | Ensembles without orchestrator code |
| **`retry`** | Re-execute until a SQL predicate holds, with feedback in the prompt | Bounded self-healing inside the function |
| **`wards`** | Pre/post validators, blocking or advisory | Type/shape contracts at the function boundary |

Every operator is one row in `rvbbit.operators`. Edit the prompt →
cache invalidates by content hash. `EXPLAIN (SEMANTIC ON) SELECT …`
previews the dollar cost before you pay. Receipts live in
`rvbbit.receipts`. Embeddings in `rvbbit.embedding_cache`. All
queryable. All in your backup.

## Quick start

```bash
docker run -d --name rvbbit \
    -p 55433:5432 \
    -e POSTGRES_PASSWORD=rvbbit \
    -e POSTGRES_DB=demo \
    ghcr.io/ryrobes/rvbbit-postgres:latest

psql postgresql://postgres:rvbbit@localhost:55433/demo \
    -c 'SELECT rvbbit.rvbbit_version();'
```

Tarball + bare-metal install paths are in [PACKAGING.md](./PACKAGING.md).

## Rvbbit tables: the storage layer that backs all of this

The same engine that hosts the operators also rewrites scans against
`USING rvbbit` tables through a learned router — picking between
native PG, DuckDB, DataFusion, and parquet layouts on a per-query
basis, transparently. The numbers:

**ClickBench, 10M rows, geomean of 43 queries** (median of 3 runs):

| System  | geomean | sum of medians | wins (best of 43) |
|---|---:|---:|---:|
| **rvbbit** | **80ms** | **8.1s** | **31** |
| AlloyDB | 231ms | 62.6s | 12 |
| Hydra | 422ms | 73.4s | 0 |
| Citus | 1.09s | 118.1s | 0 |

**TPC-H scale 1, 22 queries, 4-way vs AlloyDB:**

| System | geomean | sum of medians | wins | failures |
|---|---:|---:|---:|---:|
| **rvbbit** | **66ms** | **1.7s** | **14** | **0** |
| AlloyDB | 139ms | 11.5s | 8 | 1 |
| Hydra | 232ms | 8.6s | 0 | 2 |
| Citus | 804ms | 46.4s | 0 | 2 |

Beats Hydra and Citus consistently across 100k–10M scales. Beats
AlloyDB on geomean at every scale tested; trades query-by-query on
small point lookups. Runs every TPC-H query the competitors crash on.

[Full benchmark output →](./bench/clickbench/README.md)

## Bigfoot demo

If "ticket triage" reads as boring-money, run
[`make bigfoot-demo`](./docs/BIGFOOT-DEMO.md) — 5,000 BFRO sasquatch
encounter reports, every semantic primitive exercised on real data,
no faked outputs. Topic clustering, semantic diff between Texas and
Washington sightings, k-nearest-neighbor over witness narratives, the
whole operator stack. ~3 minutes start to finish on a GPU box.

## Documentation

The README is the elevator pitch. Everything serious is in
[`docs/`](./docs/):

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
