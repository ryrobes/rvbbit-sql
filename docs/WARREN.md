# Warren Remote Capability Agents

Warren is Rvbbit's optional deployment control plane for model, runtime, and
tool sidecars. The database remains the source of truth. A Warren agent runs on
any host with the right resources, polls Postgres for deployment jobs, starts
the requested service, then registers the resulting endpoint back into Rvbbit.

The first agent is Rust (`warren-agent`). Its steady-state job is small:
communicate with Postgres, build or pull Docker sidecars, probe deployed
services, and report endpoint/backend/runtime state back to the database.

## Shape

- Rvbbit Postgres stores nodes, jobs, and deployments in SQL catalog tables.
- Warren agents register themselves with labels such as `{"gpu": true}`.
- SQL queues a deployment job with a target selector.
- A matching Warren claims the job using `FOR UPDATE SKIP LOCKED`.
- GPU-targeted jobs are admitted only when the node has enough unreserved VRAM
  for the capability's `resources.gpu.vram_required_bytes` reservation.
- The Warren builds from trusted local templates for bundled V1 packs, or
  pulls/runs a declared sidecar image when `runtime.image` is present.
- Model capabilities call `rvbbit.register_backend(...)`,
  `rvbbit.create_operator(...)`, and `rvbbit.reload_backends()`.
- Runtime capabilities call runtime-specific registration functions such as
  `rvbbit.register_python_runtime(...)` and
  `rvbbit.register_mcp_gateway(...)`.
- Rvbbit query execution keeps using the normal backend/operator or runtime
  node machinery.

This keeps routing simple: SQL asks for a capability; Warren decides where it
runs; Rvbbit stores only the backend/operator or runtime catalog rows it
already knows how to execute.

Built-in catalog entries should follow the same a-la-carte path as external
capabilities. The shared `docker/docker-compose.sidecars.yml` file is a local
development harness for hand-run services, not the deployment contract. For
curated capabilities, the contract is: a catalog row identifies one pack,
Warren materializes one generated project under its work directory, Docker
pulls a declared image or builds the declared runtime fallback, and the agent
writes the bootstrapped backend/operator or runtime rows back to SQL.

Built-ins are stored like standalone repositories under
`capabilities/packs/<category>/<pack>/`:

- `rvbbit-pack.yaml`: root metadata for fast catalog/marketplace queries.
- `capability.yaml`: the deployable Warren manifest.
- Optional local source/runtime files for packs that build instead of pulling a
  prebuilt image.

System runtime packs set `system_runtime: true` and
`capability_role: operator_runtime` in `rvbbit-pack.yaml`. These are not model
specialists; they unlock workflow node kinds or SQL primitives. The current
built-ins are `runtimes/python-runtime` for `kind: python` operator nodes and
`runtimes/mcp-gateway` for SQL MCP calls plus `kind: mcp` operator nodes.

External catalogs should use the same shape. A Warren-installable pack can be
published as a Git repository, tarball, or other fetched artifact as long as the
root metadata points at a deployable `capability.yaml`.

## Catalog

Core tables and view:

- `rvbbit.warren_nodes`: registered agent hosts, labels, capacity, heartbeat,
  inventory, and future auth metadata.
- `rvbbit.warren_jobs`: queued/running/completed deployment requests, plus
  `phase` and `progress` for UI-visible install progress.
- `rvbbit.warren_deployments`: materialized deployment records tied to nodes
  and backend/operator/runtime names.
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

SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'runtimes/python-runtime',
  target_selector => '{"docker":true}'::jsonb
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

GPU capacity is exposed through:

```sql
SELECT
  node_name,
  gpu_names,
  gpu_mem_usable_bytes,
  gpu_provisioned_bytes,
  gpu_available_bytes
FROM rvbbit.warren_gpu_capacity;
```

The V1 scheduler uses a conservative VRAM yardstick. The agent reports GPU
inventory from `nvidia-smi`; the database treats roughly 90% of each node's GPU
memory as usable unless `warren_nodes.capacity.gpu.vram_usable_ratio` overrides
it. Active deployments reserve their declared VRAM. For now the fit check is
single-GPU conservative; multi-GPU packing can become more precise without
changing the catalog-facing resource fields.

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
  MANIFEST=capabilities/packs/smoke/warren-echo \
  TARGET='{"gpu":false}'
```

The same deploy step can be called directly:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/smoke/warren-echo \
  --dsn "$RVBBIT_DSN" \
  --target '{"gpu":false}'
```

Fresh extension installs seed the curated catalog into SQL so the UI can browse
without reading repository files. Refresh it after manifest changes with:

```bash
make capability-catalog-db
```

