# Rvbbit Duck Sidecar UI Contract

This document is the UI-facing contract for building a small real-time
dashboard for `rvbbit-duck`, including local sidecars, shared broker workers,
Duck/Vortex execution, and shared-broker fallback events.

The UI talks to Postgres with SQL. There is no separate sidecar REST API for
the dashboard. Treat the `rvbbit` schema as the contract surface.

This contract targets Rvbbit extension version `1.0.0`.

## Product Shape

A useful first UI should expose five views:

1. Sidecar overview: online/stale instances, current mode mix, recent errors.
2. Instance grid: one row per local sidecar or broker process.
3. Live query stream: recent sidecar executions with status and latency.
4. Latency and throughput charts: p50/p95/calls by node, mode, engine, layout.
5. Fallback monitor: shared broker failures that fell back to local sidecars.

The UI should be read-only for v0. Do not add process start/stop/restart
buttons until broker supervision has an explicit SQL/admin action contract.

## Core Concepts

### Instance

An instance is one `rvbbit-duck` process.

| Mode | Meaning | UI treatment |
|---|---|---|
| `local_persistent` | A PostgreSQL backend-owned `rvbbit-duck --serve` child. | Normal for simple installs. Many instances can appear under concurrency. |
| `local_oneshot` | One `rvbbit-duck` process per query. | Debug/slow path. Usually not expected in production. |
| `shared_broker` | A supervised Unix-socket broker with a fixed worker pool. | Preferred high-concurrency/bounded-memory mode. |

### Hostname And Node ID

Every table includes both:

| Field | Meaning |
|---|---|
| `hostname` | Physical/container hostname observed by the `rvbbit-duck` process. |
| `node_id` | Logical node name. Defaults to hostname, but operators can set `RVBBIT_NODE_ID` or `RVBBIT_DUCK_NODE_ID`. |

Use `node_id` for primary grouping in charts. Show `hostname` as secondary
provenance. This leaves room for future multi-node broker fleets over shared
Parquet/Vortex storage.

### Engine And Layout

Current expected values:

| Field | Common values | Meaning |
|---|---|---|
| `engine` | `duck`, `datafusion` | The sidecar execution engine. Vortex currently uses Duck. |
| `layout` | `scan`, `hive`, `vortex`, `hive:<column>` | Accelerator layout exposed to the sidecar. |

Display unknown `engine` or `layout` values as plain text. Do not fail the UI
on unknown values.

## Stable SQL Surfaces

### `rvbbit.duck_sidecar_latest`

Use this for the dashboard header and instance grid.

One row per known sidecar/broker instance, joined with its latest heartbeat.

Important columns:

| Column | UI use |
|---|---|
| `instance_id` | Stable key for the process lifetime. Use as row key. |
| `hostname`, `node_id` | Grouping and provenance. |
| `pid` | Debug/process detail. |
| `mode` | Sidecar mode pill. |
| `engine`, `layout` | Workload path label. |
| `socket_path` | Broker detail. Usually null for local sidecars. |
| `worker_count` | Configured broker worker count, or 1 for local modes. |
| `duck_threads` | Threads per worker/local sidecar. |
| `binary_path` | Debug detail. |
| `started_at` | Instance lifetime. |
| `last_heartbeat_at` | Liveness timestamp. |
| `effective_status` | UI status. Values: `starting`, `online`, `stale`, `offline`, `error`. |
| `queue_depth` | Broker queue depth at last heartbeat. |
| `active_workers` | Broker active workers at last heartbeat. |
| `rss_bytes` | Process resident memory estimate. |
| `pg_connections` | Reserved for future process-side PG connection count. Currently may be null. |
| `events_enqueued`, `events_written`, `events_dropped` | Telemetry health counters. |
| `instance_metadata`, `heartbeat_metadata` | Expandable JSON debug detail. |

Heartbeat staleness is already reflected in `effective_status`: the view marks
an instance `stale` when `last_heartbeat_at` is older than 30 seconds.

UI severity suggestion:

| Condition | Severity | Suggested UI text |
|---|---|---|
| `effective_status = 'online'` and `events_dropped = 0` | ok | Online |
| `effective_status = 'online'` and `events_dropped > 0` | warn | Online, telemetry drops |
| `effective_status = 'stale'` | warn | Stale heartbeat |
| `effective_status in ('offline', 'error')` | error | Offline/Error |
| no rows | muted/warn | No sidecar activity observed |

Header query:

```sql
SELECT
    count(*) AS instances,
    count(*) FILTER (WHERE effective_status = 'online') AS online,
    count(*) FILTER (WHERE effective_status = 'stale') AS stale,
    count(*) FILTER (WHERE mode = 'shared_broker') AS shared_brokers,
    count(*) FILTER (WHERE mode = 'local_persistent') AS local_sidecars,
    coalesce(sum(rss_bytes), 0) AS rss_bytes,
    coalesce(sum(queue_depth), 0) AS queue_depth,
    coalesce(sum(active_workers), 0) AS active_workers,
    coalesce(sum(events_dropped), 0) AS telemetry_drops
FROM rvbbit.duck_sidecar_latest;
```

Instance grid query:

```sql
SELECT
    instance_id,
    node_id,
    hostname,
    pid,
    mode,
    engine,
    layout,
    effective_status,
    socket_path,
    worker_count,
    duck_threads,
    started_at,
    last_heartbeat_at,
    queue_depth,
    active_workers,
    rss_bytes,
    events_written,
    events_dropped
FROM rvbbit.duck_sidecar_latest
ORDER BY
    CASE effective_status
        WHEN 'online' THEN 0
        WHEN 'stale' THEN 1
        ELSE 2
    END,
    node_id,
    mode,
    engine,
    layout,
    started_at DESC;
```

### `rvbbit.duck_sidecar_query_events`

Use this for live stream, per-query details, and raw chart data.

One row per sidecar query execution event.

Important columns:

| Column | UI use |
|---|---|
| `id` | Monotonic event id. Use for incremental polling. |
| `observed_at` | Event timestamp. |
| `instance_id`, `hostname`, `node_id`, `pid` | Process provenance. |
| `mode`, `engine`, `layout`, `worker_id` | Execution path. |
| `command` | Sidecar command, usually null for SQL query, `prewarm` for prewarm. |
| `query_hash` | Query grouping key. Raw SQL is intentionally not stored. |
| `status` | Query status from sidecar response. Expected: `ok`, `fallback`. |
| `queue_wait_ms` | Time waiting in broker queue. Null outside shared broker mode. |
| `elapsed_ms` | Total sidecar request wall time as observed by `rvbbit-duck`. |
| `execute_ms` | Engine execution median from the sidecar summary. |
| `route_safety_ms` | Route safety/catalog visibility check time. |
| `parquet_prewarm_ms` | Metadata/footer prewarm time when executor/cached catalog changed. |
| `row_count` | Rows returned to PostgreSQL, capped by request max rows. |
| `result_format` | `json` or `arrow_ipc_file`. |
| `arrow_ipc_bytes` | IPC payload size when Arrow IPC transport is used. |
| `repeat_count`, `timeout_s`, `max_rows` | Request settings. |
| `error` | Error string for fallback/error events. |
| `cache` | Expandable JSON for catalog/executor/footer/route-safety cache detail. |
| `tables` | Expandable JSON table/file/row summaries used by the query. |
| `metadata` | Expandable JSON request detail. |

Live stream query:

```sql
SELECT
    id,
    observed_at,
    node_id,
    mode,
    engine,
    layout,
    worker_id,
    status,
    query_hash,
    round(elapsed_ms::numeric, 1) AS elapsed_ms,
    round(coalesce(queue_wait_ms, 0)::numeric, 1) AS queue_wait_ms,
    round(coalesce(execute_ms, 0)::numeric, 1) AS execute_ms,
    row_count,
    result_format,
    arrow_ipc_bytes,
    left(coalesce(error, ''), 160) AS error_preview
FROM rvbbit.duck_sidecar_query_events
ORDER BY id DESC
LIMIT 100;
```

Incremental polling query:

```sql
SELECT
    id,
    observed_at,
    node_id,
    mode,
    engine,
    layout,
    worker_id,
    status,
    query_hash,
    elapsed_ms,
    queue_wait_ms,
    execute_ms,
    row_count,
    result_format,
    arrow_ipc_bytes,
    error
FROM rvbbit.duck_sidecar_query_events
WHERE id > $1
ORDER BY id ASC
LIMIT 500;
```

Use the highest returned `id` as the next cursor. Poll every 1-2 seconds for a
live dashboard. If no rows are returned, keep the same cursor.

Latency sparkline query:

```sql
SELECT
    date_bin(interval '5 seconds', observed_at, timestamp with time zone 'epoch') AS bucket,
    node_id,
    mode,
    engine,
    layout,
    count(*) AS calls,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS p50_elapsed_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_elapsed_ms,
    max(elapsed_ms) AS max_elapsed_ms,
    count(*) FILTER (WHERE status <> 'ok') AS non_ok
FROM rvbbit.duck_sidecar_query_events
WHERE observed_at >= clock_timestamp() - interval '10 minutes'
GROUP BY 1, 2, 3, 4, 5
ORDER BY bucket DESC, calls DESC;
```

Top slow query shapes:

```sql
SELECT
    query_hash,
    node_id,
    mode,
    engine,
    layout,
    count(*) AS calls,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS p50_elapsed_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_elapsed_ms,
    max(elapsed_ms) AS max_elapsed_ms,
    count(*) FILTER (WHERE status <> 'ok') AS non_ok
FROM rvbbit.duck_sidecar_query_events
WHERE observed_at >= clock_timestamp() - interval '1 hour'
GROUP BY query_hash, node_id, mode, engine, layout
ORDER BY p95_elapsed_ms DESC NULLS LAST
LIMIT 25;
```

Cache diagnostics query:

```sql
SELECT
    observed_at,
    node_id,
    mode,
    engine,
    layout,
    query_hash,
    cache->>'catalog_cache_hit' AS catalog_cache_hit,
    cache->>'executor_cache_hit' AS executor_cache_hit,
    cache->>'route_safety_cache_hit' AS route_safety_cache_hit,
    cache->>'route_safety_local_hit' AS route_safety_local_hit,
    (cache->>'parquet_footer_hits')::bigint AS parquet_footer_hits,
    (cache->>'parquet_footer_misses')::bigint AS parquet_footer_misses,
    round(elapsed_ms::numeric, 1) AS elapsed_ms
FROM rvbbit.duck_sidecar_query_events
ORDER BY id DESC
LIMIT 100;
```

### `rvbbit.duck_sidecar_query_summary`

Use this for minute-level charts when the UI does not need raw event detail.

Columns:

| Column | UI use |
|---|---|
| `minute` | Time bucket. |
| `hostname`, `node_id` | Grouping. |
| `mode`, `engine`, `layout`, `status` | Series dimensions. |
| `calls` | Query count. |
| `p50_elapsed_ms`, `p95_elapsed_ms`, `max_elapsed_ms` | Latency charts. |
| `rows_returned`, `arrow_ipc_bytes` | Throughput/transport detail. |

Recent summary query:

```sql
SELECT *
FROM rvbbit.duck_sidecar_query_summary
WHERE minute >= date_trunc('minute', clock_timestamp() - interval '1 hour')
ORDER BY minute DESC, calls DESC;
```

### `rvbbit.duck_sidecar_fallback_events`

Use this for the operational warning panel.

One row is recorded when shared broker mode is enabled, the socket path fails,
and the extension falls back to local sidecar execution.

Important columns:

| Column | UI use |
|---|---|
| `observed_at` | Fallback timestamp. |
| `hostname`, `node_id`, `backend_pid` | Postgres backend provenance. |
| `database_name`, `role_name` | User/session context. |
| `engine`, `layout`, `socket_path` | Failed shared-broker target. |
| `reason` | Failure text. |
| `fallback_mode` | Local path attempted after broker failure. |
| `query_hash` | Query grouping key. |
| `metadata` | Reserved JSON detail. |

Fallback panel query:

```sql
SELECT
    observed_at,
    node_id,
    hostname,
    database_name,
    role_name,
    engine,
    layout,
    socket_path,
    fallback_mode,
    query_hash,
    left(reason, 240) AS reason
FROM rvbbit.duck_sidecar_fallback_events
WHERE observed_at >= clock_timestamp() - interval '24 hours'
ORDER BY observed_at DESC
LIMIT 100;
```

Fallback rate query:

```sql
SELECT
    date_bin(interval '5 minutes', observed_at, timestamp with time zone 'epoch') AS bucket,
    node_id,
    engine,
    layout,
    socket_path,
    count(*) AS fallbacks
FROM rvbbit.duck_sidecar_fallback_events
WHERE observed_at >= clock_timestamp() - interval '24 hours'
GROUP BY 1, 2, 3, 4, 5
ORDER BY bucket DESC, fallbacks DESC;
```

UI severity:

| Condition | Severity | Meaning |
|---|---|---|
| no fallback events in current window | ok | Shared broker path is stable or unused. |
| occasional fallback, query still succeeds | warn | Broker supervision/socket path may be flaky. |
| repeated fallback for same socket path | error | Shared broker is probably down or misconfigured. |

### `rvbbit.duck_sidecar_heartbeats`

Use this only for detailed time-series or debugging. Most UI panels should read
`rvbbit.duck_sidecar_latest`.

Heartbeat history query:

```sql
SELECT
    observed_at,
    instance_id,
    node_id,
    mode,
    engine,
    layout,
    queue_depth,
    active_workers,
    rss_bytes,
    events_enqueued,
    events_written,
    events_dropped
FROM rvbbit.duck_sidecar_heartbeats
WHERE instance_id = $1
ORDER BY observed_at DESC
LIMIT 300;
```

## Suggested Dashboard Layout

### Header Cards

Use the header query from `duck_sidecar_latest`.

Cards:

- Online instances
- Shared brokers
- Local sidecars
- Total RSS
- Queue depth
- Telemetry drops
- Fallbacks in last hour

Fallbacks in last hour:

```sql
SELECT count(*) AS fallbacks_last_hour
FROM rvbbit.duck_sidecar_fallback_events
WHERE observed_at >= clock_timestamp() - interval '1 hour';
```

### Instance Grid

Columns:

- status pill
- node
- mode
- engine/layout
- pid
- workers/threads
- queue/active workers
- RSS
- last heartbeat
- telemetry drops

Default sort:

1. stale/error first
2. shared brokers before local sidecars
3. node id
4. newest heartbeat

### Live Stream

Columns:

- time
- status
- node
- mode
- engine/layout
- worker
- query hash
- elapsed
- queue wait
- execute
- rows
- result format
- error preview

Use incremental polling by `id`. Keep a client-side ring buffer of the most
recent 200-500 events.

### Charts

Recommended first charts:

- Calls/sec by `mode + layout`
- p95 `elapsed_ms` by `mode + layout`
- p95 `queue_wait_ms` for `shared_broker` only
- RSS over time by `instance_id`
- Fallbacks over time by `socket_path`

### Detail Drawer

When a user clicks a query event, show:

- event metadata
- cache JSON
- tables JSON
- error string
- related events with same `query_hash`

Related events query:

```sql
SELECT *
FROM rvbbit.duck_sidecar_query_events
WHERE query_hash = $1
ORDER BY observed_at DESC
LIMIT 50;
```

## UI State And Formatting Rules

Recommended formatting:

| Value | Format |
|---|---|
| `*_ms` | `<1ms`, `12ms`, `1.23s` depending magnitude. |
| `rss_bytes`, `arrow_ipc_bytes` | IEC units: KiB, MiB, GiB. |
| timestamps | Relative age in tables, exact timestamp in tooltip/detail. |
| `query_hash` | First 10-12 chars in table, full value in copyable detail. |
| JSON columns | Collapsed by default; pretty-print in detail drawer. |

Do not display raw SQL because it is not stored in this telemetry contract.
If the UI needs SQL text, correlate with route/query-lens telemetry separately
using `query_hash` and time windows.

## Correlating With Routing Telemetry

The sidecar telemetry tells what happened inside `rvbbit-duck`. The routing
tables tell why the router chose a path.

