# Future Vortex — what to revisit as the project matures

A forward-looking watch-list for rvbbit's Vortex integration. Captured 2026-06 after
shipping the native+vortex arc (read-swap → projection pushdown → filter pushdown →
`RvbbitNativeVortex` router candidate). The goal: when Vortex ships something new on
the way to / past 1.0, know what it unlocks for us and what to do.

See also: `docs/NATIVE_VORTEX_PLAN.md` (the as-built design), `crates/pg_rvbbit/src/vortex_adapter.rs`
(the *only* module that imports `vortex::*` on the read path — the churn firewall).

## The standing principle: format stabilizes, the library keeps moving

Vortex (now a Linux Foundation / LFAI&Data project under `vortex-data`) is taking the
**file format** to 1.0 via "editions" — backwards-compatible since 0.36, so our written
`.vortex` files survive library upgrades with no data rewrite. But the maintainers have
said they have **no plans to give the Rust/Python/etc. libraries a semantic 1.0** — the
*library API* will keep churning (roughly weekly). That matches what we lived:

- **Keep every `vortex::*` import inside `vortex_adapter.rs`.** It already is. The churn
  surface is the scan/expr API (`ScanBuilder`, `expr::{select,col,eq,…}`,
  `into_array_stream`, `into_arrow_preferred`). One file changes when we bump `vortex`.
- **Pin the `vortex` version** and bump deliberately; re-run `bench/verify_vortex_filter.py`
  + the `native-vortex-verify.sql` byte-identical A/B after every bump.
- Forwards-compat is explicitly *not* in the v1 format — a newer writer's file may not read
  on an older lib. Keep the writer and reader on the same `vortex` version (we do — one crate).

## Why native+vortex will never beat duck+vortex (don't re-litigate this)

rvbbit's native CustomScan emits tuples **one at a time** (PG's Volcano executor; ~77ns/row
is emission overhead, not decode). Vortex's compute-over-compressed advantage only lands in a
**vectorized, parallel** consumer — which is exactly why `duck+vortex` crushes everything,
including `duck+parquet`. So:

- **Don't** build a vectorized native aggregate executor — it duplicates rvbbit's rewriter
  projected-aggregate path *and* duck+vortex, and is still capped by the row-at-a-time ceiling.
