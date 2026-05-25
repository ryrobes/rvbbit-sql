# Warren UI Contract

This document is the v0 contract for building a Warren dashboard or adding
Warren panels to the Rvbbit UI. It is written for UI builders who should not
need to inspect extension SQL to understand the available state.

Warren is an optional deployment scheduler for capability sidecars. Postgres is
the control plane. A `warren-agent` process runs on a host with Docker and
optional CPU/GPU resources, polls Postgres for jobs, deploys sidecars, then
registers resulting Rvbbit backends and SQL operators.

## Core Screens

A useful first UI should expose five views:

| View | Primary source | Purpose |
|---|---|---|
| Inventory | `rvbbit.warren_inventory` | Node list with latest metrics and active deployments. |
| Jobs | `rvbbit.warren_jobs` | Deployment queue, running jobs, failures, and history. |
| Deployments | `rvbbit.warren_deployments` | Sidecars currently known to Warren, linked to backend/operator names. |
| Metrics | `rvbbit.warren_node_latest_metrics`, `rvbbit.warren_node_metrics` | CPU, memory, disk, and GPU observability. |
| Capability deploy | `capabilities/catalog.json`, manifest YAML, `rvbbit.deploy_capability(...)` | Queue new deployments from curated capability packs. |

The UI should be data-driven. It should not hardcode curated capability names,
backend names, generated SQL bodies, or deployment states beyond the enums in
this document.

## Inventory View

Use `rvbbit.warren_inventory` for the default dashboard. It intentionally joins
nodes, latest metrics, and active deployments so the UI can render a useful
overview with one query.

```sql
SELECT
  node_id,
  node_name,
  base_url,
  labels,
  capacity,
  node_status,
  version,
  last_heartbeat,
  latest_metrics_at,
  cpu_pct,
  load1,
  mem_used_bytes,
  mem_total_bytes,
  gpu_count,
  gpu_util_pct,
  gpu_mem_used_bytes,
  gpu_mem_total_bytes,
  deployment_id,
  kind,
  deployment_name,
  deployment_status,
  endpoint_url,
  backend_name,
  operator_name,
  health,
  error,
  deployment_updated_at
FROM rvbbit.warren_inventory
ORDER BY node_name, deployment_name NULLS FIRST;
```

Render this grouped by `node_name`. A node with no active deployments still
appears with `deployment_id IS NULL`.

Recommended node cards:

| Field | UI treatment |
|---|---|
| `node_name` | Primary node label. |
| `node_status` | Status pill. |
| `last_heartbeat` | Relative age; warn if stale. |
| `labels` | Filter chips and placement hints. |
| `capacity` | Human-readable resource summary when known. |
| `cpu_pct`, `load1` | Small utilization indicators. |
| `mem_used_bytes`, `mem_total_bytes` | Memory bar. |
| `gpu_count`, `gpu_util_pct`, `gpu_mem_*` | GPU summary; hide or gray out when `gpu_count` is `0` or null. |
| `deployment_*` | Active deployment rows under the node. |

Heartbeat staleness is a UI policy. A practical default is:

| Condition | Suggested UI state |
|---|---|
| `last_heartbeat IS NULL` | `unknown` |
| `now() - last_heartbeat < interval '30 seconds'` | `fresh` |
| `now() - last_heartbeat < interval '2 minutes'` | `stale` |
| otherwise | `offline_or_blocked` |

The database currently does not mark stale nodes offline automatically.

## Node Registry

Use `rvbbit.warren_nodes` when the UI needs node-only data that is not
deployment-expanded.

```sql
SELECT
  node_id,
  name,
  base_url,
  labels,
  capacity,
  inventory,
  status,
  version,
  last_heartbeat,
  created_at,
  updated_at
FROM rvbbit.warren_nodes
ORDER BY name;
```

Node statuses:

| Status | Meaning |
|---|---|
| `registered` | Catalog row exists, but an agent has not recently marked it ready. |
| `ready` | Agent is available to claim work. |
| `busy` | Agent is alive and may be processing work. |
| `draining` | Reserved for future stop-accepting-work behavior. |
| `offline` | Reserved for explicit offline marking. |
| `error` | Reserved for node-level error state. |

