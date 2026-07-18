# Kit Panels — the contract (draft for argument, v0)

The second app species. Iframe apps stay forever as the unlimited sandbox;
panels are the curated shelf: server-rendered, sanitized, SQL-driven surfaces
that ship **as rows in the database** and render natively in Data Rabbit — no
iframe, native theme, direct data path. This is the PowerBuilder/Lotus-forms
layer that kits (mapping surfaces, onboarding checklists, audit queues) are
authored on.

**This document is the ABI.** Kits are versioned artifacts sitting in customer
databases rendering against whatever lens they run. Everything else in the kit
stack can be wrong and fixed later; the panel contract compounds. Argue here,
not in code review.

---

## 1. Doctrine (the decisions that shape everything)

1. **Panels live in the database, not in lens state.** Back up the DB, the
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
   enough that a Calliope-authored panel is safe for the same reason a
   hand-authored one is: there is nothing dangerous to say in the language.
5. **Versioned from day one.** Every panel row carries `template_version`.
   The renderer supports old versions; kits pin what they were authored
   against.
6. **Panels look native by default.** No custom CSS in v1 — a built-in class
   vocabulary (the System Health look) plus the desktop's theme tokens.
   Uniformity is a feature (the Lotus lesson), and it's also the smaller
   sanitizer.

## 2. Storage (engine-side, migration)

```sql
CREATE TABLE rvbbit.panels (
    panel_id          text PRIMARY KEY,      -- 'system-health/overview', 'kit.construction/onboarding'
    kit               text,                  -- NULL = standalone panel
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
- `params`: declared panel inputs `[{ name, type, default?, from_bus? }]` —
  the only values interpolation and queries can reference.

Install/upsert via `rvbbit.upsert_panel(...)` (validates template against the
sanitizer + vocabulary at write time, so a bad panel fails at install, not at
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
| Refresh | *(none in v1)* | After any action or bus param change, the whole panel re-renders. Fragment targeting (`rv-target`) is v1.1 — earn it with a real need. |

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
  (`href` v1: only `rv-open` verbs that map to desktop actions — open panel,
  open SQL window with provided text; never raw URLs).
- Sanitized **twice**: at install (fail loudly) and at render (defense in
  depth — a row edited by hand in psql still can't escape).
- Interpolated values are escaped after sanitation; islands receive data as
  JSON props, never as markup.

Threat model to hold ourselves to: *a malicious panel row, inserted by
someone with DB write access, renders in the victim's desktop.* It must not be
able to execute script, exfiltrate via requests (no URLs), invoke actions that
weren't declared alongside it, or read anything the viewer's governed
connection couldn't already read.

## 5. Rendering pipeline (lens-side)

```
GET  /api/panel/render   {connectionId, panelId, params}
  → load row → sanitize → run named queries (read-only, bound params)
  → expand rv-each/rv-if → escape interpolations
  → emit HTML + island manifest [{id, kind, query, props, data}]
POST /api/panel/action   {connectionId, panelId, action, args}
  → validate against action def → execute → receipt → client re-renders
```

Desktop side: window kind `"panel"` mounts the HTML, hydrates islands into
real components, subscribes declared `from_bus` params, re-renders on bus
changes and after actions. Theme is native — no materialization needed.

## 6. Modules & contracts (phase 2 — pointer, not spec)

A kit manifest groups panels/cubes/metrics into **modules**, each gated by
**contracts**: named read-only queries returning violations, in the spirit of
operator tests and KPI checks. Module green → children enabled (visible on
the desktop); red → the module's onboarding panel is the only thing offered.
The gate state itself is just a query — the System Health pattern promoted to
an installable object. Spec'd separately once panels v1 exists, because the
onboarding checklist and the contract report ARE panels.

## 7. Dogfood & acceptance

**Rebuild the System Health window as a panel shipped as rows.** Acceptance
for v1 = the panel version reaches parity: status cards from queries,
red/green from data (rule 2: colors are columns), remedy buttons that open a
SQL window with built-not-run SQL (`rv-open`), zero writes. If the vocabulary
can't express System Health, the vocabulary is wrong — find out on our own
window, not on a construction company.

Second dogfood: a fake "kit onboarding" checklist panel exercising actions
(one harmless write w/ confirm) and bus params.

## 8. Open questions (argue here)

1. **Template syntax bikeshed**: `rv-*` attributes as drafted, or `<template>`
   elements? Attributes sanitize and read better; templates nest better.
2. **Pagination/limits**: islands inherit component behavior; but raw
   `rv-each` needs a hard row cap (500?) — panels are surfaces, not exports.
3. **Action authorization**: v1 ships actions runnable by anyone who can open
   the panel (the connection's own grants still apply). Do kits need a
   role gate in the action def (`requires_role`) from day one?
4. **Where does panel state live** (e.g. checklist "dismissed" flags)? A
   `rvbbit.panel_state` k/v per (panel, key)? Or insist state = real tables
   the kit owns (purer, rule 2)? Leaning: kit-owned tables, no generic k/v.
5. **Naming**: `panels` vs `surfaces` vs `forms`. `panels` drafted; "forms"
   is the most Lotus-honest but collides with HTML forms in every sentence.
