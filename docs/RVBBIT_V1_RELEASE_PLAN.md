# Rvbbit V1 Release Plan

Date: 2026-05-28

This plan freezes the current optimization work into a releaseable shape. The
goal is a stable Postgres extension with clear SQL semantics, observable
acceleration state, and conservative routing defaults.

## Experiment Gates

### 1. Lance Semantic Text Fast Path

Gate: GO as explicit opt-in.

Ship:
- `rvbbit.lance_enable_text(...)` / `rvbbit.lance_refresh_text(...)`
- `rvbbit.knn_text(...)` uses a ready Lance text index when one exists
- silent fallback to the existing exact/cache path when no index exists
- `rvbbit.lance_text_indexes` as SQL-facing index state

Do not ship as default automation yet:
- automatic Lance index creation
- stale-index detection as a hard route requirement
- background refresh/eviction

Validation signal:
- smoke result rank matched the existing path with max score drift around
  float precision
- 10k warm-cache query timing improved from about 22.6 ms to about 5.4 ms

### 2. Validated Layout Variants

Gate: GO for validated variants and Duck-Hive. NO-GO for DataFusion-Hive as a
default/no-profile route.

Ship:
- `rvbbit.layout_variant_status`
- `rvbbit.layout_variant_status_for(rel)`
- variant refresh only exposes layouts whose row counts and files validate
  against the canonical parquet layout
- router and sidecar catalogs only see `status = 'ready'` variants
- no-profile variant fallback prefers `duck_hive`

Do not ship as default automation:
- `datafusion_hive` automatic route selection
- nested/multi-column Hive layout selection
- query-pattern-driven hierarchy selection

Validation signal:
- SQL smoke showed heap, Duck-Hive, and DataFusion-Hive matched on grouped
  partition-key filters
- ClickBench 50k forced zoo completed; Duck-Hive had only the expected
  unsupported-regexp forced failure, while DataFusion-Hive still failed Q23

### 3. Shadow Learned Router

Gate: GO as observability only. NO-GO for active learned routing.

Ship:
- `rvbbit.route_shadow_explain(query, log := false)`
- `rvbbit.route_shadow_decisions`
- shadow prediction from exact observed candidate medians, falling back to the
  existing observation-curve machinery
- route-explain table detection fix: table references can come from either
  original SQL or plan text

Do not ship:
- learned model taking over default routing
- automatic exploration on user traffic
- ONNX/model runtime inside the backend

Validation signal:
- smoke kept actual route as no-profile native while shadow predicted
  `duck_vector` from observations at 0.83 confidence and logged one row

## V1 Routing Defaults

Default routing should stay conservative:
- native path for metadata aggregates, regex semantics, native rewrites, and
  small/simple shapes
- in-process DataFusion for broad vectorizable analytical parquet shapes
- Duck vector path for complex analytical shapes where rules/profile say it is
  worthwhile
- Duck-Hive only when a validated variant exists and the shape is variant
  friendly
- DataFusion-Hive only when explicitly forced or intentionally trained and
  enabled after further stability work

Recommended default flags:
- keep `rvbbit.route_hive = on`
- keep `rvbbit.route_duck_hive = on`
- set no-profile fallback to avoid `datafusion_hive`
- keep hot memory opt-in/manual for now
- keep Lance text acceleration opt-in/manual for now

## Hardening Checklist

Release blockers:
- add upgrade migrations for new catalog tables/functions:
  `rvbbit.lance_text_indexes`, `rvbbit.layout_variant_status`,
  `rvbbit.route_shadow_decisions`, and helper functions
- run full e2e suite, including semantic SQL, Warren, time travel, route
  training, and paid LLM paths
- rerun benchmark smoke matrix:
  ClickBench 50k, 200k, 1M; TPC-H 0.05; TPC-DS 0.1; TATP offline
- verify Query Lens and Adaptive Routing UI show benchmark/user route history
- document DataFusion-Hive as experimental and disabled from default fallback

Current validation status:
- `cargo fmt --all --check`: pass
- `cargo check -p pg_rvbbit`: pass, with existing warnings
- `cargo check --manifest-path crates/rvbbit_duck/Cargo.toml`: pass
- focused compact/router unit tests: pass
- fresh extension install smoke: pass
- 0.59.0 -> 0.60.0 upgrade SQL replay: pass
- live pytest suite: 385 passed, 12 skipped
- live real-world acceptance harness: pass; Gemini ADC skipped because
  credentials were not mounted
- Warren deploy/probe/operator smoke: pass
- ClickBench 50k release smoke: auto-router 0 failures; forced DataFusion
  still fails Q28 `regexp_replace`, which remains a forced-path capability gap

Remaining pre-release soak:
- ClickBench 200k and 1M
- TPC-H 0.05
- TPC-DS 0.1
- TATP offline
- Query Lens / Adaptive Routing UI verification against fresh benchmark rows

Post-release follow-ups:
- background Lance/hot-store refresh and eviction
- stale-index checks for Lance text indexes
- nested Hive layout design based on observed filter dimensions
- offline shadow-regret reporting from `route_shadow_decisions`
- optional trained-model route activation after enough regret data exists

## Rollback

Operational rollback knobs:
- `SET rvbbit.route_hive = off`
- `SET rvbbit.route_duck_hive = off`
- `SET rvbbit.route_datafusion_hive = off`
- `SET rvbbit.route_force_candidate = 'rvbbit_native'`
- `SET rvbbit.duck_backend = off`
- `SET rvbbit.force_heap_scan = on`

Data rollback:
- canonical heap remains the source of truth
- canonical parquet can be rebuilt from heap
- invalid variants are hidden by `layout_variant_status`
- Lance text indexes are optional accelerators and can be refreshed or ignored
