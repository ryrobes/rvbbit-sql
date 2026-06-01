# Warren UI Contract

This document is the v0 contract for building a Warren dashboard or adding
Warren panels to the Rvbbit UI. It is written for UI builders who should not
need to inspect extension SQL to understand the available state.

Warren is an optional deployment scheduler for capability sidecars. Postgres is
the control plane. A `warren-agent` process runs on a host with Docker and
optional CPU/GPU resources, polls Postgres for jobs, deploys sidecars, then
registers resulting Rvbbit backends/operators or runtime endpoints.

## Core Screens

A useful first UI should expose six views:

| View | Primary source | Purpose |
|---|---|---|
| Inventory | `rvbbit.warren_inventory` | Node list with latest metrics and active deployments. |
| Jobs | `rvbbit.warren_jobs` | Deployment queue, running jobs, failures, and history. |
| Deployments | `rvbbit.warren_deployments` | Sidecars currently known to Warren, linked to backend/operator/runtime names. |
| Metrics | `rvbbit.warren_node_latest_metrics`, `rvbbit.warren_node_metrics` | CPU, memory, disk, and GPU observability. |
| Execution runtimes | `rvbbit.python_runtimes`, `rvbbit.python_envs`, `rvbbit.mcp_gateways` | Runtime endpoints for workflow node kinds such as `kind: python` and `kind: mcp`. |
| Capability deploy | `rvbbit.capability_catalog`, `rvbbit.deploy_catalog_capability(...)` | Queue new deployments from curated capability packs. |

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
  runtime_name,
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
  phase,
  claimed_by,
  claimed_at,
  attempts,
  endpoint_url,
  backend_name,
  operator_name,
  runtime_name,
  error,
  progress,
  logs,
  created_at,
  updated_at,
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

Job phases:

`phase` is the UI-facing install progress label within the broader job status.
Treat it as an extensible text value. Current Warren agents use:

| Phase | Meaning |
|---|---|
| `queued` | Job is waiting for a matching node. |
| `claimed` | A Warren node has claimed the job. |
| `preparing` | The agent is preparing the local deployment workspace. |
| `scaffolding` | The agent is writing compose/template artifacts. |
| `starting` | Docker image pull/build/run has started. |
| `waiting_health` | Container or published health probe is being waited on. |
| `registering_backend` | Backend/operator SQL registration is in progress. |
| `registering_runtime` | Runtime catalog registration is in progress. |
| `probing_backend` | SQL backend probe is running. |
| `probing_runtime` | Runtime-specific probe is running. |
| `ready` | Warren completed the deployment. |
| `failed` | Warren failed the job. |

`progress` is structured JSON for the current or latest phase. Render it in an
expandable detail panel; do not assume every phase includes the same keys.

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
  runtime_name,
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

For model backend health, join to `rvbbit.backend_health` on `backend_name`.
For execution runtime health, join to the runtime catalog, currently
`rvbbit.python_runtimes` on `runtime_name`. `warren_deployments.status =
'running'` means Warren completed the deployment workflow. It does not replace
backend probes or runtime health checks.

```sql
SELECT
  d.deployment_id,
  d.node_name,
  d.name AS deployment_name,
  d.status AS deployment_status,
  d.backend_name,
  d.runtime_name,
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

Runtime deployment state:

```sql
SELECT
  d.deployment_id,
  d.node_name,
  d.name AS deployment_name,
  d.status AS deployment_status,
  d.runtime_name,
  r.endpoint_url,
  r.status AS runtime_status,
  r.labels,
  r.health,
  r.updated_at AS runtime_updated_at
FROM rvbbit.warren_deployments d
LEFT JOIN rvbbit.python_runtimes r
  ON r.name = d.runtime_name
WHERE d.runtime_name IS NOT NULL
ORDER BY d.updated_at DESC;
```

The `health` JSON can include agent-written details such as sidecar health and
backend probe output. Treat it as arbitrary JSON and render it in an expandable
details panel.

## Python Runtimes

Use `rvbbit.python_runtimes` for registered execution endpoints that can run
`kind: python` operator nodes. A Warren-deployed Python runtime appears here
with `runtime_source = 'warren'`. The local shortcut is also Warren-driven: it
queues the built-in catalog item, runs one Warren claim, and registers
`python_default` from the materialized sidecar:

```bash
make python-runtime-up
```

```sql
SELECT
  name,
  endpoint_url,
  language,
  status,
  labels,
  runtime_source,
  install_manifest,
  health,
  created_at,
  updated_at
