# Dashboards — a governed, live, inspectable registry for Claude-built apps

**Status:** design / brainstorm-of-record · **Owner:** Ryan · **Date:** 2026-06-12 ·
**Parent:** [WAREHOUSE_MCP_PLAN.md](./WAREHOUSE_MCP_PLAN.md)

> One line: *Claude (in Cowork) builds a dashboard, publishes it to rvbbit with one
> tool call, and it becomes a **live, shareable, time-travelable, audited** web app that
> works without Claude — and in rvbbit-lens every chart and number is a doorway into
> the warehouse, because we read the queries underneath and let lens explore and
> re-filter them under unchanged rendering.*

---

## 1. The reframe (why this isn't "save some HTML")

The MCP we built is the **build-time** channel: Claude searches, samples, validates,
runs, and assembles a dashboard. The thing a *published* dashboard needs — and the
thing that makes this a product — is a **runtime** channel: a way for the dashboard to
get fresh, governed data from a browser when Claude isn't in the room. That runtime
channel is exactly the `get_connection` / "promote to a live governed connection"
forward-bet in `WAREHOUSE_MCP_PLAN.md §3`. We already shipped the substrate (OAuth +
login, read-only governance, `metric()`, AS-OF, freshness, the `mcp_activity` log).
This plan is the connective tissue.

**The artifact is the easy part; the data umbilical is the product.**

---

## 2. Principles (the decisions we converged on — hold these)

1. **The artifact is the single source of truth.** It carries its own inline SQL. We do
   *not* abstract queries into a second set of managed objects to keep in sync.
2. **Edges are a *derived, regenerable index*, not an entity.** Re-run extraction on
   publish/edit. There's nothing to independently maintain — the artifact *is* the
   manifest. (This dissolves the "keep the manifest in sync" problem entirely.)
3. **Live-only is the point.** A dashboard worth publishing makes real data calls. We
   *allow* fully-materialized artifacts ("dead trees" — data baked in), but they're
   second-class: no deps, no edges, no freshness, no time-travel. We **nudge against
   them** (UI contrast + the MCP prompt), not forbid them.
4. **The read-only mirror makes it safe.** Everything runs against the **Temporal
   Mirror** (a read replica), never prod. So raw inline SQL from an artifact has zero
   blast radius — can't write, can't touch prod, mirror is rebuildable. This is why we
   can skip query pre-registration and injection machinery. `safe_select` stays for
   cost/sanity, **not** as a security boundary.
5. **Two tiers, gracefully degrading.** *Inspection* works on any artifact (read the
   queries → open + explore). *Manipulation* (filter/time-travel/drill under unchanged
   rendering) needs the artifact to fetch through lens's data-broker. Nudge for it;
   never require it.
6. **Reuse, don't rebuild.** Inspection, lineage, freshness, time-travel, profiling —
   all already exist in lens. This plan *points them at a new object*, it doesn't
   re-implement them.

---

## 3. Architecture

```
  Cowork: Claude builds + publishes ──► rvbbit.dashboards (versioned source)
                                          │
                          publish-time extraction (OpenRouter LLM
                          + route_explain + data_search) ──► deps/edges (derived index)
                                          │
  Browser (outside Claude) ──► /d/{slug} (render, behind login)
                                  └─ artifact data calls ──► /api data path
                                        = run_sql on the MIRROR, validated + LOGGED
                                          (rvbbit.mcp_activity, tagged dashboard_id)
                                          │
  rvbbit-lens: the dashboard as an APP ── data-broker can rewrite the base query
                                          (filter / AS-OF / re-grain) → same rendering
                                          + every edge → Finder / Scry / Metric Inspector
```

**The data path is the chokepoint.** A live dashboard can only get data by calling the
governed query path — which is `run_sql` with a `dashboard_id` tag. So:
- the deps are **observable, not inferred** — the SQL that runs *is* the edge;
- the **`mcp_activity` log already captures them** (tables resolved via `route_explain`);
- validation, cost caps, AS-OF, freshness, audit are **inherited** for free.

---

## 4. The two tiers

### Inspection (any artifact, always works)
We know each panel's base query (extracted at publish, confirmed at runtime). In lens, a
**"Sources" sidebar** lists them; clicking one **opens the base SQL in a fresh lens SQL
window** — runnable, editable, time-travelable — *without touching the dashboard*. The
dashboard is immutable here; exploration spawns separate windows. Invariant worth
keeping: **what runs = what you inspect = what's stored** (the artifact's SQL is the one
true copy).

### Manipulation (artifact fetches through the lens data-broker)
Because lens can see *and rewrite* the base query, it can wrap it —
`SELECT * FROM (<base query>) WHERE region='EU'`, swap the GROUP BY, inject an AS-OF —
and feed the result to the **same chart**. The author never built a filter bar, a
time-slider, or drill-down; **lens adds them universally**. "Mutability" = mutate the
*view* (filter/grain/time), never the data (read mirror). This is the only thing the
**recommended build stack** is for: a data client (`rvbbitQuery(sql)` / `rvbbitMetric`)
that routes data through a bridge lens owns. Extraction does *not* need the stack (the
LLM reads any artifact); the stack only unlocks manipulation.