`labels` are used for placement. A queued job can be claimed when
`warren_nodes.labels @> warren_jobs.target_selector`.

Example labels:

```json
{"capability": true, "docker": true, "gpu": false}
```

Example GPU labels:

```json
{"capability": true, "docker": true, "gpu": true, "cuda": true, "region": "lab"}
```

`capacity` is informational in v0. It is still useful for UI display and future
placement features.

Example capacity:

```json
{"vram_gb": 24, "slots": 2, "disk_gb": 500}
```

`inventory` is updated from metrics when GPU data is available. It is an array
so multiple GPUs can be represented.

## Job Queue

Use `rvbbit.warren_jobs` for queue and history screens.

```sql
SELECT
  job_id,
  kind,
  desired_state,
  name,
  target_selector,
  status,
  claimed_by,
  claimed_at,
  attempts,
  endpoint_url,
  backend_name,
  operator_name,
  error,
  logs,
  created_at,
  started_at,
  finished_at,
  manifest
FROM rvbbit.warren_jobs
ORDER BY created_at DESC
LIMIT 200;
```

Job kinds:

| Kind | Current behavior |
|---|---|
| `capability` | Implemented. Deploys a capability manifest sidecar. |
| `trained_model` | Implemented through the same manifest-shaped path. |
| `mcp_server` | Catalog shape reserved. |
| `compose` | Catalog shape reserved. |
| `custom` | Catalog shape reserved. |

Desired states:

| Desired state | Meaning |
|---|---|
| `running` | Deploy or keep service running. |
| `stopped` | Reserved for future stop workflow. |
| `removed` | Reserved for future teardown workflow. |

Job statuses:

| Status | Meaning |
|---|---|
| `queued` | Waiting for a matching Warren node. |
| `running` | Claimed by a Warren node. |
| `completed` | Warren finished and registered the deployment. |
| `failed` | Warren failed the job and wrote `error`/`logs`. |
| `cancelled` | Reserved for future cancellation. |

Recommended job filters:

- Status: `queued`, `running`, `failed`, `completed`.
- Kind: `capability`, `trained_model`, future kinds.
- Node: `claimed_by`.
- Target selector JSON contains a key/value, for example `gpu = true`.

Useful queue summary:

```sql
SELECT status, kind, count(*) AS jobs
FROM rvbbit.warren_jobs
GROUP BY status, kind
ORDER BY status, kind;
```

The UI may show queued jobs that have no matching node with this diagnostic:

```sql
SELECT j.job_id, j.name, j.target_selector
FROM rvbbit.warren_jobs j
WHERE j.status = 'queued'
  AND NOT EXISTS (
    SELECT 1
    FROM rvbbit.warren_nodes n
    WHERE n.status IN ('ready', 'busy')
      AND n.labels @> j.target_selector
  )
ORDER BY j.created_at;
```

## Deployments

Use `rvbbit.warren_deployments` for deployment history and details. This table
is the materialized result of completed or failed Warren jobs.

```sql
SELECT
  deployment_id,
  job_id,
  node_id,
  node_name,
  kind,
  name,
  status,
  endpoint_url,
  backend_name,
  operator_name,
  manifest,
  compose_project,
  work_dir,
  health,
  error,
  created_at,
  updated_at,
  stopped_at
FROM rvbbit.warren_deployments
ORDER BY updated_at DESC;
```

Deployment statuses:

| Status | Meaning |
|---|---|
| `starting` | Reserved for deployments not yet fully registered. |
| `running` | Registered and expected to serve traffic. |
| `stopped` | Reserved for future stopped state. |
| `failed` | Deployment job failed or final probe failed. |
| `removed` | Reserved for future teardown state. |

For active runtime health, join to `rvbbit.backend_health` on
`backend_name`. `warren_deployments.status = 'running'` means Warren completed
the deployment workflow. It does not replace backend probes.

```sql
SELECT
  d.deployment_id,
  d.node_name,
  d.name AS deployment_name,
  d.status AS deployment_status,
  d.backend_name,
  h.n_calls,
  h.n_errors,
  h.avg_latency_ms,
  h.p95_latency_ms,
  h.last_call_at
FROM rvbbit.warren_deployments d
LEFT JOIN rvbbit.backend_health h
  ON h.name = d.backend_name
ORDER BY d.updated_at DESC;
```

