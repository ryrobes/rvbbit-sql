# Kit Plates — the contract (draft, v0.1)

The second app species — **plates**: a prepared surface an image is pressed
from, which is exactly what these are (templates pressed from data), and it
extends the photography metaphor the product already owns — the lens, the
photographed scenes. Each kit's gated entry plate is its **switchboard**
(thanks, Access '95). Iframe apps stay forever as the unlimited sandbox;
plates are the curated shelf: server-rendered, sanitized, SQL-driven surfaces
that ship **as rows in the database** and render natively in Data Rabbit — no
iframe, native theme, direct data path. This is the PowerBuilder/Lotus-forms
layer that kits (mapping surfaces, onboarding checklists, audit queues) are
authored on.

**Reference companion:** the complete SQL API, tables, template
vocabulary, and lifecycle live in `KITS_REFERENCE.md` — this document is
the rationale and the as-built history; that one is the lookup.

**This document is the ABI.** Kits are versioned artifacts sitting in customer
databases rendering against whatever lens they run. Everything else in the kit
stack can be wrong and fixed later; the plate contract compounds. Argue here,
not in code review.

---

## 1. Doctrine (the decisions that shape everything)

1. **Plates live in the database, not in lens state.** Back up the DB, the
   kit's surfaces travel with it. Lens is the renderer — the Notes client, not
   the NSF file.
2. **Logic lives in SQL.** The template has no expression language. If you
   need a computed value, a conditional flag, a color, a label — the query
   returns it as a column. Templates place data; they never compute it.
3. **Writes go through named actions, never inline SQL.** Templates cannot
   contain SQL. Forms invoke kit-shipped, parameterized statements *by name*,
   server-side, receipted. The sanitizer never has to reason about SQL because
   SQL can't appear.
4. **Safe by construction, not by review.** The vocabulary's ceiling is low
   enough that a Calliope-authored plate is safe for the same reason a
   hand-authored one is: there is nothing dangerous to say in the language.
5. **Versioned from day one.** Every plate row carries `template_version`.
   The renderer supports old versions; kits pin what they were authored
   against.
6. **Plates look native by default.** No custom CSS in v1 — a built-in class
   vocabulary (the System Health look) plus the desktop's theme tokens.
   Uniformity is a feature (the Lotus lesson), and it's also the smaller
   sanitizer.
7. **No visual builder — ever.** Authoring is agent-composed iteration
   (Calliope + SQL) and hand-written rows. We are not building a Retool/VB
   drag surface; the composition loop the assistant already runs IS the
   editor. This is a doctrine, not a deferral.

## 2. Storage (engine-side, migration)

```sql
CREATE TABLE rvbbit.plates (
    plate_id          text PRIMARY KEY,      -- 'system-health/overview', 'kit.construction/onboarding'
    kit               text,                  -- NULL = standalone plate
    title             text NOT NULL,
    description       text,
    template_version  integer NOT NULL DEFAULT 1,
    template          text NOT NULL,         -- sanitized at install AND at render
    queries           jsonb NOT NULL DEFAULT '{}'::jsonb,
    actions           jsonb NOT NULL DEFAULT '{}'::jsonb,
    params            jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
```

- `queries`: `{ name: { sql, description?, cache_ttl_ms? } }` — **read-only**,
  executed through the lens's existing governed query path, bound to declared
  params only. A query that isn't SELECT-shaped fails at install.
- `actions`: `{ name: { sql, args: [{name, type, required?}], confirm?,
  description } }` — parameterized writes. Executed server-side; every
  invocation lands in the receipts/audit trail. `confirm: true` renders a
  confirmation affordance before firing.
- `params`: declared plate inputs `[{ name, type, default?, from_bus? }]` —
  the only values interpolation and queries can reference.

Install/upsert via `rvbbit.upsert_plate(...)` (validates template against the
sanitizer + vocabulary at write time, so a bad plate fails at install, not at
render).

## 3. Template vocabulary v1 (count it on two hands)

Mustache-flavored to match the operator templates the house already speaks.
HTML plus:

| Verb | Form | Notes |
|---|---|---|
| Interpolate | `{{ row.col }}`, `{{ params.x }}` | Always HTML-escaped. No raw/triple-stache. No expressions, no filters. |
| Loop | `rv-each="query_name"` on any element | Element repeats per row; `row.*` in scope. Nested loops via nested queries, not nested scopes. |
| Show/hide | `rv-if="row.flag"` / `rv-if="!row.flag"` | Truthiness of a single field path. That's the whole expression language. Want more? Put it in the query. |
| Island | `<rv-grid query="q"/>`, `<rv-chart query="q" spec="col"/>`, `<rv-metric query="q" value="col" label="col"/>` | Replaced with hydration stubs; the lens mounts its REAL components (ResultGrid, Vega chart view, metric card). The DataWindow layer. |
| Action form | `<form rv-action="name">` + plain inputs | Input `name=` maps to action args. Server validates types. `confirm` from the action def. |
| Param emit | `rv-emit="param_name"` on a clickable | Publishes to the desktop param bus (same bus as everything else). |
| Param control | `rv-emit` on `<select>` / `<input type="search\|range\|date\|number\|checkbox">` | Emits on the native change event. Selects can source options from a query (`query`/`value`/`label`/`placeholder` attrs); the server marks `selected`/`checked` from resolved params — control state comes from SQL, never client state. |
| Refresh | *(none in v1)* | After any action or bus param change, the whole plate re-renders. Fragment targeting (`rv-target`) is v1.1 — earn it with a real need. |

**Explicitly not in v1:** expressions, custom CSS/style attributes, inline
event handlers, includes/partials, client-side state, arbitrary custom
elements, raw HTML injection. Every one of these is a one-way door — we walk
through when a real kit surface can't be built without it.

## 4. Sanitizer (the load-bearing wall)

- **Tag allowlist**: structural + text + table + form elements + the `rv-*`
  islands. No `script`, `style`, `iframe`, `object`, `embed`, `link`, `meta`,
  no SVG (v1 — islands cover charts).
- **Attribute allowlist**: `class` (from the built-in vocabulary), `rv-*`,
  form basics (`name`, `value`, `type`, `placeholder`), `title`, `colspan`
  and friends. **No `on*`, no `style`, no `href` except same-app routes**
  (`href` v1: only `rv-open` verbs that map to desktop actions — open plate,
  open SQL window with provided text; never raw URLs).
- Sanitized **twice**: at install (fail loudly) and at render (defense in
  depth — a row edited by hand in psql still can't escape).
- Interpolated values are escaped after sanitation; islands receive data as
  JSON props, never as markup.

Threat model to hold ourselves to: *a malicious plate row, inserted by
someone with DB write access, renders in the victim's desktop.* It must not be
able to execute script, exfiltrate via requests (no URLs), invoke actions that
weren't declared alongside it, or read anything the viewer's governed
connection couldn't already read.

## 5. Rendering pipeline (lens-side)

```
GET  /api/plate/render   {connectionId, plateId, params}
  → load row → sanitize → run named queries (read-only, bound params)
  → expand rv-each/rv-if → escape interpolations
  → emit HTML + island manifest [{id, kind, query, props, data}]
POST /api/plate/action   {connectionId, plateId, action, args}
  → validate against action def → execute → receipt → client re-renders
```

Desktop side: window kind `"plate"` mounts the HTML, hydrates islands into
real components, subscribes declared `from_bus` params, re-renders on bus
changes and after actions. Theme is native — no materialization needed.

## 6. Switchboard logic — no new runtime

Kits will carry genuinely weird vertical business logic. The failure mode to
design against is not complexity — it is *a medium where complexity can
hide*. So: **there is no client-side logic runtime, and there never will be
one** (doctrine 3 and 7's sibling). No embedded JS workflow engine, no FBP
runner, no rules DSL. The graph runtime the switchboard needs already exists:
operator `steps` are a typed, acyclic, explicitly-wired DAG stored as rows,
executed server-side, receipted per node, and already visualized + replayable
in the operator-flow window. Any work kits demand of that system compounds
for everything else that uses it.

Logic lives in exactly three tiers plus one new object:

- **Tier 1 — verdicts are columns.** Most vertical logic is a CASE expression
  and a join wearing a trench coat. It stays SQL (rule 2).
- **Tier 1.5 — decision tables as rows** (the one new object).
  `rvbbit.kit_rules`: `{rule_id, kit, module, applies_when (SQL predicate),
  verdict, priority, description}` — priority-ordered, non-recursive by
  construction. Every rule is independently readable; an agent authoring
  rules can only append rows; "visualizing the logic" is rendering the table
  (on a plate). A controller can read a rule aloud.
- **Tier 2 — operators & flows** for anything multi-step or model-assisted:
  escalations, LLM-in-the-loop judgments, sequenced actions. Kit operators
  are **kit-scoped/private** — namespaced (`kit.construction/…` or a
  `scope` marker on `rvbbit.operators`) so they never pollute user-facing
  pickers, autocomplete, or the operator pool; needs a small engine
  affordance (operator visibility flag) noted here as a v1 dependency.
- **Tier 3 — plates trigger, never think.** The client's entire logic budget
  is `rv-if` on a column. Actions may invoke an operator or flow; the plate
  renders whatever verdict comes back. All "workflow" is action → engine →
  re-render.

**Every verdict carries its why.** Rows a kit flags are stamped with the
`rule_id` that fired and, when a rule leaned on a model, the `receipt_id` —
so "why is this pay application in the exception queue?" is a right-click
(the semantic-lineage gesture) landing on either a human-readable rule row or
a full model trace. Spaghetti is structurally impossible rather than merely
discouraged: decision tables cannot recurse, steps DAGs are acyclic, plates
have no expression language, and every layer is rows in one database rendered
by one graph viewer.

## 7. Modules & contracts (phase 2 — pointer, not spec)

A kit manifest groups plates/cubes/metrics into **modules**, each gated by
**contracts**: named read-only queries returning violations, in the spirit of
operator tests and KPI checks. Module green → children enabled (visible on
the desktop); red → the kit's **switchboard** (its onboarding/launcher plate)
is the only thing offered.
The gate state itself is just a query — the System Health pattern promoted to
an installable object. Spec'd separately once plates v1 exists, because the
onboarding checklist and the contract report ARE plates.

## 8. Dogfood & acceptance

**Rebuild the System Health window as a plate shipped as rows.** Acceptance
for v1 = the plate version reaches parity: status cards from queries,
red/green from data (rule 2: colors are columns), remedy buttons that open a
SQL window with built-not-run SQL (`rv-open`), zero writes. If the vocabulary
can't express System Health, the vocabulary is wrong — find out on our own
window, not on a construction company.

Second dogfood: a fake "kit onboarding" checklist plate exercising actions
(one harmless write w/ confirm) and bus params.

## 9. Decisions & remaining questions

**SETTLED — plate state (2026-07-18):** kit-owned real tables, no generic
k/v store. Simpler, nothing new to depend on inside a machine we don't
control, and rule 2 stays pure. Eat the latency; revisit only if it hurts in
practice.

**SETTLED — action authorization (2026-07-18): Postgres-native, two layers.**
The hard wall is GRANTs: actions execute server-side as the connection's own
user, so an action touching `kit_construction.payapp_reviews` fails unless
that user holds INSERT on it — enforcement we get for free, no new auth
surface. On top, an action def may declare `requires_role`; the renderer
checks `pg_has_role(current_user, role, 'member')` and hides/disables the
affordance. Kits ship their roles (`CREATE ROLE IF NOT EXISTS
kit_construction_approver`). This serves both real deployment shapes without
an SSO roadmap: shops with a handful of shared logins grant roles to those
logins; shops with SSO→Postgres mapping already resolve people to roles.
Honest limit, documented: on a shared Data Rabbit connection the grain is the
connection user, not the human — which matches how shared-login shops already
think.

**OPEN:**

1. **Template syntax bikeshed**: `rv-*` attributes as drafted, or `<template>`
   elements? Attributes sanitize and read better; templates nest better.
2. **Pagination/limits**: islands inherit component behavior; but raw
   `rv-each` needs a hard row cap (500?) — plates are surfaces, not exports.
**SETTLED — naming (2026-07-18):** the species is **plates** (photographic
plates: prepared surfaces pressed from data; zero modern-tooling collision;
on-metaphor with the lens/scene-photography language). A kit's entry/onboarding
plate is its **switchboard**.

## 10. v1 as built (2026-07-18)

Shipped against this contract, same day: migration `0157_plates`
(`rvbbit.plates` + `upsert_plate` install tripwires + `plate_action_log`
audit), lens renderer (`src/lib/server/plates.ts`: sanitize-before-expand,
values entity-escaped, belt sanitize after; cheerio expansion; islands
manifest), `/api/plate/{render,action,list}`, `PlateWindow`/`PlatesWindow` +
desktop wiring, and the built-in class vocabulary. Three sample plates prove
the styles compose: `demo/health-mini` (status cards, tones as data,
build-not-run remedies via rv-open-sql), `demo/bigfoot-dashboard`
(metric/chart/grid islands + rv-emit chips → the param bus),
`demo/field-notes` (kit-owned table, validated action, confirm-gated
destructive action, audit rows).

Two implementation notes for the record: PG regex has no `\b` (it's
backspace — use `\y`), and React portals targeting nodes inside a
`dangerouslySetInnerHTML` subtree silently dropped children — islands are
React-owned nodes physically relocated into their hosts in a layout effect
instead (robust: React keeps updating the node wherever it lives).

Follow-on (2026-07-18): the plate HTML is no longer passed through
`dangerouslySetInnerHTML` at all — it is applied imperatively in a layout
effect, only when the string actually changes. Because island relocation
mutates the subtree, React's innerHTML handling re-applied the whole body
on unrelated prop commits — including the focus bump when you mousedown an
unfocused window. Replacing the mousedown target mid-click makes the
browser suppress the click, which surfaced as "first click only focuses,
second click works". With React never owning those children, one click on
an unfocused plate both focuses the window and runs the control — buttons,
forms, and chart marks alike.

## 11. Contracts + dogfood as built (2026-07-18)

Migration `0158_kit_contracts`: `plates.module` column,
`rvbbit.kit_contracts` (kit, module, contract_id, violations_sql —
SELECT-shaped, empty result = green), and `kit_contract_status(kit)` which
EXECUTEs each contract and **fails closed** (a contract that errors counts
as one violation carrying the error text). Enforcement is two-layer: the
shelf greys gated plates with a `gated · N` badge (courtesy), and
`renderPlate` re-checks the gate and refuses with the contract's own
violation sample (the wall). The refusal names the switchboard as the way
out — the switchboard itself has no module, so it always renders.

Verified end-to-end with `field-kit`: contract `has_enough_notes`
(< 3 notes = violation) gated `field-kit/insights` red → three notes logged
through the switchboard's own intake form → status flipped green → the wall
opened and the insights islands rendered. The gate text a user sees is the
contract's own row: "only 0 field note(s) logged — need at least 3".

Dogfood #2 — **full System Health parity as `system/health` plate rows**:
the seven status cards (metadata weight w/ db-size ratio, tombstones,
generations, catalog snapshots, orphaned files, vacuum, maintenance crons),
tombstone top-10 with per-row `Rebuild SQL`, and all six remedies as
SQL-built-by-SQL (`string_agg` over the same top-N the TS window computed)
opened built-not-run. The TS System Health window can eventually retire in
favor of this row — it ships to clients as data.

Renderer hardening that fell out of it: **(a)** each plate query now runs in
its own try/catch — a failing query degrades to an inline `plate-error`
where it's consumed instead of killing the surface; **(b)** a query def may
carry `"database": "..."` to run against a sibling db on the same server.
Both were forced by pg_cron: `cron.job` exists only in cron's home db
(`postgres` here), and a plain reference fails at parse time on any other
db — no `to_regclass()` guard can save a query that names a missing
relation. The cron card + jobs list route to the home db; the install-jobs
remedy branches on `cron.database_name` and emits `schedule_in_database`
targeting the current db when it isn't the home.

## 12. Layout vocabulary + plate reactivity (2026-07-18)

**Bespoke arrangements, sanctioned classes.** Templates still carry no
styles (the sanitizer strips them); arrangement comes from a small layout
vocabulary in the lens stylesheet: `plate-split` (fixed rail + fluid main),
`plate-columns` (auto-fit equal columns), `plate-rail` (stacked nav buttons
with `<small>` badges), `plate-kv` (dl/dt/dd definition grid),
`plate-toolbar` (chip row), `plate-feed`/`plate-feed-item`/`plate-feed-meta`
(timeline), `plate-banner` (`-big`/`-note` spans). Proven by two seeds in
`docs/examples/plates/seed_layout_plates.sql`: `demo/sightings-console`
(master-detail: state rail → kv summary + detail table, zero islands) and
`demo/notes-wall` (banner + filter chips + feed).

**Param loop-back.** `rv-emit` now serves both hands: it still publishes to
the desktop param bus (other windows cross-filter), and if the emitted
field is one the plate DECLARES in its own params, the window re-renders
itself with the new value. That one rule makes master-detail work inside a
single plate with no new vocabulary — the render response includes resolved
`params` so the client knows which fields loop back.

**from_bus — cross-plate (and cross-window) filtering.** A param declared
`{"name": "state", "from_bus": true}` is SUBSCRIBED to the desktop param
bus: any window's cascading eq emit of that field re-renders the plate with
the value. Clicking a state chip in `demo/bigfoot-dashboard` drives
`demo/sightings-console`'s whole master-detail — two plates, no coupling
beyond the field name. For from_bus fields the local loop-back copy is
skipped; the bus round-trip is the single source of truth, so toggle-off
(re-clicking the same value clears the bus entry) falls the plate back to
its declared default, consistent with every other desktop param. The window
re-fetches only when ITS declared bus fields change value.

**Charts emit too.** `rv-emit="field"` on an `<rv-chart>` opts the chart
into the same emit path as buttons: clicking a mark publishes
`datum[field]` (bus toggle = click-again-to-unselect; from_bus round-trip
re-renders the plate). When the chart's query also ships a `sel` column,
the active mark stays full-opacity and the rest dim — the chart edition of
selection-as-a-column. Test-harness note: Vega resolves the clicked item
from hover state, so synthetic clicks without a preceding pointermove
always miss — drive chart tests with trusted mouse input.

**Selection state is a column.** No client-side "active" tracking: the
query compares against its own param — `CASE WHEN state = {{ params.state }}
THEN 'active' ELSE '' END AS sel` — and the template writes it into
`class="{{ row.sel }}"`; `.plate-rail button.active` / `.plate-toolbar
button.active` are part of the sanctioned vocabulary. Static affordances
get the same treatment via a one-row query (the notes-wall "All authors"
chip). Same idiom as tones-as-data: the SQL knows what's selected because
the SQL received the param.

**Plate reactivity (the cheap kind).** After any successful plate action,
the window broadcasts `rvbbit:plate-data-changed {plateId, kit}`. Every
open plate in the SAME KIT re-renders (kit = the sharing scope; plates in
one kit are presumed to watch the same tables; kit-less plates form their
own bucket), and the shelf re-lists so contract gates flip live. Honest
limits, on purpose: same browser only, and only plate ACTIONS trigger it —
mutations from SQL windows or external writers don't. The upgrade path if
that ever matters is LISTEN/NOTIFY under a server-sent stream, not polling;
the event contract stays the same.


## 13. Control primitives (2026-07-18)

Dropdown, slider, datepicker, search box, and checkbox are not new nouns —
they are `rv-emit` on form controls, firing on the native change event
through the same emit path as buttons and chart marks (loop-back, bus,
from_bus, toggle semantics all inherited). Two server-side services keep
them controlled-by-SQL:

- `<select rv-emit="state" query="state_opts" value="state" label="label"
  placeholder="All states">` builds its options from a query's rows and
  marks the option matching the resolved param `selected`. Authored static
  options get the same selected-marking.
- `<input type="checkbox" rv-emit="class_a" rv-value="Class A">` is marked
  `checked` when the param holds its rv-value.

Text-ish inputs reflect state via plain interpolation
(`value="{{ params.q }}"`). Numbers coerce client-side (range/number →
Number). Search boxes emit on change (blur/Enter) — no keystroke storms by
construction. `demo/report-finder` exercises all six against the bigfoot
locations table; because its state/season are `from_bus`, a freshly opened
finder arrives already scoped to whatever the desktop's chips say.

Field notes: `''::date` fails at Postgres PARSE time even in a dead branch
(constants fold early) — write `nullif({{ params.x }}, '')::date`. And a
click on a `<select>` must not run the click-emit path (it would swallow
the dropdown) — form controls are excluded there; change owns them.

## 14. Navigation, tabs, pagination, live search (2026-07-18)

The second batch of primitives, still zero new nouns:

- **`rv-open="plate:<id>"`** (+ optional `rv-open-title`) — the desktop
  verb: open another plate. Switchboard → module, drill-through, "see the
  full finder". `plate:` is the only scheme v1.
- **Tabs** — a `tab` param whose sections are `rv-if`s. This needed the one
  vocabulary extension of the batch: `rv-if="query.column"` is now legal
  OUTSIDE rv-each, evaluated against the query's first row (still no
  expressions — the SQL computes `show_browse` because it received the
  param). `.plate-tabs` styles the strip; active is a `sel` column.
- **Pagination** — a number-typed `page` param; prev/next/pageno/has_prev
  are COLUMNS of a pager query (`greatest(page-1,0) AS prev` — the math
  lives in SQL), emitted back via `rv-value="{{ row.next }}"`. Declared
  param types now coerce (`"type": "number"`), so `OFFSET {{ params.page }}
  * 12` is sound. `.plate-pager` styles the strip.
- **Radio groups** — `rv-emit` radios; the server checks the one whose
  value matches the param.
- **Live search** — `rv-live` on a search input emits while typing
  (debounced 400ms). The renderer preserves the focused control's value,
  focus, and caret across the refetch swap, so typing is continuous.
- **`rv-confirm`** on emit buttons — window.confirm before firing.

Engineering notes that will matter later:
- **Islands moved from relocation to PORTALS.** React unmounts a node via
  its tracked parent; when tabs removed an island that relocation had
  moved into a plate-body host, removeChild threw and took down the tree.
  With the HTML imperatively owned, portals are safe again (the original
  portal failure was React rewriting innerHTML behind them — impossible
  now). Pattern: apply innerHTML → collect hosts → setState → portals
  render into hosts on the follow-up pass.
- **sanitize-html drops empty attribute values** (`value=""` vanishes; a
  value-less radio then reports "on" through cheerio's DOM emulation).
  Empty values are real vocabulary ("All" options clear a param), so they
  ride through both sanitize passes as a `__rv_blank__` marker restored at
  the end. Bare `rv-live` is normalized to `rv-live="live"` pre-sanitize.
- **Toggle is a click gesture.** Click-again-to-unselect applies to chips
  and chart marks only; change-driven controls just SET. (A refetch swaps
  the DOM under a focused input; the detached node fires a stray change on
  blur which would re-emit the same value and toggle the filter straight
  back off. Detached nodes are also ignored outright.)

`demo/casebook` exercises the whole batch: tabs, SQL pagination, radio
class filter, live title search, and rv-open into demo/report-finder.

## 15. The editor arrives + plates ship by default (2026-07-18)

**Assistant authoring (doctrine §1.7 realized).** The Desktop Assistant's
command protocol gains `upsert_plate` / `open_plate` (0160 patches the
0146 prompt with the full vocabulary via anchored replaces that fail loudly
on drift). The lens apply pipeline went async with a serial pending phase:
a plate install round-trips through /api/plate/upsert →
rvbbit.upsert_plate before its open_plate runs, and apply_report carries
the installer's verbatim verdict — a rejected template is the feedback
loop an agent iterates on. First live test: one prompt ("compact notes
leaderboard — chart of notes per author + latest 10") produced a valid
banner + plate-columns + chart-island + grid-island plate, installed and
opened in a single turn. There is no visual builder; this is the editor.

**System plates ship with the product (0159).** Plates are data, so
shipping them is a migration of idempotent upsert_plate() calls:
`system/health` (the full maintenance surface) and `rvbbit/welcome` (the
product's own switchboard — readiness cards + built-not-run starters,
including an accelerate-your-largest-table script computed by SQL against
the actual database). The blank-slate first-run now lands on a surface
instead of an empty desk. The TS System Health window's retirement is
unblocked but not executed.

All of the above rides release 4.0.12 (migrations 0157–0160).

## 16. Kit packaging — kits are artifacts (2026-07-18)

`rvbbit.kits` (0162) gives kits identity beyond a tag: version, title,
description, and `setup_sql` — the idempotent DDL prologue (schemas,
kit-owned tables/views, roles) that the author owns. Three verbs:

- **`export_kit(kit)`** renders everything the kit owns as ONE ordered,
  idempotent SQL script: upsert_kit (metadata + setup travel together) →
  setup DDL → upsert_plate per plate (collision-proof dollar-quoting) →
  module assignments → upsert_kit_contract per contract → RESERVED
  sections (operators / rules / metric_defs / cube_defs) so the format
  never needs a v2 when the switchboard logic tier lands.
- **`publish_kit(kit)`** wraps that script in a kind='kit'
  capability_catalog entry (manifest.install_sql, api rvbbit.capability/v1)
  — kits ride the exact channels Clover does: capability_search finds
  them, catalog.json import carries them, and the script doubles as a
  downloadable `<kit>-install.sql`.
- **Install** is running the script in one transaction — validate with
  ROLLBACK first (house policy since FUNCTIONrvbbit).

Round-trip proven: field-kit exported from bench, installed on a fresh
4.0.12 container (+0162), arrived complete — plates with module
assignments, contract gating RED on the empty box with its own violation
text, GREEN after three notes. The kit lifecycle travels as SQL.

Cubes/metrics doctrine (settled with Ryan): kits ship metric/cube
DEFINITIONS bound to canonical kit-owned VIEW names; the onboarding
plates generate the BINDING (mapping views over the customer's real
tables), not the definitions. Shipping stays deterministic; the moment
the mapping contract goes green, every shipped metric lights up at once.
Those definitions will ride the reserved manifest sections.

## 17. The switchboard logic tier lands (2026-07-18)

**Tier 2 — `rvbbit.kit_rules` decision tables (0164).** A rule set is a
priority-ordered decision table: each rule is one boolean SQL EXPRESSION
over a jsonb `subject` (semicolons rejected — expressions, not statements)
plus the verdict it decides. `rule_verdict(kit, set, subject)` is
first-match-wins; the winning `rule_id` rides with the verdict (provenance
— surface it in a title attribute), and a BROKEN rule wins loudly with
`{"rule_error": true}` instead of silently falling through. Consumption is
set-based: `CROSS JOIN LATERAL rvbbit.rule_verdict('kit','set',
to_jsonb(row))`. Dogfood: field-kit's `triage` table (urgent / sighting /
sparse / routine) renders as tone chips on the switchboard's notes table
via the new `plate-chip` class.

**Tier 3 — kit-scoped operators (0165).** `operators.kit` +
`operators.visibility` ('public' | 'kit'); `set_operator_kit()` hides a
kit's helper operators from DISCOVERY — capability_crawl excludes them
(anchored patch of the LIVE crawl definition: the prompt-patch pattern
applied to a function), their search docs are deleted on hide, and the
lens pickers filter them with a `to_jsonb(o)->>'visibility'` predicate
that stays parseable on pre-0165 databases. Scoping is hygiene, not an
execution wall: the operator still runs anywhere — plate ACTIONS call
operators/flows by name (that was always true; it's the tie-in).

**Both travel.** export_kit v2 fills two more reserved sections: rules as
upsert_kit_rule calls, kit operators as DELETE + jsonb_populate_record
INSERTs (column-drift tolerant both directions). Round-trip proven again:
4 triage rules + a private operator installed on a fresh 4.0.12 box and
"URGENT: injured hiker" hit the priority-10 rule first try. 0166 teaches
the assistant the whole tier (columns → rules → operators, cost-projected).

## 18. Rule observability — two planes (2026-07-18)

Rules deliberately do NOT write receipts (set-based evaluation would
firehose the receipt system — the delete_log lesson). Observability is
two planes instead (0167):

- **LIVE** — `kit_rule_sets` registers each set's `subject_sql`;
  `rule_set_distribution(kit, set)` re-evaluates the decision table over
  CURRENT data on demand. Read-only safe, zero storage, never stale. This
  is what the shipped `system/rules` plate renders.
- **PERSISTENT** — `kit_rule_stats` (one bounded row per rule: matches,
  errors, last error, last matching subject as a specimen, plus a
  '(no match)' fall-through counter) and `kit_rule_log` (errors always;
  full trace only under `SET rvbbit.rule_log = 'all'`). Captured only in
  WRITE-context evaluations — plate renders run in READ ONLY transactions
  and self-disable via a cheap `transaction_read_only` GUC check, NOT
  caught exceptions (a caught exception is a subtransaction per row on a
  hot path). `prune_kit_rule_log()` keeps the log bounded.

The admin UI is a plate, naturally: `system/rules` ships with the product
(kit dropdown → live distribution with shares → the decision table with
dead-rule ambers and error reds carrying last-error tooltips → recent log
→ debug-trace and prune scripts built-not-run). export_kit v3 ships
rule-set registrations so the live plane arrives with the kit.

plpgsql scar for the record: an `ON CONFLICT (…, rule_id)` target inside
a function whose OUT param is also named rule_id is ambiguous —
`#variable_conflict use_column` resolves it without renaming the API.

## 19. Kit lifecycle hardening (2026-07-18)

Four holes closed in one pass (0168 + lens):

- **Preflight.** Kits declare `requires` ({min_migration, extensions,
  operators}); exported scripts open with `kit_preflight_assert()` which
  fails with a human sentence before touching anything. Live proof: the
  first install attempt on bench failed CORRECTLY — the box couldn't prove
  it had 0167 because hand-applied psql migrations bypass the
  schema_migrations ledger (now recorded; the dev-loop lesson: psql -f
  applies must be ledgered or preflight will rightly refuse). Floor:
  preflight protects targets ≥ 0168; older boxes still die on the first
  missing function.
- **Version regression guard.** upsert_kit refuses to downgrade
  (numeric-aware compare; non-numeric versions exempt). The old 5-arg
  signature is DROPPED, not overloaded — the psycopg ambiguity trap.
- **Uninstall.** remove_kit() strips every kit-owned ROW (plates,
  contracts, rules, rule sets, stats, log, operators, registry) with an
  itemized report, and REPORTS data objects named by setup_sql as "left
  in place" — never drops them. The catalog entry survives: uninstalling
  returns the kit to "available" (the store listing outlives the install).
- **Self-test.** validate_kit() dry-runs every plate query with defaults
  bound, EXPLAINs every action with dummy args (parses + plans, never
  executes), evaluates every rule against an empty subject, probes rule-set
  subjects, and checks contract evaluability — the FUNCTIONrvbbit lesson
  as a function. Exported scripts close with the self-test hint.

**The store loop is closed.** The Plates shelf lists catalog kits not
currently installed ("available to install") with one-click Install:
preflight → validate in an explicit BEGIN/ROLLBACK pass → install (one
multi-statement call = one implicit transaction, all-or-nothing) →
validate_kit self-test → shelf regroups. Verified end-to-end on bench:
publish → remove_kit → Install → "installed field-kit — self-test clean".

**requires_role affordances (§ action auth, now built).** An action may
declare requires_role; renderPlate replaces its form with a quiet note
when pg_has_role() says no (unknown role = not allowed), and
runPlateAction refuses server-side. The GRANT wall remains the real
enforcement — this is the affordance layer from the settled design.

Follow-up parked: teach the assistant requires_role + validate_kit via a
prompt-patch migration when the next batch of assistant lessons lands.

**Language settled (2026-07-18, Ryan's reframe):** capabilities get
INSTALLED; kits get SET UP. The shelf section reads "shipped kits — run
setup to activate", the button says Set up, and a kit whose capability
isn't installed shows an amber "needs <requirement>" chip instead — the
preflight evaluated UPFRONT (via requires, the functional truth of
"capability installed") rather than as a click-time error. You cannot set
up a kit standing on a capability that isn't there; the mental model
holds: install the capability → its kit asks for setup → setup does the
kit's install work.

## 20. Opt-in role gating (2026-07-18)

Settled posture (Ryan): the less there is to DO, the better — but the
guardrails exist. Default: roles don't exist for you. Plates render for
everyone, actions run for everyone, and the connection user's GRANTs are
the real allow/deny (DBA territory). Kit objects are owned by whoever ran
setup; on shared-connection shops that's the whole story.

Three orthogonal OPT-IN layers (0169 + lens):
1. **Surface** — `set_plate_role(plate_id, role)`: shelf shows 🔒 with the
   role name (and the grant_kit hint in the tooltip), render and action
   APIs refuse with a human sentence. Checked before the contract gate —
   person-gate, then data-gate. Travels with the kit (export v5).
2. **Action** — `requires_role` in the action def (0168-era): form swaps
   to a note, API refuses.
3. **Grants** — `grant_kit(kit, role, read|write)`: the choreography as
   one call — CREATE ROLE if absent, USAGE on the kit's schemas (parsed
   from setup_sql, same honest regex as remove_kit), SELECT for read,
   +INSERT/UPDATE/DELETE + sequence usage for write (identity columns).
   Existing objects only; rerun after kit upgrades.

All affordance layers sit ON TOP of the GRANT wall, never instead of it.
Unknown role = not allowed, everywhere.

War story for the file: a literal NUL byte had ridden into plates.ts via
an earlier bash heredoc (inside a template literal, where a space
belonged) — TypeScript compiled it without a murmur; an anchored-replace
assert was what finally caught it. Heredoc-written source deserves a
NUL-byte sweep.

## 21. The Fitting (2026-07-18)

Naming settled (Ryan): kits ship TARGETS, the FITTING ROOM adapts them,
an accepted mapping is a FITTING — the tailor's session and the
pipefitter's connector in one word. Kit, plate, fitting: quartermaster
energy.

**Division of labor (settled §"native vs plate"):** the adapt workbench
is a NATIVE lens app, not a plate — it browses the whole schema, streams
proposals, previews data, edits SQL: it thinks, and plates trigger,
never think. It is one generic app serving every kit, stateless by
doctrine: expectations (`kit_targets`) and accepted connections
(`kit_fittings`, with proposal provenance) are rows; the canonical VIEW
is the artifact.

**Engine (0171):** `upsert_kit_target` (schema-qualified view name +
columns jsonb w/ name/type/description/required); `fitting_candidates`
(catalog-KG ranking via data_search over the target's semantic
descriptions — freshness follows the crawl); `fitting_check` (temp-view
probe: does the SELECT run, does it produce the target's columns —
required missing = fail, optional absent = ok, type mismatch = verify
hint); `fitting_apply` (checks → CREATE OR REPLACE VIEW → record the
fitting, fail-loud); `fitting_violations(kit)` — contract fuel: kits
gate modules on it with one line. export_kit v6 ships targets; fittings
stay per-box.

**Lens:** the Fitting Room window (target rail w/ fitted dots → expected
columns → candidate chips + any-table picker → drafted mapping SELECT
(exact-name columns map through; missing required become
`/* TODO map this */ NULL::type` placeholders) → Preview & check →
Accept). `rv-open` gains the `app:` scheme
(`app:fitting?kit=field-kit`) so switchboards link out; Accept fires the
plate-data event so switchboards and the shelf flip live.

**Loop proven end-to-end on bench:** field-kit shipped a
`demo_kit.v_field_notes` target + a `targets_fitted` contract → insights
gated red → switchboard → Fitting Room → picked field_notes, fixed the
TODO (`created_at AS noted_at`) → all checks pass → Accept → view
created, fitting recorded, BOTH switchboard contracts GREEN and the
shelf ungated without a manual refresh.

Design note: fitting_check validates SHAPE (a NULL::type placeholder
passes). Data QUALITY is the contracts' job — a kit wanting populated
columns ships a contract like "v_field_notes.noted_at must not be all
NULL". Next in this arc: clover_llm-assisted drafting for non-obvious
column matches, and metric_defs/cube_defs bound to canonical views
filling their reserved manifest sections.

**LLM drafting (0172):** `fitting_draft(kit, target, schema, rel)` is now
the one drafting entry point — clover_llm maps the source onto the spec
(the created_at → noted_at rename came back correct with zero human
edits), with the deterministic name-match draft as the built-in fallback
whenever Clover is unavailable or replies with anything that isn't a
plain SELECT. The reply is fence-stripped and shape-checked; the human
still reviews and fitting_check remains the judge. The Fitting Room shows
a "drafted by clover_llm / name-match" badge so you always know which
hand drew the map. A few truncated sample values ride in the prompt —
same trust boundary as every clover_* operator.

**Crime-kit experiment fixes (0173/0174):** finding #2 → target columns
may declare `"values": [...]` (a closed vocabulary); fitting_check
samples 1000 rows and FAILS out-of-vocabulary emissions (the old NYC
mapping's raw law_cat_cd now fails with "9, F, I, M, V — derive with a
CASE"); fitting_draft teaches clover to DERIVE values-constrained
columns — the redraft produced a classifier-style CASE over
ofns_desc/pd_desc → 'violent', and the overview's violent count went
0 → 27,469. Finding #1 → the assistant gained the register_kit command
(metadata only; contracts/targets/setup stay operator work; downgrade
refusals surface in apply_report) — maiden flight registered Crime
Analytics v0.2.0 in the same turn that fixed the metric.

## 22. Kit composition — foundation kits (2026-07-18)

Strategy settled (Ryan): scheduling + CRM are the common substrate of
every small/medium trade vertical; build each ONCE as a foundation kit,
domain kits depend on and extend them, differentiation = the ML/LLM
weave (they're on Clover anyway). Not-Salesforce is enforced by the
vocabulary's ceiling. v0 = simple + a UX test; richer islands
(<rv-schedule>) only when a design partner proves them load-bearing.

Mechanics (0175): requires.kits in preflight ("scheduling>=0.3.0" —
missing/version-behind failures speak human); remove_kit refuses to
strand dependents (p_force override); plates.listens jsonb +
set_plate_listens() = cross-kit reactivity for overlay plates (verified
live; travels in export v7). Doctrine: tweaks are ROWS — foundations
expose config tables, domain kits seed them, forks are forbidden.

Canon drafts for both foundations: docs/FOUNDATION_KITS_PLAN.md.

## 23. Assistant-builder round 2 — the scheduling foundation kit (2026-07-18)

Protocol repeat of the crime experiment, now canon-first: operator
scaffolds (kit + setup DDL + target w/ status values contract + two
contracts + day_check rules + switchboard; seed_scheduling_kit.sql),
synthetic shop seeded (Beacon Hill Heating & Air, 122 appointments,
every rule violation planted; seed_scheduling_shop.sql), then ONE
prompt to the assistant produced today-board / week-list / intake —
installed, opened, zero render errors, validate_kit green after
polish. Write path proven end-to-end in-browser: mark_done, confirm-
gated cancel, and a booking whose ends_at came from job_types.default_
minutes — with the board self-refreshing after each action.

Unprompted wins: canon-view reads with base-table writes; per-assignee
rv-if active gates; confirm on the destructive action; a defaults
query prefilling date/time; durations in dropdown labels; validation-
by-join in the INSERT (invalid assignee/job type = clean no-op).

Findings → fixes:
1. MACHINERY (fixed, lens): per-row action args ride the submit
   BUTTON's name/value — the native HTML idiom — but the sanitizer
   stripped button `name` and the client built `FormData(form)`
   without the submitter, so args vanished. Allowlisted name/value on
   buttons + `FormData(form, submitter)`. The assistant's instinct was
   right; the machinery caught up.
2. VOCABULARY GAP (taught, 0176): it HARDCODED the five crew names as
   five template columns + ten near-identical queries (and 14
   copy-pasted day queries in week) — config-as-rows violated at the
   surface; a new hire needs plate re-authoring. No nested rv-each
   exists, so 0176 teaches the GROUPED FEED pattern (one query,
   partition header flag, rv-if header inside rv-each). True dynamic
   side-by-side columns = banked island (<rv-board>-shaped, with
   <rv-schedule> later).
3. SHARP EDGE (taught, 0176): bare `{{arg}}::date` in action SQL works
   live but fails validate_kit's empty-dummy EXPLAIN — teach
   `nullif({{arg}},'')::date`.
4. NOTES: week plate guessed `extract(isodow)` against hours.dow
   (documented 0=Sunday) — harmless Mon–Sat, silently wrong for
   Sunday hours; config tables carry no machine-readable column docs,
   so conventions must ride the target/prompt. v0 treats the DB
   TimeZone as shop-local. Windows opened by open_plate stack exactly
   on top of each other (placement cascade = lens polish, banked).

Fresh-shop SELF-FIT doctrine (new): setup_sql creates the canon view
over the kit's own table only if absent AND records a kit_fittings row
only if none exists — a customer's accepted fitting is never clobbered
by setup re-runs. fitting_violations requires a RECORDED fitting, so
the guarded insert is what turns the switchboard green on day one.
Candidate engine nicety: rvbbit.self_fit(kit, target, select_sql).

## 24. Tier-1 layout palette + rv-group (2026-07-18)

Ryan's question — "is plate plainness a CSS limit or a dialect limit?"
— resolved: neither; the PALETTE is the dialect for looks, and it was
deliberately tiny. The sanitizer wall (no style attrs, no URLs) stays;
looks grow by growing the versioned class palette, and since `class`
is already data-driven, the tones-as-data idiom generalizes to
PLACEMENT-AS-DATA: SQL computes layout classes.

Shipped (lens): plate-cal 7-col grid + c1..c7/r1..r8 cells (dense flow
stacks chips under their day), plate-cal-head/-chip, plate-bar +
w0..w100 (5% steps) + toned fills, hue-1..8 category accents,
plate-avatar, plate-dot, plate-empty. Renderer: rv-group="query:column"
— partitions one query's rows by first-appearance order, clones the
wrapper per group ({{ group.key }}/{{ group.count }}; rv-each="group"
inside; no nesting; islands forbidden) via synthetic per-group results
feeding the existing rv-each pass.

Proof: week rebuilt as a real calendar — TWO queries replace fourteen
(day columns as cell classes, capacity bars per day incl. explicit
closed state); today rebuilt on rv-group + plate-columns — crew columns
from ROWS (hire/deactivate = zero re-authoring), idle techs get
plate-empty. Both live on bench, validate_kit green. 0177 teaches the
assistant rv-group + the palette and upgrades 0176's "not expressible
yet" caveat.

GOTCHA (SQL): PG LEAST/GREATEST ignore NULLs — least(100, NULL) = 100.
The closed-Sunday bar rendered full-red by accident; make closed/empty
states explicit CASE branches.

Ladder status: Tier 1 (palette) ✓, Tier 2 (rv-group) ✓; Tier 3 =
interaction islands (<rv-board> drag-between-columns → named action,
<rv-schedule> drag-to-reschedule, <rv-map>) — build when a kit needs
the INTERACTION, not the look.
