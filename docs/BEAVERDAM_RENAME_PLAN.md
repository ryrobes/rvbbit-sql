# Beaverdam Rename Plan

This is a staged plan for renaming RVBBit's optional storage acceleration layer
to **Beaverdam** before release. It is intentionally written as a context file,
not as a migration diff, so the rename can be picked up later without
rediscovering the current boundaries and blast radius.

## Context

The product-level name remains **RVBBit**. Semantic SQL, workflow operators,
provider catalogs, KG features, and the extension as a whole are still RVBBit.

**Beaverdam** is the optional storage acceleration layer: it builds and routes
through file-backed and memory-backed structures beside the ordinary Postgres
heap while preserving the vanilla SQL contract. It includes the accelerator
files, row group/file metadata, layout variants, refresh/rebuild/compaction,
time-travel metadata, hot memory cache, Lance-backed storage indexes, and
Duck/Vortex worker paths when they are used to serve accelerated table scans.

**Warren** remains the name for the capability-node/runtime subsystem. It should
not absorb Beaverdam storage language.

The current public names are pre-release and are already a mix of terms:
`rvbbit table`, `lakehouse`, `compact`, `acceleration`, `layout variants`,
`hot store`, `row_groups`, and `duck_sidecar`. The rename should make it clear
that a user can use RVBBit without using Beaverdam.

## Principles

- Do a hard pre-release rename. Do not add long-lived compatibility aliases for
  old public names unless a phase absolutely needs a temporary bridge inside the
  same PR.
- Keep the `rvbbit` SQL schema. The schema is the product namespace; Beaverdam
  is a subsystem inside it.
- Do not over-theme lower-level objects. Prefer direct technical words:
  `refresh`, `rebuild`, `compact`, `layout`, `row_group`, `worker`, `route`.
- Keep semantic SQL names normal. Do not rename unrelated functions, operators,
  provider catalogs, receipts, model configuration, KG APIs, or Warren APIs just
  because they live under `rvbbit`.
- Keep historical benchmark rows as historical data. Old `system` values in
  `bench_history` do not need to be rewritten unless we explicitly want a data
  migration for analytics.
- Treat heap as the bottom-layer source of truth. Beaverdam names should
  describe optional accelerator structures, not replace or obscure the heap.

## Public Positioning

Suggested copy:

> Beaverdam is RVBBit's optional poly-engine acceleration layer. It builds
> Parquet, Vortex, Lance, and memory-backed structures beside the ordinary
> Postgres heap so analytical, time-travel, and vector workloads can take faster
> paths without changing the SQL contract.

Short UI labels:

- `RVBBit`: extension/product.
- `Beaverdam`: optional storage acceleration.
- `Warren`: capability/runtime graph.
- `Beaverdam worker`: shared Duck/Vortex worker/broker process.
- `Beaverdam layout`: a physical file layout for a table, such as canonical,
  hive, or vortex.
- `Beaverdam refresh`: append/incremental accelerator-file creation.
- `Beaverdam rebuild`: full accelerator-file rebuild from authoritative heap.
- `Beaverdam compact`: expensive physical file maintenance that rewrites or
  coalesces existing accelerator files.

## Boundary Map

### In Scope

- SQL catalog tables/views that describe accelerated storage.
- SQL helper functions for refresh, rebuild, compaction, layout status, row
  group/file inspection, time-travel timeline, hot cache, and table-local Lance
  accelerators.
- GUCs and environment variables that control storage refresh, layout variants,
  hot store behavior, and Beaverdam worker behavior.
- Rust modules that query or maintain Beaverdam metadata.
- Bench loaders/runners that call refresh/rebuild functions or force storage
  route candidates.
- Docs and UI contract files that currently say `Rvbbit table`, `lakehouse`,
  `acceleration`, `compact`, `duck sidecar`, or similar.
- Telemetry for the Duck/Vortex worker path.

### Out Of Scope

- The `rvbbit` SQL schema itself.
- Semantic SQL functions/operators and model/provider configuration.
- Warren capability nodes and runtime API.
- KG tables and functions, unless a specific KG table is moved onto Beaverdam
  storage.
- Generic route training/profile concepts that choose among all engines. Only
  storage-specific labels should move under Beaverdam.
- Existing historical benchmark result rows.

## Current Inventory

Rerun this before starting the rename:

```bash
rg -n "rvbbit\.row_groups|rvbbit\.layout_variant_status|rvbbit\.acceleration_|rvbbit\.refresh_acceleration|rvbbit\.compact|rvbbit\.hot_|rvbbit\.lance_|duck_sidecar|Rvbbit table|rvbbit table|layout variants|compact_hive|compact_vortex" docs bench crates/pg_rvbbit crates/rvbbit_duck
```

Focused code inventory:

```bash
rg -n "CREATE TABLE (IF NOT EXISTS )?rvbbit\.|CREATE OR REPLACE VIEW rvbbit\.|CREATE OR REPLACE FUNCTION rvbbit\.|extension_sql!|fn (compact|refresh_|rebuild_|layout_variant|row_groups|time_travel|hot_|lance_)" crates/pg_rvbbit/src crates/pg_rvbbit/sql crates/rvbbit_duck/src
```

The important current storage surfaces are:

- `crates/pg_rvbbit/src/catalog.rs`
- `crates/pg_rvbbit/src/compact.rs`
- `crates/pg_rvbbit/src/scan.rs`
- `crates/pg_rvbbit/src/custom_scan.rs`
- `crates/pg_rvbbit/src/planner.rs`
- `crates/pg_rvbbit/src/rewriter.rs`
- `crates/pg_rvbbit/src/router.rs`
- `crates/pg_rvbbit/src/df.rs`
- `crates/pg_rvbbit/src/lance.rs`
- `crates/pg_rvbbit/src/duck_backend.rs`
- `crates/pg_rvbbit/src/duck_telemetry.rs`
- `crates/pg_rvbbit/src/bitmap.rs`
- `crates/pg_rvbbit/src/sketches.rs`
- `crates/pg_rvbbit/src/provider_catalog.rs`
- `crates/pg_rvbbit/src/telemetry.rs`
- `crates/rvbbit_duck/src/main.rs`
- `bench/clickbench/*`
- `bench/tpch/*`
- `bench/tpcds/*`
- `bench/sidecar_load/*`
- `docs/DUCK_SIDECAR.md`
- `docs/RVBBIT_DUCK_UI_CONTRACT.md`
- `docs/LAKEHOUSE.md`
- `docs/RVBBIT_PRODUCTION_SHAPE.md`
- `docs/TIME_TRAVEL.md`
- `docs/TUNING.md`
- `docs/RVBBIT_V1_RELEASE_PLAN.md`

## Target Naming Map

These names are the proposed final public surface. Some entries are marked as
decisions because they affect UI vocabulary and should be settled before code
changes begin.

### Catalog Tables And Views

| Current | Proposed | Notes |
| --- | --- | --- |
| `rvbbit.tables` | `rvbbit.beaverdam_tables` | Registered Beaverdam-enabled relations. |
| `rvbbit.row_groups` | `rvbbit.beaverdam_row_groups` | Keep `row_group` if Parquet remains canonical; see open decision below. |
| `rvbbit.group_stats` | `rvbbit.beaverdam_group_stats` | Statistics used for pruning/planning. |
| `rvbbit.column_bitmaps` | `rvbbit.beaverdam_column_bitmaps` | Storage-side bitmap metadata. |
| `rvbbit.text_dictionaries` | `rvbbit.beaverdam_text_dictionaries` | Dictionary-id text acceleration metadata. |
| `rvbbit.generations` | `rvbbit.beaverdam_generations` | Time-travel/storage-generation metadata. |
| `rvbbit.delete_log` | `rvbbit.beaverdam_delete_log` | Pending delete/tombstone tracking. |
| `rvbbit.row_group_variants` | `rvbbit.beaverdam_layout_files` | Preferred over `row_group_variants` because Vortex/Hive are layout files, not always row groups. |
| `rvbbit.layout_variant_status` | `rvbbit.beaverdam_layout_status` | UI-facing status for canonical/hive/vortex layouts. |
| `rvbbit.acceleration_state` | `rvbbit.beaverdam_state` | Per-table refresh watermark/state. |
| `rvbbit.acceleration_operations` | `rvbbit.beaverdam_operations` | Refresh/rebuild/compact operation log. |
| `rvbbit.acceleration_operation_phases` | `rvbbit.beaverdam_operation_phases` | Phase-level observability. |
| `rvbbit.acceleration_status` | `rvbbit.beaverdam_status` | SQL-facing status view. |
| `rvbbit.hot_objects` | `rvbbit.beaverdam_hot_objects` | Manual/automatic hot memory cache state. |
| `rvbbit.lance_text_indexes` | `rvbbit.beaverdam_lance_text_indexes` | Only for table-local Lance storage indexes. |
| `rvbbit.duck_sidecar_instances` | `rvbbit.beaverdam_worker_instances` | Public concept becomes Beaverdam worker. |
| `rvbbit.duck_sidecar_heartbeats` | `rvbbit.beaverdam_worker_heartbeats` | Keep engine details in columns, not table name. |
| `rvbbit.duck_sidecar_query_events` | `rvbbit.beaverdam_worker_query_events` | Includes hostname/node for future multi-node workers. |
| `rvbbit.duck_sidecar_fallback_events` | `rvbbit.beaverdam_worker_fallback_events` | Extension-side fallback telemetry. |
| `rvbbit.duck_sidecar_latest` | `rvbbit.beaverdam_worker_latest` | UI header/source view. |
| `rvbbit.duck_sidecar_query_summary` | `rvbbit.beaverdam_worker_query_summary` | Rollup view. |