The `health` JSON can include agent-written details such as sidecar health and
backend probe output. Treat it as arbitrary JSON and render it in an expandable
details panel.

## Metrics

Latest metrics per node:

```sql
SELECT
  node_name,
  collected_at,
  cpu_pct,
  load1,
  load5,
  load15,
  mem_used_bytes,
  mem_total_bytes,
  gpu_count,
  gpu_util_pct,
  gpu_mem_used_bytes,
  gpu_mem_total_bytes,
  metrics
FROM rvbbit.warren_node_latest_metrics
ORDER BY node_name;
```

Historical metrics for charts:

```sql
SELECT
  collected_at,
  cpu_pct,
  load1,
  mem_used_bytes,
  mem_total_bytes,
  gpu_util_pct,
  gpu_mem_used_bytes,
  gpu_mem_total_bytes
FROM rvbbit.warren_node_metrics
WHERE node_name = $1
ORDER BY collected_at DESC
LIMIT 1000;
```

Typed metric columns:

| Column | Meaning |
|---|---|
| `cpu_pct` | CPU utilization from `/proc/stat`; first sample may be null. |
| `load1`, `load5`, `load15` | Load averages from `/proc/loadavg`. |
| `mem_used_bytes`, `mem_total_bytes` | Memory use from `/proc/meminfo`. |
| `gpu_count` | Number of GPUs reported by `nvidia-smi`. |
| `gpu_util_pct` | Aggregate/representative GPU utilization. |
| `gpu_mem_used_bytes`, `gpu_mem_total_bytes` | GPU memory summary. |
| `metrics` | Full raw JSON payload. |

Metric writes are best-effort. A missing or stale metric row should not be
interpreted as proof that the agent is down; use heartbeat age and metrics age
together.

Metric retention is controlled manually:

```sql
SELECT rvbbit.prune_warren_metrics('7 days'::interval);
```

## Capability Deployment Action

The UI should use the curated capability catalog for browsing, then queue the
selected manifest with `rvbbit.deploy_capability`.

Catalog sources:

| Source | Purpose |
|---|---|
| `capabilities/catalog.json` | Fast browse/search list. |
| `capabilities/manifests/**/*.yaml` | Full manifest shown on detail/deploy screen. |

Recommended deploy flow:

1. User picks a capability from `capabilities/catalog.json`.
2. UI loads the full manifest YAML from `manifest_path`.
3. UI offers target selector controls based on known Warren labels.
4. UI renders or sends the manifest JSON to SQL.
5. SQL queues the job with `rvbbit.deploy_capability(...)`.
6. UI navigates to the job detail row and watches status.

SQL shape:

```sql
SELECT rvbbit.deploy_capability(
  capability_manifest => $manifest$ { ... } $manifest$::jsonb,
  target_selector => '{"gpu":true}'::jsonb,
  job_name => NULL
);
```

CLI equivalent:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/manifests/smoke/warren-echo.yaml \
  --dsn "$RVBBIT_DSN" \
  --target '{"gpu":false}'
```

The `smoke/warren-echo.yaml` capability is the safest first UI test because it
does not download a model.

## Placement UI

The scheduler's v0 matching rule is simple and predictable:

```sql
warren_nodes.labels @> warren_jobs.target_selector
```

This means a node with labels:

```json
{"capability": true, "docker": true, "gpu": true, "cuda": true, "region": "lab"}
```

matches a target selector:

```json
{"gpu": true}
```

and:

```json
{"gpu": true, "region": "lab"}
```

but not:

```json
{"gpu": false}
```

The UI can build target selector controls from the union of observed node
labels:

```sql
SELECT key, jsonb_agg(DISTINCT value) AS observed_values
FROM rvbbit.warren_nodes n
CROSS JOIN LATERAL jsonb_each(n.labels)
GROUP BY key
ORDER BY key;
```

Recommended default selectors:

| Capability preference | Selector |
|---|---|
| CPU/no model/smoke | `{"gpu": false}` or `{}` |
| GPU preferred | `{"gpu": true}` |
| CUDA required | `{"gpu": true, "cuda": true}` |
| Region/host pool | Add custom labels such as `{"region":"lab"}`. |

If a selector is empty (`{}`), any ready/busy node can claim the job.

## Backend And Operator Links

A completed Warren deployment usually registers:

- `backend_name`: row in `rvbbit.backends` and `rvbbit.backend_health`.
- `operator_name`: SQL operator created by `rvbbit.create_operator(...)`.

Useful checks:

```sql
SELECT *
FROM rvbbit.backend_health
WHERE name = $1;

