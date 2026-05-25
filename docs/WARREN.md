# Warren Remote Capability Agents

Warren is Rvbbit's optional deployment control plane for model and tool
sidecars. The database remains the source of truth. A Warren agent runs on any
host with the right resources, polls Postgres for deployment jobs, starts the
requested service, then registers the resulting endpoint back into Rvbbit.

The first agent is Rust (`warren-agent`). Generated Hugging Face serving
containers still use the existing FastAPI template because the Python model
ecosystem is the useful capability there; Warren itself is not a Python control
plane.

## Shape

- Rvbbit Postgres stores nodes, jobs, and deployments in SQL catalog tables.
- Warren agents register themselves with labels such as `{"gpu": true}`.
- SQL queues a deployment job with a target selector.
- A matching Warren claims the job using `FOR UPDATE SKIP LOCKED`.
- The Warren scaffolds/builds/runs the sidecar and calls
  `rvbbit.register_backend(...)`, `rvbbit.create_operator(...)`, and
  `rvbbit.reload_backends()`.
- Rvbbit query execution keeps using the normal backend/operator machinery.

This keeps routing simple: SQL asks for a capability; Warren decides where it
runs; Rvbbit stores only the endpoint and operator definition it already knows
how to execute.

## Catalog

Core tables and view:

- `rvbbit.warren_nodes`: registered agent hosts, labels, capacity, heartbeat,
  inventory, and future auth metadata.
- `rvbbit.warren_jobs`: queued/running/completed deployment requests.
- `rvbbit.warren_deployments`: materialized deployment records tied to nodes
  and backend/operator names.
- `rvbbit.warren_node_metrics`: append-only node telemetry snapshots.
- `rvbbit.warren_node_latest_metrics`: latest telemetry row per node.
- `rvbbit.warren_inventory`: UI-friendly node plus active deployment view.

Primary functions:

```sql
SELECT rvbbit.register_warren_node(
  node_name => 'gpu-1',
  node_base_url => 'http://10.0.0.8',
  node_labels => '{"gpu":true,"cuda":true,"capability":true}'::jsonb,
  node_capacity => '{"vram_gb":24,"slots":2}'::jsonb
);

SELECT rvbbit.deploy_capability(
  capability_manifest => $manifest_json$ { ... capability manifest ... } $manifest_json$::jsonb,
  target_selector => '{"gpu":true}'::jsonb
);

SELECT * FROM rvbbit.warren_inventory ORDER BY node_name, deployment_name;
```

Telemetry is written by the agent through:

```sql
SELECT rvbbit.record_warren_metrics(
  node_name => 'gpu-1',
  metric_doc => '{"system":{"load1":0.5},"summary":{"gpu_count":1}}'::jsonb
);

SELECT rvbbit.prune_warren_metrics('7 days'::interval);
```

Lower-level queue functions are available for custom flows:

- `rvbbit.enqueue_warren_job(kind, name, manifest, target_selector, desired_state)`
- `rvbbit.claim_warren_job(node_name)`
- `rvbbit.complete_warren_job(...)`
- `rvbbit.fail_warren_job(...)`
- `rvbbit.warren_heartbeat(...)`
- `rvbbit.record_warren_metrics(...)`
- `rvbbit.prune_warren_metrics(...)`

## Running An Agent

Local dev stack, using Docker DNS as the registered endpoint:

```bash
make warren-agent
```

The Makefile target expands to `cargo run -p warren-agent` with sensible dev
defaults. Override `WARREN_NODE`, `WARREN_LABELS`, `WARREN_CAPACITY`,
`WARREN_WORK_DIR`, `WARREN_DOCKER_NETWORK`, or `RVBBIT_DSN` as needed.

Queue a capability for the agent:

```bash
make capability-deploy \
  MANIFEST=capabilities/manifests/smoke/warren-echo.yaml \
  TARGET='{"gpu":false}'
```

The same deploy step can be called directly:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/manifests/smoke/warren-echo.yaml \
  --dsn "$RVBBIT_DSN" \
  --target '{"gpu":false}'
```

`capabilities/manifests/smoke/warren-echo.yaml` is the recommended first test.
It uses the existing FastAPI sidecar template with an `echo` handler and
`python:3.12-slim`, so it validates Warren, Docker, backend registration,
probing, and operator wiring without downloading a model.

After Warren completes the job:

```sql
SELECT * FROM rvbbit.warren_inventory ORDER BY node_name, deployment_name;
SELECT jsonb_pretty(rvbbit.backend_probe('warren_smoke_echo'));
SELECT rvbbit.warren_smoke_echo('hello from SQL')->>'echo';
```

Remote GPU host on the same private network:

```bash
cargo run -p warren-agent -- \
  --dsn postgresql://postgres:rvbbit@10.0.0.5:5432/bench \
  --node gpu-1 \
  --advertise-base-url http://10.0.0.8 \
  --labels '{"capability":true,"docker":true,"gpu":true,"cuda":true}' \
  --capacity '{"vram_gb":24,"slots":2}' \
  --work-dir /var/lib/rvbbit/warren