Then queue by catalog id:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'smoke/warren-echo',
  target_selector => '{"gpu":false}'::jsonb
);
```

To run the pack's own Warren acceptance SQL, use:

```bash
make capability-test MANIFEST=capabilities/packs/smoke/warren-echo
```

`capabilities/packs/smoke/warren-echo` is the recommended first test.
It uses the existing FastAPI sidecar template with an `echo` handler and
`python:3.12-slim`, so it validates Warren, Docker, backend registration,
probing, and operator wiring without downloading a model.

After Warren completes the job:

```sql
SELECT * FROM rvbbit.warren_inventory ORDER BY node_name, deployment_name;
SELECT jsonb_pretty(rvbbit.backend_probe('warren_smoke_echo'));
SELECT rvbbit.warren_smoke_echo('hello from SQL')->>'echo';
```

To deploy the managed Python execution runtime instead of a model backend:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/runtimes/python-runtime \
  --dsn "$RVBBIT_DSN" \
  --target '{"docker":true}'
```

For the local stack, the shortcut is still Warren-driven. It starts the core
database, queues the catalog item, runs `warren-agent --once`, and verifies the
registered runtime:

```bash
make python-runtime-up
```

After Warren completes the job, the deployment has `runtime_name =
'python_default'` instead of a backend name:

```sql
SELECT name, endpoint_url, status, runtime_source
FROM rvbbit.python_runtimes
WHERE name = 'python_default';

SELECT deployment_name, runtime_name, endpoint_url
FROM rvbbit.warren_inventory
WHERE runtime_name IS NOT NULL;
```

Users can then define Python workflow envs against that named runtime entirely
from SQL:

```sql
SELECT rvbbit.create_python_env(
  env_name => 'analytics',
  python_version => '3.12',
  requirements => ARRAY['rapidfuzz==3.9.7'],
  runtime_name => 'python_default'
);
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

If `--advertise-base-url` is omitted, Warren registers a Docker-network URL
such as `http://rvbbit-<service>:8080/predict` for model backends or
`http://rvbbit-<service>:8080/run` for runtime sidecars. That is correct when
the generated sidecar and `pg-rvbbit` are on the same Docker network. In this
default mode generated sidecars use Docker `expose` only and do not publish a
host port, so many capabilities can all listen on container port `8080` without
colliding on the Warren host. If Warren runs on a different box, pass the URL
that the Postgres host can reach; in that mode Warren also applies the generated
`compose.host-ports.yaml` overlay and publishes the selected host port.

Useful environment variables mirror the CLI:

- `RVBBIT_DSN`
- `WARREN_NODE`
- `WARREN_WORK_DIR`
- `WARREN_TEMPLATE_DIR`: template root such as `capabilities/templates`, or a
  specific legacy template directory.
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
- `target_selector`: scheduling policy can grow beyond label matching and the
  V1 VRAM reservation check into richer tenant or placement policy without
  changing job shape.

Do not expose Warren agents or generated model containers directly to the
public internet in this prerelease shape.

## UI Contract

A UI should treat Warren as an observable deployment scheduler. The detailed
field-level contract lives in [WARREN_UI_CONTRACT.md](WARREN_UI_CONTRACT.md).

At a high level:

- Show `rvbbit.warren_inventory` grouped by node.
- Show queued/running/failed jobs from `rvbbit.warren_jobs`, using `phase` and
  `progress` for install progress and troubleshooting detail.
- Show latest node telemetry from `rvbbit.warren_node_latest_metrics`.
- Chart node telemetry history from `rvbbit.warren_node_metrics`.
- Surface node label and capacity JSON as filters/placement hints.
- Let users browse `rvbbit.capability_catalog` and queue with
  `rvbbit.deploy_catalog_capability(...)`.
- Let advanced users choose a target selector, for example `{"gpu":true}` or
  `{"region":"lab"}`.
- Link deployed `backend_name` to `rvbbit.backend_health`.
- Link deployed `runtime_name` to runtime catalogs such as
  `rvbbit.python_runtimes`.

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

## Warren-Installable Pack Contract

A pack that can be installed by Warren should keep the root small and
inspectable:

- `rvbbit-pack.yaml` with `api_version: rvbbit.pack/v1`, stable `id`, display
  metadata, `capability: capability.yaml`, source/runtime hints, exported
  backend/runtime/operator names, and install mode.
- `capability.yaml` with `api_version: rvbbit.capability/v1`, `kind`,
  `runtime`, backend/runtime registration, operators, and smoke checks.
- Prebuilt-image packs should set `runtime.image`; production catalogs should
  also pin `runtime.image_digest`.
- Build-mode packs should include or reference the runtime source/template that
  Warren is expected to build.
- `acceptance.tests` may define small SQL blocks for pack-level tests. The
  runner deploys the pack through Warren, runs one Warren claim, then executes
  each SQL block with `ON_ERROR_STOP`; the SQL should raise on failure.
- Runtime sidecars must expose `GET /health`; model backends must expose
  Rvbbit's batch `POST /predict`; Python runtimes must expose `POST /run`.

Sidecars do not need direct database credentials in the normal flow. Warren
talks to Postgres, starts Docker, probes the service, and registers the
resulting endpoint. A custom control-plane integration should use the same SQL
functions Warren uses: claim or enqueue jobs, complete/fail jobs, register
backends or runtimes, and record health/metrics through the Warren tables.
