# Semantic operator execution — batching & concurrency plan

> Status: **PLAN — not yet built** (written 2026-06-05). Read-only diagnosis + phased
> design. Companion to the accelerator/freshness docs. The headline: most of the machinery
> already exists; the bottleneck is **coverage gaps**, not a missing engine.

## The problem (verified)

Semantic scalar operators (`rvbbit.summarize`, `rvbbit.about`, …) run **per-row, tuple-at-
a-time**, against backends with far more headroom than the path ever uses. Live evidence
from one real query (`cost_events.created_at`, the reliable wall clock — `receipts.invocation_at`
is batch-flushed and useless): **197 `summarize` calls, ~1.35 s each, 267 s of backend work
done in ~115 s wall = only ~2.3× concurrency** — i.e. just incidental Postgres parallel
workers, not rvbbit. The openrouter backend allows `max_concurrent=8`; the reranker behind
`about()` can batch hundreds per call. Neither lever was used. (Connection pooling is fine —
a shared pooled `reqwest` client — so it's purely per-row dispatch.)

The user's exact symptom: `SELECT rvbbit.summarize(col) FROM t` over 500 rows "took a bit
but finished"; adding `WHERE rvbbit.about(...) > 0.7` **timed out**. The plan below explains
that asymmetry precisely and closes it.

## What already exists (do not rebuild)

The collect→batch→scatter design is **already implemented** and on the hot path for one case:

- **`rvbbit.prewarm_operator(op_name, sql, max_concurrent)`** (`prewarm.rs:177`) — collects
  the operator's inputs over a query, dedups by content hash, dispatches either
  **batched specialist** calls (`dispatch_batched_specialist`, `prewarm.rs:252` — chunks by
  `batch_size`, `Semaphore(spec.max_concurrent)`, one pool task per chunk) or **concurrent
  per-row** calls (`dispatch_per_row`, `prewarm.rs:227` — one pool task per row, for LLM /
  multi-step ops), then writes results to **L1 + L2** (`rvbbit.receipts`).
- **The cache key matches exactly.** `prewarm.rs::build_hash` (655) reproduces
  `operators.rs::input_hash` (735) byte-for-byte: `blake3(op.name + op.model + model_override
  + inputs_json + prompt_seed)`. So after a prewarm, the per-row scalar calls resolve from
  L2 (`invoke_with_cache` → `lookup_cached`, `operators.rs:623/759`) **without provider calls**.
- **An implicit prewarm rewrite already fires automatically.** `try_implicit_prewarm_rule`
  (`rewriter.rs:8981`), installed in the post-parse-analyze hook, detects semantic operator
  calls in a simple single-table SELECT's **target list**, finds the input column + driving
  relation, and emits a `prewarm_operator` pass **before** the per-row evaluation. This is
  why `SELECT summarize(col) FROM t` already batches/fans-out and finishes.

### The gaps (this is the whole plan)

Grounded in `rewriter.rs:8981–9050` and `prewarm.rs`:

1. **WHERE-clause operators are not prewarmed — and worse, they disable prewarm for the
   *entire* query.** `rewriter.rs:9018–9027`: if any semantic op appears in `jointree.quals`,
   the rule logs *"semantic operators in WHERE are not prewarmed yet"* and **returns** — so
   the SELECT-list ops lose their prewarm too. **This is the `about()`-in-WHERE timeout.**
2. **ORDER BY operators skip prewarm** (`rewriter.rs:9029`).
3. **The rule only handles the simple shape** (`rewriter.rs:8985–9012`): single-table,
   no GROUP BY / HAVING / DISTINCT / CTE / aggregates / window / sublinks / set-ops / joins.
   Anything more complex gets no implicit prewarm.
4. **Multi-step ops can't use the batch path even when their expensive step is batchable.**
   `dispatch_batched_specialist` only fires for **single-step** specialist ops
   (`single_specialist_name`, `prewarm.rs:399`). `about()` is **two steps** (a
   `rerank_bge_m3` *specialist* + a trivial `json_get` *code* step), so it falls to
   `dispatch_per_row` — concurrent fan-out, **but not true reranker batching** (a reranker
   could score 500 rows in a handful of calls).
5. **Whole-batch re-execute on partial cache miss** (`prewarm.rs:340`) — a chunk with any
   miss re-sends the whole chunk; the MV flow already filters cached-first, the operator flow
   doesn't.
6. **Leader-only for `sql`/`mcp` step ops** (`prewarm.rs:65`) — SPI is illegal on pool
   threads, so those run sequentially on the leader (loses concurrency). Not relevant to
   summarize/about, but a known ceiling.

## Shape × backend strategy (the user's nuance, made concrete)

Only **`shape='scalar'`** has the per-row problem. The others are already "one backend call"
and need no change — and the implicit rule already, correctly, **bails on `hasAggs`**:

| shape | execution today | action |
|---|---|---|
| **scalar** | per-row `invoke_with_cache` | **batch or fan-out (this plan)** |
| **aggregate** | SFUNC accumulates → FFUNC one call/group (`operators.rs:452/471`) | none |
| **dimension** | one call → splits rows | none |
| **rowset** | one call sees whole resultset | none |

Within scalar, the lever depends on the backend:

| operator | backend | batchable? | lever |
|---|---|---|---|
| `summarize` | LLM chat (gpt-mini) — no batch API | no | **concurrent fan-out** (`dispatch_per_row` at pool size) |
| `about` | `rerank_bge_m3` specialist (`client_batches=true`) **wrapped in a 2-step op** | yes, but blocked by multi-step | **fan-out now; batch the rerank step later** |
| embeddings / single-step rerank / classify | specialist (`client_batches=true`) | yes | **true batch** (`dispatch_batched_specialist`) |

This matches the user's framing: scalars run as soon as their inputs are materialized; we
**lean on Postgres' planner for filter ordering** and only change *how the scan's inputs are
serviced* — prewarm batches/fans-out over the rows the scan will touch, then the planner's
per-row filter hits cache.

## Phased plan (tests at each stage)

Every phase is measured with the **stub transport** (`specialists/stub.rs` — deterministic,
offline, no live LLM) for deterministic pg_tests, plus a `cost_events`-based wall-clock A/B
for realistic runs. Speedups below assume the live timings above.

### Phase 0 — measurement harness (no behavior change)
- A pg_test helper that runs an operator over N rows with the **stub transport** under two
  configs — serial (`RVBBIT_POOL_SIZE=1`, `batch_size=1`) vs concurrent/batched — and asserts
  wall-clock from `cost_events` (set `rvbbit.query_id` first; `min/max(created_at)` span;
  effective concurrency = `sum(latency_ms)/wall_ms`). Gives every later phase a green/red bar.
- Files: new `freshness`-style test module or `pipeline.rs` tests; `costs.rs` views
  (`query_costs`, `receipt_costs`).
- **Exit:** a deterministic test that *demonstrates* serial vs concurrent on the stub.

### Phase 1 — prewarm WHERE-clause operators *(the `about()` fix; biggest win)*
- In `try_implicit_prewarm_rule`, instead of bailing at `rewriter.rs:9018–9027`, **collect
  the WHERE quals' op calls and prewarm them** (union with the target-list calls), then let
  Postgres run its filter per-row against the now-warm cache. Keep the safety-cap logic
  (`implicit_prewarm_max_rows`) — for a full-table-scan WHERE that's the right bound to honor.
- Both `summarize` (SELECT) and `about` (WHERE) then get **concurrent fan-out**. Expected:
  the timeout query drops from "serial × 500" to "≈ pool-width concurrent."
- **Test:** stub-transport query with a semantic op in WHERE → assert a prewarm pass ran and
  wall-clock ≈ serial/poolwidth; correctness unchanged (same rows out).
- **Risk:** prewarming a WHERE op warms rows the filter will discard (wasted work on very
  selective filters). Mitigate with the existing row cap + a GUC to opt out; note it in the
  doc. Net win whenever the op is the dominant cost (always true here).

### Phase 2 — prewarm ORDER BY-driving operators
- Same treatment for `sort_clause_contains_rvbbit_op` (`rewriter.rs:9029`). Lower frequency
  than WHERE but identical mechanism.
- **Test:** `ORDER BY rvbbit.about(...) DESC LIMIT k` → prewarm fires, wall-clock improves.

> **Live finding (2026-06-05, Phase 1 A/B):** Phase 1 landed and prewarm now fires for
> WHERE + const-arg ops (verified). But the `about()` A/B showed only ~1.2× (43.8s→35.4s),
> and raising `rerank_bge_m3.max_concurrent` 4→8 changed *nothing* — per-call latency
> ballooned 125ms→529ms under load. Conclusion: a **single local sidecar (BGE reranker)
> saturates**; concurrent requests just queue at it. So for local single-sidecar specialists,
> **Phase 4 (batching) is the lever, not Phase 3 (concurrency)**. Phase 3 remains the lever
> for high-concurrency *LLM* providers (summarize). Reordered recommendation below:
> **0 → 1 → 4 → 3 → 2 → 5** (batching before concurrency).

### Phase 3 — make fan-out actually reach pool width
- Verify `dispatch_per_row` LLM fan-out hits `RVBBIT_POOL_SIZE` / `provider_max_concurrent`
  (we measured ~2.3×, far below 8). Audit the semaphores (`specialists/mod.rs:147/166`,
  per-backend `max_concurrent` vs process-wide `provider_max_concurrent`) and surface them as
  tunable per-backend in `rvbbit.backends`. Expose effective concurrency in the cost views.
- **Test:** stub op, pool=8 → assert effective concurrency ≈ 8; pool=1 → ≈ 1.
- Expected: `summarize` 197×1.35 s from ~115 s → **~33 s** at 8×.

### Phase 4 — true batching for multi-step ops whose hot step is a batch specialist
- Teach prewarm to recognize a multi-step op whose **expensive step is a single batch-capable
  specialist** (e.g. `about` → `rerank_bge_m3`) and **batch that step across rows**, running
  the trivial code post-step locally per row. Generalize `single_specialist_name` →
  "dominant batchable specialist step." Also add **cached-first filtering** before chunking
  (`prewarm.rs:340`) so partial hits don't re-send whole chunks (copy the MV flow).
- Expected: `about` over 500 rows from "500 fan-out calls" → **a handful of batched rerank
  calls** (rerankers are built for this).
- **Test:** stub specialist with a batch counter → assert N rows produce ⌈N/batch_size⌉ calls.

### Phase 5 — broaden coverage (the "set-oriented / lean-on-planner" generalization)
- Extend implicit prewarm beyond the simple single-table shape: **joins (multi-RTE)**,
  grouped/CTE/subquery shapes currently bailed at `rewriter.rs:8985–9012`. For joins, prewarm
  each op against its own driving relation. This is the deeper lift (parse-tree analysis +
  per-relation input extraction) and where "lean on the planner, batch the scan" fully
  generalizes.
- **Test:** join query with a semantic op on each side → both prewarmed.

## Testing strategy

- **Deterministic (CI):** `specialists/stub.rs` echo transport (offline, ~0 latency) for
  correctness + call-count assertions (batch counting, prewarm-fired assertions). `local_embed`
  for a realistic-CPU path where needed.
- **Wall-clock A/B:** set `rvbbit.query_id`, run identical workloads under serial vs
  concurrent/batched config, compare `min/max(cost_events.created_at)` span and effective
  concurrency. Knobs: `RVBBIT_POOL_SIZE`, `provider_max_concurrent`, `spec.batch_size`,
  `spec.max_concurrent` (note: pool is `OnceLock`-lazy — set env before first operator call;
  `reload_backends()` after `ALTER rvbbit.backends`).
- **Regression:** full pg_test suite + a live E2E reproducing the user's `summarize` +
  `about`-in-WHERE query, asserting it completes well under the UI timeout.

## Risks / gotchas
- WHERE-prewarm warms filtered-out rows — bounded by the row cap; add a GUC opt-out.
- Parallel-query workers skip receipt writes (`operators.rs:785`) — prewarm runs in the
  leader before planning, so this is safe; keep it that way.
- `op.model` / `op.steps` changes invalidate the hash channel — prewarm must use the exact
  catalog `op.model` at prewarm time (already does).
- Whole-batch re-execute on partial miss until Phase 4.
- `sql`/`mcp` step ops stay leader-sequential (out of scope; document the ceiling).

## Open decisions
1. **Auto vs opt-in for WHERE prewarm** — default-on (matching the existing SELECT behavior)
   with a GUC kill-switch, vs opt-in. Recommended: default-on + `rvbbit.implicit_prewarm` GUC.
2. **Row-cap policy for WHERE** — reuse `implicit_prewarm_max_rows`, or a separate cap for
   filtered scans.
3. **Phase ordering** — Phase 1 alone fixes the reported timeout; 3 and 4 are the
   throughput multipliers. Recommended sequence: 0 → 1 → 3 → 4 → 2 → 5.
