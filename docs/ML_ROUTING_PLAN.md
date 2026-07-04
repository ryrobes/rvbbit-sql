# ML Routing Layer — Plan

Status: design (2026-07-03). Motivated by the GQE-never-auto-selected diagnosis below.

> **Update 2026-07-04:** `rvbbit.route_gpu_gqe` now **defaults ON** (router.rs
> `candidate_gate_enabled` + duck_backend.rs `gqe_route_gate_enabled`). The gate is
> inert without a working GQE runtime — the `gqe_routes_available` runtime check
> makes GQE `available:false` on any box without `gqe-cli`/GPU — so it costs nothing
> off-GPU while letting GQE-capable machines route to and self-train on GQE with no
> manual opt-in. The `SET rvbbit.route_gpu_gqe=on` steps below are now redundant
> (kept for historical context). Accelerator status treats GQE as an optional
> accelerator: its absence is `info`, never a `warn`.

## Why (the diagnosis that motivated this)

ClickBench 1M auto-router run `clickbench_20260703T233539Z` (system `rvbbit`), 43 queries:

| engine | count | how it was chosen |
|--------|-------|-------------------|
| native | 27 | native hard rules (fire before the cold path) |
| duck_vortex | 14 | no-profile "variant-friendly analytical" fallback |
| datafusion | 2 | no-profile (count-distinct) |
| **gpu_gqe** | **0** | — |

Root causes, in order of impact:

1. **The `route_gpu_gqe_prior` GUC was never enabled** (the bench harness doesn't set it; it defaults off). So the warm-prior never activated and GQE stayed structurally excluded from the cold path — exactly as before the prior existed. This alone explains the 0.
2. Even with the prior on + GQE warm, only **~8 of 43** queries would move to GQE: the 2 `datafusion` (count-distinct, no timestamp) + 6 of the 14 `duck_vortex` (non-timestamp). The other 8 no-profile queries reference a timestamp column and are vetoed by the pre-existing GQE temporal gate (`gpu_gqe_temporal_reference_reason`, router.rs:6235).
3. The remaining **27 hit native hard rules** and never reach the cold path where the prior lives.

The deeper lesson: **the prior is a narrow, blunt lever.** It only touches the untrained cold path, only large-analytical shapes, and only where GQE passes its own eligibility vetoes. The forced-GQE 5M run showed GQE *winning 17 queries* — many of which the auto-router currently sends to native/vortex via hard rules. A prior can't recover those; only a data-driven ranker that knows GQE's *measured* performance per shape can.

Two things unlock GQE in auto **today**, no new code:
- **Enable the prior:** `SET rvbbit.route_gpu_gqe=on; SET rvbbit.route_gpu_gqe_prior=on;` in the bench GUCs, and warm GQE (`SELECT rvbbit.warm_gpu_gqe();` once, or let `accel_tick` do it). Moves ~8 queries to GQE.
- **Train with GQE warm (the real unlock):** run `rvbbit.route_optimize_auto()` with GQE warm. It benches every eligible engine per shape and pins the winner into `rvbbit.route_overlay`, which applies to **all** queries (not just the cold path). Previously it benched GQE cold (60s auto-start inflated the median → no pin); the round-4 warm machinery fixes that. This is the pre-ML, exact-match version of what this plan generalizes.

## What the ML layer is

A per-engine **latency predictor** that ranks the *eligible* candidates for a query and picks the fastest, inserted as a new layer in the existing decision cascade. It generalizes the `route_overlay` (exact shape_key → pinned engine) to *unseen* shapes by predicting from features instead of looking up an exact match.

### Non-negotiable invariants (what stays hard-coded)

1. **Eligibility stays deterministic.** The model only ranks candidates that already pass `candidate_availability` (AS-OF, regex, collation, GQE temporal/join limits, dirty-overlay, etc.). The model never decides *safety* — a misprediction costs latency, never correctness.
2. **Overlay exact pins win over the model.** For a shape we've actually benched, a measured pin beats a prediction.
3. **Predict latency (regression), not the winning engine (classification).** Lets us drop unavailable engines from the argmin for free, apply a margin threshold (don't switch for a predicted 3% gain), and stay sane on ties.

### Where it slots (router.rs decision cascade)

```
force override
overlay exact pins            (measured, highest confidence)
native metadata hard rules    (count/minmax/filter — provably native-best)
>>> ML LAYER <<<              (eligible query, no exact pin: rank by predicted latency)
no-profile heuristics         (today's cold fallback — becomes the model's fallback)
```

The ML layer is `Option<RouteDecision>`: `Some(engine)` when the model is loaded, confident, and the margin clears a threshold; `None` → fall through to today's heuristics. GUC-gated (`rvbbit.route_ml_enabled`, default off), so it's a pure addition.

## Model + training

- **Model:** a small gradient-boosted regression **per engine** predicting log-latency from features. ~100–300 shallow trees each; inference is microseconds (a handful of comparisons per tree) — not a latency concern. One model per engine (native, duck, duck_vortex, datafusion, datafusion_vortex, gpu_gqe, pg) so adding/removing an engine is independent.
- **Features (the real lever — enrich first):** current `RouteFeatures` (table_rows, aggregate_count, group_by, join_count, distinct, order_by, widths, shape signatures) **plus** the stats the KG already has but the router ignores: **selectivity** (WHERE estimated rows / table rows), **NDV** of group/distinct keys, **result-cardinality estimate**, per-column widths. These help the current heuristics too, so land them first (Stage 1).
- **Labels:** `(features, engine, elapsed_ms)` triples — already produced by `route_optimize` benches and every `route_executions` row. The bench history (`bench_history.query_results.detail`) is a ready-made training set.
- **Training pipeline:** offline in Python (sklearn/lightgbm) reading `route_observations` + `route_executions` + bench history → export each engine's trees as a compact JSON/flat-array into a catalog table `rvbbit.route_model (engine, version, params jsonb, trained_at)`. Rust loads + evaluates at startup (memoized), no per-query training, no heavy Rust deps. Retrain on a schedule (pg_cron) or on demand (`rvbbit.train_route_model()`).

## Staging

- **Stage 1 — features (fork-independent foundation).** Add selectivity, NDV, result-cardinality to `RouteFeatures` + `build_features`. Immediately improves the current cost model; required by any model. Unit-testable.
- **Stage 2 — the inference seam.** `rvbbit.route_model` catalog table + a Rust tree-ensemble evaluator + the GUC-gated `ml_route_decision` hook slotted above the no-profile layer, ranking eligible engines by predicted latency with a margin threshold. Off by default, no-op without a model → cannot regress routing. This is the "layer."
- **Stage 3 — training + rollout.** Python trainer over route_executions/bench history → `route_model`; `rvbbit.train_route_model()`; shadow-mode first (log the model's pick alongside the live decision via the existing `route_shadow_decisions` machinery) to compare against the heuristics before flipping it live.
- **Stage 4 — exploration synergy.** A regression model with an uncertainty estimate can *drive* exploration (try an engine when the model is unsure), replacing the deterministic warm-prior with a principled bandit. Feeds its own training data.

## Status (implemented 2026-07-03)

Stages 2 + 3 landed and validated:
- `crates/pg_rvbbit/src/route_model.rs` — tree-ensemble evaluator (5 unit tests).
- `router.rs` — `feature_value`, `ml_models` (memoized load), `ml_route_decision` hook at the top of `choose_no_profile_route`, GUCs `rvbbit.route_ml_enabled` (default off) + `rvbbit.route_ml_min_margin` (default 0.15).
- migration `0125_route_model` — `rvbbit.route_model` table.
- `scripts/train_route_model.py` — trains per-engine sklearn GBMs from `bench_history` forced runs; JSON export reproduces `sklearn.predict` exactly (self-checked). Trained 6 engines (native R²=.92, gpu_gqe R²=.88, datafusion R²=.96).

Validated end-to-end: with ML on, the model routes heavy aggregates to `gpu_gqe` (where it learned GQE wins) and simple count/count-distinct shapes to `native` — the same shape the heuristic sent to `datafusion` goes to GQE under the model. Off by default → default routing and parity unchanged (344/344).

### Training data: three sources, in preference order

The model needs per-engine latency labels. `train_route_model()` reads whichever of these exist:

1. **`route_observations` (preferred) — the self-training loop.** `route_optimize_query`/`route_optimize_auto` replay real logged query shapes (`route_shape_samples`, captured from actual traffic) across *every eligible engine* and now log each engine's timing to `route_observations`. This is **unbiased** — it times every engine, not just the one the router picked — so an engine the router currently avoids (e.g. GQE) still gets labels. It's real queries + full engine coverage + automatic. This is the durable source.
2. **Forced bench runs** (`rvbbit_<engine>_forced`) — unbiased per-engine timings, but synthetic (bench queries) and manual. A fine bootstrap.
3. **Auto bench runs** (`system='rvbbit'`, `include_auto`) — one (engine, latency) per query: the engine the router chose. **Biased** to the router's picks (the feedback loop); a supplement only.

The self-improving loop (nightly pg_cron):
```sql
SELECT rvbbit.route_self_train();   -- = route_optimize_auto() then train_route_model()
-- replays the top_k hottest logged shapes across all engines (read-only, budget-bounded),
-- logs route_observations, refits route_model. No forced sweeps, no Python.
```
Forced sweeps remain useful to bootstrap a brand-new engine or scale quickly; after that the
self-train loop keeps the model current from real traffic:
```
BENCH_SYSTEMS=rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_datafusion_forced,\
rvbbit_datafusion_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced  BENCH_LIMIT=2000000  <run bench>
```

### Enable it (all SQL — no Python needed)
```sql
-- 1. train from bench_history (forced sweeps + auto runs). Pure in-DB, callable
--    from any SQL client or the UI:
SELECT rvbbit.train_route_model();                    -- writes rvbbit.route_model
--    args: rvbbit.train_route_model(min_samples := 20, include_auto := true)
-- 2. turn the layer on:
SET rvbbit.route_ml_enabled = on;                     -- session, or ALTER SYSTEM / bench GUCs
-- optional: SET rvbbit.route_ml_min_margin = 0.10;   -- how much faster than native to switch
```
`rvbbit.train_route_model()` is a pure-Rust gradient-boosting trainer (no
Python/sklearn); `scripts/train_route_model.py` is an offline equivalent for
hyperparameter experiments. Retrain whenever the workload or hardware changes; the
router reloads `route_model` within the memo TTL. The model is a cheap, derived
artifact — bench_history is the durable source of truth, so a retrain is one call.

**GOTCHA — the bench reset wipes the models.** `rvbbit.route_model` and the trained models live in the `rvbbit` schema, created by `migrate()` (not the base extension). A bench run with `--reset-rvbbit-extension` drops the whole schema, so the sequence is: run the (forced) bench → `psql -f crates/pg_rvbbit/sql/migrate.sql` (recreate the table) → `python3 scripts/train_route_model.py` → re-run the bench with `rvbbit.route_ml_enabled=on` **and without** `--reset-rvbbit-extension` so the models survive into the measured run.

### Placement choice (v1 = conservative)

The hook sits in the cold/no-profile path, so native metadata hard rules (count/minmax — provably native-best, no scan) and overlay exact pins still win first. This is the safe v1: the model only decides shapes the heuristics were guessing at. A future GUC (`route_ml_override_native`) could move the hook above the native rules for the fully-model-driven version — sound only once the model is trusted, since a well-trained model already predicts native fastest for native-optimal shapes (it was trained on native's real timings for them).

## Safety / rollout

- Off by default; shadow-mode comparison before live (Stage 3).
- Eligibility filter unchanged → no correctness risk.
- Margin threshold + overlay-pin precedence → the model only overrides heuristics when it's confidently better.
- Fully explainable: log per-engine predictions in `route_explain` (like the current candidates array) so a wrong pick is diagnosable.
