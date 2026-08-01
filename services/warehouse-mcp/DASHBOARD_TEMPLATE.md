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
2. **One round trip, many flat queries.** Each `callMcpTool` adds ~1.5s of fixed host overhead, and several fired in parallel can hit a concurrency/timeout cap — so `composePayload()` batches every part into **one** `run_sql_multi` call. But each part stays its own FLAT query on the wire: routable by the accelerated engines, visible to the catalog/source map, and individually promotable later. Never hand-write a `json_build_object` payload query.
3. **Declare the tool.** The fully-qualified `mcp__<server-id>__run_sql` must be listed in the artifact's `mcp_tools`, and the id must match your server.

## Engine gotchas (rvbbit_native read-only guard)

- `::type` casts are rejected (`unsupported token: ::json`). Use `json_agg` / `row_to_json` bare, or `cast(x as t)` only if unavoidable.
- Reserved words can't be bare column aliases — `month` fails, use `ym`. Sanity-check aliases you template.
- Numeric columns may arrive stringified (`"4649793.4000"`) depending on the path — `fmt.*` helpers coerce with `+x`, or `parseFloat` where you do math.

## Allowed libraries

The sandbox CDN allowlist is integrity-pinned. Only these load; anything else is silently blocked:

- Chart.js 4.5.0 — charts
- Grid.js 5.0.2 (+ its theme CSS) — sortable/searchable tables
- Mermaid 11.10.0 — diagrams (commented out by default)

> shadcn/ui is **not** usable in-sandbox (React + Tailwind build step, plus non-allowlisted CDNs). The template instead ships a small shadcn-inspired design-token CSS block (`:root` variables for `--card`, `--muted`, `--primary`, `--radius`, semantic colors, badges, buttons, KPIs). Restyle once at the top; everything inherits. On your own `publish_dashboard` host you're *not* sandboxed and can use any stack you like.

## Adapting it

Edit only the two marked blocks:

- **EDIT 1 — CONFIG:** set `SERVER_ID`, the title, and the `PARTS` map. Each part is `{ shape:'row'|'list', sql:'select ...' }` — one FLAT query per concern. `row` → first row as an object, `list` → array of row objects (preserves the query's `ORDER BY`).
- **EDIT 2 — RENDER:** read `p.<name>` (object or array) and lay out KPIs / `chart()` / `table()`.

The `load()` boot logic, bridge helpers, formatters, and chart/table wrappers between the `FRAMEWORK` markers stay as-is.

## Describe the visible business objects

Warehouse automatically compiles a semantic map after an HTML artifact is published. It
renders the exact saved version, inventories visible values and live query evidence, asks the
RVBBIT agent operator for candidate definitions, and independently replays each proposed SQL
definition before attaching it. This work is asynchronous and never blocks or rejects a good
dashboard publication.

The template also includes `bindBusinessObject(id, selector, rawValue, context)` for cases
where you want to author a particularly important or unusual definition yourself. Give that
value a stable DOM id, call this helper after rendering it, and pass a matching
`manifest.semantic_map` to the publish or live-app tool. Authored ids and bindings override
generated ones; the compiler fills the remaining gaps. The original manifest and the verified
compiler overlay are both keyed to the same immutable artifact version.

Each object contains:

- `meaning`: a label, plain-language description, optional unit, and human-readable formula;
- `bindings`: stable DOM selectors or element ids;
- `parameters`: typed filter/context inputs with safe publication defaults;
- `evaluator`: independent read-only SQL that recreates the value, plus its result shape and
  value column;
- optional display hints and source query names.

For example, an authored JavaScript `avg(rows.map(r => r.value))` callout should have an evaluator that
computes that average in SQL and returns one row as `value`. This contract uses arbitrary SQL;
it does not require creating a cube, governed metric, or extra database object. At runtime,
pass the exact current filter values in `context`. The Artifact Lens can then explain what the
number means and independently recreate it without guessing from the dashboard's broader
query trace.

For repeated values, reuse one parameterized definition and bind each DOM element separately:

```js
document.querySelectorAll('.regional-rate').forEach((element, index) => {
  bindBusinessObject(
    'regional_drop_rate',
    element,
    rows[index].drop_rate,
    { region: rows[index].region }
  );
});
```

The hosted runtime stores the raw value and context per element, so selecting the APAC label
replays APAC SQL even when EU, LATAM, and US labels share the same semantic definition.

## Two deployment targets

| | In-app Cowork artifact | Your `publish_dashboard` host |
|---|---|---|
| Data call | `callMcpTool('…run_sql')` | injected `rvbbitQuery()` |
| Row shape | objects keyed by column | positional arrays |
| Auth | connector OAuth (once) | session cookie on your domain |
| Sandbox | yes — CDN allowlist only | no — any stack |

This template targets the in-app path. For the hosted path, swap `makeRunSql` for your `rvbbitQuery` and adjust the row parsing.