- **Do** keep native+vortex for what it's good at (below), and route big parallel analytics to
  `duck_vortex` / `datafusion_vortex` (already the router's policy).

## Watch-list — when Vortex ships X, do Y

| Watch for (Vortex) | Status today (0.73) | rvbbit action | Query shapes it unlocks |
|---|---|---|---|
| **Aggregate / GROUP-BY terminator in the scan API** (compute count/sum/min/max/group over compressed, no full Arrow decode) | **Not present.** `ScanBuilder` exposes only `with_projection` + `with_filter`; aggregation always full-decodes to Arrow then folds, so vortex == parquet on full-scan aggregates. This is the single biggest gap. | Wire it into the **rewriter's projected-aggregate fold** (`scan.rs` `vector_float_agg` / `scan_numeric_sum_count`), which currently reads parquet only. That path is already rvbbit's vectorized analytical engine — feeding it source-side vortex aggregation is the highest-ROI future change. | Analytical aggregates / group-bys (ClickBench-shaped). Could close much of the gap to duck+vortex on the *non*-parallel path. |
| **GPU: `vortex-cuda` / decode-S3→GPU / GPU compute kernels** | Roadmap, **unpublished**. The format is *designed* for direct-to-GPU decode (skip the CPU), but no crate yet. | Add a new router engine/layout (`vortex_gpu` Candidate) gated on GPU hosts (rvbbit already has GPU host targeting via Warren). Route large scans/aggregates there when a GPU is present. | Large scans + aggregates where GPU throughput dominates; embedding/vector workloads co-located with the data. Potentially transformative for the analytical path. |
| **`Selection::IncludeByIndex`** (push an explicit candidate row-set into the scan) | **Present in 0.73**, but we deferred wiring it (Phase 5 in the plan). | Combine **Lance KNN** (or a bloom/zone prefilter) → candidate row ids → `with_selection(IncludeByIndex)` → vortex materializes only those rows. | KNN + SQL filter hybrids; late-materialization "top-N after a cheap prefilter". A genuinely *differentiated* move (candidate-set into a columnar scan) we can do before anyone else. |
| **More compute-over-compressed kernels** (ops evaluable without decode) | Eq/Lt/Gt/Le/Ge, case-sensitive LIKE (FSST), IN-as-OR are pushed today via `vortex_adapter::translate`. ILIKE / NOT LIKE / regex / BETWEEN / IS NULL are residual. | Extend `translate` + `lower_pushed_qual` as Vortex adds compressed kernels for those ops. | Selective string/dict filters (FSST/Dict columns) — the case where compute-over-compressed already shows a small edge. |
| **Date / Timestamp / Decimal literal pushdown** maturity | We push int/text/bool/float8 filters; **date/timestamp/numeric are residual** (literal-vs-column type matching + epoch handling we punted on). | Extend `lower_pushed_qual` (typoid gate) + `scalar_lit` to emit `Date32`/`Timestamp(µs)`/`Decimal` literals once the `expr`/`scalar` API stabilizes at 1.0. | **Time-range filters on the clustered time column** — the most common analytical WHERE; lets the source zone-prune instead of rvbbit's chunk-level pruning alone. |
| **Richer per-zone statistics** (histograms, bloom, distinct sketches beyond Min/Max/NullCount) | ZoneMapLayout ships Min/Max/NullCount; we read these into `rvbbit.row_group_variants.stats` for chunk pruning. | Read any richer stats Vortex exposes into our prune path (`row_group_may_satisfy`) for tighter skipping. | Equality / membership / range filters on high-cardinality columns where Min/Max is too coarse. |
| **Internally parallel / multi-threaded scan** in the library | Async single stream today. | Mostly benefits the duck/datafusion+vortex routes; native stays Volcano-bound. Re-evaluate only if it changes the cost model materially. | Large cold scans (I/O + decode parallelism). |

## Where native+vortex can already win (and gets better as the above land)

These are the shapes the `rvbbit_native_vortex` candidate should be trained/profiled to pick:

- **Highly selective filtered scans on a clustered column** — chunk pruning (engine-agnostic) +
  vortex's smaller compressed footprint = less cold I/O. Already competitive; better with
  date/ts pushdown.
- **Point lookups / small-N** — the native sweet spot (Volcano tax paid on few tuples), and
  vortex compression means less to read.
- **Wide-string selective `LIKE`** — FSST compute-over-compressed avoids decoding non-matching rows.
- **Vector + SQL hybrids** — once `IncludeByIndex` is wired (Lance KNN candidates → vortex scan).

It costs nothing to keep the candidate around: the `.vortex` files are already built for the
duck/datafusion vortex routes, so `rvbbit_native_vortex` is "free" and a learned profile can pick
it where it ties or wins (we already measured native+vortex beating native+parquet on some shapes).

## Quick re-validation checklist after any `vortex` bump

1. `cargo check -p pg_rvbbit --features pg18`
2. `bench/verify_vortex_filter.py` (filter-shape correctness, parquet vs vortex)
3. `docker/sql/native-vortex-verify.sql` (byte-identical A/B incl. timestamps + Cyrillic text)
4. Force `route_force_candidate='rvbbit_native_vortex'` + `EXPLAIN` → confirm `Rvbbit Layout: vortex_scan`.

## Sources
- [Towards Vortex 1.0 — SpiralDB](https://spiraldb.com/post/towards-vortex-10)
- [What if we just didn't decompress it? — SpiralDB](https://spiraldb.com/post/what-if-we-just-didnt-decompress-it)
- [Vortex: a Linux Foundation Project — SpiralDB](https://spiraldb.com/post/vortex-a-linux-foundation-project)
- [vortex-data/vortex (GitHub)](https://github.com/vortex-data/vortex) · [`vortex-scan`](https://github.com/spiraldb/vortex/tree/develop/vortex-scan)
- [vortex.dev](https://vortex.dev/)
