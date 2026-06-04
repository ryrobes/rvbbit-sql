# Pipeline Cascades — `THEN` rowset operators + the shape-keyed SQL synthesizer

Status: **design / plan**. Date: 2026-06-04. Inspired by the old larsql
"pipeline / cascade" feature (`rabbit-lars`), reworked for an in-Postgres
(pgrx) world where queries must pass the Postgres parser first.

## What we're building

Chained, full-resultset post-processing for a SQL query — an "in-query
pipeline" that runs the base query, then pipes the whole rowset through a chain
of semantic operators, each producing a new rowset:

```sql
select * from something
then pivot('rowcounts by class and season')
then analyze('what stands out?');
```

Each stage is a semantic operator (same operator / Warren / receipts machinery
we already have) that takes a rowset and returns a rowset, possibly of a totally
different shape. Each step's resultset is persisted so you can inspect what the
data looked like at every stage.

## The hard constraint, and the two-layer architecture

Postgres lexes and parses **before any extension hook runs**. rvbbit's rewriter
is a `post_parse_analyze_hook` (it can rewrite a parsed `Query` tree in place —
that's how the DuckDB path swaps a query for `jsonb_to_recordset(...)` — but the
input must already be valid SQL). There is no `pre_parse_hook`. So bare
`SELECT … THEN …` can **never** be raw SQL sent to Postgres.

The old lars system didn't have a magic parser either — it **is** the
Postgres-wire-protocol server, so it split the string on a top-level `THEN`
(token-aware: respects strings/comments/CASE-depth/paren-depth) *before* anything
parsed it. It's middleware, not a grammar.

So the feature is two layers, and we can have both:

1. **The engine** — a table function `rvbbit.flow('select … then … then …')` that
   works in any client (psql, DataGrip, lens). The `THEN`s live inside a
   dollar-quoted string, so PG never parses them; rvbbit splits and executes.
   This is the portable substrate.
2. **The bare-`THEN` sugar** — only a client that rewrites text before sending can
   do it. The **lens SQL Desktop** does exactly what lars did: detect a top-level
   `THEN` in the editor and wrap the statement as
   `SELECT * FROM rvbbit.flow($$ …verbatim… $$)` before POSTing (injection point:
   `runSql` in `data-grid-window.tsx`, before the `fetch`). One splitter, in Rust,
   shared by all clients; the lens only needs to *detect* a top-level `THEN`.

In the SQL Desktop you type bare `then`; everywhere else you call `rvbbit.flow(...)`.

## Core abstractions

### Arity × strategy (orthogonal axes)

A stage operator declares two independent things:

- **Arity** — what it consumes: `scalar` (one value), `rowset` (a whole
  resultset). (Existing operator shapes are `scalar` / `aggregate` / `dimension`;
  `rowset` is new. The aggregate FFUNC — `agg_run_inner` passing `{{ collection }}`
  — already proves the engine can hand a whole row-collection to an operator, so
  `rowset` is a small addition.)
- **Strategy** — how it computes the output:
  - `value-llm` — the LLM directly produces the answer/transformed table. Content-
    addressed cache (existing receipts). Non-deterministic, token-heavy. Good for
    `analyze` / `summarize`.
  - `synth-sql` — the LLM produces **SQL keyed by the input's structural shape**,
    cached, then executed natively. **Shape-addressed cache (new).** Deterministic
    execution, ~K LLM calls for K shapes, auditable. The high-leverage path.
  - `code` / `python` — deterministic, no LLM (existing `code` / `python` step
    kinds).

|                 | value-llm                         | synth-sql                                              |
|-----------------|-----------------------------------|--------------------------------------------------------|
| **scalar**      | `summarize`, `classify` (today)   | `reshape(col, 'E.164 phone')` — 50M rows / ~50 shapes  |
| **rowset**      | `analyze`, `summarize`            | `pivot`, `group`, `top`, `filter`                      |

`synth-sql` is the unifying primitive: the rowset `pivot` (shape = table schema,
application = statement over `_input`) and the scalar `reshape` (shape = value
pattern, application = expression bound per row) are the **same machine** with a
different shape-function and application mode.

### `synth_sql` — the LLM as a memoized, shape-keyed SQL compiler

The standout idea from lars, generalized. The LLM is a compiler from
`(intent + structural shape) → SQL code`, invoked once per distinct shape; the
compiled code is cached and executed in-engine.

- **Shape function**: input → structural fingerprint. Default for strings:
  collapse digit-runs→`d`, letter-runs→`a`, keep punctuation (`(303) 555-1234`
  → `(ddd) ddd-dddd`); for rowsets: ordered `(column,type)` list + a small profile
  (ndv buckets, distinct values for low-cardinality string columns — exactly what
  lars's `table_sql_execute` mode fed the model). **Pluggable** (an operator may
  supply a custom shape SQL expression). The granularity is the key correctness/
  hit-rate knob.