FROM rvbbit.python_runtimes
ORDER BY name;
```

Use `rvbbit.python_envs` for SQL-managed package environments. When
`runtime_name` is set, the effective endpoint is resolved from
`rvbbit.python_runtimes` at call time; `python_envs.endpoint_url` is only for
direct endpoint overrides.

```sql
SELECT
  e.name,
  e.runtime_name,
  coalesce(r.endpoint_url, e.endpoint_url) AS effective_endpoint_url,
  e.python_version,
  e.requirements,
  e.env_hash,
  e.status,
  e.timeout_ms,
  e.updated_at
FROM rvbbit.python_envs e
LEFT JOIN rvbbit.python_runtimes r
  ON r.name = e.runtime_name
ORDER BY e.name;
```

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

The UI should use the SQL capability catalog for browsing, then queue the
selected row with `rvbbit.deploy_catalog_capability`.

### What Changes From The JSON Catalog

The old UI path treated `capabilities/catalog.json` as the browse source, loaded
the manifest YAML from disk, then sent the full manifest JSON to
`rvbbit.deploy_capability(...)`. The table-backed catalog changes that contract:

| Area | Old Contract | New Contract |
|---|---|---|
| Browse source | `capabilities/catalog.json` from repo files. | `rvbbit.capability_catalog` from SQL. |
| Detail source | Load `manifest_path` from local disk. | Read `manifest` and `catalog_entry` JSONB from the selected SQL row. |
| Deploy action | UI sends full manifest JSON to `rvbbit.deploy_capability(...)`. | UI sends only `catalog_id` to `rvbbit.deploy_catalog_capability(...)`. |
| Install intent | UI infers intent after Warren claims or completes a job. | Queued jobs are stamped with known `backend_name`, `runtime_name`, and first `operator_name`. |
| Refresh | Rebuild local JSON file. | Fresh installs are seeded; publish manifest changes into SQL with `catalog publish` or `make capability-catalog-db`. |
| File access | UI needs repo file access for catalog details. | UI can browse and deploy through database access only. |

For UI code, this means the catalog should become a normal database-backed
resource. Do not require the browser, API server, or desktop UI to read files
from `capabilities/` for the default Warren deployment flow.

Catalog sources:

| Source | Purpose |
|---|---|
| `rvbbit.capability_catalog` | Primary UI browse/search/deploy source. |
| `capabilities/catalog.json` | Build artifact used by the CLI; optional fallback only. |
| `capabilities/packs/**/rvbbit-pack.yaml` | Root pack metadata used by catalog publishing tools and future marketplace sync. |
| `capabilities/packs/**/capability.yaml` | Full deployable Warren manifest stored in the SQL catalog. |

Fresh extension installs seed the SQL catalog from the bundled canonical seed.
Publish or refresh the SQL catalog from the repo after manifest changes with:

```bash
capabilities/tools/rvbbit-capability catalog publish \
  --dsn "$RVBBIT_DSN" \
  --prune
```

Browse query:

```sql
SELECT
  id,
  name,
  title,
  description,
  tags,
  kind,
  system_runtime,
  capability_role,
  source_provider,
  source_model,
  coalesce(catalog_entry->>'catalog_visibility', 'public') AS catalog_visibility,
  catalog_entry->>'pack_path' AS pack_path,
  catalog_entry->>'runtime_mode' AS runtime_mode,
  catalog_entry->'acceptance_tests' AS acceptance_tests,
  catalog_entry->'acceptance' AS acceptance,
  manifest #>> '{runtime,image}' AS runtime_image,
  backend_name,
  runtime_name,
  runtime_language,
  runtime_template,
  runtime_handler,
  runtime_port,
  health_path,
  endpoint_path,
  device,
  operators,
  active,
  updated_at
FROM rvbbit.capability_catalog
WHERE active
  AND coalesce(catalog_entry->>'catalog_visibility', 'public') = 'public'
