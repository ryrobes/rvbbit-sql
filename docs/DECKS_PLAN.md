# Decks — the narrative artifact (bolt-slides, rvbbit-ified)

> "Have the assistant make a Deck out of a desktop full of queries —
> external URL, iframe, lean on this for all the nice design."

**STATUS: P0 BUILT + published end-to-end 2026-07-24.** The runtime lives
at `services/warehouse-mcp/deck-runtime/` (vendored upstream @ 9ad90e6a,
spec renderer, `window.RvbbitDeck.render`); built assets are versioned on
the shared CDN at `rvbbit.ai/dist/deck-runtime/0.1.0/` (306KB js / 46KB
css — so published deck HTML is ~4KB: refs + inline spec). First deck
("Sasquatch, by the numbers", 8 slides, pinned bigfoot data) authored as
pure JSON and published through the EXISTING `create_live_app` tool with
`app_kind='deck'` — accepted as-is, live at `/apps/<slug>` with a
`hub_url`, capture-verified rendering with a healthy bridge and no CSP
conflict on the cross-origin runtime. As-built deltas: shipped the P1
shared-asset shape immediately (inline-bundle P0 was skipped — smaller
publishes won); spec gained `data.column` (pluck the measure for
points/cell — first-numeric-column guessing picks the year, found in
test); ChartSlide wraps bare charts in centered titled slides. Remaining
from the phases below: validation + deck MCP tools + Hub chip (P1),
assistant desktop→deck flow (P2), live-mode surfacing/PDF/roles (P3).

