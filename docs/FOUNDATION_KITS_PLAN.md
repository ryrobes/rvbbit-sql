# Foundation Kits — scheduling & crm (draft v0 for Ryan to shoot at)

The thesis: every small/medium trade vertical (HVAC, plumbing, spas,
construction subs) needs the same two boring things — scheduling and a
CRM — and the differentiator is never the UI chrome, it's the ML/LLM
woven through them (they're on a Clover plan anyway). Build each ONCE as
a foundation kit; domain kits `requires.kits` them and tweak via seeded
rows, never forks. We are explicitly not building Salesforce: the plate
vocabulary's low ceiling is the complexity governor, and that's a
feature.

**Composition mechanics: SHIPPED (0175).** `requires.kits`
(`["scheduling>=0.3.0"]`) in preflight with human sentences for all three
failure modes; `remove_kit` refuses to strand dependents (force
override); `set_plate_listens(plate, kits[])` gives overlay plates
cross-kit reactivity (verified live: a demo-kit action refreshed a
listening crime-kit plate exactly once). Listens travel in exports (v7).

**v0 philosophy (Ryan):** not complicated, evolve as needed, and treat it
as a UX test — "how does this feel." Ship list/board views inside the
existing vocabulary first; a real drag-calendar island (`<rv-schedule>`)
only when a design partner proves it's load-bearing. Richer islands are
coming eventually regardless — the island mechanism is the open-ended
extension point, one hydrated lens component at a time.

## scheduling kit v0 — proposed canon

Targets (the fittable spine — a shop with existing data fits it; a fresh
shop's tables ARE the canon via setup_sql):

- `scheduling.v_appointments`: appt_id text, customer_id text,
  assignee text (crew/tech/room), job_type text, starts_at timestamptz,
  ends_at timestamptz, status text **values ["booked", "confirmed",
  "in_progress", "done", "cancelled", "no_show"]**, address text?,
  notes text?, lat/lon double precision?
- Config-as-rows (the tweak surface): `scheduling.job_types` (name,
  default_minutes, buffer_minutes, color_tone), `scheduling.assignees`
  (name, skills text[], active), `scheduling.hours` (dow, open, close).

Plates: **today board** (assignee columns via plate-columns, appointment
chips with status tones, book/reschedule/cancel actions), **week list**
(day sections, capacity bar per day), **intake** (form + job_type
dropdown seeded from config), **switchboard** (contracts + fitting link).

Rules (the decision-table showcase): double-booking detection
(overlapping assignee intervals → verdict), outside-hours warning,
job_type/skill mismatch. All observable in system/rules from day one.

The LLM weave (the actual product): natural-language intake via the
assistant ("furnace grinding noise, tomorrow morning if possible" →
classified job_type, duration estimate, slot suggestions); no-show risk
scoring (clover_llm_score over history); day-plan summarization; demand
forecasting as a metric_def once those ship in manifests.

## crm kit v0 — proposed canon

Targets: `crm.v_customers` (customer_id, name, phone?, email?, address?,
first_seen, last_seen, status **values ["lead", "active", "lapsed"]**),
`crm.v_interactions` (interaction_id, customer_id, at, channel **values
["call", "text", "email", "visit", "job", "note"]**, summary, outcome?).

Plates: customer list (live search + status radios), customer card
(kv + interaction feed + log-interaction action), follow-up queue
(rules-driven: "no contact in 30d after quote" → verdict chips).

LLM weave: `clover_llm_same_entity` dedupe queue (the killer feature at
this scale — every trade shop's customer table is full of "Bob Smith" /
"Robert Smith HVAC"), interaction summarization, follow-up drafting,
lead triage rules over free-text notes.

## Sequencing

1. Canon specs above — agreed 2026-07-18.
2. ~~Build scheduling v0 via the assistant-builder protocol~~ **DONE
   2026-07-18**: scaffold in `docs/examples/plates/seed_scheduling_kit.sql`
   (+ `seed_scheduling_shop.sql` — Beacon Hill Heating & Air, planted
   rule violations), assistant built today/week/intake from one prompt,
   full write path proven in-browser, validate_kit green. Findings and
   fixes (submitter FormData + button name allowlist; 0176 grouped-feed
   + nullif-cast teachings; fresh-shop self-fit doctrine) in
   KIT_PLATES_PLAN §23.
3. ~~crm v0 same way; wire the cross-kit overlay demo~~ **DONE
   2026-07-18**: scaffold `seed_crm_kit.sql` + book `seed_crm_book.sql`
   (customers derived from the shop's appointment names — cross-kit
   joins are real); assistant built directory/card/follow-ups from one
   prompt with ZERO missteps, composing the new vocabulary unprompted
   (plate-dot tones, plate-empty, query-driven form select for the
   channel dropdown, from_bus card, rule_verdict LATERALs, CTE
   insert + last_seen touch). THE CROSS-KIT CIRCLE, all three planes
   proven in one gesture: scheduling/intake + /edit look customers up
   in crm.v_customers (READ; unknown customers stay selectable as
   "(not in CRM)"), completed jobs surface in crm.v_interactions via a
   union FITTING (DATA — and the follow-up rules got smarter for free:
   gone_quiet 6→0 because jobs now count as touchpoints), and
   crm/customer-card listens=['scheduling'] refreshed live when a
   booking landed (REACTIVITY). Details: KIT_PLATES_PLAN §27.
4. **sales v0 — DONE 2026-07-18** (the trinity's third leg, unplanned
   in this doc's first draft but demanded by "table stakes"): scaffold
   `seed_sales_kit.sql` (v_quotes/v_invoices targets with values
   contracts, deal_watch + ar_watch rule sets, sales.thresholds
   config-as-rows, switchboard) + `seed_sales_book.sql` (80 quotes /
   $300K, aged invoices). FIRST SHIPPED requires.kits: sales declares
   ["crm"] and preflight enforces it. The assistant built pipeline
   (rv-board with stage columns from a VALUES CTE, drag = move_quote
   stamping decided_at, dblclick = edit loop), quote editor (from_bus,
   CRM customer lookup, create-invoice action with thresholds CROSS
   JOIN + NOT EXISTS dupe guard), new-quote, invoice ledger (per-row
   submit-button mark-paid, ar_watch tones), and a reports surface
   (metric cards, two chart islands, top customers, AR aging) — five
   plates, one prompt, zero missteps, designed with the utility
   palette from the start. KIT_PLATES_PLAN §29.
5. First domain kit (HVAC or Construction) = requires scheduling +
   crm + sales + seeds + overlay plates. Design partner drives what
   generalizes.