---

## 5. Dependency extraction (LLM-default, deterministic resolution)

A new member of the crawl family: `catalog_crawl` (structure) → `data_crawl` (data) →
**`dashboard_crawl`** (usage/intent). Division of labor:
- **OpenRouter LLM (a smart model) finds the queries/metrics** in whatever Claude built —
  robust to arbitrary structure, no stack required. This is the default.
- **`route_explain` resolves the tables** deterministically from each SQL (no LLM
  guessing on lineage; gives `rvbbit_tables`).
- **`data_search` resolves fuzzy metric mentions** ("revenue") to canonical catalog nodes.
- **Runtime observation** (the `mcp_activity` rows the dashboard generates) confirms +
  completes the set, catching conditionally-triggered queries.

Output: a **derived deps index** per dashboard version, regenerated on change. A
detected dead tree (no extractable queries) is simply marked `materialized` and skipped.

---

## 6. The catalog gets a third layer → the flywheel

Dashboards become first-class **KG nodes** with edges: `dashboard --uses--> table /
metric / column`, `--owned_by--> team`, `--forked_from--> dashboard`. The catalog now
encodes **how the company analyzes its data**, not just what it contains. And it runs
both ways:
- **Catalog → artifact:** free inspectability, lineage, freshness, time-travel, trust.
- **Artifact → catalog:** dashboards are the richest usage signal you have. Tables used
  in many dashboards rank higher in `data_search`; "this table answers churn questions"
  annotations *come from* the dashboards on it; N dashboards computing the same thing →
  "bless it as a metric?"; the new edges sharpen Claude's next grounding.

That closes the loop the `mcp_activity` log opened: **usage enriches the catalog →
Claude gets better → dashboards get better → more usage.**

---

## 7. "For free" — every edge is a tool you already shipped

| In the dashboard you… | …lens gives you, for free | (the feature already built) |
|---|---|---|
| open a panel's source | the **base SQL in a scratch window** | the lens SQL editor (metric Creator preview path) |
| click a number/chart | its **source table** + live vitals | the **Finder instrument panel** |
| click a column | **distribution / aggregate / top-values** | the **Scry field-focused column opens** |
| hover a metric panel | **definition, versions, trend, check** | the **Metrics Inspector** (bitemporal) |
| drag a time slider | the **whole dashboard as-of any point** | AS-OF GUC + lazy time-travel infra |
| glance at a panel | "**as of 04:00 / 2 days stale / drift**" | `accel_freshness` + Catalog **Drift** |
| "what feeds this?" | the **lineage graph** | the **Scry canvas** (neighbor spider, edges) |
| "is this real?" | the **receipt** (query + snapshot + version) | the **`mcp_activity` log** + bitemporal |

---

## 8. The rvbbit superpowers (what Looker/Metabase structurally can't do)

- **Time-travel dashboards** — every dashboard gets a free AS-OF slider.
- **Pin-to-snapshot ("board-meeting mode")** — freeze a *live* dashboard to a snapshot,
  reproducible forever, shareable publicly (frozen = no auth). Distinct from a dead tree:
  it's a deliberate freeze *with* provenance.
- **Reproducible + audited by construction** — every view is a receipt.
- **Metrics-backed = the official number** — panels on `metric()` inherit the check verdict.
- **Impact analysis / blast radius** — "I'm changing `orders.amount` — what breaks?" The
  reverse edges answer instantly. Schema-change safety with zero data engineers.
- **Self-healing** — Drift detects a source change → flags only the affected dashboards →
  offers Claude-regeneration.
- **Trust badges** — blessed metrics + fresh + no drift = 🟢; ad-hoc SQL over a stale
  table = ⚠️. Non-tech users know which shared dashboard to believe.
- **Semantic discovery** — "find dashboards about churn" / "dashboards touching PII."
- **Cross-team metric reconciliation** — detect when two teams compute "active users"
  differently (same concept, divergent SQL). The "everyone has different numbers" disease
  is exactly what the semantic layer can catch.
- **Onboarding by exploration** — a new hire clicks through the company's dashboards in
  lens and learns the data model; the dashboards *are* the living data dictionary.
- **Claude reuses instead of rebuilds** — "does a dashboard already show EU revenue?" →
  yes, fork it, because the deps are searchable in the catalog.

---

## 9. Collaboration (small business, each team builds their own)

- **Team spaces + a shared, semantically-searchable gallery** (reuse `embed`/`data_search`).
- **Fork & remix across teams with lineage/attribution.** Open a panel's SQL → tweak in
  the scratch window → "save as new dashboard" → you've forked. Exploration *becomes*
  creation.
- **Usage analytics for free** from `mcp_activity`: trending vs. abandoned, by team/user.
- **Delivery + alerts** reuse pg_cron + the alerts/flow engine: "email this Monday,"
  "ping #finance if this panel breaches."

---

## 10. Data model (sketch)

