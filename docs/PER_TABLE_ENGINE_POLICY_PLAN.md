# Per-table engine / layout policy — make routing & rebuild pathways table-scoped

> Status: **BUILT & verified live 2026-06-05** (uncommitted). Implemented essentially
> as designed below: `rvbbit.accel_policy.denied_engines/denied_layouts` +
> `rvbbit.set_table_engine(rel, target, enabled)`; the router gate in
> `candidate_denied_by_table_policy` (called from `candidate_availability`);
> `Candidate::engine()/layout()`; per-table rebuild gating via
> `table_denies_layout` in compact.rs; Freshness-tab engine/layout toggle chips.
> Idempotent `ALTER ... ADD COLUMN IF NOT EXISTS` migration included (review catch).
> Verified: `route_explain` shows deny → `*_vortex` / `duck_*` go unavailable with
> the policy reason; deny-vortex → rebuild materializes no vortex files; 104 pg_tests
> + 2 router unit tests green. Companion to `ACCELERATOR_FRESHNESS_PLAN.md`.

## The ask

Today the choice of execution **engine** (DuckDB sidecar vs in-process DataFusion
vs native PG vs pg_rowstore) and **layout** (parquet scan/vector, hive, vortex) is
**global** — controlled by GUCs (`rvbbit.route_duck_*`, `rvbbit.compact_vortex_layout`,
`rvbbit.df_inprocess`, …). We want to enable/disable engines/layouts **per table**, so
that from the UI or SQL you can:

1. change **rebuild semantics** per table (which layout files get materialized), and
2. **force routing choices** per table by *reducing pathways* — i.e. remove candidates
   so the router is constrained to the ones that remain.

This is **gating, not preference-weighting** — the clean model. You remove options and
let the learned profile / no-profile heuristic pick among what's left, instead of
fighting it.

## Verdict: LOW complexity (one chokepoint, re-checked everywhere, no cache work)

The architecture already funnels every routing path through a single function that
*already has table context*:

```
fn candidate_availability(candidate, features, tables: &[RvbbitTableMetric]) -> (bool, String)   // router.rs:4469
```

`RvbbitTableMetric` carries `.oid`. Crucially, **all three decision paths re-validate
through it**:

- no-profile heuristics — `candidate_availability(...)` at router.rs:1093 / 2437 / 4098
- trained-profile cache hit — `candidate_can_route()` (router.rs:4633) → `candidate_availability(...).0` (4642)
- forced (`rvbbit.route_force_candidate`) — `forced_route_decision()` (4652) → `candidate_availability(...)` (4657)

**Implication:** add a per-table allow/deny check at the top of `candidate_availability`
and it instantly applies to no-profile, profile-cached, AND forced routing. A denied
candidate returns `(false, "disabled for <table>")` and the existing fallback machinery
(`first_available_candidate`, profile-candidate-falls-through, `forced-unavailable →
native`) handles the consequence automatically.

