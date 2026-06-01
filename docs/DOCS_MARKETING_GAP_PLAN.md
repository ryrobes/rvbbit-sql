# RVBBIT Docs And Marketing Gap Plan

Date: 2026-06-01
Scope: source docs in `./docs/`, current docs site in `../rvbbit-docs`, and
public positioning patterns from Snowflake and Databricks.

This is a content plan, not an implementation spec. Its job is to make sure the
public site explains the parts of RVBBIT that are novel, useful, and defensible
without overstating pre-release or roadmap-only work.

## Executive Read

The current docs site explains the main shape: Semantic SQL, Cascades, MCP,
Beaverdam, routing, Warren, time travel, and operations. That is a good
foundation, but it still undersells the features that make RVBBIT look less
like "a fast table experiment" and more like a SQL-native AI system:

- task-specific semantic functions and examples from the demo corpus;
- local/default embeddings and replaceable embedding backends;
- knowledge graph as durable semantic memory with evidence;
- receipts, cost ledger, provider catalogs, and diagnostics;
- capability packs as a portable model/runtime catalog;
- SQL-first governance and observability;
- the contrast with semantic BI products such as Snowflake Cortex Analyst and
  Databricks Genie.

The landing page should stay focused on product hooks. The docs should then
give a clear route into how each hook works from SQL.

## Competitive Context

Snowflake is positioning around these ideas:

- **Semantic Views / Cortex Analyst**: business concepts, metrics, dimensions,
  and relationships stored as database metadata and usable by AI and SQL.
- **Cortex AISQL**: model-backed SQL functions such as completion,
  classification, summarization, translation, embeddings, similarity, and
  aggregation.
- **Cortex Search**: managed hybrid vector/keyword retrieval with reranking.
- **AI Observability and cost management**: tracing, evaluations, latency,
  usage, and cost surfaces.
- **Managed MCP / agents**: tool and agent runtime surfaces beside the database.

Databricks is positioning around these ideas:

- **AI/BI Genie and business semantics**: domain-specific natural language
  spaces curated with tables, examples, instructions, metrics, and trusted
  assets.
- **Databricks AI Functions**: task-specific SQL functions plus `ai_query` for
  general model serving.
- **Vector Search**: SQL-accessible vector and hybrid search over governed
  indexes.
- **Unity Catalog / AI Gateway style governance**: access control, usage
  tracking, and centrally managed semantic context.

Reference pages checked:

- Snowflake Semantic Views:
  <https://docs.snowflake.com/en/user-guide/views-semantic/overview>
- Snowflake Cortex Search:
  <https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview>
- Snowflake Cortex AI Functions:
  <https://docs.snowflake.com/en/user-guide/snowflake-cortex/aisql>
- Snowflake AI Observability:
  <https://docs.snowflake.com/en/user-guide/snowflake-cortex/ai-observability>
- Snowflake Cortex cost management:
  <https://docs.snowflake.com/en/user-guide/snowflake-cortex/ai-func-cost-management>
- Databricks AI/BI Genie:
  <https://docs.databricks.com/aws/en/genie>
- Databricks AI Functions:
  <https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-functions-builtin>
- Databricks `ai_query`:
  <https://docs.databricks.com/aws/en/large-language-models/ai-query>
- Databricks `vector_search`:
  <https://docs.databricks.com/aws/en/sql/language-manual/functions/vector_search>

RVBBIT should not try to sound like an enterprise BI suite. The stronger angle
is narrower and more interesting:

> RVBBIT makes Postgres itself a SQL-native AI runtime: model calls, tool calls,
> semantic workflows, receipts, cost accounting, local models, KG memory, and
> optional poly-engine storage all remain queryable from SQL.

## Current Coverage

Covered reasonably well on the docs site:

| Area | Current site page |
| --- | --- |
| High-level product shape | `content/docs/overview.md` |
| Quick SQL path | `content/docs/quickstart.md` |
| Operators / Cascades | `content/docs/semantic-sql.md`, `content/docs/cascades.md` |
| MCP | `content/docs/mcp.md` |
| Beaverdam | `content/docs/beaverdam.md`, `content/docs/time-travel.md` |
| Routing / training | `content/docs/routing-training.md` |
| Warren | `content/docs/warren.md` |
| Duck/Vortex worker | `content/docs/duck-vortex-worker.md` |
| Benchmarks | `/benchmarks` |

Source docs that are not currently referenced by the docs site:

- `BIGFOOT-DEMO.md`
- `LARS_SEMANTIC_OPERATOR_AUDIT.md`
- `LOCAL_EMBEDDINGS.md`
- `PHASE_1_FOLLOWUPS.md`
- `PROVIDER_CATALOGS.md`
- `witness-system.md`

Several source docs are referenced but compressed too aggressively:

- `COSTS_AND_RECEIPTS.md`
- `COSTS_UI_CONTRACT.md`
- `DIAGNOSTICS.md`
- `EMBEDDINGS.md`
- `KNOWLEDGE_GRAPH.md`
- `CAPABILITIES.md`
- `CALLABLE_SURFACES.md`
- `TUNING.md`

## Highest-Value Missing Public Pages

### 1. Semantic Function Library

Status: feature exists in source docs and demos.

Why it matters:

Snowflake and Databricks expose AI features as obvious SQL functions. RVBBIT can
do this too, but the current site mostly explains the operator machinery rather
than the usable surface. The first-time reader needs to see examples like
classification, extraction, sentiment, semantic similarity, dedupe, topic
clustering, novelty detection, and explain/cost preview.

Source material:

- `docs/BIGFOOT-DEMO.md`
- `docs/EMBEDDINGS.md`
- `docs/LARS_SEMANTIC_OPERATOR_AUDIT.md`
- `docs/OPERATORS.md`

Proposed docs page:

- `content/docs/semantic-functions.md`

Landing page treatment:

- Add a section after Cascades or MCP: "AI functions, but editable and
  auditable."
- Show 3 short examples:
  - `semantic_case(...)`
  - `triples_rows(...)` or `extract`
  - `explain_semantic(...)` / receipt query

Content outline:

- Task-specific functions:
  - classify/sentiment/relevance/extract/rerank/similarity;
  - embeddings-based functions that do not need an LLM call;
  - LLM-backed operators when richer reasoning is needed.
- Difference from Databricks/Snowflake:
  - built-in-like SQL ergonomics;
  - user-editable operators;
  - receipts and cost policies;
  - local/self-hosted backends as normal providers.
- Good examples:
  - support triage;
  - entity extraction into KG;
  - semantic dedupe;
  - novelty detection over two SQL subqueries.

### 2. Embeddings And Retrieval

Status: feature exists; underexplained publicly.

Why it matters:

Snowflake has Cortex Search and Databricks has Vector Search. RVBBIT has local
embeddings, `knn_text`, table materialization, cache semantics, Lance-backed KG
resolution, and RAG patterns, but the public site currently treats embeddings as
a subsection of Semantic SQL.

Source material:

- `docs/EMBEDDINGS.md`
- `docs/LOCAL_EMBEDDINGS.md`
- `docs/KNOWLEDGE_GRAPH.md`
- `docs/LAKEHOUSE.md`

Proposed docs page:

- `content/docs/retrieval.md`

Landing page treatment:

- Add a compact "Retrieval without leaving Postgres" block.
- Emphasize: local CPU embedding default, replaceable backend, cache/materialize
  path, SQL joins, KG expansion, Lance where useful.

Content outline:

- `rvbbit.embed`, `similarity`, `knn_text`, `materialize_embeddings`.
- Cache semantics and when to purge.
- Replace the default `embed` backend with OpenAI-compatible or local model.
- RAG pattern: SQL filter -> vector retrieval -> KG context -> operator.
- What is not yet the same as a fully managed hybrid search service:
  - no public "CREATE SEARCH INDEX" abstraction yet;
  - ANN/hybrid indexing is evolving through Lance/Beaverdam/KG paths.

### 3. Knowledge Graph And Evidence Memory

Status: feature exists; page content is too short relative to novelty.

Why it matters:

This is one of the strongest differentiators. Snowflake and Databricks focus on
semantic business models for NL-to-SQL and BI. RVBBIT also has a durable,
SQL-queryable semantic memory layer with nodes, aliases, edges, evidence,
merge review, triple extraction, and RAG context preview.

Source material:

- `docs/KNOWLEDGE_GRAPH.md`
- `docs/EMBEDDINGS.md`
- `docs/COSTS_AND_RECEIPTS.md`

Proposed docs page:

- Expand `content/docs/semantic-sql.md` only slightly.
- Add `content/docs/knowledge-graph.md` as a real page.

Landing page treatment:

- Add one concise tile in "What it is" or a later "semantic memory" section:
  "Knowledge graph with evidence, not a black-box vector store."

Content outline:

- Basic node/edge/evidence SQL.
- Graph namespaces via `graph_id`.
- `triples_rows` -> `kg_ingest_triples` / `kg_ingest_table`.
- Evidence drawer and query provenance.
- Merge review and alias resolution.
- RAG context preview.
- Lance acceleration for large node sets.

### 4. Receipts, Costs, And AI Observability

Status: feature exists; under-marketed and only partially documented.

Why it matters:

This is table stakes against Snowflake AI Observability and Databricks usage
governance, and it is also a major trust story. "AI in SQL" sounds dangerous
unless the site immediately explains receipts, sub-call traces, token counts,
latency, costs, provider settlement, and policy controls.

Source material:

- `docs/COSTS_AND_RECEIPTS.md`
- `docs/COSTS_UI_CONTRACT.md`
- `docs/OPERATORS.md`
- `docs/MCP.md`

Proposed docs page:

- `content/docs/receipts-costs.md`

Landing page treatment:

- Add a line near Cascades: "Every operator call can leave a receipt."
- Add a small SQL example:
  - `SELECT * FROM rvbbit.receipts ORDER BY invocation_at DESC LIMIT 10;`
  - `SELECT rvbbit.cost_audit_summary();`

Content outline:

- Receipt = one semantic operator invocation.
- `sub_calls` = model/specialist/MCP trace.
- `query_id` ties receipts, KG evidence, MCP calls, and cost events.
- Cost ledger states: pending, settled, estimated, free, uncosted, error.
- Policies by model/backend/MCP tool.
- Maintenance path: `rvbbit.maintain()`.
- UI/dashboard recipes.

### 5. Provider Catalogs And Model Admin

Status: feature exists; omitted from the public site.

Why it matters:

The current site says "models" but does not explain how model choice,
availability, default provider, self-hosted models, rates, and credential
presence are managed. Snowflake/Databricks both benefit from looking managed
and governed. RVBBIT needs to show it has SQL-visible model administration.

Source material:

- `docs/PROVIDER_CATALOGS.md`
- `docs/DIAGNOSTICS.md`
- `docs/COSTS_AND_RECEIPTS.md`
- `docs/EMBEDDINGS.md`

Proposed docs page:

- `content/docs/providers.md`

Landing page treatment:

- Not a full section unless space allows. Mention in "Operations" or
  "Postgres contract": "Provider catalogs and cost policies are visible in SQL."

Content outline:

- `provider_catalog`, `provider_models`, `model_rate_cards`,
  `provider_model_catalog`.
- `refresh_provider_catalogs`, `provider_catalog_summary`, `maintain`.
- `register_self_hosted_model`.
- `set_default_provider`.
- credential presence checks with `env_present`.
- model rate confidence: provider, seeded, manual, actual, unknown.

### 6. Capability Packs And Warren Runtime Catalog

Status: feature exists; Warren page is too small.

Why it matters:

This is the "platform" story that keeps RVBBIT from looking like a pile of SQL
functions. It also explains how local/specialist models become managed runtime
surfaces. Databricks has Model Serving and AI Functions; Snowflake has managed
Cortex services. RVBBIT has portable capability packs and Warren deployment.

Source material:

- `docs/CAPABILITIES.md`
- `docs/WARREN.md`
- `docs/WARREN_UI_CONTRACT.md`
- `docs/LARS_SEMANTIC_OPERATOR_AUDIT.md`

Proposed docs page:

- Expand `content/docs/warren.md`.
- Add `content/docs/capability-packs.md` if the expanded page gets too long.

Landing page treatment:

- Add one line to Warren: "Install model/runtime packs from SQL."
- Consider a small list of example packs:
  - GLiNER extraction;
  - DeBERTa classification;
  - local embeddings;
  - Python runtime;
  - MCP gateway.

Content outline:

- What a capability pack contains.
- SQL deployment path.
- Curated V1 packs.
- Backend runtime contract vs execution runtime contract.
- Smoke tests and UI install flow.
- Safety: users should not hand-build server venvs.

### 7. Diagnostics And Production Readiness

Status: feature exists; operations page only hints at it.

Why it matters:

"Try this extension" should have a clear health and setup story. RVBBIT has
`doctor`, `provider_doctor`, `env_present`, source-visible maintenance, and e2e
harnesses. Put this in front of users before they hit sharp edges.