```sql
-- the artifact (source of truth), versioned
rvbbit.dashboards        (id, slug UNIQUE, name, description, owner_email, team,
                          status text,            -- 'live' | 'materialized' (dead tree)
                          latest_version int, created_at, updated_at)
rvbbit.dashboard_versions(dashboard_id, version, html text,   -- the artifact source
                          kind text,              -- 'live' | 'materialized' | 'pinned'
                          pinned_as_of timestamptz,           -- snapshot for board mode
                          created_by, created_at, notes)

-- DERIVED index (regenerated by dashboard_crawl; safe to truncate + rebuild)
rvbbit.dashboard_deps    (dashboard_id, version, panel_hint,
                          kind text,              -- 'query' | 'metric' | 'table' | 'column'
                          object_ref text,        -- canonical catalog node when resolved
                          base_sql text,          -- the panel's query (for 'query' kind)
                          confidence real, source text)   -- 'llm' | 'route_explain' | 'runtime'

-- runtime queries reuse rvbbit.mcp_activity, tagged with dashboard_id (no new table)
-- catalog KG: a dashboard node + uses→ edges, regenerated from dashboard_deps
```

---

## 11. Tool surface (build-time MCP)

- **`publish_dashboard(name, html, team?, description?, kind='live')`** → store a version,
  run `dashboard_crawl` (extract deps + upsert catalog edges), return the live URL + slug.
- **`list_dashboards(team?, search?)`** → semantic discover/reuse.
- **`get_dashboard(slug, version?)`** → source + deps (to fork or inspect).
- **`update_dashboard(slug, html, notes?)`** → new version + re-extract.
- **Runtime (not MCP):** the data path the artifact calls — `run_sql`-equivalent on the
  mirror, `safe_select`-gated, AS-OF-able, logged to `mcp_activity` with `dashboard_id`.

---

## 12. Governance & safety

- **Read-only mirror** = the load-bearing safety property (see §2.4). No writes possible.
- **Auth for viewing:** live dashboards require login (the OAuth we built) → governed +
  audited. Pinned snapshots can be public (frozen data). Per-dashboard.
- **PII:** reuse the catalog's PII tagging to flag/deny sensitive columns; "which
  dashboards touch PII" is a manifest query.
- **Cost/row caps** inherited from the query path.
- **Dead trees** are allowed but marked and inert (no edges) — the contrast is the nudge.

---

## 13. Phased plan

- **Phase 0 — Live repository (the "it outlives Claude" win).** `publish_dashboard` +
  `rvbbit.dashboards` + serve `/d/{slug}` behind login + the data path (inline SQL on the
  mirror, validated + logged). Dead trees allowed (stored + served, no deps). *Outcome:*
  dashboards persist, share by URL, show live data — without Claude.
- **Phase 1 — Catalog-linked inspection.** `dashboard_crawl` (LLM + `route_explain` +
  `data_search`) → deps + KG edges. Lens "Sources" sidebar → open base SQL in a window;
  the Finder/Scry/Inspector drill-throughs + freshness/drift badges light up for free;
  semantic `list_dashboards`. *Outcome:* every edge is explorable.
- **Phase 2 — Manipulation tier.** The recommended stack / data-broker → filter-on-top,
  AS-OF slider, drill, re-grain — all under unchanged rendering, in lens. *Outcome:* a
  static Claude dashboard becomes an interactive BI surface.
- **Phase 3 — Collaboration + flywheel.** Teams/gallery, fork/lineage, usage analytics,
  cross-team metric reconciliation, impact analysis, self-healing on drift, trust badges,
  scheduled delivery.

---

## 14. Open questions

1. **Standalone manipulation** (outside lens) — how much do we support without the lens
   data-broker? (URL-param filters? a lightweight JS bridge in the served page?)
2. **Panel → query targeting** for filter-on-top — does Claude tag panels with data-ids
   (so a filter can hit one panel), or does lens apply filters globally? Tagging is a soft
   convention, not a hard contract.
3. **Rendering arbitrary React safely** — sandboxed iframe + postMessage data bridge;
   plain-HTML/fixed-chart-lib is the sweet spot, full arbitrary React needs a runtime.
4. **The recommended stack's exact shape** — the `rvbbitQuery`/`rvbbitMetric` client API +
   a chart lib, shipped as a template + nudged in the MCP prompt.
5. **Materialized detection** — extraction finds no queries ⇒ mark `materialized`; surface
   the "static, no live data" badge.
6. **Where the host lives** — warehouse-mcp service (data + minimal render + auth) vs.
   rvbbit-lens (rich gallery + the app experience, reusing scenes/homebase/present-mode).
   Likely both: service hosts the URL + data, lens is the management/exploration UI.

---

## 15. The pitch

> Every dashboard Claude builds becomes a **catalog citizen** — its queries read at
> publish, linked into the graph, served live on the read mirror, and opened in lens as
> an app where every element is a doorway into the warehouse and the whole thing can be
> re-filtered and time-traveled under unchanged rendering. It outlives the Claude chat,
> works in any browser, and every dashboard makes the catalog (and Claude) smarter. A BI
> product that falls out of Claude + rvbbit — assembled almost entirely from parts already
> shipped.