- **Cache** (`rvbbit.synth_cache`): keyed by `(operator, shape_fingerprint,
  prompt_hash)` → `generated_sql` (+ validation status, sample, created_at,
  pinned). On **miss**: call the LLM (reusing `invoke_with_cache` → a receipt),
  validate the SQL on a **sample of that shape's real values** with an
  error-feedback retry (lars did 3 attempts, feeding the engine error back into the
  prompt), store it. On **hit**: fetch the cached SQL and execute (bind-and-run) —
  no LLM call.
- **Application**:
  - rowset → register the rowset as `_input` (temp table or `WITH … AS MATERIALIZED`
    CTE) and run the generated statement via SPI (the `sql` step kind /
    `run_step_sql` already executes SQL; generalize it from `LIMIT 1` to a rowset).
  - scalar → the generated SQL is an *expression* over the column; apply it across
    the relation in one set-based statement (one expression per shape, `CASE` on
    shape or a join to the per-shape snippet).
- **Sandbox**: synthesized SQL runs read-only — no DDL/DML/side-effect functions,
  scoped to `_input` / the target column. (lars had this safety layer.)
- **Auditability**: `synth_cache` is data. You can read, edit, pin, and freeze the
  ~K snippets; downstream 50M rows flow through audited deterministic SQL.
  "LLM transforms your data" → "LLM writes K reviewable SQL snippets once."

### Step persistence (debugging / Bret-Victor observability)

- Each `flow()` run gets a `run_id`; each stage's rowset lands in
  `rvbbit.flow_steps(run_id uuid, step_idx int, stage text, spec text, rows jsonb,
  n_rows int, created_at timestamptz)`.
- Inspection SRFs: `rvbbit.flow_steps(run_id)` (list stages) /
  `rvbbit.flow_step(run_id, idx)` (a step's rows).
- Receipts already capture the **LLM** side per stage (cost / latency / the
  generated SQL), grouped by `query_id`; `flow_steps` adds the **data** side.
  Together: see the data and the operation at every step.
- TTL: lars used GC-by-reference + a row cap, no time-TTL. Ours: a `created_at` +
  `reap_flow_steps(interval)` (mirror `reap_stale_training_runs`), plus "don't
  store > N rows — keep a sample + count." Persist best-effort so it never taxes
  pipeline latency.

## Output shape & composability

- Stages change shape arbitrarily → `rvbbit.flow(spec) RETURNS SETOF jsonb` (one
  jsonb object per row). The lens DataGrid renders jsonb rows directly; typed
  downstream consumers use `jsonb_to_recordset(...) AS t(col type, …)` or an
  optional `flow_typed(spec, coldef)` variant.
- `rvbbit.flow(...)` is a table function → composes anywhere in a `FROM`. Bare
  `THEN` is top-level-statement-only (like lars).

## Phased plan (each phase independently verifiable; treat as a /goal)

**Phase 0 — scaffolding.**
- `rvbbit.flow_steps` table + `rvbbit.synth_cache` table.
- Port lars's token-aware `THEN` splitter to Rust (strings / comments / CASE-depth
  / paren-depth; case-insensitive; multiple `THEN`s; function-call vs infix-string
  args). Unit tests for false-split avoidance (`CASE … THEN … END`, `THEN` in a
  string, subquery parens).
- Verify: splitter unit tests pass.

**Phase 1 — the engine + first value-mode stage.**
- `rvbbit.flow(spec text) RETURNS SETOF jsonb` (Rust SRF, `SetOfIterator<JsonB>`):
  split → run head via SPI (`SELECT to_jsonb(q) FROM (<head>) q`) → for each stage,
  dispatch a rowset operator → persist to `flow_steps` → return final rowset.
- New `shape='rowset'` operator + `_exec_op_rowset(op_name, rows jsonb, opts)`
  entry point reusing `invoke_with_cache` → receipts. Seed `analyze` (value-llm:
  table-as-JSON in the prompt, returns a new table).
- Verify (psql): `SELECT * FROM rvbbit.flow($$ select … then analyze('…') $$)`
  returns rows; receipts + flow_steps populated.

**Phase 2 — the `synth_sql` primitive + structural rowset stages (the core). LANDED.**
- `synth_sql` core (`src/synth.rs`): shape fingerprint (schema cols+types + sorted
  distinct-value sets of low-cardinality text cols), `synth_cache` lookup, model
  synth on miss (→ receipt via invoke_with_cache), execute the generated statement
  over the rowset registered as `_input` (jsonb_to_recordset) — isolated by
  `PgTryBuilder` so a bad generation fails the stage, not the surrounding query.
- Operators marked by `parser='sql'` (the synth strategy) vs `'json'` (value mode);
  seeded `pivot` / `group` / `top` / `filter` (synth-sql), `analyze` / `enrich` (LLM value-mode), and `sample` builtin.
