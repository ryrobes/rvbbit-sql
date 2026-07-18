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

## 6. Modules & contracts (phase 2 — pointer, not spec)

A kit manifest groups plates/cubes/metrics into **modules**, each gated by
**contracts**: named read-only queries returning violations, in the spirit of
operator tests and KPI checks. Module green → children enabled (visible on
the desktop); red → the kit's **switchboard** (its onboarding/launcher plate)
is the only thing offered.
The gate state itself is just a query — the System Health pattern promoted to
an installable object. Spec'd separately once plates v1 exists, because the
onboarding checklist and the contract report ARE plates.

## 7. Dogfood & acceptance

**Rebuild the System Health window as a plate shipped as rows.** Acceptance
for v1 = the plate version reaches parity: status cards from queries,
red/green from data (rule 2: colors are columns), remedy buttons that open a
SQL window with built-not-run SQL (`rv-open`), zero writes. If the vocabulary
can't express System Health, the vocabulary is wrong — find out on our own
window, not on a construction company.

Second dogfood: a fake "kit onboarding" checklist plate exercising actions
(one harmless write w/ confirm) and bus params.

## 8. Decisions & remaining questions

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