### SQL Functions

| Current | Proposed | Notes |
| --- | --- | --- |
| `rvbbit.refresh_acceleration(regclass, bool)` | `rvbbit.beaverdam_refresh(regclass, bool)` | Incremental/watermark refresh. |
| `rvbbit.rebuild_acceleration(regclass, bool)` | `rvbbit.beaverdam_rebuild(regclass, bool)` | Full rebuild from heap. |
| `rvbbit.compact(regclass)` | `rvbbit.beaverdam_refresh(regclass)` or `rvbbit.beaverdam_compact(regclass)` | Current behavior is partly compatibility wrapper; settle semantics first. |
| `rvbbit.compact(regclass, keep_heap)` | `rvbbit.beaverdam_compact(regclass, keep_heap)` | Only if it really performs physical compaction/rewrite. |
| `rvbbit.refresh_layout_variants(regclass)` | `rvbbit.beaverdam_refresh_layouts(regclass)` | Layout variant refresh. |
| `rvbbit.refresh_layout_variants_xid_range(...)` | `rvbbit.beaverdam_refresh_layouts_xid_range(...)` | Internal/manual layout refresh slice. |
| `rvbbit.row_groups_for(regclass)` | `rvbbit.beaverdam_row_groups_for(regclass)` | Metadata inspection helper. |
| `rvbbit.layout_variant_status_for(regclass)` | `rvbbit.beaverdam_layout_status_for(regclass)` | UI/status helper. |
| `rvbbit.acceleration_phase_log_for(regclass)` | `rvbbit.beaverdam_phase_log_for(regclass)` | Operation observability helper. |
| `rvbbit.time_travel_timeline(regclass)` | `rvbbit.beaverdam_time_travel_timeline(regclass)` or keep current | See open decision. |
| `rvbbit.hot_load(...)` | `rvbbit.beaverdam_hot_load(...)` | Manual hot-cache load. |
| `rvbbit.hot_load_columns(...)` | `rvbbit.beaverdam_hot_load_columns(...)` | Column-subset hot-cache load. |
| `rvbbit.hot_evict(...)` | `rvbbit.beaverdam_hot_evict(...)` | Manual hot-cache eviction. |
| `rvbbit.hot_cache_reset()` | `rvbbit.beaverdam_hot_cache_reset()` | Backend-local reset. |
| `rvbbit.hot_status()` | `rvbbit.beaverdam_hot_status()` | Hot-cache UI/status helper. |
| `rvbbit.lance_enable_text(...)` | `rvbbit.beaverdam_lance_enable_text(...)` | Table-local text/vector acceleration. |
| `rvbbit.lance_refresh_text(...)` | `rvbbit.beaverdam_lance_refresh_text(...)` | Table-local Lance refresh. |

Standalone Lance utilities such as demo dataset creation, raw Lance dataset
inspection, or non-table-local Lance APIs may keep `rvbbit.lance_*` names if
they are not specifically Beaverdam table acceleration.

### GUCs And Environment Variables

