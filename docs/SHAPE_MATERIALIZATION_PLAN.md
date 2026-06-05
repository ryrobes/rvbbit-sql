# Shape Materialization — unifying the late-bound data plane

Status: **design** (2026-06). Foundational refactor that ties together synth,
flow/pipelines, the lens projection, and the cache.

## The through-line

Postgres is **early-bound**: every column, type, and plan is fixed at parse/analysis
time, before a row moves. rvbbit's semantic layer is inherently **late-bound**: the
SQL, the shape, and the logic aren't knowable until the model has seen the intent
*and* (sometimes) the data. Almost every mechanic we've built is a bridge across
that seam, and each does one of three moves:

1. **Defer shape to runtime** — `jsonb` is the carrier. `flow()` pipes jsonb between
   stages, `synth()` returns `SETOF jsonb`, operators speak jsonb. It's the "any
   shape" escape hatch Postgres' type system accepts.
2. **Compile-and-cache to amortize** — the model is a *memoized compiler*: `reshape`
   (value-shape → expression), `pivot`/`group` (rowset-shape → SELECT over `_input`),
   `synth` (intent + retrieved schema → SELECT over real tables). Invoke once per
   distinct shape, cache the deterministic SQL. **Memoizing the late-binding collapses
   it back to early-binding after first contact.**
3. **Re-materialize to interoperate** — the last mile: turn runtime-shaped jsonb back
   into real typed columns so early-bound consumers (CTEs, the rollup/drag-out UI,
   charts, the profile tab, downstream pipeline stages) can use it.

Every friction point we've hit is the *same seam* — late-bound data re-entering an
early-bound consumer:

- "Can't `SELECT season FROM synth()` in a CTE" — parse-time needs static columns.
- "Drag-out treats fields as jsonb" — the rollup classifier needs real types.
- "`synth(…) then analyze()` sees `value`, not the fields" — the stage needs the
  expanded rowset.

And the fix was always the same: **re-materialize the shape.**

## The problem this plan fixes

We currently re-materialize the *same shape* in **three** independent places, and
two of them **guess the types by sampling rows**:

| Layer | Implementation | Schema source |
| --- | --- | --- |
| Client / JS | `expandFlowResult` + `inferJsonbColumns` | inferred from sampled result rows |
| SQL | `buildJsonbProjection` (`(__v->>'k')::numeric …`) | the same client inference |
| Server | *(missing)* `flow()` single-jsonb-head unwrap | — |

Three implementations of one operation — "give this jsonb a typed relational
shape" — and the client ones sample data (hence the numeric-drift and
first-N-rows caveats). Meanwhile **jsonb-per-row is the wrong long-term substrate**:
it serializes every value, carries no column stats, defeats the planner, and bloats
storage. For the 50M-row pipeline scenarios this is the scaling ceiling.

The key observation: **the authoritative schema already exists, for free, at the
compiler.** `synth_sql` already `PREPARE`s the generated SQL to validate it, and a
prepared statement's tuple descriptor is the exact column names and Postgres types
of `SELECT season, count(*) …`. We have the truth in hand and throw it away,
forcing three downstream re-derivations.

## Strategy

Two prongs, same root idea — *the compiler emits the schema; everyone downstream
consumes it; materialize into typed relations instead of jsonb blobs where it
scales.*

### Prong 1 — the compiler emits the schema

Capture the result schema (column names + Postgres type OIDs) at generation time
and persist it next to the cached SQL. Then every re-materialization consumes that
one truth instead of re-inferring:

- the lens replaces `inferJsonbColumns` with the emitted schema (drift caveats gone);
- `buildJsonbProjection` casts to the authoritative types;
- the flow head-unwrap (below) knows the head's shape without sampling;
- base-SQL users get a real column list to compose against.

### Prong 2 — materialize into typed relations, not jsonb blobs

Where we currently pass/return jsonb, prefer a typed relation:

- **`synth`**: keep `SETOF jsonb` as the convenience form, but add a path that yields
  real typed columns — e.g. a per-shape **typed view/function** generated from the
  cached schema (`rvbbit.synth_view(name, intent)` → a real relation you can `JOIN`,
  CTE, and aggregate in plain SQL, no jsonb projection needed).
- **`flow()` stages**: materialize each stage's output into a **typed temp relation**
  (or a known-schema tuplestore) instead of a `Vec<jsonb>`. The synth-sql stages then
  run over a real `_input` table with stats, not `jsonb_to_recordset`; value-mode
  stages still see jsonb only when the model genuinely needs the raw rows. This is the
  scaling win: the planner can index/hash/stream typed relations.

Both are unlocked by Prong 1 — you can only materialize a typed relation once you
know the types.

## What it retires

- **Lens type inference + drift caveats** → consume the emitted schema.
- **`flow()` head-unwrap loose thread** → becomes "materialize the head with its
  emitted schema"; this also makes `synth(…) then …` work and lets the lens project
  flow blocks (today the projection is synth-only because a bare-`then` block's SQL
  isn't valid standalone).
- **Self-filtering a synth block** → the materialized relation has real columns, so a
  `WHERE season = …` self-filter just works (today it's suppressed).
- **jsonb serialization overhead** at pipeline scale.

## Phasing

- **Phase A — schema capture (foundational).** At generation, capture the generated
  SELECT's column schema (names + type OIDs) via the prepared statement / a
  `SELECT … WHERE false` tuple descriptor, inside the existing read-only validation
  subtransaction. Add `result_schema jsonb` to `rvbbit.synth_cache`; populate it in
  `synth_cache_put`. Expose it: `rvbbit.synth_schema(intent, operator) → TABLE(name,
  type)` (and surface in the Cache app). No behavior change yet — pure enablement.
- **Phase B — lens consumes the emitted schema.** Replace `inferJsonbColumns` with the
  server schema where available (fall back to inference when absent). Retires the
  numeric-drift / first-N-rows caveats; `buildJsonbProjection` casts to real types.
- **Phase C — typed `synth` relation.** `rvbbit.synth_view(name, intent[, operator])`
  generates a view over the projection using the cached schema → a real typed
  relation for plain-SQL composition (CTEs, joins). Optional `synth_record` variant.
- **Phase D — flow stage materialization.** Materialize flow stage outputs into typed
  temp relations; synth-sql stages run over real `_input`. Single-jsonb-head unwrap
  falls out (materialize the head with its schema). The scaling win.

Phase A is the linchpin and is self-contained; B–D build on it independently.

## Notes / risks

- Capturing the schema plans the SQL (constant-folding can fire `IMMUTABLE`-labelled
  functions). Do it in the same always-rolled-back read-only subtransaction the
  validator uses, so any plan-time side effect is undone — same guarantee as
  execution. `SELECT … WHERE false` reads no rows.
- Type OID → name mapping: store the OID and resolve via `format_type`/`regtype` for
  display; the lens already maps the common numeric OIDs.
- Keep `SETOF jsonb` as the default surface (it's what makes the convenience form and
  the `then` interchange work); the typed forms are additive.
