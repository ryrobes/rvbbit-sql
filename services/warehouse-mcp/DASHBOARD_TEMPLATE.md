# rvbbit live-dashboard boilerplate

A drop-in starter for building live dashboards on top of the rvbbit MCP server. Ship `dashboard-boilerplate.html` as an example in the MCP (e.g. as a skill asset or a `publish_dashboard` template) so any generated dashboard inherits the same data plumbing and look.

## How the live data works

The artifact never calls the network directly — the in-app sandbox blocks it. Instead it calls a host-provided bridge:

```
artifact  ->  window.cowork.callMcpTool('mcp__<server-id>__run_sql', { sql })
          ->  Cowork host  ->  your MCP server  ->  warehouse
```

Auth is the connector authorization the user grants once when adding the MCP server. No login inside the artifact, nothing re-prompts on refresh, and DB credentials never reach the browser.

## The three rules baked into the template

1. **Wait for the bridge.** `window.cowork.callMcpTool` is injected a moment after the inline script runs; calling it immediately hangs. `waitForBridge()` polls for it first.
2. **Minimize round trips, not rows.** Each `callMcpTool` adds ~1.5s of fixed host overhead, and several fired in parallel can hit a concurrency/timeout cap. Compose every piece of a view's data into **one** `run_sql` with `json_build_object`; the database does all the aggregation in ~100ms. `composePayload()` builds that SQL for you.
3. **Declare the tool.** The fully-qualified `mcp__<server-id>__run_sql` must be listed in the artifact's `mcp_tools`, and the id must match your server.

## Engine gotchas (rvbbit_native read-only guard)

- `::type` casts are rejected (`unsupported token: ::json`). Use `json_agg` / `row_to_json` bare, or `cast(x as t)` only if unavoidable.
- Reserved words can't be bare column aliases — `month` fails, use `ym`. Sanity-check aliases you template.
- Through the bridge, numbers come back as real JSON numbers (`4649793.4`), not the stringified `"4649793.4000"` of raw `run_sql` rows — so no `parseFloat` needed when you go through `composePayload`.

## Allowed libraries

The sandbox CDN allowlist is integrity-pinned. Only these load; anything else is silently blocked:

- Chart.js 4.5.0 — charts
- Grid.js 5.0.2 (+ its theme CSS) — sortable/searchable tables
- Mermaid 11.10.0 — diagrams (commented out by default)

> shadcn/ui is **not** usable in-sandbox (React + Tailwind build step, plus non-allowlisted CDNs). The template instead ships a small shadcn-inspired design-token CSS block (`:root` variables for `--card`, `--muted`, `--primary`, `--radius`, semantic colors, badges, buttons, KPIs). Restyle once at the top; everything inherits. On your own `publish_dashboard` host you're *not* sandboxed and can use any stack you like.

## Adapting it

Edit only the two marked blocks:

- **EDIT 1 — CONFIG:** set `SERVER_ID`, the title, and the `composePayload({...})` map. Each part is `{ shape:'row'|'list', sql:'select ...' }`. `row` → one object (`row_to_json`), `list` → array preserving the sub-SELECT's `ORDER BY` (`json_agg`).
- **EDIT 2 — RENDER:** read `p.<name>` (object or array) and lay out KPIs / `chart()` / `table()`.

The `load()` boot logic, bridge helpers, formatters, and chart/table wrappers between the `FRAMEWORK` markers stay as-is.

## Two deployment targets

| | In-app Cowork artifact | Your `publish_dashboard` host |
|---|---|---|
| Data call | `callMcpTool('…run_sql')` | injected `rvbbitQuery()` |
| Row shape | objects keyed by column | positional arrays |
| Auth | connector OAuth (once) | session cookie on your domain |
| Sandbox | yes — CDN allowlist only | no — any stack |

This template targets the in-app path. For the hosted path, swap `makeRunSql` for your `rvbbitQuery` and adjust the row parsing.
