# The Hub — a front door for MCP-only users

> "A bridge for those who only use the MCP on external devices and only
> consume dashboards via the external MCP URL."

**STATUS: BUILT (P0–P2), browser-verified end to end 2026-07-21.** Ships as
migration 0200 (view + hub_pins + hub kit/plates/layout), lens `/?hub`
wall entry + `rv-shot`/`rv-frame` artifact islands + `/api/rvbbit/thumb`
proxy, and warehouse-mcp auto-thumbnails + `/thumbs` + `hub_url` in tool
responses. As-built deltas from the plan below: favorites shipped as
box-wide **pins** (rvbbit.hub_pins + an audited `toggle_pin` action —
per-user favorites arrive with per-user keys); search is ILIKE (the KNN
upgrade stays banked); the peek is a dedicated slot pane, not a modal;
deep links are `/?hub&sel=<kind>:<slug>` and every publish/update tool
returns one as `hub_url`. Env contract: warehouse-mcp takes
`LENS_PUBLIC_URL` (+ `WAREHOUSE_LIVE_APP_CAPTURE_DIR`, compose-defaulted
to the durable volume); lens takes `RVBBIT_APP_BASE` (browser-reachable
warehouse origin), optional `RVBBIT_APP_BASE_INTERNAL` (compose-network
fetch target for thumbs), and `WAREHOUSE_MCP_KEY` (thumb-proxy bearer).

Working name: **the Hub**. ("Bridge" is already a term of art in
ARCHIPELAGO — the param bus between app islands — so we don't reuse it.
Rabbit-verse alternatives welcome; the noun matters less than the URL.)

## §1 The problem

The most successful usage pattern in the field is people who never open
DataRabbit at all. They talk to the warehouse through Claude Desktop or
Gchat (Hermes + MCP), create live apps / dashboards / metrics through tool
calls, and consume the results as bare `WAREHOUSE_PUBLIC_URL/apps/<slug>`
links pasted into chat. For them RVBBIT is a chat personality plus a pile
of unlisted URLs.

That's a leaky funnel in both directions:

- **Consumption**: their artifacts have no index. Finding last week's
  dashboard means scrolling chat history for a link.
- **Conversion**: nothing ever shows them that the artifacts live in a
  real product. There is no path from "I use the chat thing" to "I use
  DataRabbit," because they never see a surface that connects the two.

The Hub is one URL that fixes both: an artifact browser that *feels like*
Tableau Server's browse page — cards, thumbnails, search, deep links —
but is actually a DataRabbit layout wall. They come for the index; the
breadcrumbs into the full product are one click deep, never forced.

## §2 Doctrine

1. **The Hub is a plate wall, not a new surface species.** It's a
   full-bleed layout (chromeless PlateWindows — walls were *designed* to
   not look like DataRabbit) whose plates are SELECTs over the artifact
   tables. No new rendering machinery; the gallery is a query.
2. **Distribution through the transcript.** MCP creation/edit tools
   already return `url`; they additionally return `hub_url`, and the tool
   docstrings tell agents to surface it. Every artifact an agent creates
   becomes an invitation to the Hub, delivered in the channel these users
   actually live in. Without this the Hub is a page nobody knows about.
3. **Breadcrumbs are a ladder, not a wall.** Browse → peek (modal with
   the live artifact + its lineage) → desktop (same object, real window,
   now they're in DataRabbit) → assistant (the same brain they already
   talk to via MCP, with a face). No signup cliff, no tour, no forced
   chrome. Each rung is optional.
4. **Three front doors, one machinery.** Builder door (crm/home
   layouts), newcomer door (Field Guide), consumer door (Hub). They
   share the wall/layout substrate and differ only in which plates
   populate them. We do not maintain three bespoke landing pages.
5. **Honest attribution.** Phase-0 warehouse-mcp is a shared key, so
   `owner_email` may be uniform on some boxes. v1 is "what the team
   made," personal views arrive with per-user keys (WAREHOUSE_MCP_PLAN
   Phase 1 — this feature is its forcing function).

## §3 What already exists (why this is assembly)

| Need | Already built |
|---|---|
| Artifact rows | `rvbbit.live_apps` (slug, name, description, owner_email, team, status, app_kind, updated_at) + `rvbbit.dashboards`/`dashboard_versions` (created_by) |
| Lineage | `live_apps.queries/tables/metrics` columns + `dashboard_deps`/`dashboard_sources` |
| Thumbnails | `capture_live_app` PNGs (`WAREHOUSE_LIVE_APP_CAPTURE_DIR`; temp dir today — make durable + auto-capture) |
| Live render | warehouse-mcp serves `/apps/<slug>` + `/d/<slug>`; no X-Frame-Options set, iframes fine |
| Entry-by-URL | lens `/?scene=<id>` share-link pattern (param handled + stripped in desktop-shell); homebase capability-URL posture |
| Wall rendering | layouts + wall mode + plate modals + `rv-open` deep links (0187) |
| Search | `capability_search` (metrics/cubes/ops) + `data_search`; artifact-text KNN is the same catalog_docs machinery |
| Facets | `entity_categories` taxonomy (metrics + alerts today; extend to apps) |
| Open-on-desktop | desktop shortcuts / add-icon conduit; assistant `open` commands |