```

If `--advertise-base-url` is omitted, Warren registers
`http://rvbbit-<service>:8080/predict`, which is correct when the generated
sidecar and `pg-rvbbit` are on the same Docker network. If Warren runs on a
different box, pass the URL that the Postgres host can reach.

Useful environment variables mirror the CLI:

- `RVBBIT_DSN`
- `WARREN_NODE`
- `WARREN_WORK_DIR`
- `WARREN_TEMPLATE_DIR`
- `WARREN_ADVERTISE_BASE_URL`
- `RVBBIT_DOCKER_NETWORK`
- `WARREN_POLL_MS`
- `WARREN_METRICS_MS`: telemetry interval in milliseconds; `0` disables
  metrics writes.
- `WARREN_PORT_BASE`
- `WARREN_DRY_RUN`

## Telemetry

`warren-agent` records basic host metrics and opportunistic GPU metrics. The
collector is intentionally dependency-light:

- Linux host metrics come from `/proc/loadavg`, `/proc/stat`, `/proc/meminfo`,
  `/proc/uptime`, and `df -Pk <work_dir>`.
- NVIDIA GPU metrics come from `nvidia-smi` when it is available.
- Missing GPU support is represented as an empty `gpus` array plus
  `gpu_probe.available = false`, not as an agent failure.
- Metrics write failures are non-fatal; Warren logs the error and keeps
  claiming deployment jobs.

The raw JSON is stored in `rvbbit.warren_node_metrics.metrics`. Common fields
are also extracted into typed columns so dashboards do not need to parse JSON
for every chart:

| Column | Source |
|---|---|
| `cpu_pct` | `metrics.system.cpu.usage_pct` |
| `load1`, `load5`, `load15` | `metrics.system.load*` |
| `mem_used_bytes`, `mem_total_bytes` | `metrics.system.memory.*` |
| `gpu_count` | `metrics.summary.gpu_count` |
| `gpu_util_pct` | `metrics.summary.gpu_util_pct` |
| `gpu_mem_used_bytes`, `gpu_mem_total_bytes` | `metrics.summary.gpu_mem_*` |

The first CPU sample may have `cpu_pct = NULL` because CPU utilization is
computed from two `/proc/stat` samples. Subsequent samples include the delta.

Useful dashboard queries:

```sql
SELECT
  node_name,
  collected_at,
  cpu_pct,
  load1,
  mem_used_bytes,
  mem_total_bytes,
  gpu_count,
  gpu_util_pct,
  gpu_mem_used_bytes,
  gpu_mem_total_bytes
FROM rvbbit.warren_node_latest_metrics
ORDER BY node_name;

SELECT
  collected_at,
  cpu_pct,
  gpu_util_pct,
  gpu_mem_used_bytes
FROM rvbbit.warren_node_metrics
WHERE node_name = 'gpu-1'
ORDER BY collected_at DESC
LIMIT 200;
```

## Security Hook Points

The v0 implementation assumes a trusted private network. The catalog already
has room for stronger policies:

- `warren_nodes.shared_key_hash`: shared-secret or API-key verification later.
- `warren_nodes.auth_config`: JSON policy/config surface for future mTLS,
  signed jobs, scoped node tokens, or per-tenant authorization.
- `target_selector`: scheduling policy can grow from label matching to
  resource-aware or tenant-aware matching without changing job shape.

Do not expose Warren agents or generated model containers directly to the
public internet in this prerelease shape.

## UI Contract

A UI should treat Warren as an observable deployment scheduler. The detailed
field-level contract lives in [WARREN_UI_CONTRACT.md](WARREN_UI_CONTRACT.md).

At a high level:

- Show `rvbbit.warren_inventory` grouped by node.
- Show queued/running/failed jobs from `rvbbit.warren_jobs`.
- Show latest node telemetry from `rvbbit.warren_node_latest_metrics`.
- Chart node telemetry history from `rvbbit.warren_node_metrics`.
- Surface node label and capacity JSON as filters/placement hints.
- Let users queue a capability by reading a manifest from the capability
  catalog and calling `rvbbit.deploy_capability(...)`.
- Let advanced users choose a target selector, for example `{"gpu":true}` or
  `{"region":"lab"}`.
- Link deployed `backend_name` to `rvbbit.backend_health`.

Initial job kinds are:

| Kind | Status |
|---|---|
| `capability` | implemented by `warren-agent` |
| `trained_model` | implemented by `warren-agent` using the capability manifest shape |
| `mcp_server` | catalog shape reserved |
| `compose` | catalog shape reserved |
| `custom` | catalog shape reserved |

Initial agent behavior is intentionally conservative: one claimed job at a
time, Docker Compose as the runtime, and label-subset matching for placement.