| Current | Proposed | Notes |
| --- | --- | --- |
| `rvbbit.compact_variants_sync` / `RVBBIT_COMPACT_VARIANTS_SYNC` | `rvbbit.beaverdam_variants_sync` / `RVBBIT_BEAVERDAM_VARIANTS_SYNC` | Sync vs async layout variant refresh. |
| `rvbbit.compact_hive_layout` / `RVBBIT_COMPACT_HIVE_LAYOUT` | `rvbbit.beaverdam_hive_layout` / `RVBBIT_BEAVERDAM_HIVE_LAYOUT` | Enables Hive layout build. |
| `rvbbit.compact_hive_keys` / `RVBBIT_COMPACT_HIVE_KEYS` | `rvbbit.beaverdam_hive_keys` / `RVBBIT_BEAVERDAM_HIVE_KEYS` | Hive partition key override. |
| `rvbbit.compact_hive_variants` / `RVBBIT_COMPACT_HIVE_VARIANTS` | `rvbbit.beaverdam_hive_variants` / `RVBBIT_BEAVERDAM_HIVE_VARIANTS` | Hive variant count. |
| `rvbbit.compact_hive_min_distinct` / `RVBBIT_COMPACT_HIVE_MIN_DISTINCT` | `rvbbit.beaverdam_hive_min_distinct` / `RVBBIT_BEAVERDAM_HIVE_MIN_DISTINCT` | Hive key selection floor. |
| `rvbbit.compact_hive_max_distinct` / `RVBBIT_COMPACT_HIVE_MAX_DISTINCT` | `rvbbit.beaverdam_hive_max_distinct` / `RVBBIT_BEAVERDAM_HIVE_MAX_DISTINCT` | Hive key selection ceiling. |
| `rvbbit.compact_vortex_layout` / `RVBBIT_COMPACT_VORTEX_LAYOUT` | `rvbbit.beaverdam_vortex_layout` / `RVBBIT_BEAVERDAM_VORTEX_LAYOUT` | Enables Vortex layout build. |
| `rvbbit.compact_keep_heap` / `RVBBIT_COMPACT_KEEP_HEAP` | `rvbbit.beaverdam_keep_heap` / `RVBBIT_BEAVERDAM_KEEP_HEAP` | Confirm whether this is still needed now heap is gold source. |
| `rvbbit.compact_refresh_variants` / `RVBBIT_COMPACT_REFRESH_VARIANTS` | `rvbbit.beaverdam_refresh_variants` / `RVBBIT_BEAVERDAM_REFRESH_VARIANTS` | Refresh layouts after canonical refresh. |
| `rvbbit.acceleration_operation_id` | `rvbbit.beaverdam_operation_id` | Internal operation context. |
| `rvbbit.hot_store_budget_mb` / `RVBBIT_HOT_STORE_BUDGET_MB` | `rvbbit.beaverdam_hot_budget_mb` / `RVBBIT_BEAVERDAM_HOT_BUDGET_MB` | Per-backend decoded Arrow budget. |
| `rvbbit.hot_store_route_max_rows` / `RVBBIT_HOT_STORE_ROUTE_MAX_ROWS` | `rvbbit.beaverdam_hot_route_max_rows` / `RVBBIT_BEAVERDAM_HOT_ROUTE_MAX_ROWS` | Router ceiling for hot path. |

Keep engine-specific knobs technical when they truly are engine-specific. For
example, `rvbbit.duck_backend_mode` may be clearer than a generic Beaverdam
name if it only controls the Duck executable/broker path.

### Route Candidate Labels

Route candidate labels are both internal identifiers and benchmark dimensions.
Rename them only if the UI needs clearer grouping.

Recommended v1 approach:

- Keep concrete engine labels: `native`, `datafusion`, `duck`, `duck_vortex`,
  `duck_hive`, `datafusion_mem`, `datafusion_vortex`.
- Group them under a UI category named `Beaverdam`.
- Do not rename historical benchmark `system` values.
- If new labels are introduced, add a benchmark-side display map instead of
  mutating old result rows.

## Phased Plan

### Phase 0: Freeze The Contract

Goal: finalize the names above and write down the exact final public surface.

Work:

- Decide the open questions at the bottom of this file.
- Add the final naming map to the release plan.
- Decide whether current install SQL/migrations are squashed pre-release or
  whether the rename ships as an upgrade migration.

Blast radius: low. Documentation only.

Validation:

- This file has no `TODO` names left for the first implementation phase.
- `docs/RVBBIT_V1_RELEASE_PLAN.md` links to this plan or contains the same
  subsystem boundary.

Rollback: delete or revise docs.

### Phase 1: Docs And UI Language

