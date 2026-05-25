# Cost And Receipt UI Contract

This document is the v0 contract for building a dedicated Rvbbit cost,
receipt, and provider-audit panel. It is written for UI builders who should
not need to inspect extension SQL to understand the available state.

The UI talks to Postgres with SQL. There is no separate REST API for this
surface.

## Core Screens

A useful first UI should expose six views:

| View | Primary source | Purpose |
|---|---|---|
| Overview | `rvbbit.cost_audit_summary()` | One-call health, cost coverage, and receipt queue state. |
| Gaps | `rvbbit.cost_audit_gaps` | Receipts needing attention: missing, pending, stale, uncosted, or errored cost rows. |
| Queries | `rvbbit.query_costs`, `rvbbit.receipts` | Cost and call rollups by `query_id`. |
| Receipts | `rvbbit.receipts`, `rvbbit.receipt_cost_audit`, `rvbbit.receipt_costs` | Operator-call audit trail and cost coverage. |
| Cost events | `rvbbit.cost_latest`, `rvbbit.cost_events` | Latest per-call cost state and append-only event history. |
| Policies | `rvbbit.cost_policies`, `rvbbit.set_cost_policy(...)` | Configure free/fixed/rate/settled cost behavior. |

The UI should be data-driven. It should not hardcode operator names, backend
names, models, MCP tools, or provider ids. The enum values in this document are
the stable contract.

## Concepts

- **Receipt**: one semantic operator invocation in `rvbbit.receipts`. It stores
  input hash, output/error, token/latency totals, `query_id`, and `sub_calls`.
- **Sub-call**: one entry inside `receipts.sub_calls`, usually a model,
  specialist, or MCP call. Chargeable kinds are `llm`, `specialist`, and `mcp`.
- **Cost event**: one append-only fact in `rvbbit.cost_events`. A call can have
  multiple events over time, for example `pending -> settled`.
- **Latest cost**: the current state of a cost request, exposed by
  `rvbbit.cost_latest`.
- **Query id**: `uuid` tying together all receipts, KG evidence, MCP calls, and
  cost events spawned by the same SQL query when available.
- **Cost policy**: user-configurable fallback pricing rule for calls that do
  not return provider cost directly.

## Overview Panel

Use this for the dashboard header:

```sql
SELECT rvbbit.cost_audit_summary();
```

Current JSON shape:

```json
{
  "receipt_queue_pending": 0,
  "receipts": {
    "total": 1208,
    "ok": 24,
    "no_chargeable_sub_calls": 48,
    "missing_cost_events": 0,
    "pending": 0,
    "stale_pending": 0,
    "uncosted": 575,
    "errors": 561
  },
  "cost_events": {
    "latest_calls": 1190,
    "pending": 0,
    "settled": 0,
    "estimated": 54,
    "free": 0,
    "uncosted": 575,
    "error": 561
  }
}
```

Recommended header cards:

| Card | Source | Suggested treatment |
|---|---|---|
| Total query/operator receipts | `receipts.total` | Neutral count. |
| Missing cost events | `receipts.missing_cost_events` | Red if nonzero; offer maintenance action. |
| Stale pending | `receipts.stale_pending` | Amber/red; suggests reconcile problem. |
| Pending cost calls | `cost_events.pending` | Amber; normal briefly after provider calls. |
| Estimated cost calls | `cost_events.estimated` | Neutral/blue; actual provider cost unavailable. |
| Uncosted calls | `cost_events.uncosted` | Amber; may need cost policies. |
| Receipt queue | `receipt_queue_pending` | Amber if nonzero; offer flush/maintenance action. |

Polling every 5-15 seconds is enough for the overview. Cost reconciliation is
eventual, not real-time.

## Audit Statuses

`rvbbit.receipt_cost_audit.audit_status` is the main UI status enum.

