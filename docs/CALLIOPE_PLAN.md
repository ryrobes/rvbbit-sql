# Calliope — the living artifact notebook

Status: pilot implementation

Calliope is the optional creation surface for the standalone warehouse Hub. It
is deliberately not a smaller DataRabbit desktop and not a chat transcript with
large tool payloads pasted into it. A conversation produces tangible surfaces
in a separate, time-ordered stage:

```
sessions (mine) | newest surface strata -> older history | Calliope
```

The feature exists only when `WAREHOUSE_HERMES_URL` and
`WAREHOUSE_HERMES_API_KEY` are configured. Without both, the existing gallery
is byte-for-byte the user experience: no Calliope affordance and no Calliope
routes.

## Ownership

There are intentionally two visibility planes in the pilot:

* Published warehouse artifacts are shared with every authenticated user.
* Calliope sessions, turns, attachments, and surface history are listed and
  addressable only by the normalized human identity in the signed warehouse
  browser session.

`auth.read_session_full(request)["identity"]` is the owner. It is not accepted
from a request body. In Burrow this is important: `identity` is the human email,
while `sub` may be the PostgreSQL role the human executes as.

This is an organizational boundary, not a separate company-memory tenant.
Calliope uses the current/default Hermes profile and its shared memory. The Hub
never creates a Hermes profile. An optional `WAREHOUSE_HERMES_MEMORY_KEY` is a
single company-wide key, never derived from the user.

## Warehouse records

Hermes remains authoritative for agent conversation and company memory.
Postgres stores the Hub-owned index and visual ledger:

* `rvbbit.calliope_sessions` maps a local opaque ID and owner email to a Hermes
  session ID. Friendly titles are local, so different users may use the same
  title even though Hermes titles are globally constrained.
* `rvbbit.calliope_turns` mirrors the human/Calliope prose needed to resume the
  UI and gives every visible causal step a stable local ID. `turn_kind`
  distinguishes actual Hermes chat from evidence searches, and `evidence_refs`
  freezes the bounded evidence used by a chat turn.
* `rvbbit.calliope_surfaces` is an append-only projection of tool results. An
  update creates a new surface and points to the prior surface in its lineage;
  it never mutates history. Federated evidence sets use the same ledger and can
  therefore be resumed, rerun, and linked back to later chat turns.
* `rvbbit.calliope_attachments` records owner-gated image files stored under
  `WAREHOUSE_CALLIOPE_DIR`.
* `rvbbit.calliope_design_profiles` holds company-visible names, ownership, and
  the current version pointer. Only the creator may revise or archive the
  original; any authenticated user may use or fork it.
* `rvbbit.calliope_design_profile_versions` holds immutable human-editable
  Markdown, compact structured design tokens, and the exact compiled prompt.
* `rvbbit.calliope_design_profile_assets` records frozen uploaded images, URL
  viewport captures/extractions, and selected scratchpad captures. Binary data
  remains under `WAREHOUSE_CALLIOPE_DIR`; Postgres stores authenticated,
  company-visible references alongside the profile version.

Published apps, dashboards, and decks still live in
`rvbbit.dashboards`/`rvbbit.dashboard_versions`. A surface holds only their
slug/version reference and the rendering metadata needed by the notebook.

## Company evidence resolver

Search is a scratchpad operation, not a second conversational product. The slim
resolver above the stage fans a query across three existing company substrates:

* ACL-filtered `rvbbit.brain_search` document chunks, including synchronized
  ticket, document, and meeting-note sources;
* shared published artifacts plus their enriched semantic-map objects; and
* `rvbbit.search_data_weighted` warehouse semantics.

The federator normalizes these into typed evidence cards and tolerates a partial
resolver failure. An evidence-search turn and evidence surface persist the exact
working set under the signed-in user's session, but neither is sent to Hermes nor
rendered as chat. Multi-selection is explicit. The browser may submit only
`surface_id`/`evidence_id` handles; the server verifies ownership and rehydrates
the authoritative records before compiling bounded, clearly delimited agent
context. The completed chat turn stores a compact evidence snapshot so a resumed
notebook still explains what grounded the answer. If no individual card is selected,
**Ask Calliope** attaches one server-hydrated search-set handle instead: the query,
corpus/count summary, and bounded result gists and locators. This preserves the useful
shape of the whole search without copying full document chunks into the prompt.

Artifact semantic maps and sanitized organizational question/answer memory are
not yet projected into the company knowledge graph by this first pass. The
resolver creates the product seam for that later attention-graph work without
requiring a separate search browser or changing Hermes' shared-memory boundary.

## Turn protocol

The browser sends a user message, optional image data URLs, and the currently
selected surface to the warehouse server. The server:

1. resolves the local session under the authenticated owner;
2. creates a local turn and persists validated images; user markup keeps both
   the flattened image sent to Hermes and a transparent overlay linked to its
   source image;
3. sends the turn to the same Hermes session using
   `/api/sessions/{id}/chat/stream`;
4. supplies ephemeral Calliope instructions plus a compact, fresh surface
   summary (never full historical rowsets);
5. streams prose and tool progress to the browser;
6. projects the completed interleaved Hermes tool transcript into immutable
   surfaces;
7. when a visual build ends in `capture_live_app`, sends the saved screenshot
   back to Hermes as a private image continuation for a bounded visual
   self-check (at most two per user request); and