Useful join pattern:

```sql
SELECT
    q.observed_at,
    q.node_id,
    q.mode,
    q.engine,
    q.layout,
    q.status,
    q.elapsed_ms,
    r.route,
    r.candidate,
    r.route_source,
    r.reason
FROM rvbbit.duck_sidecar_query_events q
LEFT JOIN LATERAL (
    SELECT *
    FROM rvbbit.route_executions r
    WHERE r.query_hash = q.query_hash
      AND r.executed_at BETWEEN q.observed_at - interval '5 seconds'
                            AND q.observed_at + interval '5 seconds'
    ORDER BY abs(extract(epoch FROM (r.executed_at - q.observed_at)))
    LIMIT 1
) r ON true
ORDER BY q.observed_at DESC
LIMIT 100;
```

Treat this as a best-effort correlation. Sidecar events and route executions
are produced by different processes.

## Capability Check

A UI should run this on startup and degrade gracefully if objects are missing:

```sql
SELECT
    to_regclass('rvbbit.duck_sidecar_latest') IS NOT NULL AS has_latest,
    to_regclass('rvbbit.duck_sidecar_query_events') IS NOT NULL AS has_query_events,
    to_regclass('rvbbit.duck_sidecar_fallback_events') IS NOT NULL AS has_fallback_events;
```

If any field is false, show:

```text
Duck sidecar telemetry requires pg_rvbbit 1.0.0 or newer.
```

Version check:

```sql
SELECT extversion
FROM pg_extension
WHERE extname = 'pg_rvbbit';
```

## Polling Guidance

Recommended v0 polling:

| Panel | Query source | Interval |
|---|---|---:|
| Header cards | `duck_sidecar_latest` plus fallback count | 2s |
| Instance grid | `duck_sidecar_latest` | 2s |
| Live stream | `duck_sidecar_query_events WHERE id > cursor` | 1s |
| Latency charts | raw events or `duck_sidecar_query_summary` | 5s |
| Fallback panel | `duck_sidecar_fallback_events` | 5s |

Avoid full-table scans:

- Always use `observed_at >= clock_timestamp() - interval ...` for charts.
- Use `id > cursor` for live streams.
- Limit detail tables.
- Do not poll heartbeat history unless a detail drawer is open.

## Empty States

| Situation | UI copy |
|---|---|
| No instances | `No Duck sidecar activity observed yet.` |
| No query events | `No Duck sidecar queries recorded in this window.` |
| No shared brokers | `Shared broker mode is not currently observed.` |
| No fallbacks | `No shared broker fallback events in this window.` |
| Objects missing | `Duck sidecar telemetry requires pg_rvbbit 1.0.0 or newer.` |

## Safety Rules For UI Agents

- Do not `DELETE`, `UPDATE`, or `TRUNCATE` telemetry tables from dashboard UI.
- Do not try to manage OS processes from SQL in v0.
- Do not assume a shared broker exists. Local persistent sidecars are normal.
- Do not treat `local_persistent` as an error by itself.
- Do not fail on unknown `engine`, `layout`, `status`, or JSON keys.
- Do not expose `socket_path` as a user-editable control in a read-only
  dashboard.
- Do not use `pg_stat_activity` as the primary sidecar source. It is useful for
  debugging, but the contract is the `rvbbit.duck_sidecar_*` tables and views.

## Minimal First Pass

A small first dashboard can be built from only these three queries:

```sql
-- 1. Header and instance list
SELECT *
FROM rvbbit.duck_sidecar_latest
ORDER BY node_id, mode, engine, layout, started_at DESC;

-- 2. Live stream
SELECT *
FROM rvbbit.duck_sidecar_query_events
ORDER BY id DESC
LIMIT 100;

-- 3. Fallbacks
SELECT *
FROM rvbbit.duck_sidecar_fallback_events
ORDER BY observed_at DESC
LIMIT 50;
```

That gives enough signal to answer:

- Is the broker/local sidecar path alive?
- Which node/mode/layout is serving queries?
- Are queries succeeding?
- Is queue wait growing?
- Are shared broker fallbacks happening?
- Is telemetry itself dropping events?