Source material:

- `docs/DIAGNOSTICS.md`
- `docs/ACCEPTANCE_HARNESS.md`
- `docs/RVBBIT_PRODUCTION_SHAPE.md`
- `docs/TUNING.md`

Proposed docs updates:

- Expand `content/docs/operations.md`.
- Add a "Doctor" section to `content/docs/quickstart.md`.

Landing page treatment:

- Probably no top-level section. Maybe one sentence in release/operations:
  "Setup and provider health are SQL-checkable."

Content outline:

- `rvbbit.doctor(false)` for cheap checks.
- `rvbbit.doctor(true)` and `provider_doctor(true)` for live probes.
- Provider setup checks.
- Maintenance jobs and `pg_cron` optionality.
- E2E harness as release confidence, not user-facing setup.

### 8. Security, Permissions, And Secrets

Status: spread across docs; not cohesive.

Why it matters:

AI-in-database raises immediate questions: who can call models, see prompts,
see receipts, read secrets, run sidecars, register MCP servers, and query
external tools? The site needs a single security page before v1.

Source material:

- `docs/MCP.md`
- `docs/DUCK_SIDECAR.md`
- `docs/WARREN.md`
- `docs/WARREN_UI_CONTRACT.md`
- `docs/COSTS_UI_CONTRACT.md`
- `docs/DIAGNOSTICS.md`

Proposed docs page:

- `content/docs/security.md`

Landing page treatment:

- One sentence only: "Secrets stay in environment/catalog references; calls and
  costs are observable in SQL." Avoid implying more access-control polish than
  exists.

Content outline:

- Environment variable references such as `${GITHUB_TOKEN}`.
- `env_present` never exposes secret values.
- MCP gateway and sidecar process boundaries.
- Heap remains source of truth.
- Audit visibility caveats.
- Roles/grants still need a clear v1 public contract if not already finalized.

### 9. Examples And Demo Narratives

Status: rich demo exists; public site has no examples section.

Why it matters:

The BFRO demo is odd, but it proves range: semantic retrieval, clustering,
outliers, entity extraction, NLI classification, approximate distinct,
semantic materialized projections, predicate bitmaps, explain/cost preview, and
shared cache. It should be transformed into cleaner business-flavored examples.

Source material:

- `docs/BIGFOOT-DEMO.md`
- `docs/LARS_SEMANTIC_OPERATOR_AUDIT.md`

Proposed docs page:

- `content/docs/examples.md`

Landing page treatment:

- Add an "Example paths" strip:
  - support triage;
  - compliance extraction;
  - customer risk memory;
  - repo intelligence via MCP.

Content outline:

- Rewrite demo examples around business/support/logistics data.
- Keep BFRO as an optional fun demo, not the primary public story.
- Each example should show:
  - table shape;
  - SQL call;
  - result shape;
  - receipt/observability query.

### 10. Product Boundaries And Comparison

Status: implicit; not documented.

Why it matters:

RVBBIT overlaps with data warehouses, vector DBs, AI function systems,
workflow engines, BI semantic layers, and Postgres extensions. A clear "what
it is / is not" page reduces confusion and prevents overclaiming.

Proposed docs page:

- `content/docs/positioning.md`

Landing page treatment:

- Not a separate section. Use the copy to sharpen the core message:
  "Use Postgres as the control plane for semantic work."

Content outline:

- RVBBIT is:
  - a Postgres extension;
  - SQL-native semantic operator runtime;
  - tool/model/cost/receipt catalog;
  - optional storage acceleration layer.
- RVBBIT is not:
  - a hosted BI product;
  - a replacement for all warehouse workloads;
  - a generic autonomous agent loop;
  - a vector DB only;
  - a required new table format for semantic SQL.
- Where Snowflake/Databricks are stronger:
  - managed enterprise governance;
  - hosted BI/NLQ workflows;
  - large integrated platform surface.
- Where RVBBIT is differentiated:
  - works inside ordinary Postgres;
  - SQL-callable multi-step operators;
  - MCP/tool calls as relational sources;
  - receipts/costs in local tables;
  - local/self-hosted models as first-class;
  - heap fallback plus optional acceleration.

## Landing Page Revision Plan

Do this in small slices so the page does not become a wall of feature claims.

### Slice A: Rebalance Current Sections

Current landing order:

1. Hero
2. Cascades
3. MCP
4. What it is
5. Benchmarks
6. Architecture
7. Docs
8. Release band

Recommended order:

1. Hero
2. Cascades
3. Semantic Functions
4. MCP
5. Knowledge / Retrieval
6. Receipts / Observability
7. Beaverdam benchmark snapshot
8. Architecture
9. Docs

Reasoning:

- Cascades and MCP are good hooks.
- Semantic functions and receipts explain immediate utility and trust.
- Beaverdam should stay important, but not dominate the semantic product story.

### Slice B: Add Four High-Signal SQL Examples

Keep these tiny:

```sql
SELECT ticket_id, rvbbit.review_risk(body, tier)
FROM support_tickets;
```

```sql
SELECT *
FROM rvbbit.knn_text('docs'::regclass, 'body', 'renewal risk', 10);
```

```sql
SELECT *
FROM rvbbit.triples_rows('Acme reported late shipments.', 'customer risk');
```

```sql
SELECT rvbbit.cost_audit_summary();
```

### Slice C: Add "Why This Is Different"

Use plain language:

- Task-specific AI functions are useful, but RVBBIT lets users define and audit
  their own.
- NL-to-SQL semantic layers are useful, but RVBBIT keeps model/tool workflows
  callable from SQL itself.
- Vector search is useful, but RVBBIT combines embeddings, graph evidence,
  local models, and Postgres joins.
- Fast columnar paths are useful, but they remain optional because heap is the
  source of truth.

## Suggested Execution Order

### Phase 1: Trust And Immediate Usefulness

1. Add `semantic-functions.md`.
2. Add `receipts-costs.md`.
3. Add landing sections for semantic functions and receipts.
4. Add quickstart "doctor" checks.

This answers: "What can I do today, and how do I know it is not a black box?"

### Phase 2: Semantic Memory And Retrieval

1. Add `retrieval.md`.
2. Add `knowledge-graph.md`.
3. Add a landing "semantic memory" section.
4. Add one RAG example that uses SQL + `knn_text` + `kg_context`.

This answers: "How is RVBBIT more than one-off model calls?"

### Phase 3: Platform Shape

1. Add `providers.md`.
2. Expand Warren and/or add `capability-packs.md`.
3. Add diagnostics/operations examples.
4. Add security page.

This answers: "How does this become an installable, operable system?"

### Phase 4: Positioning And Examples

1. Add `examples.md`.
2. Add `positioning.md`.
3. Convert BFRO demo into a polished public example or keep it as a fun demo.
4. Add a comparison narrative that does not attack Snowflake/Databricks.

This answers: "Where does this sit in the market?"

## Copy Rules

Use these rules while writing the public docs:

- Lead with SQL examples.
- Avoid saying "agent" unless the feature genuinely behaves like an agent.
- Do not claim a hosted/managed enterprise feature where RVBBIT is a local
  extension plus sidecars.
- Say "optional Beaverdam" consistently; semantic SQL must not sound dependent
  on the storage layer.
- Say "receipts" and "costs" early when discussing model calls.
- Treat the witness system as roadmap/lab material only until implemented.
- Treat unified `rvbbit.callables` as future unless the SQL view exists.
- Prefer "SQL-native workflows" over vague "AI database" language.

## Things To Avoid Marketing Before They Exist

- Snowflake-style semantic views / BI metric layer, unless implemented as real
  SQL catalog objects.
- Databricks Genie-style natural-language BI spaces, unless there is a real
  user-facing flow with evaluation and permissions.
- Unified callable catalog, unless `rvbbit.callables` exists.
- Witness/steward/council system, except as a roadmap page clearly marked
  experimental/pre-build.
- Fully managed hybrid search, unless there is a supported index lifecycle and
  query contract.

## Definition Of Done For This Content Track

- Landing page explains Cascades, semantic functions, MCP, retrieval/KG,
  receipts/costs, and Beaverdam without burying semantic SQL under storage.
- Docs nav has clear pages for:
  - Semantic Functions
  - Retrieval
  - Knowledge Graph
  - Receipts And Costs
  - Providers
  - Security
  - Examples
- Quickstart includes:
  - default provider / embedding checks;
  - `doctor(false)`;
  - one receipt/cost query after a semantic call.
- Every public page has source docs listed.
- No page claims roadmap-only features as current behavior.
- `npm run lint`, `npm run check:sources`, and `npm run build` pass in
  `../rvbbit-docs`.