ORDER BY title;
```

Recommended browse query with install state:

```sql
SELECT
  c.id,
  c.name,
  c.title,
  c.description,
  c.tags,
  c.kind,
  c.system_runtime,
  c.capability_role,
  c.source_provider,
  c.source_model,
  coalesce(c.catalog_entry->>'catalog_visibility', 'public') AS catalog_visibility,
  c.catalog_entry->>'pack_path' AS pack_path,
  c.catalog_entry->>'runtime_mode' AS runtime_mode,
  c.catalog_entry->'acceptance_tests' AS acceptance_tests,
  c.catalog_entry->'acceptance' AS acceptance,
  c.manifest #>> '{runtime,image}' AS runtime_image,
  c.backend_name,
  c.runtime_name,
  c.runtime_language,
  c.runtime_template,
  c.runtime_handler,
  c.runtime_port,
  c.health_path,
  c.endpoint_path,
  c.device,
  c.operators,
  c.active,
  c.updated_at,
  b.name IS NOT NULL AS backend_registered,
  b.n_calls,
  b.n_errors,
  b.avg_latency_ms,
  coalesce(r.name, m.name) IS NOT NULL AS runtime_registered,
  coalesce(r.status, m.status) AS runtime_status,
  m.name IS NOT NULL AS mcp_gateway_registered,
  m.status AS mcp_gateway_status
FROM rvbbit.capability_catalog c
LEFT JOIN rvbbit.backend_health b
  ON b.name = c.backend_name
LEFT JOIN rvbbit.python_runtimes r
  ON r.name = c.runtime_name
LEFT JOIN rvbbit.mcp_gateways m
  ON m.name = c.runtime_name
WHERE c.active
  AND coalesce(c.catalog_entry->>'catalog_visibility', 'public') = 'public'
ORDER BY c.title;
```

Use `kind = 'runtime_sidecar'` for runtime capability cards. Rows with
`system_runtime = true` and `capability_role = 'operator_runtime'` are broader
workflow runtimes, currently CPython and MCP Gateway. Use `kind = 'hf_backend'`
for model/backend cards. Runtime-sidecar rows normally have
`backend_name IS NULL`; backend rows normally have `runtime_name IS NULL`.

Detail panels should read these fields from the selected row:

| Field | UI Use |
|---|---|
| `manifest` | Exact Warren deploy payload used by `deploy_catalog_capability`. Show as advanced JSON detail. |
| `catalog_entry` | Generated browse entry from the CLI. Useful for compatibility with old card rendering. |
| `catalog_entry.pack_path` / `catalog_entry.pack_manifest_path` | Source pack location for inspect/provenance views. |
| `manifest_path` | Provenance/debug only. Do not require the UI to load this file. |
| `catalog_entry.acceptance_tests` | Compact list of named pack acceptance tests for badges and search. |
| `catalog_entry.acceptance` | Runnable acceptance test contract: optional `target_selector`, `setup_sql[]`, `tests[{name, description, sql}]`, and `teardown_sql[]`. UIs may execute this SQL after Warren deploys a pack. |
| `catalog_entry.catalog_visibility` | `public`, `example`, or `internal`; default browse views should show `public` and hide examples/internal smoke packs unless the user asks for them. |
| `tags`, `kind`, `system_runtime`, `capability_role`, `device`, `runtime_*`, `manifest.runtime.image`, `catalog_entry.runtime_mode` | Filters and deployment badges. |
| `backend_name`, `runtime_name`, `operators` | Install-state joins and post-deploy navigation. `operators` includes raw wrappers plus bundled high-level child operators. |

For V1 built-ins, both runtime sidecars use `catalog_entry.runtime_mode =
'build'` and have no `manifest.runtime.image`. The UI should render that as a
normal Warren install path, not as missing metadata; Warren builds from its
trusted local templates.

For model capability rows, prefer rendering `operators` as the installed user
surface. A single Warren capability may install several SQL operators: a
reranker can provide `about`/`means`, an embedding model can provide
`semantic_embed`/`similar_to`, and an extractor can provide both raw JSON
entity output and workflow-friendly predicates. Use `manifest->'operators'`
for signatures, return types, infix metadata, and multi-step detail views.

Recommended deploy flow:

1. User picks a capability from `rvbbit.capability_catalog`.
2. UI displays metadata columns and the optional `manifest` JSON detail panel.
3. UI offers target selector controls based on known Warren labels.
4. SQL queues the job with `rvbbit.deploy_catalog_capability(...)`.
5. UI navigates to the job detail row and watches status.

SQL shape:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'runtimes/python-runtime',
  target_selector => '{"docker":true}'::jsonb,
  job_name => NULL
);
```