Upstream: [stackblitz/bolt-slides](https://github.com/stackblitz/bolt-slides)
(MIT, ~64 files, pushed 2026-07-16). Assessed in full before this plan:
the repo is (1) a Slidev-style paged **deck engine** — glass dock,
thumbnail rail, grid overview, click-builds, synced presenter mode with
notes, an annotation layer — (2) ~28 polished **slide components**
(`Cover`, `Agenda`, `Bento`, `Charts`, `BigNumber`, `StatGrid`, `Split`,
`Comparison`, `Timeline`, `Pricing`, `VisualDashboard`, `Globe`, …),
(3) a **one-file token theme surface** (`tokens.css`), and (4) a
**SKILL.md** that is doctrine-as-prompt for any agent: "never touch the
engine," "author from the user's real input, never reskin the starter,"
"center what stands alone." The components read like they were designed
for data decks; the skill reads like one of our kit briefs. We are not
adopting a framework so much as vendoring taste.

## §1 Thesis

Every artifact species we ship answers a different question:

| Species | Question | Shape |
| --- | --- | --- |
| Dashboard / wall | "what's happening?" | spatial, all-at-once, ambient |
| Live app | "let me work the data" | interactive tool |
| **Deck** | **"let me tell you what happened"** | **sequential, narrated, presented** |

The narrative species is missing, and it's the one the Hub audience asks
for by name: "can you turn this into something I can present Thursday?"
A desktop full of queries already contains everything a deck needs —
the data, the charts, the titles, an implied outline (the spatial
arrangement; tile mode proved the desktop *has* a reading order) — and
the assistant has all of it as context. The missing piece is a renderer
with taste and a publish path. We have the publish path. bolt-slides is
the taste.

**The differentiator: decks whose numbers are live.** Every exported
deck in the world is stale at present-time. A deck served from the
warehouse renders `BigNumber` and `Charts` through `rvbbitQuery(sql)` —
current at the moment it's on the projector, with receipts behind every
figure. "The deck that's never stale" beats anything in bolt-slides'
own README, and only we can say it.

## §2 Doctrine (what keeps this small)

1. **Spec, not code.** The assistant authors a **deck spec** — ordered
   slide rows of `{component, props, notes, builds}` plus one theme
   token block — never JSX. The engine + components are a pre-built,
   versioned runtime bundle the spec renders against. This is the
   plates move applied to slides: DB-native artifact, revisable,
   diffable, kit-scopable; no build step, no codegen, no sandbox.
2. **The assistant IS the editor.** v1 has no GUI slide editor. You
   make a deck by asking; you change it by asking. Revisions ride the
   existing version machinery. (A slide *sorter* is cheap later; an
   editor is a tarpit — see the plates lifecycle for the pattern.)
3. **Don't touch the engine** — upstream's own rule #1, kept verbatim.
   We vendor `src/deck/` + `src/components/` + `tokens.css` as-is
   (MIT notice retained), build once to a static bundle, and upgrade by
   re-vendoring. No forks of engine internals; anything we need that
   the engine lacks becomes a *new* component beside it, not a patch
   inside it.
4. **Ride the dashboards rails end to end.** A deck is a published
   artifact at `/d/<slug>` exactly like a live app: same
   `rvbbit.dashboards` registry (`app_kind='deck'`), same versions,
   same Hub card, same thumbnails, same `hub_url`, same auth/burrow
   posture, same iframe embedding in a desktop window. Zero new
   serving infrastructure.

## §3 Architecture

### The runtime bundle (once)

Vendor upstream into `services/warehouse-mcp/deck-runtime/` (source) →
Vite library build → two static assets served by warehouse-mcp:

    /deck-runtime/deck-<version>.js     (engine + components + React, IIFE)
    /deck-runtime/deck-<version>.css    (chrome + tokens defaults)

The bundle exposes `window.RvbbitDeck.render(el, spec)`. Version is
pinned per published deck (the published HTML references the exact
bundle it was authored against), so engine upgrades never mutate old
decks — same immutability contract as capability versions.

### The spec (per deck)

```jsonc
{
  "deck": {
    "title": "Q3 Billing Complaints — what changed",
    "theme": { "accent": "#5eead4", "surface": "...", "fontScale": 1 },
    "slides": [
      { "component": "Cover",
        "props": { "kicker": "Support · Q3", "title": "Billing anger is down 41%" },
        "notes": "open with the one number; hold a beat" },
      { "component": "BigNumber",
        "props": { "label": "angry-about-billing tickets / wk" },
        "data": { "sql": "SELECT ... FROM tickets WHERE means(body,'angry about billing') ...",
                   "bind": "value", "mode": "live" },
        "notes": "this is means() in a WHERE clause — say so" },
      { "component": "Charts",
        "props": { "kind": "line", "title": "Weekly trend" },
        "data": { "sql": "...", "bind": "series", "mode": "pinned" } }
    ]
  }
}
```

- `data.sql` — ONE flat query per data-bearing slide, executed through
  the same `rvbbitQuery` bridge live apps use (read-only, mirrored,
  logged). `bind` names the prop the rowset feeds.
- `data.mode` — `"live"` (query at view time; the never-stale deck) or
  `"pinned"` (rowset snapshot embedded in the published version at
  publish time; the deck as evidence). Pinned is the default: a deck is
  usually a record of an argument made on a date. Live is the demo
  flex and the standing-meeting deck. Both appear in the receipt line.
- Unknown components / bad props fail slide-local: the deck renders,
  the bad slide shows a diagnostic card (graceful degradation is a
  tested contract everywhere else; same here).

### Publishing

`publish` renders a ~30-line host HTML — bundle `<script>`/`<link>`
refs, the spec inline (`<script type="application/json">`), one
`render()` call — and pushes it through the **existing**
`publish_dashboard` path with `app_kind='deck'`. Everything downstream
(versions, `/d/<slug>`, Hub card, auto-thumbnail, `hub_url`, capture)
works today with zero changes. The spec ALSO lands in
`dashboard_versions.manifest.deck` so tools can read/patch decks
without parsing HTML.

## §4 Authoring — the desktop-to-deck flow

New assistant capability (lens side), same shape as layout commands:

- **"Make a deck from this desktop"** → the assistant walks the visible
  windows (titles, SQL, result shapes, chart specs, spatial order —
  the tile-mode ordering is the default outline), picks a narrative
  (the brief teaches: cover → thesis → evidence slides → so-what),
  emits a spec, publishes, opens the deck in a desktop window (iframe)
  and drops the `hub_url` in chat.
- **Deck brief** = SKILL.md adapted to spec authoring: keep upstream's
  taste rules (center-what-stands-alone, no bullet walls, one idea per
  slide), swap "write JSX" for "emit spec rows," add our rules (every
  number cites a query; prefer `pinned`; `live` only when asked; never
  invent data — the desktop's queries are the only sources). Ships as
  a brief the assistant loads on demand (desktop-panels pattern —
  zero per-turn context).
- **MCP parity** (warehouse-mcp): `create_deck(spec)` /
  `update_deck(slug, patch)` / `get_deck(slug)` — thin validators over
  the dashboards tools, so Hermes/Claude-Desktop users get decks the
  same way they get dashboards. The Hub audience is the deck audience.

## §5 Phases

- **P0 — runtime + rails (the proof).** Vendor upstream, build the
  bundle, serve it; hand-author one spec (bigfoot data, obviously);
  publish through the existing tools; verify `/d/<slug>` renders with
  dock/rail/presenter working, Hub card + thumbnail appear, iframe
  window opens on the desktop. *Exit: a presentable deck URL nobody
  wrote HTML for.*
- **P1 — spec contract.** JSON-schema validation, slide-local error
  cards, `pinned`/`live` data binding through rvbbitQuery, receipts
  line per data slide, `create_deck`/`update_deck`/`get_deck` MCP
  tools, `app_kind='deck'` chip + filter in the Hub.
- **P2 — the assistant flow.** Desktop→outline extraction, the deck
  brief, publish + open-in-window + hub_url handoff, revision loop
  ("make slide 4 a comparison instead").
- **P3 — presented data.** AS-OF pinning surfaced properly (each
  version stamps its data time; re-publish = re-pin), PDF export via
  the existing `render_pdf` op for the email-me-the-deck crowd,
  per-deck burrow roles (a deck for the board ≠ a deck for the team).
- **P4 — polish, only if pulled.** Slide sorter on the deck window,
  deck-from-scene, theme-from-lens-theme, a `MetricCallout` component
  bound to `rvbbit.metric()` so official numbers come from the metric
  layer, not ad-hoc SQL.

## §6 Open questions

- **Escape hatch**: do we ever allow a raw-HTML slide (`component:
  "Html"`) for one-off visuals? Leaning yes-but-sandboxed (same
  posture as live-app HTML — it's already our trust model).
- **React duplication**: the bundle carries its own React; an iframe
  boundary makes that a non-issue. Confirm bundle size is civil
  (~200KB gz expected; Globe pulls three.js — lazy-load or drop it).
- **Upstream drift**: re-vendor cadence. Proposal: opportunistic, only
  for components we want; the engine is stable enough to freeze.
- **Naming**: "Decks" is fine and honest. Rabbit-verse alternatives
  ("Burrow Briefings"?) considered and set aside; the noun matters
  less than the URL, again.

## §7 License

Upstream is MIT (stackblitz/bolt-slides). The vendored directory keeps
its LICENSE file intact plus a NOTICE line naming the commit vendored
from. Nothing else required; nothing else planned.