| Status | Meaning | UI severity | Typical action |
|---|---|---|---|
| `ok` | Receipt has chargeable sub-calls and each has current cost state. | Good | None. |
| `no_chargeable_sub_calls` | Receipt had no `llm`, `specialist`, or `mcp` sub-calls. | Neutral | Hide by default. |
| `missing_cost_events` | Receipt has chargeable sub-calls without ledger rows. | High | Run maintenance/backfill. |
| `pending` | Provider cost is expected later. | Low/medium | Wait or reconcile. |
| `stale_pending` | Pending cost is older than the built-in stale threshold. | High | Reconcile; check provider credentials. |
| `uncosted` | Rvbbit observed a call but has no actual or estimated price. | Medium | Add a cost policy or model rate. |
| `errors` | Underlying call errored. | Medium/high | Inspect receipt/sub-call error. |

## Gaps View

Use `rvbbit.cost_audit_gaps` for the default "needs attention" table:

```sql
SELECT
  receipt_id,
  operator,
  query_id,
  invocation_at,
  chargeable_sub_calls,
  cost_event_sub_calls,
  missing_cost_events,
  pending_calls,
  estimated_calls,
  uncosted_calls,
  error_calls,
  oldest_pending_at,
  audit_status,
  total_cost_usd
FROM rvbbit.cost_audit_gaps
ORDER BY
  CASE audit_status
    WHEN 'missing_cost_events' THEN 1
    WHEN 'stale_pending' THEN 2
    WHEN 'uncosted' THEN 3
    WHEN 'errors' THEN 4
    WHEN 'pending' THEN 5
    ELSE 9
  END,
  invocation_at DESC
LIMIT 500;
```

Recommended filters:

- `audit_status`
- `operator`
- `query_id`
- `invocation_at` range
- `missing_cost_events > 0`
- `uncosted_calls > 0`
- `error_calls > 0`

## Query Cost View

Use `rvbbit.query_costs` for query-level rollups:

```sql
SELECT
  query_id,
  costed_calls,
  pending_calls,
  estimated_calls,
  uncosted_calls,
  error_calls,
  total_cost_usd,
  first_event_at,
  last_event_at
FROM rvbbit.query_costs
ORDER BY last_event_at DESC
LIMIT 500;
```

For richer query rows, join receipts:

```sql
SELECT
  r.query_id,
  count(*) AS receipts,
  count(*) FILTER (WHERE r.error IS NOT NULL) AS receipt_errors,
  count(DISTINCT r.operator) AS operators,
  coalesce(sum(r.n_tokens_in), 0) AS tokens_in,
  coalesce(sum(r.n_tokens_out), 0) AS tokens_out,
  coalesce(sum(r.latency_ms), 0) AS latency_ms,
  qc.costed_calls,
  qc.pending_calls,
  qc.estimated_calls,
  qc.uncosted_calls,
  qc.error_calls,
  qc.total_cost_usd,
  min(r.invocation_at) AS first_receipt_at,
  max(r.invocation_at) AS last_receipt_at
FROM rvbbit.receipts r
LEFT JOIN rvbbit.query_costs qc USING (query_id)
WHERE r.query_id IS NOT NULL
GROUP BY
  r.query_id,
  qc.costed_calls,
  qc.pending_calls,
  qc.estimated_calls,
  qc.uncosted_calls,
  qc.error_calls,
  qc.total_cost_usd
ORDER BY last_receipt_at DESC
LIMIT 500;
```

Clicking a query should drill into all receipts and cost events with that
`query_id`.

## Receipt List

Use this for the default receipt table:

```sql
SELECT
  r.receipt_id,
  r.operator,
  r.model,
  r.query_id,
  r.n_tokens_in,
  r.n_tokens_out,
  r.latency_ms,
  r.error,
  r.invocation_at,
  a.audit_status,
  a.chargeable_sub_calls,
  a.cost_event_sub_calls,
  a.pending_calls,
  a.estimated_calls,
  a.uncosted_calls,
  a.error_calls,
  a.total_cost_usd
FROM rvbbit.receipts r
LEFT JOIN rvbbit.receipt_cost_audit a USING (receipt_id)
ORDER BY r.invocation_at DESC
LIMIT 500;
```

