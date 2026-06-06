# Native + Vortex — Phased Implementation Plan

**Status:** design (not started) · **Date:** 2026-06-06
**Author:** design pass + adversarial review (code-architect blueprint, 2 reviewers)
**Prereqs read:** the native-reads-vortex gap analysis and the Vortex feature ROI survey (this session).

---

## 1. Thesis

rvbbit's `native` route is a thin in-process Arrow reader that materializes Postgres
tuples directly (no DataFusion planning, no DuckDB sidecar). It wins at low/medium
row counts where engine setup dominates. Vortex is not "Parquet but smaller" — it is a
**compute-capable** columnar format (filter-over-compressed + zone-map pruning), and
those tricks are reachable from the **embedded Rust path** the native scan uses.

**The high-ROI version of native+vortex is NOT a reader swap.** Feeding the existing
native Arrow loop with Vortex bytes gets you storage/decode parity only. The win is
having the native CustomScan materialize tuples **from Vortex's `ScanBuilder`** — push
projection + a translated filter expression into Vortex, let it prune zones and filter
compressed data, and only materialize surviving rows. That is how native+vortex can
**beat** native+parquet on filtered scans, not just match it.

Out of scope (decided in the ROI survey): GPU (`vortex-cuda` is unpublished/roadmap),
Vortex-for-vectors (keep Lance — Vortex has no ANN index), custom encodings, aggregate/
GROUP BY pushdown (not in the 0.73 scan API). The encoding cascade + decode speed is
already realized (vortex layout default-on).

---

## 2. Architecture decisions

- **New candidate `Candidate::RvbbitNativeVortex` ("rvbbit_native_vortex", route `native_vortex`).**
  Executes inside the *same* native CustomScan callback chain as `RvbbitNative`, but
  swaps `ParquetRecordBatchReader` for a Vortex `ScanBuilder`-driven reader. A distinct
  candidate (vs a capability flag on `RvbbitNative`) is required so the adaptive cost
  model can learn it as its own cost curve — but that means the cost-model machinery has
  to be widened to know about it (see Phase 4; this is real work, **not** free).
- **Two separate signals, not one.** The static GUC `rvbbit.native_vortex` *enables* the
  path; a **per-query `thread_local` flag** (mirroring `PG_ROWSTORE_ROUTE_SELECTED`,
  `router.rs:762`) set by the router's decision *activates* it for a given query. The
  executor checks both. (Review correction: the rewriter does **not** gate native routes
  — non-duck/df candidates fall through to the CustomScan by construction, so the GUC
  alone would mean "always vortex," skipping the router.)
- **All Vortex API calls behind one adapter module** (`vortex_adapter.rs`). Vortex's
  library API is pre-1.0 and churns ~weekly (0.74 already reshaped scan traits); the file
  format is stable since 0.36 (no data-rewrite risk). Isolation = only `vortex_adapter.rs`
  upgrades on a version bump.
- **Timestamp fix on the read side**, using the PG `typoid` we already have — local,
  no re-compaction of existing files (see Phase 1/3).

### Confirmed seams (code-verified)
- `current_reader: Option<ParquetRecordBatchReader>` — `custom_scan.rs:140`
- `RowGroupReader::open_projected(path,&col_refs)` — `custom_scan.rs:2859` (def `rvbbit_storage/src/row_group.rs:1176`)
- `make_reader_for` (`Int64`@2295, `Timestamp`@2314) / `read_via` epoch offset @3369 — the timestamp seam
- `fill_slot_from_batch` @3223 · `ColumnReader` enum @226 · `current_filter_bitmask` @196 · `delete_bitmaps` @206
- RG pruning `row_group_may_satisfy` @1674 · `row_group_clause_impossible_stats` @1727
- `write_vortex_record_batch` → `column_stats: Vec::new()` @`compact.rs:2467`; timestamp→Int64 cast @`compact.rs:2382`; multi-chunk synthetic rg_id @`compact.rs:2355-2361`
- shared tokio RT `df::with_lance_runtime` (`pub(crate)`) @`df.rs:233`
- router `Candidate` enum @`router.rs:659`; `candidate_availability` @4556; `candidate_gate_enabled` @4432; `choose_route` @1948; cost-curve `choose_from_observation_curve` @2530 (hardcoded `candidate IN (...)`); `RouteCurveSample` @880; `route_profile_points` positional reads @2395
- rewriter allowlist `try_duck_backend_rewrite` @`rewriter.rs:692`; `PG_ROWSTORE_ROUTE_SELECTED` thread_local @`router.rs:762`