## §4 Architecture

**Entry**: `LENS_URL/?hub=1` (lens is a single-page app; query param is the
house pattern, handled and stripped like `?scene=`). Opens straight into
wall mode on the system `hub` layout — no desktop visible first. Auth
posture per install: on trusted/VPN boxes the plain URL; where exposure
matters, a capability token param following the scene-share pattern.

**Index**: `rvbbit.artifact_index` view — UNION of live_apps, dashboards,
metric_defs (latest version), cube_defs, alert rules → one row per
artifact: `(kind, slug/name, title, description, owner, team, category,
updated_at, thumb, lineage jsonb)`. The gallery plate is `SELECT * FROM
rvbbit.artifact_index ORDER BY updated_at DESC` with facet WHEREs. Plates
doctrine holds: logic in SQL, zero bespoke API.

**Cards → peek**: card click opens a plate modal. Apps/dashboards render
as an iframe island pointed at `WAREHOUSE_PUBLIC_URL/apps/<slug>`
(cross-origin: hub is lens-served, artifacts are warehouse-mcp-served —
that's fine, the app already talks to its own origin via rvbbitQuery).
Metrics/cubes/alerts open their existing detail plates. Below the fold: the
**lineage strip** — "pulls metric `revenue_mtd` from cube `orders`", each
a link to a sibling modal. This is the moment artifacts stop being
unlisted URLs and become a connected system; Tableau never had it.

**Breadcrumb**: an unassuming "Open in DataRabbit" on every peek → exits
wall mode and opens the same object as a real desktop window (and offers
the desktop-icon conduit). Same object, no re-learning.

**Transcript loop**: `_hub_url()` in server.py (needs `LENS_PUBLIC_URL`
env — the hub is on the lens host, not the warehouse-mcp host) emitted as
`hub_url` from create/update/publish/list tools, with a docstring nudge:
"share hub_url with the user — it's the browsable index of everything
they've made."

## §5 Phases

**P0 — the front page.**
- `rvbbit.artifact_index` view (migration; apps + dashboards + metrics +
  cubes to start).
- System `hub` layout + gallery plate + search box plate (title/description
  ILIKE first; KNN later). Kind badge, owner, relative time on cards.
- `/?hub=1` entry → wall mode, param stripped.
- Peek modal: iframe island for apps/dashboards, existing detail plates
  for metrics/cubes. "Open in DataRabbit" breadcrumb.
- server.py: `LENS_PUBLIC_URL` + `hub_url` in create_live_app,
  update_live_app, publish_dashboard, update_dashboard, list_live_apps,
  get_live_app (+ docstring nudges).

**P1 — the Tableau-server feel.**
- Durable thumbnails: point `WAREHOUSE_LIVE_APP_CAPTURE_DIR` at a served
  volume; auto-capture (async, best-effort) after create/update; thumb
  path/URL in artifact_index. Cards go from text to visual.
- Facets: kind / team / category (`entity_categories` extended to apps).
- Recents + favorites (homebase per-home state, same as scenes).
- Search upgraded to KNN over artifact docs (catalog_docs machinery).

**P2 — deeper breadcrumbs.**
- Lineage strip wired for all kinds (live_apps lineage cols +
  dashboard_deps + metric/cube defs).
- Assistant docked on the hub wall — the same corpus they talk to over
  MCP, now with a face; "ask about this dashboard" from any peek.
- Alerts join the index (status chip: breaching/ok).

**P3 — identity.**
- Per-user MCP keys (WAREHOUSE_MCP_PLAN Phase 1) → "Mine" vs "Team"
  views; `owner_email` becomes real per-artifact identity (the OAuth
  caller is already recorded on the dashboard path today).
- Optional: per-user hub URLs (capability tokens) so a Gchat user's link
  opens pre-filtered to their team.

## §6 Open questions

- **Name.** Hub is the working name; the URL param and layout id should
  be finalized before P0 lands (renames after are churn).
- **Auth default.** Ship `?hub` open (trusted-network posture, matching
  today's `/apps/<slug>` links) or capability-token-required? Leaning:
  match whatever posture the box already uses for app links — the Hub
  must never be *harder* to reach than the artifacts it indexes.
- **Thumbnail storage.** Served volume on warehouse-mcp vs bytes in PG.
  Leaning volume + path in the index (PNGs don't belong in the catalog).
- **Do scenes join the index?** Scenes are builder artifacts; probably
  P3-if-ever. The Hub indexes *consumption* artifacts.
