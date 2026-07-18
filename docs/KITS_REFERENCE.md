# Kits & Plates — Reference

The complete SQL API, storage, template vocabulary, and lifecycle for the
plates/kits system. This is the *reference*; the design rationale and
as-built history live in `KIT_PLATES_PLAN.md` (the ABI — argue there).
Working examples: `docs/examples/plates/`. Everything here shipped across
migrations 0157–0169.

**The nouns, one breath each:**

- **Plate** — a server-rendered, sanitized, SQL-driven surface stored as a
  row in `rvbbit.plates`. Templates place data; SQL computes everything.
- **Kit** — a family of plates + contracts + rules + operators + setup DDL
  that travels as one SQL artifact. Registered in `rvbbit.kits`.
- **Switchboard** — a kit's ungated entry plate (no `module`), where red
  contracts get fixed.
- **Module** — a named group of plates inside a kit, gated by contracts.
- **Contract** — a SELECT returning *violation rows*; empty = green. Red
  modules refuse to render.
- **Rule set** — a priority-ordered decision table (`rvbbit.kit_rules`);
  first match wins, rule_id is provenance.

---

## 1. Tables

| Table | Purpose |
|---|---|
| `rvbbit.plates` | The surfaces. `plate_id` PK, `kit`, `module`, `title`, `description`, `template_version`, `template`, `queries` jsonb, `actions` jsonb, `params` jsonb, `requires_role` |
| `rvbbit.kits` | Kit registry: `kit` PK, `version`, `title`, `description`, `setup_sql` (idempotent DDL prologue), `requires` jsonb |
| `rvbbit.kit_contracts` | `(kit, module, contract_id)` PK, `violations_sql`, `description` |
| `rvbbit.kit_rules` | `(kit, rule_set, rule_id)` PK, `priority` (lower first), `when_sql` (boolean expr over `subject` jsonb), `verdict` jsonb, `active` |
| `rvbbit.kit_rule_sets` | `(kit, rule_set)` PK, `subject_sql` — the SELECT producing a set's subject rows (powers live observability) |
| `rvbbit.kit_rule_stats` | Bounded telemetry: one row per `(kit, rule_set, rule_id)` — matches, errors, last_matched_at, last_error, last_subject specimen. `'(no match)'` counts fall-throughs |
| `rvbbit.kit_rule_log` | Append log: errors always; every match when `rvbbit.rule_log = 'all'`. Prune with `prune_kit_rule_log()` |
| `rvbbit.plate_action_log` | Audit: every action invocation (plate_id, action, args, error) |
| `rvbbit.operators` (+cols) | `kit` and `visibility` (`'public'` \| `'kit'`) — kit-private operators leave discovery, keep executing |
| `rvbbit.capability_catalog` | Kits publish as `kind='kit'` entries; `manifest.install_sql` carries the artifact |

## 2. SQL functions

### Authoring

```sql
rvbbit.upsert_plate(p_plate_id text, p_title text, p_template text,
                    p_queries jsonb, p_actions jsonb, p_params jsonb,
                    p_kit text, p_description text,
                    p_template_version integer DEFAULT 1) → text
```
Installs or replaces a plate. Tripwires at install: template rejected on
`<script|style|iframe|object|embed|link|meta>`, `on*=` handlers, or
`javascript:`; every query must be SELECT-shaped (PG regex note: `\y` is
the word boundary — `\b` is backspace). `module` is set separately
(`UPDATE rvbbit.plates SET module = …`) to avoid signature churn.

```sql
rvbbit.set_plate_role(p_plate_id text, p_role text) → void   -- NULL/'' clears
rvbbit.upsert_kit(p_kit text, p_title text, p_description text DEFAULT NULL,
                  p_setup_sql text DEFAULT NULL, p_version text DEFAULT '0.1.0',
                  p_requires jsonb DEFAULT '{}') → void
```
`upsert_kit` refuses version *downgrades* (numeric-aware; non-numeric
versions exempt). `setup_sql` is the kit's idempotent DDL prologue
(schemas, tables, views, roles) — the author owns its idempotency and its
upgrade story (use `IF NOT EXISTS` + idempotent `ALTER`s).