**No cache-key changes.** Because availability is re-checked live on every decision
(including cached profile hits), a policy change takes effect on the next query with
zero invalidation. (If gating were baked into the shape-keyed cache — `shape_key()`
router.rs:3954 — we'd be in invalidation hell. We are not.)

## The two knobs (they couple)

1. **Rebuild semantics** — which layouts get *built* per table. The four gate helpers in
   `compact.rs` read a global GUC via `compact_setting(env, guc)`:
   - `vortex_layout_enabled()`  (compact.rs:364)
   - `hive_layout_enabled()`    (compact.rs:320)
   - `dual_layout_enabled()`    (compact.rs:274, cluster)
   - `sync_variant_layouts_enabled()` (compact.rs:284, master)

   Make each take `rel_oid` and check a per-table override first, else fall back to the
   global GUC/env. **The call sites already have `rel_oid`**, so this is ~4 tiny edits +
   one lookup helper.

2. **Route gating** — which candidates the router may *pick* for a table. The
   `candidate_availability` injection above.

**They couple for free:** deny a layout's *build* → its candidate auto-goes-unavailable
(no files on disk → `vortex_availability()`/`table_has_vortex_scan` returns false).
So the simplest UX is **one knob per table**: "vortex: off" ⇒ *don't build it and don't
route to it*. Splitting build-vs-route is a later refinement (only matters if you want to
build files you never route to — wasteful — or route-allow something you haven't built —
impossible).

## Recommended design

### Granularity = engine + layout, not 9 candidates
Toggle `duck` / `datafusion` / `native` (engine) and `vortex` / `hive` (layout). The
allowed candidate set is the surviving cross-product:
- "disable duck for table X" → kills `duck_vector`, `duck_hive`, `duck_vortex`
- "disable vortex" → kills `duck_vortex`, `datafusion_vortex`

Matches the mental model, far fewer toggles than 9 per-candidate switches. Model as
**deny-sets** (`denied_engines`, `denied_layouts`) so "no policy" = allow everything.

### Storage — extend `rvbbit.accel_policy`
Add columns to the existing per-table policy table (introduced in the freshness work):
```sql
ALTER TABLE rvbbit.accel_policy
  ADD COLUMN denied_engines text[] NOT NULL DEFAULT '{}',   -- subset of {duck,datafusion,native,pg_rowstore}
  ADD COLUMN denied_layouts text[] NOT NULL DEFAULT '{}';   -- subset of {scan,vector,hive,vortex}
```
Co-locates with freshness + rebuild policy; the Adaptive Routing → **Freshness** cockpit
tab is the natural UI home (a toggle row per table). Precedence:
**per-table override → global GUC → code default** (same pattern `compact_setting` uses).

### Plumbing — zero extra round-trips
The `RvbbitTableMetric` fetch (router.rs ~3668, already joins `rvbbit.tables`) gains the
two policy columns, so the gate check reads them straight off the metric struct already
fetched per query. The compact-side helpers do a small `Spi::get_one` lookup keyed by
`rel_oid` (or reuse the catalog read already happening in compact).

### The gate (sketch)
```rust
// inside candidate_availability(candidate, features, tables), first check:
let engine = candidate.engine();   // duck | datafusion | native | pg_rowstore
let layout = candidate.layout();   // scan | vector | hive | vortex
for t in tables {
    if t.denied_engines.contains(engine) || t.denied_layouts.contains(layout) {
        return (false, format!("{} disabled for {}.{}", candidate.as_str(), t.schema, t.relname));
    }
}
// ... existing availability checks ...
```

## The one real design decision: multi-table queries

The router picks **one** candidate for the whole query (whole-query, all-or-nothing —
confirmed: row-group pruning happens, but a single backend executes). So for a join:

> A candidate is allowed **iff every touched table allows it** (most-restrictive wins /
> any table can veto a pathway).

That's the safe, intuitive semantics and it's the simple `for t in tables` loop above. A
table with no policy vetoes nothing.

## Honest caveats

- **Forced candidate**: a per-table deny overrides a global `route_force_candidate`
  (force → unavailable → native). Arguably correct (veto wins); decide explicitly.
- **Build-vs-route waste**: only if you route-deny without build-deny. The unified knob
  avoids it.
- **Profile training self-prunes**: the trainer also goes through availability, so it
  won't waste A/B runs on a candidate a table has denied — nice, self-consistent.
- **Prepared-statement plan cache**: a backend that already *planned* a query keeps its
  plan until re-parse. Policy changes affect new parses immediately; in-flight prepared
  plans lag by one. Negligible; bump a version GUC if it ever matters.
- **`as_str`/`from_str` + engine()/layout() accessors**: `Candidate` (router.rs:43) needs
  small `engine()`/`layout()` mappers if they don't already exist.

## Scope / build order (≈ a day, same shape as accel_policy)

1. `ALTER TABLE rvbbit.accel_policy` + extend `set_accel_policy()` (freshness.rs) with the
   two deny-set args. pg_tests: upsert, effective view shows denies.
2. `Candidate::engine()` / `Candidate::layout()` accessors (if missing).
3. Per-table gate at the top of `candidate_availability` (router.rs:4469). Add the two
   columns to the `RvbbitTableMetric` fetch (~3668) + struct. pg_tests: deny duck →
   duck_* unavailable; multi-table intersection.
4. Make the 4 `compact.rs` layout helpers `rel_oid`-aware (per-table override → GUC).
   pg_test: per-table vortex off ⇒ no vortex variant built even with global on.
5. Freshness cockpit: a per-table engine/layout toggle row (`routing-freshness-tab.tsx`)
   + `setAccelPolicySql` extension.
6. Live E2E: deny vortex on one table, keep on another; confirm builds + routing differ;
   confirm a join across an allow + deny table denies the candidate.

## Why this is worth doing

It turns the router from a single global policy into a **per-table control surface** —
the same "reduce pathways to steer the system" lever, scoped where it belongs. It composes
with (doesn't fight) the learned profile, needs no cache redesign, and reuses the
`accel_policy` + Freshness-cockpit surfaces already built. Open choices to confirm before
building: **(a)** reuse `accel_policy` vs a dedicated `table_engine_policy` table;
**(b)** unified build+route knob vs separate from day one. Recommended: reuse + unified.