Recommended columns:

| Column | UI treatment |
|---|---|
| `operator` | Primary label. |
| `model` | Secondary label or chip. |
| `audit_status` | Status pill using the enum above. |
| `total_cost_usd` | Currency; show estimated/uncosted counts beside it. |
| `n_tokens_in`, `n_tokens_out` | Compact token counters. |
| `latency_ms` | Duration. |
| `query_id` | Clickable drilldown; gray if null. |
| `error` | Error icon; expand full text on detail page. |
| `invocation_at` | Relative time plus exact tooltip. |

## Receipt Detail

Load the full receipt plus cost rows:

```sql
SELECT
  r.*,
  a.audit_status,
  a.chargeable_sub_calls,
  a.cost_event_sub_calls,
  a.missing_cost_events,
  a.pending_calls,
  a.estimated_calls,
  a.uncosted_calls,
  a.error_calls,
  a.total_cost_usd
FROM rvbbit.receipts r
LEFT JOIN rvbbit.receipt_cost_audit a USING (receipt_id)
WHERE r.receipt_id = $1::uuid;
```

Render `sub_calls` as an ordered timeline:

```sql
SELECT
  (sub.ord - 1)::int AS sub_call_index,
  sub.value AS sub_call,
  c.status,
  c.cost_source,
  c.cost_usd,
  c.provider_request_id,
  c.provider_generation_id,
  c.upstream_id,
  c.created_at AS cost_updated_at
FROM rvbbit.receipts r
CROSS JOIN LATERAL jsonb_array_elements(
  CASE
    WHEN jsonb_typeof(coalesce(r.sub_calls, '[]'::jsonb)) = 'array'
    THEN coalesce(r.sub_calls, '[]'::jsonb)
    ELSE '[]'::jsonb
  END
) WITH ORDINALITY AS sub(value, ord)
LEFT JOIN rvbbit.cost_latest c
  ON c.receipt_id = r.receipt_id
 AND c.sub_call_index = (sub.ord - 1)::int
WHERE r.receipt_id = $1::uuid
ORDER BY sub.ord;
```

Important `sub_call` JSON keys to display when present:

| JSON key | Meaning |
|---|---|
| `step` | Pipeline step name. |
| `kind` | `llm`, `specialist`, `mcp`, or internal kinds such as `code`. |
| `model` | Model/tool/backend identifier used by the step. |
| `backend` | Rvbbit backend name when available. |
| `transport` | Transport such as `openai_chat`, `anthropic`, `gemini`, `mcp`, or sidecar type. |
| `tokens_in`, `tokens_out` | Normalized token counts. |
| `native_tokens_in`, `native_tokens_out` | Provider-native token counts when available. |
| `reasoning_tokens`, `cached_tokens` | Provider-specific token classes. |
| `latency_ms` | Step latency. |
| `cost_usd` | Inline provider cost if returned in the response. |
| `cost_source` | Provider or policy source for cost computation. |
| `provider_request_id` | Provider request id when available. |
| `provider_generation_id` | OpenRouter generation id / delayed-cost id. |
| `upstream_id` | Upstream provider id when available. |
| `error` | Step error. |
| `raw_usage` | Provider usage payload; render in expandable JSON. |

## Cost Event Timeline

`rvbbit.cost_latest` shows current state. `rvbbit.cost_events` is append-only
history. Use this for a receipt's ledger timeline:

```sql
SELECT
  event_id,
  cost_request_id,
  sub_call_index,
  source,
  backend,
  transport,
  model,
  tool,
  provider_request_id,
  provider_generation_id,
  upstream_id,
  status,
  cost_source,
  tokens_in,
  tokens_out,
  native_tokens_in,
  native_tokens_out,
  reasoning_tokens,
  cached_tokens,
  cost_usd,
  currency,
  raw,
  created_at
FROM rvbbit.cost_events
WHERE receipt_id = $1::uuid
ORDER BY sub_call_index NULLS LAST, event_id;
```

For one cost request:

```sql
SELECT *
FROM rvbbit.cost_events
WHERE cost_request_id = $1::uuid
ORDER BY event_id;
```

Treat `raw` as expandable JSON, not as stable UI schema.

## Cost Statuses

`rvbbit.cost_latest.status` is the current cost-event status.

| Status | Meaning | UI severity |
|---|---|---|
| `pending` | Waiting for provider settlement. | Low until stale. |
| `settled` | Actual provider cost is known. | Good. |
| `estimated` | Estimated from model rates or policy. | Neutral. |
| `free` | Explicitly configured zero-cost call. | Good. |
| `uncosted` | No cost information is available. | Medium. |
| `error` | Underlying call errored. | Medium/high. |

`cost_source` is intentionally more open-ended. Known values include:

- `inline`
- `openrouter_generation`
- `provider_settled`
- `model_rate`
- `policy_model_rate`
- `policy_fixed`
- `policy_free`
- `cache_hit`
- `none`

The UI should display unknown `cost_source` values as plain text, not fail.

## Policy View

List policies:

```sql
SELECT
  target_kind,
  target_name,
  policy,
  model,
  fixed_cost_usd,
  input_per_mtok,
  output_per_mtok,
  currency,
  notes,
  updated_at
FROM rvbbit.cost_policies
ORDER BY target_kind, target_name;
```

Policy target kinds:

| `target_kind` | Target name |
|---|---|
| `mcp_tool` | `server.tool` |
| `backend` | `rvbbit.backends.name` |
| `model` | model id string |

Policy values:

| Policy | Required fields | Meaning |
|---|---|---|
| `free` | none | Record status `free`, cost `0`. |
| `fixed` | `fixed_cost_usd` | Fixed estimated cost per call. |
| `model_rate` | either policy rates or `rvbbit.model_rates` | Estimate from token counts. |
| `provider_settled` | provider generation id at runtime | Create `pending`, then reconcile later. |
| `unknown` | none | Explicitly mark as uncosted. |

Save a policy with:

```sql
SELECT rvbbit.set_cost_policy(
  target_kind => $1,
  target_name => $2,
  policy => $3,
  fixed_cost_usd => $4,
  input_per_mtok => $5,
  output_per_mtok => $6,
  model => $7,
  notes => $8
);
```

The function returns the saved row as JSONB.

Good policy editor behavior:

- Use a dropdown for `target_kind` and `policy`.
- Disable irrelevant numeric inputs based on selected policy.
- Require `fixed_cost_usd` for `fixed`.
- For `model_rate`, allow either explicit rates or a `model` reference.
- Show a warning that policies affect future cost decisions and backfills, not
  already-settled actual provider cost rows.

## Maintenance Actions

Expose these as explicit admin actions, not automatic per-render calls.

```sql
SELECT rvbbit.maintain_cost_audit(
  queue_limit => 10000,
  backfill_limit => 10000,
  reconcile_limit => 1000
);
```

Return shape:

```json
{
  "flushed_receipts": 0,
  "backfilled_receipts": 1208,
  "reconciled_costs": 0,
  "summary": {
    "receipt_queue_pending": 0,
    "receipts": {},
    "cost_events": {}
  }
}
```

Individual actions:

```sql
SELECT rvbbit.flush_receipt_queue(10000);
SELECT rvbbit.backfill_cost_events_from_receipts(10000);
SELECT rvbbit.reconcile_openrouter_costs(1000);
```

Action guidance:

| Action | When to show |
|---|---|
| Maintain cost audit | Always available in admin panel. |
| Flush receipt queue | Highlight when `receipt_queue_pending > 0`. |
| Backfill cost events | Highlight when `missing_cost_events > 0`. |
| Reconcile OpenRouter | Highlight when `pending > 0` or `stale_pending > 0`. |

The UI should display function return values directly and refresh the overview
after completion.

## Provider Settlement Notes

OpenRouter costs settle after the provider call. Rvbbit records
`provider_generation_id` first, then `rvbbit.reconcile_openrouter_costs(...)`
appends a later `settled` event.