```sql
rvbbit.upsert_kit_contract(p_kit text, p_module text, p_contract_id text,
                           p_violations_sql text, p_description text) → void
rvbbit.kit_contract_status(p_kit text)
  → TABLE(module, contract_id, description, ok, violations, sample)
```
A contract's SQL returns VIOLATION rows — empty result = green. Broken
contract SQL **fails closed**: it counts as 1 violation carrying
`contract error: <message>`. Render-time enforcement: a plate with a
`module` renders only while every contract on `(kit, module)` is green;
the refusal message carries the contract's own sample text.

### Decision tables (rules)

```sql
rvbbit.upsert_kit_rule(p_kit text, p_rule_set text, p_rule_id text,
                       p_when_sql text, p_verdict jsonb,
                       p_priority integer DEFAULT 100,
                       p_description text DEFAULT NULL) → void
rvbbit.rule_verdict(p_kit text, p_rule_set text, p_subject jsonb)
  → TABLE(rule_id text, verdict jsonb)
```
`when_sql` is ONE boolean expression over `subject` (jsonb) — semicolons
rejected. Evaluation: priority order (lower first, rule_id tiebreak),
**first match wins**, zero rows = no match (add a priority-999 `true`
default if you want one). A rule whose SQL errors **wins loudly** with
`{"rule_error": true, "error": …}` — it never silently falls through.
Set-based use (the normal case):

```sql
SELECT n.*, v.rule_id, v.verdict->>'label' AS label
FROM demo_kit.field_notes n
CROSS JOIN LATERAL rvbbit.rule_verdict('field-kit', 'triage', to_jsonb(n)) v;
```

```sql
rvbbit.upsert_kit_rule_set(p_kit text, p_rule_set text, p_subject_sql text,
                           p_description text DEFAULT NULL) → void
rvbbit.rule_set_distribution(p_kit text, p_rule_set text)
  → TABLE(rule_id text, verdict jsonb, matches bigint)   -- incl. '(no match)'
rvbbit.prune_kit_rule_log(p_keep interval DEFAULT '30 days') → bigint
```
Observability is two planes. LIVE: register the set's `subject_sql`, and
`rule_set_distribution` re-evaluates the table over current data on
demand (read-only safe, never stale). PERSISTENT: `kit_rule_stats` /
`kit_rule_log`, recorded only in WRITE-context evaluations — plate
renders run in `BEGIN READ ONLY` and self-disable instrumentation via a
`transaction_read_only` check. GUCs: `rvbbit.rule_stats = on|off`,
`rvbbit.rule_log = errors|all|off` (`all` = per-evaluation debug trace).
Rules do NOT write receipts (set-based evaluation would firehose them);
`rule_id` is the provenance. The shipped `system/rules` plate is the
admin surface.

### Operators

```sql
rvbbit.set_operator_kit(p_operator text, p_kit text,
                        p_visibility text DEFAULT 'kit') → void
```
`visibility='kit'` = private: excluded from `capability_crawl`, search
doc deleted, lens pickers filter it. **Discovery hygiene, not an
execution wall** — the operator still runs anywhere by name (plate
actions may call it: `SELECT my_op({{arg}})`). GRANTs remain the wall.

### Lifecycle

```sql
rvbbit.export_kit(p_kit text) → text
```
Renders the kit as ONE ordered idempotent SQL script: preflight assert
(when `requires` is set) → `upsert_kit` (metadata + setup travel
together) → setup DDL → plates (collision-proof dollar-quoting) → module
assignments → plate roles → contracts → rules → rule-set registrations →
kit-scoped operators (`DELETE` + `jsonb_populate_record`, column-drift
tolerant) → reserved sections (`metric_defs`, `cube_defs`) → self-test
hint. Run the whole file in ONE transaction; validate with ROLLBACK
first. No data is ever exported.

```sql
rvbbit.publish_kit(p_kit text) → text            -- returns 'kit/<name>'
```
Wraps the export in a `kind='kit'` capability_catalog entry
(`manifest.install_sql`, `manifest.requires`, api
`rvbbit.capability/v1`). Kits then ride the same channels as every
capability: catalog.json import, `capability_search`, the Plates shelf.