---

## 3. Milestones

- **Minimal** (end of Phase 3): `SET rvbbit.native_vortex=on` + per-query activation → native
  *correctly* reads vortex files (projection only, no filter push), values byte-identical
  to parquet incl. timestamps/strings/`real[]`. Proves the seam.
- **Full ROI** (end of Phase 4): the router routes *filtered* scans to `rvbbit_native_vortex`;
  zone-map pruning + compressed-filter pushdown active; the adaptive model learns when it
  beats parquet. This is "native+vortex beats native+parquet on filtered scans."

---

## 4. Phases

### Phase 0 — Scaffolding / pre-work  · effort **M** · ✅ IMPLEMENTED + LIVE-VALIDATED 2026-06-06 · ships dark
**Goal:** contain the API surface, add the gate, wire the shared async RT, and **prove the
0.73 scan API shape** before building on it. **Resolved: 0.73 has NO sync `RecordBatchReader` —
the read path is async `into_array_stream().read_all()` → `into_arrow_preferred()` →
`StructArray` → `RecordBatch`, driven by `df::with_lance_runtime` `block_on`.**

- **0a. `crates/pg_rvbbit/src/vortex_adapter.rs`** — the *only* module importing `vortex*`.
  Public surface (stubs now): `open_vortex_projected(path, col_names, filter, pg_attrs) -> Result<VortexRgReader,String>`,
  `next_batch(&mut VortexRgReader)`, `vortex_file_column_stats(path) -> Vec<VortexColumnStat>`,
  `VortexPushedFilter`. Uses `df::with_lance_runtime` for `block_on`.
- **0b. GUC** `rvbbit.native_vortex` (default off) — `native_vortex_enabled()` in `duck_backend.rs` (~after :252).
- **0c.** `mod vortex_adapter;` in `lib.rs`.
- **0d. API-shape spike (exit gate):** a `#[test]` that runs the **full chain** —
  `VortexOpenOptions::open → scan() → with_projection → into_record_batch_reader/stream → next batch → collect`
  — on a known `.vortex` file. **No existing code calls `ScanBuilder` directly** (df.rs uses
  the higher-level `VortexFormat`), so this spike resolves the load-bearing unknown: does
  0.73 return a **sync `RecordBatchReader`** or an **async `Stream`**? That choice dictates
  whether `VortexRgReader` wraps a reader or a pinned stream + per-batch `block_on`.

**Exit:** ✅ `cargo check` clean; **live-probed** via `rvbbit.vortex_native_probe(path)` on a real
`.vortex` row group → `{ok:true, rows:1048576, cols:105, batches:1}`, row count **== parquet rg0
exactly**. Shipped: `vortex_adapter.rs` (reader + probe), `native_vortex_enabled()` GUC, `mod` reg.
(The probe is a loose `#[pg_extern]`; it needs a migration/version-bump to register in a deployed DB —
validated here by a temporary manual `CREATE FUNCTION` against the rebuilt `.so`.)
**Risk (confirmed OK):** RT re-entrancy — `with_lance_runtime` `block_on` from a sync PG callback works (verified live, no nesting).

---