- `rvbbit.flow_shape(rows)` (inspect the fingerprint) + `rvbbit.synth_put(op,
  prompt, sample_rows, sql)` (author/pin a snippet by hand — also the audit knob).
- Verified live (`cargo pgrx test`): fingerprint determinism/order-independence/
  schema-sensitivity; the synth_put → matching pipeline stage executes the cached
  SQL natively with no model call. (LLM-gen miss path is live-only.)
- **Phase 2.5 LANDED**: `run_synth_sql_op` now validates each generated SQL on a
  sample (first 200 rows) and feeds the Postgres error back to the model for up to
  3 attempts before caching; the synth seed prompt carries `{{ _last_sql_error }}`.
  Bad cached SQL fails the stage gracefully (PgTry subtransaction) without poisoning
  the query. (TODO: `synth_cache` admin surface — list/edit/unpin.)

**Phase 3 — lens ergonomics. LANDED** (rvbbit-lens `6149fe7`).
- `lib/sql/then-rewrite.ts`: `hasTopLevelThen()` (token-aware detector mirroring the
  Rust splitter — leaves `CASE…THEN`/strings/comments untouched) + `wrapFlow()` +
  `expandFlowResult()` (flow's single jsonb column → real grid columns).
- `data-grid-window`: `runSql` detects a top-level `THEN` and wraps as
  `SELECT * FROM rvbbit.flow($$…$$)` before sending; expands the result; tracks
  `isPipelineRun`. New **Steps** tab (`FlowStepsView`) reads `rvbbit.flow_steps` and
  shows each stage's rowset (step chips + table).
- Verified live (Playwright, against the 1.1.0 DB): bare
  `select … then pivot('counts by class')` runs through `rvbbit.flow`, the grid shows
  the synthesized crosstab, and the Steps inspector shows base(4) → pivot(1: a=3,b=1).
- TODO: show the generated SQL + receipt cost/latency per step in the inspector;
  tie the Steps query to the exact run_id (currently "latest run" — fine for the
  single-user desktop).

**Phase 4 — persistence/observability polish. LANDED.**
- `flow_steps` gained a `generated_sql` column — synth stages record the SQL the
  model authored; `persist_step` caps the stored rowset at 500 (a sample) while
  `n_rows` keeps the true count. `flow_step(run_id, idx)` SRF + `reap_flow_steps`
  already shipped (Phase 0).
- Lens Steps inspector shows the generated SQL per stage + a "showing first N of M"
  note for capped steps (rvbbit-lens). Verified through the lens API on a 1.1.1 DB.
- e2e `pipeline/flow_cascade` covers the deterministic builtins + the synth cache-hit
  pivot. (TODO: per-step receipt cost/latency; tie the Steps query to the exact
  run_id rather than "latest run".)

**Phase 5 (later — your shape-keyed scalar idea, near-free on the Phase-2 core).**
- Scalar `synth-sql` operators reusing the same primitive: `reshape(col, intent)`
  / `format_as(col, intent)`. Shape function = value-pattern; application =
  expression bound per row, one snippet per shape across the whole relation.
- The 50M-rows / ~50-shapes phone-format case: ~50 LLM calls to author the
  snippets, then deterministic SQL. Add `synth_cache` admin (list / pin / edit /
  freeze per-shape snippets).

## Risks / open decisions

- **Shape-function granularity** — correctness vs. hit-rate; default regex-class
  collapse, pluggable, always validate-on-sample.
- **SQL sandbox** — read-only, no DDL/DML/side-effects; scoped to `_input` /
  target column.
- **Dynamic shape** → `SETOF jsonb` default; `flow_typed` for typed output.
- **`synth_cache` staleness** — if a shape's semantics drift (e.g. a column's
  distinct values change), the cached SQL may go stale; tie the key to the profile,
  and allow manual invalidation / re-synth.
- **Composability inside `WITH`** — lars materialized CTE-embedded pipelines; we
  expose `flow()` as a FROM-clause table function instead (cleaner in Postgres).

## What already exists (so the new work is focused)

Dispatch (`invoke_with_cache`), content-addressed cache + `rvbbit.receipts`,
multi-step operators with `sql` / `python` / `code` step kinds, SPI→jsonb idiom
(`SELECT to_jsonb(q) FROM (%s) q`), `jsonb_to_recordset` expansion, SRF patterns
(`TableIterator` / `SetOfIterator` / PL/pgSQL `RETURNS SETOF jsonb`), the lens
query path (`runSql` → `/api/db/query` → `executeQuery`), and the aggregate FFUNC
proving whole-collection dispatch. New: the `rowset` arity + `_exec_op_rowset`,
the `flow()` SRF + `THEN` splitter, the `synth_sql` primitive + `synth_cache`, the
`flow_steps` store + lens detect-and-wrap.