```sql
rvbbit.kit_preflight(p_requires jsonb) → TABLE(requirement, ok, detail)
rvbbit.kit_preflight_assert(p_requires jsonb) → void   -- RAISES on failure
```
`requires` shape: `{"min_migration": "0167_rule_observability",
"extensions": ["pg_cron"], "operators": ["clover_llm"]}`. min_migration
checks the `rvbbit.schema_migrations` ledger — **hand-applied psql
migrations bypass the ledger**; record them or preflight will (rightly)
refuse. Preflight protects targets ≥ 0168.

```sql
rvbbit.validate_kit(p_kit text) → TABLE(item, kind, ok, detail)
```
Self-test against THIS box: dry-runs every plate query (declared param
defaults bound; `database`-routed queries skipped with a note), `EXPLAIN`s
every action with dummy args (parses + plans, never executes), evaluates
every rule against `'{}'`, probes rule-set subjects, checks contract
evaluability. Run `WHERE NOT ok` after any install or schema change.

```sql
rvbbit.remove_kit(p_kit text) → TABLE(kind, name, action)
```
Uninstall: strips every kit-owned ROW (plates, contracts, rules, rule
sets, stats, log, operators, registry) with an itemized report. Data
objects named by `setup_sql` are REPORTED as "left in place" — never
dropped. The catalog entry survives: uninstalling returns the kit to
"available".

```sql
rvbbit.grant_kit(p_kit text, p_role text, p_level text DEFAULT 'read')
  → TABLE(granted text)
rvbbit.set_plate_listens(p_plate_id text, p_kits text[]) → void
-- cross-kit reactivity: the plate also refreshes on these kits' data events
```

Composition: `requires.kits: ["scheduling>=0.3.0", "crm"]` gates setup on
foundation kits being present at version; `remove_kit` refuses to strand
dependents (`p_force := true` overrides). Foundations expose variability
as config TABLES; domain kits seed rows — never fork.
The GRANT choreography as one call: `CREATE ROLE` if absent, `USAGE` on
the kit's schemas (parsed from setup_sql), `SELECT` for `'read'`, plus
`INSERT/UPDATE/DELETE` and sequence usage for `'write'` (identity columns
need the sequences). Existing objects only — rerun after kit upgrades.

## 3. Template vocabulary (the whole language)

Values are always HTML-escaped; the sanitizer allowlists tags/attributes
twice (install + render, before AND after expansion); `style` attributes
and URLs are stripped. There is NO expression language — anything
computed is a column.

| Verb | Form | Semantics |
|---|---|---|
| Interpolate | `{{ row.col }}`, `{{ params.x }}` | Escaped. `row.*` only inside `rv-each`. |
| Loop | `rv-each="query"` | Element repeats per row (cap 500). Islands may NOT sit inside. |
| Group loop | `rv-group="query:column"` | Wrapper repeats once per distinct column value (SQL ORDER BY = layout order). `{{ group.key }}` / `{{ group.count }}` interpolate; inside, `rv-each="group"` iterates that group's rows. May not nest; composes with `plate-columns`/`plate-cal` — boards get columns from ROWS. |
| Show/hide (row) | `rv-if="row.flag"`, `rv-if="!row.flag"` | Single-field truthiness, inside `rv-each`. |
| Show/hide (top) | `rv-if="query.column"` | First-row truthiness of another query — how TABS work (tab param + query computing `show_*` booleans). |
| Grid island | `<rv-grid query="q"></rv-grid>` | Hydrates the real lens ResultGrid. |
| Chart island | `<rv-chart query="q" x="col" y="col" mark="bar\|line\|area" rv-emit="col"></rv-chart>` | Vega-lite. `rv-emit` makes marks clickable (emits `datum[col]`; click again = unselect). A `sel` column ('active'/'') dims unselected marks. |
| Metric island | `<rv-metric query="q" value="col" title="Label"></rv-metric>` | Big-number card. |
| Board island | `<rv-board query="q" group-by="col" group-label="col" id="col" title="col" value="col" note="col" tone="col" action="name" rv-emit="field" rv-open="plate:<id>"></rv-board>` | Kanban: one column per distinct `group-by` value (SQL ORDER BY = column order); LEFT-JOIN rows with NULL id = empty-column placeholders (idle groups stay drop targets). Dropping a card on another column fires the named action with args `{id, to}` — same wall as forms; `nullif`-cast `to` when it is a date. No `action` = read-only. Double-click a card: `rv-emit` publishes its id to the bus, then `rv-open` opens the target plate — the edit-loop gesture. |
| Emit (click) | `rv-emit="param" rv-value="…"` on `<button>` | Publishes to the desktop bus + loops back if the plate declares the param. Click-again-to-unselect. `rv-confirm="text"` gates with a confirm dialog. |
| Emit (change) | `rv-emit` on `<select>` / `<input type="search\|text\|range\|date\|number\|checkbox\|radio">` | Emits on change; values coerced by type. Server marks `selected`/`checked` from resolved params; radios auto-group by field. |
| Query-driven select | `<select rv-emit="x" query="opts" value="valcol" label="labelcol" placeholder="All"></select>` | Options from a query; current value selected. FORM selects (`name=`, no rv-emit) may be query-driven too — selection comes from a boolean `selected` COLUMN in the options query. NEVER template a boolean attribute (`selected`/`checked`): the sanitizer turns `attr=""` into a BARE attr, which reads as ON. |
| Live search | `rv-live` on a search input | Debounced emit-while-typing (400ms); focus/caret survive the refetch. |
| Open SQL | `rv-open-sql="{{ row.script }}" rv-open-sql-title="…"` | Opens a SQL window BUILT-NOT-RUN. The remedy gesture. |
| Open plate | `rv-open="plate:<id>" rv-open-title="…"` | Navigation (drill-through, switchboard → module). |
| Action form | `<form rv-action="name">` + inputs named after args | The ONLY write path. `confirm` from the action def. |
| Per-row action button | `<button type="submit" name="arg" value="{{ row.id }}">` inside the form | The clicked submitter's name/value rides into the args (per-row Done/Cancel on cards and tables). |