Goal: make user-facing copy describe Beaverdam without changing SQL yet.

Work:

- Rename or supersede `docs/LAKEHOUSE.md` with a Beaverdam storage doc.
- Update:
  - `docs/RVBBIT_PRODUCTION_SHAPE.md`
  - `docs/TIME_TRAVEL.md`
  - `docs/TUNING.md`
  - `docs/DUCK_SIDECAR.md`
  - `docs/RVBBIT_DUCK_UI_CONTRACT.md`
  - `docs/RVBBIT_V1_RELEASE_PLAN.md`
  - benchmark READMEs
- Replace conceptual uses of `Rvbbit table` with `Beaverdam table` when the
  text means an accelerated table. Keep `RVBBit` for the full extension.
- Replace `lakehouse` language with `Beaverdam` unless discussing the broader
  lakehouse category.
- Label the Duck/Vortex broker as the `Beaverdam worker` in docs/UI contracts,
  while still noting the current binary name if it remains `rvbbit-duck`.

Blast radius: low to medium. UI agents and docs may need updated query names
after later SQL phases, but this phase can use explanatory wording first.

Validation:

```bash
rg -n "lakehouse|Rvbbit table|rvbbit table|duck sidecar|duck_sidecar" docs bench
```

Rollback: documentation-only revert.

### Phase 2: Catalog Hard Rename

Goal: rename Beaverdam storage tables/views in the SQL catalog and every code
path that reads or writes them.

Work:

- Update `crates/pg_rvbbit/src/catalog.rs`.
- Update all Rust SQL strings in:
  - `compact.rs`
  - `scan.rs`
  - `custom_scan.rs`
  - `planner.rs`
  - `rewriter.rs`
  - `router.rs`
  - `df.rs`
  - `lance.rs`
  - `bitmap.rs`
  - `sketches.rs`
  - `provider_catalog.rs`
  - `telemetry.rs`
- Update `crates/rvbbit_duck/src/main.rs` catalog queries.
- Update migration SQL under `crates/pg_rvbbit/sql/`.
- Decide whether to use `ALTER TABLE ... RENAME TO ...` in the upgrade path or
  drop/recreate metadata tables. Since the feature is pre-release and storage
  files are rebuildable from heap, drop/recreate may be acceptable for dirty
  dev databases, but the extension upgrade should still be explicit.
- Keep old migration files readable if needed; do not rewrite historical
  migrations unless the release process squashes all pre-release migrations.

Blast radius: high. This touches the executor, router, refresh pipeline, worker
queries, time travel, Lance, benchmarks, and UI dashboards.

Validation:

```bash
cargo fmt
cargo check -p pg_rvbbit
cargo test -p pg_rvbbit --lib
cargo check --manifest-path crates/rvbbit_duck/Cargo.toml
cargo test --manifest-path crates/rvbbit_duck/Cargo.toml
```

SQL smoke:

```sql
SELECT to_regclass('rvbbit.beaverdam_tables') IS NOT NULL;
SELECT to_regclass('rvbbit.beaverdam_row_groups') IS NOT NULL;
SELECT to_regclass('rvbbit.beaverdam_layout_status') IS NOT NULL;
SELECT to_regclass('rvbbit.beaverdam_status') IS NOT NULL;
```

End-to-end smoke:

- Create a Beaverdam table.
- Insert rows.
- Run refresh/rebuild.
- Verify metadata rows exist.
- Run a routed query.
- Verify heap fallback still works when accelerator files are absent.

Rollback: revert this phase as a unit. Partial catalog rename is not safe.

### Phase 3: Function, GUC, And Environment Rename

Goal: move the public control surface from `acceleration`/`compact`/`hot_store`
names to Beaverdam names.

Work:

- Rename SQL functions in Rust extension macros and SQL migrations.
- Update benchmark loaders/runners:
  - `bench/clickbench/load_all.py`
  - `bench/clickbench/run_offline.sh`
  - `bench/clickbench/runners.py`
  - `bench/tpch/load_all.py`
  - `bench/tpch/run_offline.sh`
  - `bench/tpch/runners.py`
  - `bench/tpcds/load_all.py`
  - `bench/tpcds/run_offline.sh`
  - `bench/tpcds/runners.py`
  - `bench/tatp/run_offline.sh`
  - `bench/sidecar_load/*`
- Update e2e and acceptance tests that call refresh/compact/hot functions.
- Rename GUC lookups in `compact.rs`, router hot-store rules, and benchmark
  `SET` mappings.
