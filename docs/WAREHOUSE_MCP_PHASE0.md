# Warehouse MCP — Phase 0 tool spec (the safe analyst MVP)

**Status:** implementable spec · **Parent:** [WAREHOUSE_MCP_PLAN.md](./WAREHOUSE_MCP_PLAN.md) · **Date:** 2026-06-12

Phase 0 = the safe 80%: **discover → blessed numbers → validate → read-only run**, all
scoped + audited. Six tools. Each maps to a function we already have; the "serve
layer" (§A) is the only net-new code. No writes, no DDL, no per-user scoping yet
(single scoped role to start — §A.1).

---

## A. The serve layer (shared by every tool)

A small MCP server (extend `rvbbit-mcp-gateway` with a serve mode) that, per call:

1. **Resolves identity → a scoped, read-only PG role.** Phase 0: one static
   `warehouse_reader` role (read-only, schema-scoped) for the whole MCP instance.
   Per-user mapping is Phase 1.
2. **Runs the backing rvbbit function** on that role's connection (`BEGIN READ ONLY`,
   `statement_timeout`, `SET LOCAL rvbbit.mcp_caller = '<id>'`).
3. **Applies `as_of`** when present: prepend the `-- rvbbit: as_of <ISO ts>` directive
   to the query (the engine's existing AS-OF path).
4. **Writes a receipt** (`{caller, tool, sql, engine, rows, elapsed_ms, ts}`) — every
   read is auditable + reproducible by construction.
5. **Returns a uniform envelope:**
   ```json
   { "ok": true,  "data": { ... }, "meta": { "receipt_id": "...", "elapsed_ms": 42, "as_of": null } }
   { "ok": false, "error": { "code": "INVALID_SQL", "message": "...", "hint": "..." } }
   ```

**Read-only guard (used by `run_sql`/`ask`):** parse with `route_explain`; reject
unless `safe_select` (single SELECT/CTE, no DML/DDL/`COPY`/`;`-chaining, no volatile
writes). Belt-and-suspenders: the role lacks write grants anyway.

**Tool manifest summary**

| tool | input (required) | role-gate | writes? | backing |
|---|---|---|---|---|
| `search_data` | `query` | reader | no | `data_search` + `catalog_docs` + `catalog_fingerprint_table` |
| `describe_table` | `table` | reader | no | `catalog_docs` + `catalog_fingerprint_table` + `catalog_object_history` |
| `list_metrics` | — | reader | no | metric catalog / `metric_versions` |
| `get_metric` | `name` | reader | no | `resolve_metric` + `metric_versions` + `metric_sql` |
| `metric` | `name` | reader | no | `rvbbit.metric()` + `check_metric` |
| `validate_sql` | `sql` | analyst | no | `route_explain` (dry-run) |
| `run_sql` | `sql` | analyst | no (read-only) | engine + `route_explain` pre-check + receipts |

> **Non-tech roles get `search_data` + `list_metrics`/`get_metric` + `metric` only.**
> `validate_sql`/`run_sql` are analyst+ (per PLAN §5: free SQL to explore, blessed
> metrics to report).

---

## B. Tools

### B.1 `search_data` — semantic discovery (the moat)

Find the right tables/columns by what their **data is about**, grounded with samples
+ stats so Claude can write correct SQL.

**Input**
```json
{ "query": "customers who churned in europe",
  "limit": 8,                          // default 8, max 25
  "kinds": ["table","column"],         // optional filter; default both
  "schema": "analytics" }              // optional scope
```
**Output** (`data`)
```json
{ "matches": [
  { "table": "analytics.churn_monthly", "score": 0.83,
    "description": "monthly churn by account + region (derived)",
    "columns": [ {"name":"account_id","type":"int8","description":"FK accounts"},
                 {"name":"region","type":"text","description":"ISO region"},
                 {"name":"churned","type":"bool"} ],
    "samples": [ {"account_id":104,"region":"EU","churned":true}, ... ],   // 3-5 rows, PII-masked
    "stats": { "rows": 2400000,
               "columns": { "region": {"ndv":6,"null_pct":0,"top_values":["EU","US","APAC"]},
                            "churned": {"null_pct":0,"true_pct":0.07} } },
    "relationships": [ {"to":"analytics.accounts","on":"account_id","kind":"fk"} ],
    "freshness": { "last_synced":"2026-06-12T04:00Z", "generation":418, "stale":false },
    "drift": [] } ],
  "note": "ranked by data-KG + schema similarity" }
```
**Backing SQL (sketch)**
```sql
-- 1) rank
SELECT * FROM rvbbit.data_search(:query, :limit, :kinds, 'db_catalog');
-- 2) per hit, enrich (description + per-column stats + samples + drift)
SELECT rvbbit.catalog_docs(:table);
SELECT * FROM rvbbit.catalog_fingerprint_table(:table);   -- ndv/null/top_values/dist
-- samples: SELECT * FROM <table> LIMIT 5  (run as reader role, PII-masked)
-- relationships: from the db_catalog KG; drift: rvbbit.catalog_drift(:table)
```
**Governance:** samples honor the role's column grants + PII masks. **Errors:** empty
`matches` is `ok:true` with a `note` ("no strong matches; try broader terms").

### B.2 `describe_table` — full profile

**Input** `{ "table": "analytics.churn_monthly" }`
**Output** `data`: like a `search_data` match but exhaustive — every column with full
stats + distribution, all relationships, lineage (`sources`/`derived_from` from the
KG), `row_count`, `freshness`, `drift`. Backs onto `catalog_docs` +
`catalog_fingerprint_table` + `catalog_object_history`.
**Errors:** `TABLE_NOT_FOUND` / `NOT_AUTHORIZED` (role can't see it).

### B.3 `list_metrics` / B.4 `get_metric` — the blessed catalog

Make governed metrics the path of least resistance for numbers that matter.

`list_metrics` **Input** `{ "category": "Finance", "search": "revenue" }` (both optional)
→ `data`:
```json
{ "metrics": [ { "name":"revenue", "description":"net recognized revenue",
                 "category":"Finance › Revenue",
                 "params":[ {"name":"region","required":false,"type":"text"},
                            {"name":"period","required":true,"type":"date"} ],
                 "freshness":"2026-06-12T04:00Z", "latest_version":3 } ] }
```
`get_metric` **Input** `{ "name":"revenue" }` → adds `definition_sql`, `check_sql`,
`versions:[{version,created_at}]`. Backs onto `resolve_metric` + `metric_versions` +
`metric_sql`.

### B.5 `metric` — a blessed, governed number (the finance path)

**Input**
```json
{ "name": "revenue",
  "params": { "region": "EU", "period": "2026-Q1" },
  "as_of": null,           // data-time: the snapshot to compute over (default latest)
  "def_as_of": null }      // def-time: which metric DEFINITION version (default latest)
```
**Output** `data`
```json
{ "name":"revenue", "value": 4231900.00,        // or "rows":[...] for non-scalar
  "params_resolved": {"region":"EU","period":"2026-Q1"},
  "def_version": 3, "data_as_of":"2026-06-12T04:00Z", "def_as_of":"2026-06-12",
  "check": { "verdict":"pass", "threshold":">0" },     // null if no check
  "sql": "SELECT ...",                                  // for audit; Claude needn't show it
  "provenance": { "receipt_id":"...", "lineage":["analytics.revenue_facts"] } }
```
**Backing:** `rvbbit.metric(name, params_jsonb, as_of)` (SETOF jsonb) + `check_metric`.
**Why it matters:** finance gets the official number — not Claude's guess at which of
five revenue columns to sum — with the bitemporal twist (`def_as_of` × `as_of`) and a
pass/fail check baked in.

### B.6 `validate_sql` — plan, don't execute (the self-correct loop)

**Input** `{ "sql": "SELECT region, sum(amt) FROM ...", "as_of": null }`
**Output** `data`
```json
{ "valid": true, "safe_select": true,
  "engine": "datafusion_vortex", "est_rows": 2400000, "est_bytes": 51000000,
  "tables": [ {"table":"analytics.revenue_facts","rows":2400000} ],
  "warnings": ["no WHERE on a 2.4M-row table — consider filtering"],
  "reason": "routed to vortex (filtered scan over compressed)",
  "suggested_fix": null }
```
**Backing:** `route_explain(sql)` — never executes. Set `valid:false` + `error` on
parse failure; `safe_select:false` for any non-SELECT. Claude iterates on this
**before** touching data — cheap correctness + cost reasoning a vanilla MCP can't do.

### B.7 `run_sql` — governed read-only execute

**Input** `{ "sql": "SELECT ...", "as_of": null, "limit": 1000 }`
**Pipeline (serve layer):**
1. `validate_sql` internally → reject if `!safe_select` or `est_rows > cost_cap`
   (default 50M; configurable) → `error: COST_EXCEEDED` with the estimate + a hint
   to add filters.
2. Run on the reader role: `BEGIN READ ONLY; SET LOCAL statement_timeout=…; <sql with
   as_of directive and an enforced LIMIT>; COMMIT`.
3. Write a receipt.
**Output** `data`
```json
{ "columns": [ {"name":"region","type":"text"}, {"name":"sum","type":"numeric"} ],
  "rows": [ ["EU", 4231900.00], ... ],
  "row_count": 6, "truncated": false,
  "engine": "datafusion_vortex", "elapsed_ms": 88,
  "as_of_applied": null, "freshness_notes": ["analytics.revenue_facts synced 04:00Z"] }
```
**Errors:** `INVALID_SQL`, `NOT_SELECT`, `COST_EXCEEDED`, `TIMEOUT`, `NOT_AUTHORIZED`.

---

## C. Worked flows

**Finance (Cowork) — "EU revenue last quarter?"**
`list_metrics(search="revenue")` → `metric("revenue", {region:"EU", period:"2026-Q1"})`
→ Claude shows **$4.23M** + "official metric v3, as of the 04:00 snapshot, check:
pass." No SQL, no guessing.

**App-builder (Code) — "build a daily churn dashboard"**
`search_data("churn")` → `describe_table("analytics.churn_monthly")` →
`validate_sql("SELECT region, avg(churned::int) …")` (fix the warning, re-validate) →
`run_sql(…, limit=50)` to eyeball → wire the app against `run_sql` (Phase 2:
`get_connection` for a scoped runtime DSN so the app pulls directly).

---

## D. Build order (Phase 0)

1. **Serve path** — gateway serve mode + the reader role + the response envelope +
   the receipt write. Smallest end-to-end: one `search_data` + one `run_sql`.
2. `search_data` + `describe_table` (the grounding tools).
3. `validate_sql` + `run_sql` (with the cost pre-check + read-only guard).
4. `list_metrics` + `get_metric` + `metric`.
5. Wire the manifest; register the server (optionally as a `kind='mcp'` capability for
   visibility). Smoke-test both worked flows from a real Claude client.

## E. Deferred to Phase 1+
Per-user identity → role mapping (B.* run as the *caller's* scope); PII masking in
samples; `ask` (compose text-to-sql); `as_of` UX polish; cost caps per role;
`define_metric`/`get_connection` (promote + runtime DSN).