8. sends each new surface to the stage as it lands.

Hermes decides and executes through its configured RVBBIT MCP. The browser
materializes. No `rvbbit.desktop_commands` contract or internal
`desktop_assistant_turn` operator participates.

## Design Profiles

The Design Profiles modal is deliberately separate from the Warehouse appearance
theme. A user can combine reference images, an HTTP(S) URL, a selected Calliope
capture/artifact, and written direction. Warehouse freezes URL evidence with a
bounded Playwright viewport plus computed-style/text extraction (or a bounded HTML
fallback), then asks a short-lived Hermes session to synthesize:

* an actionable Markdown guide for creative direction, palette, typography,
  layout, components, data visualization, interaction, responsiveness,
  accessibility, and avoid rules; and
* compact JSON tokens used by the in-app preview and available to native
  system renderers.

The generated document is stored as version one. Editing Markdown always appends
a version rather than rewriting history. A notebook can pin one exact version,
or the composer can override only the next turn. Turn resolution is:

1. explicit next-turn override;
2. the selected artifact's pinned version;
3. the notebook default; then
4. no Design Profile.

The resolved version is written to the turn and every projected surface, including
a small name/version snapshot in surface presentation metadata. Its compiled prompt
is supplied on every Hermes hop, including private screenshot feedback. Old turns
therefore retain their original creative contract even after a profile is revised.

URL ingestion rejects credentials and private, loopback, link-local, or otherwise
non-global destinations by default, including redirects and browser subresources.
Authentication-like query parameters are redacted from saved, company-visible
provenance after the capture is made.
Trusted installations may explicitly opt into private URL references with
`WAREHOUSE_CALLIOPE_STYLE_ALLOW_PRIVATE_URLS=true`.

## Surface projection

Projection is deterministic and based on actual tool results:

| Warehouse tool | Surface |
| --- | --- |
| `run_sql` | query with table, SQL, provenance, and inferred chart |
| `run_sql_multi` | one query surface per named result |
| `metric` | metric callout |
| `publish_dashboard`, `create_live_app` | artifact iframe |
| `update_dashboard`, `update_live_app` | new artifact revision linked to the previous one |
| `capture_live_app` | image/capture |
| `render_pdf` | document |
| Calliope image markup | image revision with separately toggleable overlay |

Other tool calls remain compact activity receipts in chat. Projection accepts
the JSON wrappers used by Hermes MCP (`result` and/or `structuredContent`) and
uses the tool call ID as its idempotency boundary.

## Temporal interaction

Chat is conventional: oldest at the top, live edge at the bottom. The stage is
reverse chronological: a new turn stratum enters at the top and older work
accumulates below.

The two scroll positions are never mechanically locked. Instead:

* an assistant message carries chips for the surfaces it caused;
* a surface carries a link back to its turn;
* selecting a surface places a visible reference in the composer and includes
  it in the next turn context;
* new work only auto-follows when the viewer is already at the live edge; and
* otherwise a non-disruptive “new surfaces” beacon returns to the top.

This preserves spatial memory without stealing the reader’s place.

Every image surface has a Markup action. The full-screen editor uses the same
pen/arrow/box interaction as DataRabbit. “Add to message” queues the flattened
annotated WebP/PNG as Hermes vision input; when the turn starts, Calliope also
adds it to the immutable stage history and links it directly to the marked
source surface. The transparent PNG overlay is stored separately so the stage
can hide/show the marks without altering either image.

## Configuration

Warehouse:

```
WAREHOUSE_HERMES_URL=http://hermes-host:8642
WAREHOUSE_HERMES_API_KEY=...
WAREHOUSE_HERMES_MEMORY_KEY=...       # optional, one company scope
WAREHOUSE_HERMES_MCP_KEY=...          # distinct bearer Hermes presents to Warehouse MCP
WAREHOUSE_HERMES_MCP_CALLER=calliope@example.com
WAREHOUSE_CALLIOPE_DIR=/app/data/calliope
WAREHOUSE_CALLIOPE_STYLE_ALLOW_PRIVATE_URLS=false
```

Hermes default/current profile:

```yaml
mcp_servers:
  rvbbit_warehouse:
    url: https://warehouse.example.com/mcp
    headers:
      Authorization: Bearer ${WAREHOUSE_HERMES_MCP_KEY}
    # Trusted first-party server only: preserve verified native-chat authorship.
    forward_session_identity: true

platforms:
  api_server:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8642
      key: ...                         # same value as WAREHOUSE_HERMES_API_KEY
      model_name: openai/gpt-5.6-sol
```

The distinct MCP bearer is the application-security boundary: Warehouse records
Hermes/Calliope as the credential actor while authorizing the verified Chat sender or
Warehouse-linked web-session owner as the delegated human subject. Reusing the general
`WAREHOUSE_MCP_KEY` keeps forwarding attribution-only, so direct scripts cannot claim a
human by copying Hermes metadata. Direct OAuth users remain their own actor and subject.
This contract does not impersonate a Postgres role.

`model_name` should be the actual provider-routable model configured on that
profile, not the default API alias `hermes-agent`. Start the existing profile
with `hermes gateway run --accept-hooks`; Calliope uses its session API and does
not create, activate, or clone a profile.

The Hermes API key and warehouse MCP key are server-side secrets and are never
returned to the browser.