- Update Docker/compose/example environment snippets.
- Make the `compact` semantics explicit:
  - `beaverdam_refresh`: watermark/incremental accelerator creation.
  - `beaverdam_rebuild`: full rebuild from heap.
  - `beaverdam_compact`: expensive file maintenance only.

Blast radius: high. This breaks all scripts that call the old functions or set
old GUC/env names.

Validation:

```bash
rg -n "refresh_acceleration|rebuild_acceleration|rvbbit\.compact|compact_hive|compact_vortex|hot_store|RVBBIT_COMPACT|RVBBIT_HOT_STORE" docs bench crates
```

Expected result after this phase: no public references except migration
history or deliberate explanatory notes in this plan.

Benchmark smoke:

- ClickBench small load with `--rebuild`.
- TPC-H tiny scale load with `--rebuild`.
- TPC-DS tiny scale load with `--rebuild`.
- Sidecar load harness against existing Vortex files.

Rollback: revert this phase as a unit. Do not leave mixed GUC/function names.

### Phase 4: Worker Telemetry Rename

Goal: rename Duck sidecar telemetry into Beaverdam worker telemetry.

Work:

- Update `crates/pg_rvbbit/src/duck_telemetry.rs` table/view names.
- Update `crates/rvbbit_duck/src/main.rs` inserts and heartbeat writes.
- Update fallback telemetry in `crates/pg_rvbbit/src/duck_backend.rs`.
- Update docs:
  - `docs/DUCK_SIDECAR.md`
  - `docs/RVBBIT_DUCK_UI_CONTRACT.md`
- Decide whether the binary remains `rvbbit-duck` for v1. Recommended:
  keep the binary name for now because it is a concrete DuckDB/Vortex worker
  implementation, but describe it publicly as the Beaverdam worker. A binary
  rename can be a later operational release if this name causes confusion.

Blast radius: medium to high. The runtime path is narrow, but dashboard queries
and worker heartbeat writes are sensitive.

Validation:

SQL smoke:

```sql
SELECT to_regclass('rvbbit.beaverdam_worker_instances') IS NOT NULL;
SELECT to_regclass('rvbbit.beaverdam_worker_heartbeats') IS NOT NULL;
SELECT to_regclass('rvbbit.beaverdam_worker_query_events') IS NOT NULL;
SELECT to_regclass('rvbbit.beaverdam_worker_latest') IS NOT NULL;
```

Runtime smoke:

- Local per-call worker fallback with no broker running.
- Shared broker mode.
- Forced `duck_vortex` query.
- Query events appear in `beaverdam_worker_query_events`.
- Fallback events appear when shared broker mode is unavailable.

Rollback: revert this phase as a unit. Mixed old/new telemetry tables will make
the UI misleading.

### Phase 5: Benchmark And Training Surface

Goal: make benchmark, routing, and training artifacts describe Beaverdam
without corrupting historical comparisons.

Work:

- Keep old `bench_history.query_results.system` values as historical labels.
- Add display/grouping metadata if desired:
  - `system = rvbbit_duck_vortex_forced`
  - `engine_group = beaverdam`
  - `layout = vortex`
  - `worker = duck`
- Update benchmark result JSON detail to include Beaverdam layout/worker fields
  where available.
- Update route explain output so storage candidates are grouped as Beaverdam
  candidates.
- Update route training UI docs to explain that trained profiles can choose
  Beaverdam paths but are not themselves Beaverdam-only.

Blast radius: medium. Mostly Python harnesses, docs, and UI expectations.

Validation:

- Run one ClickBench targeted calibration set.
- Confirm `bench_history.query_results.detail` still contains route details.
- Confirm route UI can distinguish semantic route decisions from Beaverdam
  storage route decisions.

Rollback: safe to revert independently if SQL/API phases are complete.

### Phase 6: Internal Cleanup

Goal: make internal module names and comments match the new vocabulary where it
improves maintainability.

Work:

- Rename comments and error messages that say `Rvbbit table` when they mean a
  Beaverdam-enabled table.
- Consider file/module renames only where useful. Do not churn stable Rust
  module names unless they are part of the public conceptual confusion.
- Replace broad `acceleration` comments with `Beaverdam refresh`, `Beaverdam
  layout`, or `Beaverdam worker` where precise.