`rvbbit.deploy_capability(manifest, target_selector, job_name)` remains the
lower-level escape hatch for ad hoc manifests.

Catalog deployments stamp the queued `rvbbit.warren_jobs` row with known
catalog metadata (`backend_name`, `runtime_name`, and first `operator_name`)
before a Warren claims the job.

The deploy function returns a `job_id`. The UI should immediately navigate to
or subscribe to that job:

```sql
SELECT *
FROM rvbbit.warren_jobs
WHERE job_id = '<returned job id>'::uuid;
```

Publishing and pruning the catalog is an administrative operation in this
release. The UI may expose a "refresh catalog" action for trusted operators, but
it should call the CLI/ops path rather than attempting to mutate rows as a
normal application user. An empty catalog after extension install should be
treated as an operational problem. If the table is missing or empty, the UI may
fall back to `capabilities/catalog.json` as a read-only browse source, but
deploy buttons should be disabled or routed through the advanced ad hoc manifest
flow.

Manifest deploy CLI equivalent for ad hoc/manual testing:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/smoke/warren-echo \
  --dsn "$RVBBIT_DSN" \
  --target '{"gpu":false}'
```

The `smoke/warren-echo` capability is the safest first model-backend UI
test because it does not download a model. The
`runtimes/python-runtime` capability is the first runtime-sidecar UI test;
it should complete with `backend_name IS NULL` and `runtime_name =
'python_default'`. For local validation, `make python-runtime-up` exercises the
same Warren placement path against the built-in catalog item.

## MCP Gateway Runtime

The MCP Gateway is no longer an assumed always-on service. It is a
Warren-installable system runtime capability with catalog id
`runtimes/mcp-gateway`, similar to the CPython runtime and model sidecars.
Treat it as a prerequisite runtime for the MCP UI, SQL MCP calls, and
`kind: mcp` operator nodes.

In the built-in catalog this row is source-buildable (`runtime_mode = 'build'`)
rather than an image pull. Show `runtime_image` as optional/blank.

Recommended UI gate:

```sql
WITH gateway AS (
  SELECT name, endpoint_url, status, gateway_source, health, updated_at
  FROM rvbbit.mcp_gateways
  ORDER BY (status = 'ready') DESC, (name = 'mcp_default') DESC, updated_at DESC
  LIMIT 1
),
catalog AS (
  SELECT id
  FROM rvbbit.capability_catalog
  WHERE id = 'runtimes/mcp-gateway' AND active
  LIMIT 1
)
SELECT
  coalesce(g.name IS NOT NULL, false) AS installed,
  coalesce(g.status = 'ready', false) AS ready,
  g.name,
  g.endpoint_url,
  g.status,
  g.gateway_source,
  g.health,
  g.updated_at,
  c.id AS catalog_id