If reconciliation does not move stale pending calls:

- Check that the `openrouter` backend row has the correct `auth_header_env`.
- Check that the environment variable, normally `OPENROUTER_API_KEY`, exists in
  the Postgres container/process.
- Show the latest `raw` error in the cost event timeline.

Other providers may be `estimated` from token rates if they do not expose
actual cost.

## Charts

Useful chart queries:

Cost by day:

```sql
SELECT
  date_trunc('day', last_event_at) AS day,
  sum(total_cost_usd) AS total_cost_usd,
  sum(costed_calls) AS calls,
  sum(pending_calls) AS pending_calls,
  sum(estimated_calls) AS estimated_calls,
  sum(uncosted_calls) AS uncosted_calls,
  sum(error_calls) AS error_calls
FROM rvbbit.query_costs
GROUP BY day
ORDER BY day;
```

Cost by operator:

```sql
SELECT
  r.operator,
  count(DISTINCT r.receipt_id) AS receipts,
  coalesce(sum(c.total_cost_usd), 0)::numeric(18,9) AS total_cost_usd,
  sum(c.pending_calls) AS pending_calls,
  sum(c.estimated_calls) AS estimated_calls,
  sum(c.uncosted_calls) AS uncosted_calls,
  sum(c.error_calls) AS error_calls
FROM rvbbit.receipts r
LEFT JOIN rvbbit.receipt_costs c USING (receipt_id)
GROUP BY r.operator
ORDER BY total_cost_usd DESC, receipts DESC;
```

Cost by backend/model:

```sql
SELECT
  coalesce(backend, 'unknown') AS backend,
  coalesce(model, tool, 'unknown') AS model_or_tool,
  status,
  cost_source,
  count(*) AS calls,
  coalesce(sum(cost_usd), 0)::numeric(18,9) AS total_cost_usd
FROM rvbbit.cost_latest
GROUP BY backend, model_or_tool, status, cost_source
ORDER BY total_cost_usd DESC, calls DESC;
```

Latency and tokens by operator:

```sql
SELECT
  operator,
  count(*) AS receipts,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
  sum(n_tokens_in) AS tokens_in,
  sum(n_tokens_out) AS tokens_out
FROM rvbbit.receipts
GROUP BY operator
ORDER BY receipts DESC;
```

## Formatting

Recommended UI formatting:

- `cost_usd`: show `$0.000000` for small values; keep full precision in
  tooltip.
- `tokens_*`: compact thousands separators.
- `latency_ms`: render as `ms` under 1000, otherwise seconds.
- `query_id`, `receipt_id`, `cost_request_id`: show shortened form, copy full
  UUID on click.
- JSONB fields: collapsed by default, with copy button.

## Safety Rules For UI Agents

- Do not delete or update `rvbbit.receipts` from UI flows.
- Do not update `rvbbit.cost_events`; it is append-only.
- Do not assume a receipt has a `query_id`; queued receipts may have `NULL`.
- Do not assume `raw` or `sub_calls` JSON contains every key.
- Do not call maintenance functions on every render. Use a button or a
  scheduled job.
- Do not hide `uncosted` rows; they are important audit gaps.
- Do not treat `estimated` as actual spend. Label it clearly.
- Do not treat `no_chargeable_sub_calls` as an error.

## Minimal Smoke Test

A UI builder can validate connectivity and contract support with:

```sql
SELECT extversion
FROM pg_extension
WHERE extname = 'pg_rvbbit';

SELECT
  to_regclass('rvbbit.cost_events') IS NOT NULL AS has_cost_events,
  to_regclass('rvbbit.receipt_cost_audit') IS NOT NULL AS has_receipt_cost_audit,
  to_regprocedure('rvbbit.cost_audit_summary()') IS NOT NULL AS has_summary,
  to_regprocedure('rvbbit.maintain_cost_audit(bigint,bigint,bigint)') IS NOT NULL AS has_maintenance;

SELECT rvbbit.cost_audit_summary();
```

This contract targets Rvbbit extension version `0.48.0`.