### Phase 1 — RG-pruning stats at write  · effort **S** · ✅ IMPLEMENTED 2026-06-06 (uncommitted) · independently shippable
**Goal:** populate per-column min/max/null_count for vortex variants so rvbbit's **row-group
pruning** (`row_group_may_satisfy`) works on the native+vortex path. **(This does NOT unblock the
router's cost model — that is observed-`elapsed_ms`-driven; see Phase 4.)**

**Implemented — reuse the canonical parquet stats fn on the batch; no vortex-footer read, no epoch math:**
- `rvbbit_storage::row_group::compute_arrow_stats(&RecordBatch, text_stats)` made `pub` (was private `fn`).
- `write_vortex_record_batch` (compact.rs) now computes `compute_arrow_stats(&batch, false)` BEFORE the
  batch is moved into the Vortex writer, returning it as `RowGroupMeta.column_stats` (was `Vec::new()`).
  The variant INSERT (`register_variant_chunks`) already serializes `column_stats` to
  `rvbbit.row_group_variants.stats` exactly like the parquet path — **no schema change**.

**⚠ Timestamp epoch — the original Phase-1c was WRONG (caught by review, dropped):** parquet stores
timestamp min/max in **unix-epoch micros**, and `row_group_clause_impossible_stats` applies **no**
offset for non-DATE types — there is no "PG-epoch convention." Reusing `compute_arrow_stats` (timestamps
arrive as Int64 unix-epoch micros post the `vortex_record_batch_for_plans` cast → the Int64 arm yields
the same unix-epoch micros) produces **byte-identical** stats to parquet. The proposed
`− PG_EPOCH_OFFSET_MICROS` would have *corrupted* them → removed. NOTE: timestamp predicate pushdown
isn't wired yet (`extract_const_value` has no arm for typoids 1114/1184), so timestamp stat-pruning is
currently dead code; when Phase 2/3 adds it, the conversion belongs on the **RHS**
(`rhs + PG_EPOCH_OFFSET_MICROS`, PG-epoch→unix), mirroring the existing bloom path — NOT on the stats.

**Exit:** ✅ `cargo check` green; **live-validated** — a scratch `compact()` + `refresh_layout_variants()`
wrote a `vortex_scan` variant whose `stats` carry correct per-column min/max/null_count (e.g. `id` min 1
max 3000, `amt` min 1.5 max 4500.0); was empty `[]` before. NOTE: vortex variants are built by
`refresh_layout_variants` / `rebuild_acceleration` / `accel_tick`, NOT the initial `compact()`.

---

### Phase 2 — PG qual → Vortex expr translator  · effort **M** · ✅ IMPLEMENTED 2026-06-06 (translator + repr + unit tests; `cargo check --tests` green) · LOWERING deferred to Phase 3
**Goal:** translate the pushable subset of rvbbit's qual tree to `vortex::expr`; everything
else stays a Postgres residual qual (correctness backstop).

**Implemented:** `scan_types.rs` (PG-free `FilterRepr`/`QualRepr`/`CmpOp`/`LitRepr`) +
`vortex_adapter::translate(&FilterRepr) -> Option<VortexPushedFilter>` (Option-safe: any
non-expressible combo → None → no pushdown) + 5 unit tests. Ops: `=,<,<=,>,>=`, `IN`→`eq OR eq`,
`LIKE` (case-sensitive). Both `translate`/`VortexPushedFilter` are `#[allow(dead_code)]` until the
Phase-3 reader consumes them. **Deferred to Phase 3:** the *lowering* (`custom_scan`'s private
`PushedQual` tree → `FilterRepr`, resolving attnum→name + the timestamp RHS `+PG_EPOCH_OFFSET` and
pushable/residual split) — it belongs with the reader that uses it.

- **2a. (review correction) `scan_types.rs`** — `PushedQual`/`PushExpr`/`PgAttr` are private to
  `custom_scan.rs`; to keep the translator in `vortex_adapter` (vortex-isolated) **and**
  unit-testable, define a plain-Rust `PushedExprRepr` mirror. `custom_scan.rs` lowers its private
  tree → `PushedExprRepr`; `vortex_adapter::translate(repr, pg_attrs) -> Option<VortexPushedFilter>`.
- **2b.** Translation table (blueprint: `vortex-datafusion/convert/exprs.rs`): `=,!=,<,<=,>,>=`,
  `BETWEEN`, `IN`→`or(eq…)`, `IS [NOT] NULL`, `AND`/`OR`, `LIKE` (case-sensitive only). **Not pushable**
  (→ residual): `ILIKE`, regex `~`, modulo, bitwise, `IS DISTINCT FROM`, string-concat, JSONB eq.
- **2c. Timestamp RHS:** for `TIMESTAMP[TZ]` columns the stored value is unix-epoch, so push the
  comparison with `pg_val − PG_EPOCH_OFFSET_MICROS` as the RHS.
- **2d. V1 policy:** push only if the **whole** filter is translatable; otherwise no push (zone
  pruning still benefits from whatever filter *is* set). V2 can split top-level AND conjuncts.

**Exit:** unit tests — Eq push, range push, ILIKE not-pushed, AND-of-pushables, timestamp epoch.
**Risk:** NULL/collation/coercion mismatch vs Postgres → keep residual quals as the backstop; never
let a pushed filter be the *only* correctness gate.

---

### Phase 3 — ScanBuilder-driven native reader (the core seam)  · effort **L** · GUC-gated
**Goal:** when activated, open the vortex variant via `vortex_adapter` instead of the parquet
reader, materialize through the **existing** `fill_slot_from_batch` path. Handle all gotchas.

- **3a.** `open_vortex_projected` + `VortexRgReader` + `next_batch` in `vortex_adapter.rs`
  (`with_projection` + `with_filter` + `into_record_batch_reader|stream` inside `with_lance_runtime`).
- **3b.** `RustScanState.current_vortex_reader: Option<VortexRgReader>` (coexists with `current_reader`).
- **3c.** Add `n_rows` to `RowGroupEntry` + extend the `fetch_row_group_paths` SPI (for the low-row-count gate).
- **3d.** Exec batch-pull (~:2787): pull from `current_vortex_reader` when set, else `current_reader`.
- **3e.** Row-group-open (~:2859): if **GUC on AND per-query flag set AND** a `vortex_scan` variant
  exists for this `rg_id` AND `n_rows ≥ floor` AND no tombstones → open vortex (fallback to parquet on any error, `DEBUG1`).
- **Canonicalization (gotcha):** in `next_batch`, resolve `Utf8View/LargeUtf8 → Utf8` (native expects
  `StringArray`; df.rs already forces `with_force_view_types(false)` for parquet) and `LargeBinary→Binary`.
- **Timestamp (gotcha, read-side):** for `TIMESTAMP[TZ]` columns vortex yields `Int64`; re-cast to
  `Timestamp(Microsecond, UTC)` in `next_batch` so `make_reader_for` hits the `Timestamp` arm and the
  `−PG_EPOCH_OFFSET_MICROS` fires (@3369). *Verified by review:* the vectorized-bitmask and per-row
  predicate paths are then also correct (they key on `TimestampMicrosecondArray`). Confirm `compact`
  writes `Timestamp(Micros, UTC)` for `timestamptz`.
- **Tombstones (gotcha):** single-chunk vortex `rg_id == parquet rg_id` → bitmaps apply. **V1 guard:**
  if `delete_bitmaps` is non-empty for the table, fall back to parquet entirely (multi-chunk synthetic
  `rg_id` would miss tombstones). Decide later: enforce 1 vortex file per canonical rg.
- **Low-row-count (gotcha):** vortex file-open floor (≥64KB + two round trips + layout deser) loses to
  native/in-mem on small cold reads → skip vortex when `n_rows < floor` (default ~8192, GUC-tunable).
- **⚠ Batch cache (review blocker):** **vortex reads must bypass `ScanBatchCache`.** Its key is the
  *parquet* path; canonicalized/filtered vortex batches would collide on type variants or serve a
  partial row set. Before opening vortex: set `current_cache_key=None` + clear the accumulator; never
  store vortex batches.
- **⚠ Rescan (review high):** `rescan_custom_scan` (@3559) and the reader-exhaust branch (@2815) must
  set `current_vortex_reader=None` — else correlated subqueries / cursor rescans hit a spent reader →
  premature EOF / wrong results.

**Minimal-milestone exit:** with GUC on + forced activation, `count(*)`, `sum(col)`, full `SELECT *`
match parquet **exactly** (values, timestamps, strings, `real[]`); add `#[pg_test]`s incl. a
`timestamptz` round-trip (30-yr offset is the canary), a correlated-subquery rescan, and a JSONB/`real[]` table.

---

### Phase 4 — Router pricing (candidate + availability + cost-model widening)  · effort **M–L** · needs 0–3
**Goal:** the router can *choose and learn* `rvbbit_native_vortex`. **(Review: most of the cost-model
plumbing is here, not Phase 1.)**

- **4a. Candidate** — add `RvbbitNativeVortex` to the enum + `all()` + `as_str/route/from_str/engine/layout`.
- **4b. Per-query activation** — new `thread_local NATIVE_VORTEX_SELECTED` (mirror `PG_ROWSTORE_ROUTE_SELECTED`@762),
  set when `choose_route` picks it; the Phase-3 executor checks it (with the GUC).
- **4c. Availability** — `native_vortex_availability()` dispatched from `candidate_availability`@4556:
  GUC on; no regex; **no tombstones**; `vortex_temporal_allowed()` reuse; `vortex_scan` variant present
  for all tables; `table_rows ≥ floor`. Gate in `candidate_gate_enabled`@4432.
- **4d. choose_route** (@1948–2131): on a filtered, non-grouped scan that would pick `RvbbitNative`,
  prefer `RvbbitNativeVortex` when available; set the activation flag.
- **4e. ⚠ Cost-model widening (review blocker — the real Phase-4 cost):** add a `native_vortex_ms`
  dimension everywhere the learned model lives — `RouteCurveSample`@880, `CandidateBuckets`@892,
  `interpolate_predictions`@6478, the **hardcoded `candidate IN (...)` string literal** in
  `choose_from_observation_curve`@2530 (Rust change, *not* a migration), and the **positional**
  `route_profile_points` SELECT@2395 / INSERT@5586. Without this the new candidate is invisible to the
  curve/profile learner and can never be priced.
- **4f. ⚠ SQL migration (review-corrected table list):** new migration in the version chain
  (note: control `default_version` and the `sql/pg_rvbbit--A--B.sql` filenames are currently
  inconsistent — confirm the right next version). Widen the candidate/choice CHECKs on
  `route_observations`, `route_decisions`, `route_executions`, `route_training_results`, **and
  `route_profile_entries` (choice CHECK)**; **`ALTER TABLE route_profile_points ADD COLUMN
  native_vortex_ms double precision`** (+ its `>0` CHECK). **Do NOT touch `route_shadow_decisions`**
  — it has no candidate CHECK. Apply before any query logs the new candidate (or soft-fail route logging until applied).
- **4g. (deleted)** ~~rewriter.rs / planner.rs native dispatch~~ — **not needed.** `rvbbit_native_vortex`
  is absent from `try_duck_backend_rewrite`'s allowlist (@692) so it correctly falls through to the
  CustomScan. The only dispatch is the executor flag (4b/Phase 3).

**Full-ROI exit:** `route_explain('… WHERE …')` → `rvbbit_native_vortex`; `route_observations`/
`route_profile_points` accumulate native_vortex timings; a high-selectivity filtered scan is
measurably faster GUC-on vs off; values identical to parquet.

---

### Phase 5 — KNN `Selection::IncludeByIndex` spike  · effort **S→M** · optional
**Goal:** after Lance returns top-K row ordinals, fetch attributes via
`ScanBuilder::with_selection(Selection::IncludeByIndex(..))` instead of re-opening parquet — a
differentiated "candidate-set into the columnar scan" move. **Decision gate:** only if Phase 3
shows vortex file-open < ~5ms (KNN is few-row/low-latency; a 10ms+ open always loses to parquet).
Verify the 0.73 `Selection` payload type at compile time.

---

## 5. Hardest items / decision gates
1. **0.73 scan API shape** (sync reader vs async stream) — resolve in Phase 0 spike; it shapes `VortexRgReader`.
2. **Timestamp correctness on both axes** — read-side re-cast (Phase 3) **and** stat epoch normalization (Phase 1). Both required.
3. **Cost-model widening** (Phase 4e) — the unglamorous but load-bearing work; the candidate is inert without it.
4. **Batch-cache bypass + rescan clear** (Phase 3) — correctness, easy to forget.
5. **Decision gates:** timestamp fix read-side (chosen) vs write-side; per-query flag (chosen) vs GUC-only; tombstone tables fall-back-to-parquet (V1) vs enforce-1-file-per-rg (V2).

## 6. Implementation map
| Phase | Create | Modify | Effort |
|---|---|---|---|
| 0 | `vortex_adapter.rs` | `lib.rs`, `duck_backend.rs` | M |
| 1 | — | `vortex_adapter.rs`, `compact.rs` | S–M |
| 2 | `scan_types.rs` | `vortex_adapter.rs`, `custom_scan.rs`, `lib.rs` | M |
| 3 | — | `vortex_adapter.rs`, `custom_scan.rs` | L |
| 4 | SQL migration | `router.rs` (+ duck_backend GUC wire) | M–L |
| 5 | — | `vortex_adapter.rs`, `lance.rs` | S/spike |

Build dark behind `rvbbit.native_vortex=off`; Phases 0–2 are independently shippable; Phase 3 is the
minimal milestone; Phase 4 is full ROI. Keep `vortex` pinned to `0.73` until 0–4 land; upgrade touches
only `vortex_adapter.rs`.