FROM (SELECT 1) x
LEFT JOIN gateway g ON true
LEFT JOIN catalog c ON true;
```

Recommended behavior:

| UI surface | Behavior when no ready gateway |
|---|---|
| Capability browser | Show `runtimes/mcp-gateway` as a system runtime and allow normal Warren deployment through `rvbbit.deploy_catalog_capability(...)`. |
| MCP servers list | Show the gateway prerequisite panel and deep-link to the MCP Gateway capability install tab. Do not imply the gateway is bundled with the database. |
| MCP server detail | Keep catalog/audit/cache reads available, but disable `refresh_mcp_server`, `mcp_probe`, `mcp_call`, and `mcp_resource` actions. |
| Operator editor | Allow `kind: mcp` nodes to be inspected, but show a gateway warning and an install/open-runtime action before users run the operator. |

Registering rows in `rvbbit.mcp_servers` is SQL catalog DDL and can be
saved before the gateway exists. Discovery and execution are active operations
and require a ready gateway. In practical UI flows, prefer guiding users to
install the gateway first because tool discovery depends on it.

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

## Backend, Operator, And Runtime Links

A completed Warren deployment registers either model backend/operator plumbing
or an execution runtime:

- `backend_name`: row in `rvbbit.backends` and `rvbbit.backend_health`.
- `operator_name`: SQL operator created by `rvbbit.create_operator(...)`.
- `runtime_name`: runtime row such as `rvbbit.python_runtimes.name`.

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
| Runtime health | `rvbbit.python_runtimes` / `rvbbit.mcp_gateways` status and `health` | Whether an execution runtime endpoint is registered and ready. |

For a runtime deployment, `backend_name` and `operator_name` are expected to be
null. That is not an error; `runtime_name` is the capability handle used by
SQL-managed Python envs.

## Suggested UI Actions

Read-only actions are safe for v0:

- Browse nodes and deployments.
- Filter jobs by status, kind, selector, and node.
- Chart node metrics.
- View job manifest, logs, error, and deployment health JSON.
- Probe a deployed backend with `rvbbit.backend_probe(...)`.
- Inspect registered Python runtimes and their envs from
  `rvbbit.python_runtimes` and `rvbbit.python_envs`.
- Inspect registered MCP gateway runtimes from `rvbbit.mcp_gateways` and gate
  MCP active operations until a row is `status = 'ready'`.

Write actions available in v0:

- Queue a catalog deployment with `rvbbit.deploy_catalog_capability(...)`.
- Queue an ad hoc manifest deployment with `rvbbit.deploy_capability(...)`.
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
| `warren_jobs.phase` | Job row | Current install progress label. |
| `warren_jobs.progress` | Job row | Structured phase details such as port, container, endpoint, node, or error. |
| `warren_jobs.logs` | Job row | Agent-provided diagnostics JSON. |
| `warren_deployments.error` | Deployment row | Last deployment-level error. |
| `warren_deployments.health` | Deployment row | Sidecar health/probe details or failure details. |
| `backend_health.n_errors` | Backend health | Runtime invocation errors after registration. |
| `python_runtimes.status` | Python runtime row | Runtime registration state. |
| `python_runtimes.health` | Python runtime row | Warren/runtime probe details. |
| `mcp_gateways.status` | MCP gateway runtime row | Runtime registration state for SQL MCP calls and `kind: mcp` nodes. |
| `mcp_gateways.health` | MCP gateway runtime row | Warren/runtime probe details. |

Common failure categories:

| Symptom | Likely cause |
|---|---|
| Job remains `queued` | No node labels match the target selector, or no agent is polling. |
| Job becomes `failed` before deployment | Docker build/run/scaffold error. |
| Deployment `failed` with probe data | Sidecar started but backend/runtime probe failed. |
| Backend probe fails after deployment | Endpoint URL/network changed, sidecar exited, or operator/backend config mismatch. |
| Python runtime row missing | Runtime sidecar job failed before `rvbbit.register_python_runtime(...)`, or the extension version is too old. |
| MCP gateway row missing | Runtime sidecar job failed before `rvbbit.register_mcp_gateway(...)`, or the extension version is too old. |
| Metrics missing | `WARREN_METRICS_MS=0`, agent cannot write metrics, or metrics collector unavailable. |

## Smoke Test

Use this sequence to validate a UI flow without downloading a model:

```bash
make warren-agent
```

Then queue:

```bash
make capability-deploy \
  MANIFEST=capabilities/packs/smoke/warren-echo \
  TARGET='{"gpu":false}'
```

Or queue from SQL with the manifest JSON:

```sql
SELECT rvbbit.deploy_capability(
  capability_manifest => '<warren-echo manifest json>'::jsonb,
  target_selector => '{"gpu":false}'::jsonb
);
```

Or queue directly from the SQL catalog:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'smoke/warren-echo',
  target_selector => '{"gpu":false}'::jsonb
);
```

For the MCP runtime prerequisite, use the same catalog path:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'runtimes/mcp-gateway',
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

Use this sequence to validate the Python runtime sidecar path:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/runtimes/python-runtime \
  --dsn "$RVBBIT_DSN" \
  --target '{"docker":true}'
```

For the local preconfigured shortcut:

```bash
make python-runtime-up
```

Expected runtime sequence:

1. Job follows the same queued/running/completed flow.
2. `rvbbit.warren_deployments.runtime_name = 'python_default'`.
3. `rvbbit.python_runtimes` has a ready row named `python_default`.
4. `runtime_source = 'warren'`.
5. Python env creation can name `runtime_name => 'python_default'`.

## Version Notes

This contract targets Rvbbit extension version `1.0.0` and the Rust
`warren-agent`. Future scheduling policies should preserve the current SQL
surfaces where possible and extend JSON fields rather than replacing table
shapes.