**Idioms (logic lives in SQL):** tones — `'ok'|'warn'|'bad' AS tone` →
`class="plate-card {{ row.tone }}"`. Selection —
`CASE WHEN v = {{ params.v }} THEN 'active' ELSE '' END AS sel` →
`class="{{ row.sel }}"`. Pagination — prev/next/pageno/has_next are
COLUMNS of a pager query; emit back via `rv-value="{{ row.next }}"`.
Per-entity sections come from ONE query, never from hardcoded entity
names: `rv-group` (above) is the primitive; the grouped feed
(`row_number() OVER (PARTITION BY entity …) = 1` header flag + an
`rv-if` header inside `rv-each`) remains the inline alternative.
Placement-as-data — layout classes are COLUMNS, exactly like tones:
`plate-cal` is a 7-column calendar grid (children take `c1..c7` day
cells, `r1..r8` month rows; `plate-cal-head` pins to the top row;
`plate-cal-chip` = compact card); `plate-bar` + inner
`<div class="w45 ok">` is a capacity/progress bar (`w0..w100` in 5%
steps — SQL rounds the pct; tones color the fill). `plate-avatar`
(SQL-computed initials), `plate-dot` + tones, `plate-empty` (pair with
an `rv-if` flag), `hue-1..hue-8` category accents. Unknown classes
style as nothing — the palette is the dialect for looks.
Edit loop — pair a board/list with an edit plate: the edit plate's
record-id param declares `"from_bus": true`; the board's `rv-emit`
publishes the id on double-click and `rv-open` opens it. Prefill via a
single-row query keyed on the param — text inputs `value="{{ row.x }}"`
in SIBLING `rv-each` blocks per field group (`rv-each` never nests: an
outer loop consumes the inner loop's tokens), selects query-driven with
a `selected` column, the id shipped back in a hidden input, the UPDATE
action re-deriving computed fields. Reference: `scheduling/edit`.
PG gotcha: `LEAST/GREATEST` IGNORE NULLs — a NULL percentage slips
through `least(100, …)` as 100; make closed/empty states explicit CASE
branches.
Action arg casts — always `nullif({{arg}},'')::date` (validate_kit
EXPLAINs actions with empty-string dummies; a bare `''::date` fails at
parse).

**Utility palette (curated Tailwind subset, shadcn tokens):** plates
also speak `flex/grid/gap/p/m/space-y` (scales 0–8), `text-xs..2xl`,
`font-medium..bold`, `uppercase/tracking/leading`, `truncate`,
`tabular-nums`, `line-clamp-1..3`, semantic colors ONLY
(`text-muted-foreground`, `text-primary`, `bg-card`, `bg-muted`,
`bg-primary/10`, `border-border`, tone colors), `border-*`,
`rounded-*`, `opacity`, `overflow`. Defined in the lens's generated
`plate-utilities.css`, scoped under `.plate-body` — that file IS the
allowlist. NOT available (uncompiled AND renderer-scrubbed):
positioning, `z-*`, `inset/top/left`, transforms, screen sizing,
`pointer-events`, all arbitrary-value `[bracket]` classes, raw color
scales (`bg-blue-500` does not exist — semantic tokens keep plates
theme-proof). Doctrine: `plate-*` classes are the COMPONENT layer;
utilities are for arrangement and emphasis between them.

**CSS palette (native look only):** `plate-section`, `plate-cards`,
`plate-card` (+`ok/warn/bad`; children `-title/-value/-note`),
`plate-table`, `plate-row-flag`, `plate-form`, `plate-field(-inline)`,
`plate-toolbar`, `plate-tabs`, `plate-pager`, `plate-split`,
`plate-rail`, `plate-columns`, `plate-kv`, `plate-feed(-item/-meta)`,
`plate-banner(-big/-note)`, `plate-metric`, `plate-chip` (+tones),
`plate-grid-island`, `plate-chart`, `plate-error`. Active states:
`.plate-tabs button.active`, `.plate-rail button.active`,
`.plate-toolbar button.active`.

## 4. Queries, params, actions (the jsonb shapes)

```jsonc
// queries — read-only, each isolated (a failing query renders an inline
// error where it's consumed; it never kills the plate)
{ "name": { "sql": "SELECT … WHERE (nullif({{ params.q }}, '') IS NULL OR …)",
            "database": "postgres" } }   // optional sibling-db routing (e.g. cron.job)

// params — declared inputs; everything else is stripped
[ { "name": "q",    "default": "" },
  { "name": "page", "default": 0, "type": "number" },     // coerced (OFFSET math)
  { "name": "state","default": "", "from_bus": true } ]   // synced with desktop bus

// actions — named parameterized writes; audited in plate_action_log
{ "add_note": { "sql": "INSERT INTO t (a) VALUES ({{a}})",
                "args": [{ "name": "a", "type": "text", "required": true }],
                "confirm": false,
                "requires_role": "field_crew",             // optional affordance gate
                "description": "…" } }
```

Param binding: `{{ params.x }}` in query SQL binds as an escaped literal
(numbers unquoted). Postgres gotcha: `''::date` fails at PARSE even in a
dead branch — write `nullif({{ params.x }}, '')::date`.

`from_bus: true` = the param is subscribed to the desktop bus: any
window's cascading eq emit of that field re-renders the plate
(cross-plate filtering); toggle-off falls back to the default. Non-bus
declared params loop back locally on emit.

## 5. Reactivity

After any successful plate ACTION, the browser broadcasts
`rvbbit:plate-data-changed {plateId, kit}`: every open plate in the SAME
kit re-renders, and the shelf re-lists (contract gates flip live). Kit is
the sharing scope — give a plate the kit of the plates whose actions
write its tables. Honest limits: same browser only; actions only (psql /
SQL-window writes don't fire it, but get swept in on the next
action-triggered render). Cross-client upgrade path: LISTEN/NOTIFY under
the same event contract.

## 6. Lifecycle: install vs set up

Language: **capabilities install; kits set up.**

1. **Distribute** — catalog.json URL import (or `publish_kit` locally)
   puts the `kind='kit'` entry on the box. Import ≠ install: nothing runs.
2. **Set up** — Plates shelf → "shipped kits — run setup to activate" →
   Set up. Kits with unmet `requires` show "needs <requirement>" instead
   (preflight evaluated at LIST time — you can't set up a kit whose
   capability is absent). The click runs: preflight → the whole script in
   an explicit BEGIN/ROLLBACK (validation) → for real (one
   multi-statement call = one implicit transaction, all-or-nothing) →
   `validate_kit` self-test, verdict in the shelf.
3. **Operate** — switchboard fixes red contracts; modules unlock; rules
   decide; `system/rules` observes.
4. **Upgrade** — re-run a newer artifact (upserts + idempotent setup
   DDL). Downgrades refused.
5. **Remove** — `remove_kit()`: rows out, data stays, kit returns to
   "available".

## 7. Roles (all opt-in)

Default: roles don't exist for you — plates render and actions run for
everyone; the connection user's GRANTs are the real allow/deny. Layers:
`set_plate_role` (surface 🔒: shelf lock, render + action APIs refuse),
action `requires_role` (form → note, API refuses), `grant_kit` (the
grants themselves). Unknown role = not allowed, everywhere. Everything is
affordance ON TOP of the GRANT wall, never instead of it.

## 8. System plates (ship with the product)

`rvbbit/welcome` (readiness cards + built-not-run starters),
`system/health` (metadata weight, tombstones, remedies, cron state —
cron queries route to pg_cron's home db via the `database` key),
`system/rules` (decision-table observability). All in the `rvbbit` kit;
all just rows — customize or fork them like any plate.

## 9. Assistant authoring

The Desktop Assistant speaks `upsert_plate` / `open_plate` commands and
knows the vocabulary, idioms, kit-reactivity rule, and the logic tier
(prompt migrations 0160/0161/0166/0170 — anchored patches with fail-loud
drift detection). **Plates are an explicit pathway**: generic "build me
an X" defaults to blocks/app artifacts; the assistant installs a plate
only when the user says plate/kit (it may offer the route in one
sentence when a request smells durable, never install uninvited). Iteration loop: a rejected install returns the
tripwire's verbatim message in `apply_report`; the agent reads it and
re-upserts the same `plate_id`. There is no visual builder; this is the
editor.

## 10. The Fitting (targets → fittings → canonical views)

Kits adapt to the customer's schema through the **Fitting Room** (native
lens app; switchboards link out via `rv-open="app:fitting?kit=…"`).

```sql
rvbbit.upsert_kit_target(p_kit text, p_target text, p_description text,
                         p_columns jsonb) → void
-- target = schema-qualified view name
-- columns = [{name, type, description, required, values?}] — `values` declares
-- a CLOSED vocabulary: fitting_check samples the mapping and fails
-- out-of-vocabulary emissions; fitting_draft derives such columns via CASE
rvbbit.fitting_candidates(p_kit text, p_target text, p_k integer DEFAULT 8)
  → TABLE(schema_name, rel_name, score, matched_on)   -- catalog-KG ranked
rvbbit.fitting_check(p_kit text, p_target text, p_select_sql text)
  → TABLE(check_name, ok, detail)   -- SHAPE check: runs? columns present?
rvbbit.fitting_apply(p_kit text, p_target text, p_select_sql text,
                     p_proposal jsonb DEFAULT '{}')
  → TABLE(check_name, ok, detail)   -- checks → CREATE VIEW → record fitting
rvbbit.fitting_violations(p_kit text) → TABLE(target, problem)
rvbbit.fitting_draft(p_kit text, p_target text, p_schema text, p_rel text,
                     p_use_llm boolean DEFAULT true)
  → TABLE(draft, drafted_by, note)   -- clover_llm mapping; name-match fallback
```

Tables: `rvbbit.kit_targets` (shipped expectations — travel in exports),
`rvbbit.kit_fittings` (accepted mappings + proposal provenance — per-box,
never exported). The one-line mapping contract every kit with targets
should ship: `SELECT * FROM rvbbit.fitting_violations('<kit>')` — modules
gate red until everything is fitted. fitting_check validates SHAPE (a
`NULL::type` placeholder passes); data QUALITY belongs to contracts.
Candidate quality follows catalog freshness — crawl to refresh.

## 11. Sharp edges (the short list)

- `rv-each` row cap: 500. Plates are surfaces, not exports.
- Plate renders run `BEGIN READ ONLY` — rule instrumentation self-disables
  there (live distribution is the truth); actions are the write path.
- Islands can't sit inside `rv-each`.
- Whole-plate rerender on every param change (no fragment targeting yet).
- `export_kit` never exports data; don't put sample data in `setup_sql`.
- Kit artifacts require targets ≥ migration 0168 (preflight's floor).
- PG regex `\b` is backspace; use `\y`.
- sanitize-html drops empty attribute values; the renderer round-trips
  them (`value=""` survives) — but don't rely on exotic empty attrs.