SELECT jsonb_pretty(rvbbit.backend_probe($1));
```

The UI should show backend health next to the deployment but keep the concepts
separate:

| Concept | Source | Meaning |
|---|---|---|
| Job status | `rvbbit.warren_jobs.status` | Whether Warren completed the requested action. |
| Deployment status | `rvbbit.warren_deployments.status` | Warren's remembered service state. |
| Backend health | `rvbbit.backend_health` and `rvbbit.backend_probe(...)` | Whether Rvbbit can call the backend successfully. |

## Suggested UI Actions

Read-only actions are safe for v0:

- Browse nodes and deployments.
- Filter jobs by status, kind, selector, and node.
- Chart node metrics.
- View job manifest, logs, error, and deployment health JSON.
- Probe a deployed backend with `rvbbit.backend_probe(...)`.

Write actions available in v0:

- Queue a capability deployment with `rvbbit.deploy_capability(...)`.
- Register/update a node manually with `rvbbit.register_warren_node(...)`.
- Prune old metrics with `rvbbit.prune_warren_metrics(...)`.

Avoid adding destructive UI actions until the stop/remove job semantics are
implemented. The catalog already reserves `desired_state = 'stopped'` and
`desired_state = 'removed'`, but the current agent path is focused on starting
services.

## Error Surfaces

Show these fields prominently when present:

| Field | Source | Meaning |
|---|---|---|
| `warren_jobs.error` | Job row | Deployment failure summary. |
| `warren_jobs.logs` | Job row | Agent-provided diagnostics JSON. |
| `warren_deployments.error` | Deployment row | Last deployment-level error. |
| `warren_deployments.health` | Deployment row | Sidecar health/probe details or failure details. |
| `backend_health.n_errors` | Backend health | Runtime invocation errors after registration. |

Common failure categories:

| Symptom | Likely cause |
|---|---|
| Job remains `queued` | No node labels match the target selector, or no agent is polling. |
| Job becomes `failed` before deployment | Docker build/run/scaffold error. |
| Deployment `failed` with probe data | Sidecar started but Rvbbit backend probe failed. |
| Backend probe fails after deployment | Endpoint URL/network changed, sidecar exited, or operator/backend config mismatch. |
| Metrics missing | `WARREN_METRICS_MS=0`, agent cannot write metrics, or metrics collector unavailable. |

## Smoke Test

Use this sequence to validate a UI flow without downloading a model:

```bash
make warren-agent
```

Then queue:

```bash
make capability-deploy \
  MANIFEST=capabilities/manifests/smoke/warren-echo.yaml \
  TARGET='{"gpu":false}'
```

Or queue from SQL with the manifest JSON:

```sql
SELECT rvbbit.deploy_capability(
  capability_manifest => '<warren-echo manifest json>'::jsonb,
  target_selector => '{"gpu":false}'::jsonb
);
```

Expected UI sequence:

1. New row appears in `rvbbit.warren_jobs` with `status = 'queued'`.
2. Agent claims it and row changes to `status = 'running'`.
3. Job finishes with `status = 'completed'`.
4. `rvbbit.warren_deployments` has a `running` row.
5. `rvbbit.warren_inventory` shows the deployment under the node.
6. `rvbbit.backend_probe('warren_smoke_echo')` returns `{"ok": true, ...}`.
7. `SELECT rvbbit.warren_smoke_echo('hello')->>'echo';` returns `hello`.

## Version Notes

This contract targets Rvbbit extension version `0.47.0` and the Rust
`warren-agent` introduced with that version. Future scheduling policies should
preserve the current SQL surfaces where possible and extend JSON fields rather
than replacing table shapes.