- Keep lower-level names like `row_group`, `layout`, `candidate`, `engine`, and
  `worker` where technically accurate.

Blast radius: medium. Mostly compiler/import churn if module names move.

Validation:

```bash
rg -n "Rvbbit table|rvbbit table|acceleration|lakehouse|duck sidecar" crates docs bench
```

Expected result: remaining matches are either generic English, migration
history, or intentionally documented compatibility/history notes.

Rollback: low risk if split from SQL/API phases.

### Phase 7: Release Gate

Goal: confirm the renamed surface is coherent and not half-migrated.

Required checks:

```bash
cargo fmt
cargo check -p pg_rvbbit
cargo test -p pg_rvbbit --lib
cargo check --manifest-path crates/rvbbit_duck/Cargo.toml
cargo test --manifest-path crates/rvbbit_duck/Cargo.toml
git diff --check
```

Database checks:

- Fresh install creates Beaverdam catalog tables/views.
- Extension update path reaches the same final catalog.
- Beaverdam refresh writes canonical layout files.
- Vortex layout build still works.
- Hive layout build is either correct or disabled by default with a clear
  status reason.
- Time-travel timeline works after at least two refreshes.
- Hot cache helpers work.
- Lance text/vector storage helpers work or fail with clear opt-in errors.
- Worker telemetry appears in the new tables/views.
- No old public SQL functions are accidentally left callable unless a deliberate
  short-lived compatibility window was chosen.

Benchmark smoke:

- ClickBench small and medium with auto router.
- ClickBench forced zoo slice including `duck_vortex`.
- TPC-H tiny and one normal scale.
- TPC-DS tiny and one normal scale.
- Sidecar load harness with shared worker enabled and disabled.

Documentation checks:

```bash
rg -n "LAKEHOUSE|Lakehouse|Rvbbit table|rvbbit table|refresh_acceleration|rebuild_acceleration|rvbbit\.compact|duck_sidecar|RVBBIT_COMPACT|RVBBIT_HOT_STORE" docs bench crates
```

Accept remaining matches only for:

- historical migration files,
- this plan,
- explicit release-note migration guidance,
- historical benchmark output examples.

## Open Decisions

1. Should `rvbbit.row_groups` become `rvbbit.beaverdam_row_groups` or
   `rvbbit.beaverdam_files`?

   `row_groups` is accurate for the current Parquet canonical layer and many
   planner stats. `files` is more generic for Vortex/Lance/Hive, but may be too
   vague. A practical compromise is `beaverdam_row_groups` for canonical file
   groups and `beaverdam_layout_files` for layout variants.

2. Should `rvbbit.time_travel_timeline(...)` be renamed?

   Time travel is backed by Beaverdam generations, but it is a user-facing SQL
   feature. Keeping the current plain name may be more ergonomic. Renaming to
   `beaverdam_time_travel_timeline` makes subsystem ownership clearer.

3. Should table-local Lance APIs move under Beaverdam names?

   If the API means "accelerate this Postgres table with Lance-backed files",
   it belongs under Beaverdam. If the API is a generic Lance utility, keep the
   direct `lance_*` name.

4. Should `rvbbit-duck` be renamed?

   Recommended v1 answer: no. Keep the binary name because it describes the
   concrete implementation and keeps the "try the extension" path simple.
   Public docs can call it the Beaverdam worker and state that the current
   worker binary is `rvbbit-duck`.

5. Should route candidate labels be renamed?

   Recommended v1 answer: no. Keep labels like `duck_vortex` and group them in
   the UI as Beaverdam candidates. Renaming benchmark system labels makes
   history harder to compare.

6. Should old SQL aliases exist for one release?

   Current preference: no. This is pre-release, and fallback aliases would
   become maintenance debt. If an alias is needed to keep a phase testable, it
   should be temporary and removed before the release gate.

## Suggested Implementation Order

Do not do the whole rename in one commit. A safe sequence is:

1. Finalize this plan and update release docs.
2. Rename docs/UI copy.
3. Rename catalog tables/views and fix the extension until core tests pass.
4. Rename refresh/rebuild/compact/hot/Lance functions and GUC/env names.
5. Rename worker telemetry tables/views and docs.
6. Update benchmark/training displays and route-explain grouping.
7. Run release-gate tests and benchmark smoke.

The risky phases are catalog rename and function/GUC rename. Keep each phase
small enough that a failed benchmark or e2e run points to one class of problem.

