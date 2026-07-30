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

## Three records, three jobs

Hermes remains authoritative for agent conversation and company memory.
Postgres stores the Hub-owned index and visual ledger:

* `rvbbit.calliope_sessions` maps a local opaque ID and owner email to a Hermes
  session ID. Friendly titles are local, so different users may use the same
  title even though Hermes titles are globally constrained.
* `rvbbit.calliope_turns` mirrors the human/Calliope prose needed to resume the
  UI and gives every visible causal step a stable local ID.
* `rvbbit.calliope_surfaces` is an append-only projection of tool results. An
  update creates a new surface and points to the prior surface in its lineage;
  it never mutates history.
* `rvbbit.calliope_attachments` records owner-gated image files stored under
  `WAREHOUSE_CALLIOPE_DIR`.

Published apps, dashboards, and decks still live in
`rvbbit.dashboards`/`rvbbit.dashboard_versions`. A surface holds only their
slug/version reference and the rendering metadata needed by the notebook.

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
WAREHOUSE_CALLIOPE_DIR=/app/data/calliope
```

Hermes default/current profile:

```yaml
mcp_servers:
  rvbbit_warehouse:
    url: https://warehouse.example.com/mcp
    headers:
      Authorization: Bearer ...

platforms:
  api_server:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8642
      key: ...                         # same value as WAREHOUSE_HERMES_API_KEY
      model_name: anthropic/claude-sonnet-4.6
```

`model_name` should be the actual provider-routable model configured on that
profile, not the default API alias `hermes-agent`. Start the existing profile
with `hermes gateway run --accept-hooks`; Calliope uses its session API and does
not create, activate, or clone a profile.

The Hermes API key and warehouse MCP key are server-side secrets and are never
returned to the browser.
