# Rvbbit Capabilities

A capability pack is a portable bundle that turns an external service into a
registered Rvbbit capability: usually a model backend plus SQL operators, and
now also execution runtimes such as the managed Python sidecar.

V1 capability packs live in `capabilities/`. Most curated packs focus on
Hugging Face models, but the manifest shape also supports Warren-deployed
runtime sidecars. The directory is intentionally self-contained so it can move
to a separate `rvbbit-capabilities` repository later.

## What A Pack Contains

- `rvbbit-pack.yaml`: root metadata for fast browse, catalog sync, provenance,
  and future marketplace fetches.
- `capability.yaml`: the deployable `rvbbit.capability/v1` manifest with
  runtime, endpoint, batching/registration, and optional operator definitions.
- Optional local runtime source when the pack builds its own image.
- A prebuilt `runtime.image` reference or enough build/template metadata for
  Warren to materialize the sidecar.

The CLI scaffold output is separate from the source pack. It writes generated
files such as `register.sql`, `operator.sql`, `smoke.sql`, `compose.yaml`, and
template-rendered runtime files under the target directory.

## CLI

List curated packs:

```bash
capabilities/tools/rvbbit-capability list
```

Render SQL:

```bash
capabilities/tools/rvbbit-capability render \
  capabilities/packs/extract/gliner-medium-v2.1
```

Scaffold a runnable sidecar:

```bash
capabilities/tools/rvbbit-capability scaffold \
  capabilities/packs/extract/gliner-medium-v2.1 \
  /tmp/rvbbit-gliner
```

Build the JSON catalog artifact:

```bash
capabilities/tools/rvbbit-capability catalog build \
  --output capabilities/catalog.json
```

Build the extension install seed artifact:

```bash
capabilities/tools/rvbbit-capability catalog seed-json \
  --output crates/pg_rvbbit/src/capability_catalog_seed.json
```

Publish the catalog into Postgres for the UI:

```bash
capabilities/tools/rvbbit-capability catalog publish \
  --dsn "$RVBBIT_DSN" \
  --prune
```

Search the curated catalog:

```bash
capabilities/tools/rvbbit-capability catalog search extract
```

Install locally:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
capabilities/tools/rvbbit-capability install \
  capabilities/packs/extract/gliner-medium-v2.1 \
  --gpu
```

Queue an install through a Warren agent:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/smoke/warren-echo \
  --dsn "$RVBBIT_DSN" \
  --target '{"gpu":false}'
```

Run a pack's Warren acceptance tests:

```bash
capabilities/tools/rvbbit-capability test \
  capabilities/packs/smoke/warren-echo \
  --dsn "$RVBBIT_DSN"
```

Queue the managed Python runtime through Warren:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/runtimes/python-runtime \
  --dsn "$RVBBIT_DSN" \
  --target '{"docker":true}'
```

For the local stack, this shortcut uses the same catalog/Warren path: it queues
the built-in item, runs one Warren claim, and verifies `python_default`:

```bash
make python-runtime-up
```

Or from SQL:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'runtimes/python-runtime',
  target_selector => '{"docker":true}'::jsonb
);
```

See [WARREN.md](WARREN.md) for the remote deployment agent/catalog contract.

## Install Flow

1. Scaffold a capability.
2. Build/run the generated sidecar.
3. Apply `register.sql`.
4. Apply `operator.sql`.
5. Run `smoke.sql`.

Example:

```bash
capabilities/tools/rvbbit-capability scaffold \
  capabilities/packs/classify/deberta-v3-zero-shot \
  /tmp/rvbbit-deberta

cd /tmp/rvbbit-deberta
docker compose up -d --build
psql "$RVBBIT_DSN" -f register.sql
psql "$RVBBIT_DSN" -f operator.sql
psql "$RVBBIT_DSN" -f smoke.sql
```

With GPU access:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

Generated compose files attach the service to
`${RVBBIT_DOCKER_NETWORK:-docker_default}`. That default matches the current
dev stack. If your Rvbbit Postgres container is on another network, set
`RVBBIT_DOCKER_NETWORK` before `docker compose up`.

## Backend Runtime Contract

The generated sidecar uses the native Rvbbit specialist transport:

```http
POST /predict
{"inputs": [{"text": "..."}]}

200
{"outputs": [...]}
```

The response must contain one output per input in the same order. Operators
send templated input objects from their `steps` definition directly to this
endpoint.

## Execution Runtime Contract

Runtime sidecars expose language-specific execution surfaces rather than the
specialist `/predict` batch transport. The managed Python runtime uses:

```http
POST /run
{
  "env": {"name": "...", "python_version": "3.12", "requirements": [], "env_hash": "..."},
  "handler": {"name": "...", "code": "...", "code_hash": "...", "entrypoint": "run"},
  "inputs": {},
  "timeout_ms": 1000
}

200
{"ok": true, "output": {...}, "stdout": "", "stderr": "", "duration_ms": 12}
```

Users should not hand-build server venvs. They define package lists in SQL via
`rvbbit.create_python_env(...)`; the sidecar reconciles those specs into
persistent venvs.

## Server Metadata

Capability-generated SQL stores source metadata on `rvbbit.backends`:

- `source_provider`
- `source_model`
- `source_revision`
- `install_manifest`

This lets UIs distinguish hand-authored backends from installed capability
packs and reconstruct the original install/config shape.

Useful status surfaces:

```sql
SELECT * FROM rvbbit.backend_health ORDER BY name;
SELECT jsonb_pretty(rvbbit.backend_probe('extract_gliner'));
```

`backend_health` is passive and cheap. `backend_probe` actively calls the
backend through the same transport path used by specialist operator nodes.

## UI Builder Contract

This section is the stable v0 contract for an early capability UI. The UI
should be data-driven: read the SQL catalog, inspect installed backend state
from SQL, and call the CLI only for local scaffold/install actions. Do not
hardcode curated pack names or generated SQL bodies.

### Primary Data Sources

- `rvbbit.capability_catalog`: curated pack catalog for browsing and install
  selection from the database.
- `capabilities/catalog.json`: local build artifact generated from packs,
  and optional fallback when the database catalog has not been published.
- `capabilities/packs/**/rvbbit-pack.yaml`: root pack metadata used by the
  catalog publisher and future marketplace fetch/sync flows.
- `capabilities/packs/**/capability.yaml`: full deployable Warren manifests
  used by the CLI and stored in `rvbbit.capability_catalog.manifest`.
- `rvbbit.backend_health`: passive installed backend and usage state.
- `rvbbit.backend_probe(name)`: active backend check with default sample input.
- `rvbbit.backend_probe_with_input(name, sample_jsonb)`: active backend check
  with UI-supplied sample input.
- `rvbbit.python_runtimes`: registered Python execution endpoints, including
  Warren-deployed runtimes.
- `rvbbit.python_envs`: SQL-managed Python package environments.
- `rvbbit.ml_model_status`: passive trained-model registry with latest
  training-run state.
- `rvbbit.ml_training_runs`: queued/running/completed training jobs.
- `rvbbit.warren_inventory`: registered Warren nodes plus active deployments.
- `rvbbit.warren_jobs`: queued/running/completed remote deployment jobs.
- `rvbbit.warren_deployments`: deployment history tied to backend/operator
  names.
- `rvbbit.warren_node_latest_metrics`: latest CPU/memory/disk/GPU telemetry
  per Warren node.
- `rvbbit.warren_node_metrics`: telemetry history for charts and capacity
  debugging.

For the Warren-specific inventory, deployment, metrics, placement, and error
state contract, see [WARREN_UI_CONTRACT.md](WARREN_UI_CONTRACT.md).

### SQL Catalog Shape

The primary UI catalog is `rvbbit.capability_catalog`. Fresh extension installs
seed it from the canonical bundled capability seed. Use the CLI publish command
to refresh it after manifest changes:

```bash
capabilities/tools/rvbbit-capability catalog publish --dsn "$RVBBIT_DSN" --prune
```

Useful browse query:

```sql
SELECT
  id,
  name,
  title,
  description,
  tags,
  kind,
  source_provider,
  source_model,
  coalesce(catalog_entry->>'catalog_visibility', 'public') AS catalog_visibility,
  catalog_entry->>'pack_path' AS pack_path,
  catalog_entry->>'runtime_mode' AS runtime_mode,
  backend_name,
  runtime_name,
  runtime_language,
  runtime_template,
  runtime_handler,
  endpoint_path,
  device,
  resource_profile,
  gpu_required,
  model_size_bytes,
  vram_required_bytes,
  operators,
  active,
  updated_at
FROM rvbbit.capability_catalog
WHERE active
  AND coalesce(catalog_entry->>'catalog_visibility', 'public') = 'public'
ORDER BY title;
```

Deploy from the SQL catalog:

```sql
SELECT rvbbit.deploy_catalog_capability(
  catalog_id => 'smoke/warren-echo',
  target_selector => '{"gpu":false}'::jsonb
);
```

`manifest` contains the Warren deploy payload. `catalog_entry` contains the
JSON browse entry generated by the CLI. `rvbbit.deploy_catalog_capability(...)`
also stamps the queued `rvbbit.warren_jobs` row with the catalog's
`backend_name`, `runtime_name`, and first `operator_name` when those values are
known, so the UI can show install intent before a Warren claims the job.

GPU-capable model packs may publish `resources.gpu`. The catalog flattens the
most important values into `gpu_required`, `model_size_bytes`, and
`vram_required_bytes` for UI cards and Warren admission checks. V1 treats this
as a VRAM reservation yardstick only; it does not model GPU compute throughput.
For `device: auto` packs, Warren reserves VRAM when the deploy selector targets
a GPU node, for example `{"gpu": true}`.

### Catalog JSON Fallback Shape

`capabilities/catalog.json` is a build artifact and optional fallback. The
primary UI source is `rvbbit.capability_catalog`. The JSON artifact has this
top-level shape:

```json
{
  "schema_version": 1,
  "catalog_layout": "rvbbit.pack/v1",
  "capabilities": []
}
```

Each `capabilities[]` entry has these fields:

| Field | Type | Meaning |
|---|---:|---|
| `id` | string | Stable catalog id, normally the pack id such as `embeddings/bge-small-en-v1.5`. |
| `pack_id` | string/null | Pack metadata id from `rvbbit-pack.yaml`. |
| `pack_path` | string/null | Repo-relative pack root. |
| `pack_manifest_path` | string/null | Repo-relative `rvbbit-pack.yaml` path. |
| `manifest_path` | string | Repo-relative path to the full manifest. |
| `catalog_visibility` | string | `public`, `example`, or `internal`; default user browse should filter to `public`. |
| `name` | string | Capability pack name. |
| `title` | string | Human-readable title. |
| `description` | string/null | Short description. |
| `tags` | string[] | UI filters such as `embedding`, `extract`, `gpu`. |
| `kind` | string | `hf_backend` or `runtime_sidecar`. |
| `system_runtime` | boolean | True for operator-runtime capabilities that unlock broader workflow primitives instead of a single model specialist. |
| `capability_role` | string/null | Role hint such as `operator_runtime`. |
| `license` | string/null | Model or pack license hint. |
| `source_provider` | string/null | Usually `huggingface` for model packs or `builtin` for bundled runtimes. |
| `source_model` | string/null | Hugging Face model id or bundled capability id. |
| `source_revision` | string/null | Optional pinned model revision. |
| `backend_name` | string/null | Name registered in `rvbbit.backends`; null for runtime sidecars. |
| `backend_transport` | string/null | Usually `rvbbit` for generated model sidecars. |
| `runtime_name` | string/null | Name registered in a runtime catalog such as `rvbbit.python_runtimes`. |
| `runtime_language` | string/null | Runtime language, currently `python` or `mcp` for runtime sidecars. |
| `runtime_image` | string/null | OCI image Warren should run when the capability is image-based. |
| `runtime_mode` | string | `image` for pull/run packs, `build` for local build/template packs. |
| `install_mode` | string | Pack install mode, currently usually the same as `runtime_mode`. |
| `install_warren` | boolean/null | Whether the pack metadata declares Warren install support. |
| `install_docker` | boolean/null | Whether the pack expects Docker as the sidecar runtime. |
| `acceptance_tests` | string[] | Named pack acceptance SQL tests, if present. |
| `acceptance` | object/null | Runnable pack acceptance SQL: optional `target_selector`, `setup_sql[]`, `tests[{name, description, sql}]`, and `teardown_sql[]`. |
| `runtime_template` | string | Generated runtime template. |
| `runtime_handler` | string | Handler such as `echo`, `embedding`, `gliner`, `sequence_classification`, `tabular_classification`, `tabular_regression`, `python_runtime`, or `mcp_gateway`. |
| `runtime_port` | integer/null | Container port exposed by this runtime; defaults to `8080` when absent. |
| `health_path` | string/null | HTTP path Warren should poll for sidecar health; defaults to `/health`. |
| `endpoint_path` | string/null | Warren registration path such as `/predict` or `/run`. |
| `device` | string | Manifest preference: `auto`, `cpu`, or `cuda`. |
| `resources` | object | Optional resource profile. `resources.gpu.vram_required_bytes` is the estimated VRAM reservation. |
| `gpu_required` | boolean | True when the pack must run on GPU. `device: auto` packs may still include a GPU weight estimate with this set false. |
| `model_size_bytes` | integer/null | Estimated selected model weight bytes, usually from Hugging Face weight files. |
| `vram_required_bytes` | integer/null | Estimated VRAM reservation after headroom. |
| `operators` | string[] | SQL operator functions created by `operator.sql`; includes both raw model wrappers and bundled higher-level child operators. |

The UI can filter by `tags`, `kind`, `runtime_handler`, `source_provider`,
`source_model`, `license`, and `system_runtime`. Use `backend_name` to join
model entries to installed backend rows. Use `runtime_name` to join runtime
entries to runtime catalogs such as `rvbbit.python_runtimes` and
`rvbbit.mcp_gateways`.

Model packs may intentionally export multiple operators from one installed
Warren capability. For example, a reranker pack can install raw JSON wrappers
plus `about`, `semantic_score`, `means`, and `semantic_matches`; an embedding
pack can install `semantic_embed` and `similar_to`; an extraction pack can
replace the LLM fallback `extract(text, what)` with a specialist-backed
workflow. Treat `operators` as the user-facing capability surface, not merely
debug helpers. The full `manifest->'operators'` array contains signatures,
return types, parser hints, infix metadata, and multi-step wiring for detail
views.

### Manifest Shape

Each pack has two layers. `rvbbit-pack.yaml` is the lightweight root metadata
used for discovery, provenance, filtering, and future marketplace sync.
`capability.yaml` is the deployable payload stored in
`rvbbit.capability_catalog.manifest` and handed to Warren.

Minimal pack metadata:

```yaml
api_version: rvbbit.pack/v1
id: embeddings/bge-small-en-v1.5
name: bge_small_en_v1_5
title: BGE Small English Embeddings
capability: capability.yaml
runtime:
  mode: build
  template: hf-rvbbit-fastapi
  handler: embedding
exports:
  backend: embed_bge_small
  operators: [embed_bge_small]
install:
  mode: build
  warren: true
  docker: true
acceptance:
  target_selector:
    capability: true
    docker: true
    gpu: false
  tests:
    - name: smoke_operator_sample_table
      sql: |
        DO $$
        BEGIN
          -- Create tiny sample data, call installed functions, raise on mismatch.
        END
        $$;
```

The full capability manifest is the source of truth for install details. A UI
can display or edit advanced settings from these sections:

```yaml
api_version: rvbbit.capability/v1
kind: hf_backend
name: bge_small_en_v1_5
title: BGE Small English Embeddings
source:
  provider: huggingface
  model: BAAI/bge-small-en-v1.5
  revision: null
runtime:
  template: hf-rvbbit-fastapi
  handler: embedding
  device: auto
backend:
  name: embed_bge_small
  transport: rvbbit
  batch_size: 128
  max_concurrent: 2
  timeout_ms: 120000
operators:
  - name: embed_bge_small
    arg_names: [text]
    arg_types: [text]
    return_type: jsonb
```

The UI should treat unknown manifest keys as pass-through data. V1 supports
`kind: hf_backend` and `kind: runtime_sidecar` in the CLI, but future pack
kinds should not break catalog rendering.

Runtime sidecar example:

```yaml
api_version: rvbbit.capability/v1
kind: runtime_sidecar
name: python_runtime
title: Managed CPython Runtime
source:
  provider: builtin
  model: rvbbit/python-runtime
runtime:
  template: python-runtime
  language: python
  handler: python_runtime
  base_image: python:3.12-slim
  volumes:
    - name: python_envs
      mount: /var/lib/rvbbit
runtime_registration:
  name: python_default
  language: python
  endpoint_path: /run
  set_default: true
warren:
  endpoint_path: /run
```

System runtime sidecars are still `kind: runtime_sidecar`, but their pack
metadata sets `system_runtime: true` and `capability_role: operator_runtime`.
The built-ins are `runtimes/python-runtime` for `kind: python` operator nodes
and `runtimes/mcp-gateway` for `kind: mcp` nodes and SQL MCP calls. Treat them
as higher-level runtime primitives in the UI rather than ordinary model cards.

For V1, bundled runtime sidecars are source-build packs by default. Warren
builds them from trusted local templates, starts the container, probes it, and
registers SQL state. Published OCI images can be added later by setting
`runtime.image`; the catalog table contract already exposes both `runtime_mode`
and nullable `runtime_image`.

### Tabular Handler Shape

Tabular packs use the same `/predict` HTTP contract and same
`rvbbit.register_backend(...)` path as text/image-like model packs, but the SQL
operator shape is row-oriented:

```sql
SELECT rvbbit.predict_wine_quality(
  '{
    "fixed acidity": 7.4,
    "volatile acidity": 0.7,
    "citric acid": 0.0,
    "residual sugar": 1.9,
    "chlorides": 0.076,
    "free sulfur dioxide": 11,
    "total sulfur dioxide": 34,
    "density": 0.9978,
    "pH": 3.51,
    "sulphates": 0.56,
    "alcohol": 9.4
  }'::jsonb
);
```

Supported v0 tabular handlers:

| Handler | Input | Output |
|---|---|---|
| `tabular_classification` | one JSONB row per input | `{label, prediction, scores?}` |
| `tabular_regression` | one JSONB row per input | `{value}` |

Tabular handlers currently load trusted model artifacts from Hugging Face with
`joblib`. Manifests can provide these runtime env values:

| Variable | Meaning |
|---|---|
| `RVBBIT_TABULAR_MODEL_FILE` | Model artifact filename, usually `sklearn_model.joblib` or `model.joblib`. |
| `RVBBIT_TABULAR_CONFIG_FILE` | Optional JSON config file with `features` and `target_mapping`. |
| `RVBBIT_TABULAR_FEATURES` | Optional JSON array or comma-separated feature order. |
| `RVBBIT_TABULAR_LABELS` | Optional JSON array or comma-separated labels. |
| `RVBBIT_TABULAR_TARGET_MAPPING` | Optional JSON object mapping raw predictions to display labels. |
| `RVBBIT_TABULAR_COLUMN_PREFIX` | Optional prefix added to DataFrame columns before prediction, used by some AutoTrain models. |

Because pickle/joblib model files are executable Python object graphs, a UI
should treat tabular packs as trusted-code installs and make that visible.

### Trained Model Lifecycle

A user-trained model is a capability-backed asset whose training data came from
SQL. Rvbbit owns the catalog, lifecycle state, generated backend registration,
and optional SQL operator. A trainer worker owns the expensive training process
outside the Postgres backend.

The stable raw SQL primitives are:

| Function | Purpose |
|---|---|
| `rvbbit.train_model(...)` | Queue a training request from SQL data. Returns `run_id`. |
| `rvbbit.claim_model_training_run(worker_id)` | Atomically claim one queued run for a trainer worker. |
| `rvbbit.complete_model_training(...)` | Mark a run complete, register the serving backend, and optionally create a `predict_*` operator. |
| `rvbbit.fail_model_training(run_id, error, metrics)` | Mark a run failed and attach diagnostics. |
| `rvbbit.register_trained_model(...)` | Register an already-trained artifact directly. |

Queue a training request:

```sql
SELECT rvbbit.train_model(
  model_name => 'churn_model',
  task => 'classification',
  source_sql => $$
    SELECT tenure_months, plan_tier, monthly_spend, churned
    FROM customers_training
  $$,
  target_column => 'churned',
  feature_schema => '[
    {"name":"tenure_months","type":"float8"},
    {"name":"plan_tier","type":"text"},
    {"name":"monthly_spend","type":"float8"}
  ]'::jsonb,
  training_opts => '{"algorithm":"auto"}'::jsonb
);
```

Register a completed artifact as a backend/operator:

```sql
SELECT rvbbit.register_trained_model(
  model_name => 'churn_model',
  model_task => 'classification',
  backend_name => 'churn_model_backend',
  backend_endpoint => 'http://rvbbit-churn-model:8080',
  artifact_uri => 'file:///var/lib/rvbbit/models/churn_model/model.joblib',
  artifact_format => 'joblib',
  feature_schema => '[
    {"name":"tenure_months","type":"float8"},
    {"name":"plan_tier","type":"text"},
    {"name":"monthly_spend","type":"float8"}
  ]'::jsonb,
  target_column => 'churned',
  metrics => '{"accuracy":0.91,"auc":0.94}'::jsonb,
  operator_name => 'predict_churn'
);
```

The generated operator is row-oriented by default:

```sql
SELECT customer_id,
       rvbbit.predict_churn(to_jsonb(c.*)) AS prediction
FROM customers c;
```

The operator is just a normal `rvbbit.create_operator(...)` wrapper whose
single step calls the registered trained-model backend. Users can inspect or
edit it in `rvbbit.operators`.

Trainer worker loop:

```sql
SELECT * FROM rvbbit.claim_model_training_run('trainer-1');
-- train outside Postgres, write artifact, then:
SELECT rvbbit.complete_model_training(
  run_id => '<claimed run id>',
  backend_name => 'churn_model_backend',
  backend_endpoint => 'http://rvbbit-churn-model:8080',
  artifact_uri => 'file:///var/lib/rvbbit/models/churn_model/model.joblib',
  artifact_format => 'joblib',
  metrics => '{"accuracy":0.91}'::jsonb
);
```

The repo ships a first sklearn-based trainer worker:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
capabilities/tools/rvbbit-trainer run-once \
  --output-root .rvbbit/trained-models \
  --start-sidecar \
  --docker-network docker_default
```

`run-once` claims one queued run, executes `source_sql`, trains a tabular
classification/regression model, writes a generated capability project, starts
the generated sidecar when `--start-sidecar` is set, and calls
`rvbbit.complete_model_training(...)`.

Generated trained-model project layout:

| File | Meaning |
|---|---|
| `model.joblib` | Fitted sklearn pipeline. Trusted Python artifact. |
| `config.json` | Feature order, feature schema, target column, task, and metrics. |
| `training_metrics.json` | Holdout/evaluation metrics from the trainer. |
| `rvbbit.backend.yaml` | Capability manifest generated from the training run. |
| `compose.yaml` | Sidecar runtime for serving the trained model. |
| `register.sql` / `operator.sql` / `smoke.sql` | Standard capability SQL artifacts. |

Useful trainer commands:

```bash
# Claim and process one queued run.
capabilities/tools/rvbbit-trainer run-once --dsn "$RVBBIT_DSN"

# Process a known run id.
capabilities/tools/rvbbit-trainer train-run "$RUN_ID" --dsn "$RVBBIT_DSN"

# Train directly from SQL without a queued run. Useful for local experiments.
capabilities/tools/rvbbit-trainer train-query \
  --dsn "$RVBBIT_DSN" \
  --model-name churn_model \
  --target-column churned \
  --source-sql "SELECT tenure, plan, churned FROM customers_training"
```

The v0 trainer supports `classification` and `regression`. It uses sklearn
pipelines with numeric imputation/scaling, categorical imputation/one-hot
encoding, and random-forest defaults unless `training_opts` specifies another
supported estimator. This is intentionally conservative: stronger trainers can
be added later while preserving the same catalog and serving contract.

UI data sources:

```sql
SELECT *
FROM rvbbit.ml_model_status
ORDER BY updated_at DESC;

SELECT *
FROM rvbbit.ml_training_runs
ORDER BY created_at DESC
LIMIT 100;
```

Important fields:

| Field | Meaning |
|---|---|
| `name` | Stable model id. |
| `task` | `classification`, `regression`, `forecasting`, `anomaly`, `survival`, `causal`, or a tabular-specific alias. |
| `status` | Model state: `queued`, `running`, `active`, `failed`, `disabled`, `dropped`, or `registered`. |
| `source_sql` | Training query provenance. Display as sensitive SQL; it may contain business logic. |
| `feature_schema` | JSON array of feature names/types used by trainer and UI forms. |
| `artifact_uri` | Where the trained artifact lives. May be local file, mounted volume, or object-store URI. |
| `backend_name` | Serving backend registered in `rvbbit.backends`. |
| `operator_name` | SQL operator/function created for prediction, usually `predict_<model>`. |
| `metrics` | Trainer-emitted evaluation metrics. Treat as arbitrary JSON. |
| `latest_run_*` | Convenience columns from `rvbbit.ml_model_status` for dashboard state. |

The UI should show trained models beside curated capability packs but keep the
origin clear:

- Curated packs come from `rvbbit.capability_catalog`.
- User-trained models come from `rvbbit.ml_model_status`.
- Both ultimately register rows in `rvbbit.backends` and optionally
  `rvbbit.operators`.

### Future SQL Sugar

The verbose function calls above are the stable base API. They should keep
working even if a friendlier surface is added later.

Postgres extensions cannot add arbitrary new top-level grammar such as raw
`CREATE RVBBIT MODEL ...` without a server parser patch. Rvbbit can still add
lighter syntax in parseable forms:

- SQL functions: `SELECT rvbbit.train_model(...)`.
- Macro wrappers: `SELECT rvbbit.exec($$CREATE RVBBIT MODEL ...$$)`.
- Comment or GUC hints on normal SQL that parser hooks can inspect.
- Client/CLI preprocessing for convenience scripts.

For release, prefer sugar that lowers into the same raw catalog functions so
UIs, docs, and automation have one durable contract.

### Installed Backend Query

Use this query for the installed backend list:

```sql
SELECT
  name,
  transport,
  endpoint_url,
  batch_size,
  max_concurrent,
  timeout_ms,
  auth_header_env,
  transport_opts,
  description,
  source_provider,
  source_model,
  source_revision,
  install_manifest,
  n_calls,
  n_errors,
  avg_latency_ms,
  p50_latency_ms,
  p95_latency_ms,
  first_call_at,
  last_call_at,
  created_at
FROM rvbbit.backend_health
ORDER BY name;
```

`install_manifest IS NOT NULL` means the backend was installed from a
capability pack or compatible generated SQL. Hand-authored backends may have no
manifest and should still be shown in an "Installed Backends" view.

### Installed Runtime Query

Use this query for runtime-sidecar installs:

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

`runtime_source = 'warren'` means Warren deployed and registered the endpoint.
Join catalog entries with `kind = 'runtime_sidecar'` on
`catalog.runtime_name = python_runtimes.name`.

MCP gateway runtime state:

```sql
SELECT
  name,
  endpoint_url,
  status,
  labels,
  gateway_source,
  install_manifest,
  health,
  created_at,
  updated_at
FROM rvbbit.mcp_gateways
ORDER BY name;
```

Join MCP runtime catalog entries with `runtime_language = 'mcp'` on
`catalog.runtime_name = mcp_gateways.name`.

### Install State Model

For v0, use this state model:

| State | How To Infer |
|---|---|
| `catalog_only` | Entry exists in `rvbbit.capability_catalog`, no matching backend/runtime row. |
| `registered` | `rvbbit.backend_health.name = catalog.backend_name` or `rvbbit.python_runtimes.name = catalog.runtime_name`. |
| `used` | Registered and `n_calls > 0`. |
| `error_seen` | Registered and `n_errors > 0`. |
| `healthy` | Latest `rvbbit.backend_probe(...)` returned `{"ok": true}`. |
| `failing` | Latest probe returned `{"ok": false}` or raised a SQL/client error. |
| `runtime_ready` | Runtime row exists with `status = 'ready'`. |
| `runtime_failing` | Runtime row exists with `status IN ('failed', 'disabled')`. |
| `external` | Backend/runtime row exists with no matching catalog entry. |

Scaffolded and container-running states are currently outside the database.
If the UI manages local Docker itself, track those states in UI-local state by
output directory and compose service name. Do not infer container state from
`backend_health`; that view is database-only and passive.

### Active Probe Contract

Default probe:

```sql
SELECT rvbbit.backend_probe('embed_bge_small');
```

Custom probe:

```sql
SELECT rvbbit.backend_probe_with_input(
  'embed_bge_small',
  '{"text":"hello world","query":"hello","labels":["person","place"]}'::jsonb
);
```

Success shape:

```json
{
  "ok": true,
  "backend": "embed_bge_small",
  "transport": "rvbbit",
  "endpoint": "http://bge-small-en-v1-5:8080/predict",
  "latency_ms": 12.3,
  "output": []
}
```

Failure shape:

```json
{
  "ok": false,
  "backend": "embed_bge_small",
  "latency_ms": 12.3,
  "error": "connection refused"
}
```

Probe output can be large for embedding models. UI tables should show compact
status, latency, and output type/size by default, with raw JSON behind a detail
drawer.

### CLI Actions For The UI

Publish or refresh the SQL catalog:

```bash
capabilities/tools/rvbbit-capability catalog publish \
  --dsn "$RVBBIT_DSN" \
  --prune
```

List curated packs:

```bash
capabilities/tools/rvbbit-capability list
```

Build or refresh the JSON fallback artifact:

```bash
capabilities/tools/rvbbit-capability catalog build \
  --output capabilities/catalog.json
```

Search:

```bash
capabilities/tools/rvbbit-capability catalog search embedding
```

Scaffold without installing:

```bash
capabilities/tools/rvbbit-capability scaffold \
  capabilities/packs/embeddings/bge-small-en-v1.5 \
  .rvbbit/capabilities/bge_small_en_v1_5 \
  --force
```

Install with Docker + SQL:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
capabilities/tools/rvbbit-capability install \
  capabilities/packs/embeddings/bge-small-en-v1.5 \
  --out-dir .rvbbit/capabilities/bge_small_en_v1_5 \
  --gpu \
  --force
```

Install without running Docker or SQL, useful for preview:

```bash
capabilities/tools/rvbbit-capability install \
  capabilities/packs/embeddings/bge-small-en-v1.5 \
  --out-dir .rvbbit/capabilities/bge_small_en_v1_5 \
  --no-compose \
  --no-sql \
  --force
```

The CLI currently emits human-readable output. A UI may shell out and parse the
known output lightly for v0, but should prefer inspecting generated files and
SQL state after commands complete.

### Generated Files To Preview

After scaffold/install, the output directory contains:

| File | UI Use |
|---|---|
| `rvbbit.backend.yaml` | Show exact installed manifest. |
| `register.sql` | SQL preview before registration. |
| `operator.sql` | SQL operator preview. |
| `smoke.sql` | Smoke commands preview/run. |
| `compose.yaml` | CPU/local sidecar deployment, Docker-network only by default. |
| `compose.host-ports.yaml` | Optional overlay for publishing a host port when Postgres cannot reach the Docker network directly. |
| `compose.gpu.yaml` | NVIDIA GPU overlay. |
| `Dockerfile` | Advanced runtime inspection. |
| `main.py` | Advanced custom handler inspection/editing. |
| `requirements.txt` | Dependency preview. |
| `README.md` | Generated pack-specific instructions. |

### Environment And Runtime Controls

| Variable | Meaning |
|---|---|
| `RVBBIT_DSN` | Postgres DSN used by install and generated `psql` examples. |
| `RVBBIT_DOCKER_NETWORK` | Docker network joined by generated sidecars. Defaults to `docker_default`. |
| `RVBBIT_CAPABILITY_PORT` | Host port used only with `compose.host-ports.yaml` or Warren `--advertise-base-url`. Defaults to Docker-assigned random port in the optional overlay. |
| `RVBBIT_CAPABILITY_DEVICE` | Runtime device inside sidecar. GPU overlay sets `cuda`. |

GPU install should be exposed as an option, not forced. The generated GPU
overlay assumes Docker has NVIDIA GPU support configured.

### Suggested V0 UI

- Catalog browser: cards/table from `rvbbit.capability_catalog`, filter by
  tags, provider, runtime handler, device, and installed state.
- Capability detail: manifest summary, model/source link, operators, runtime
  settings, generated SQL preview.
- Install wizard: choose CPU/GPU, output directory, Docker network, optional
  host-port publishing, and whether to apply SQL immediately.
- Installed backends: table from `rvbbit.backend_health` with call counts,
  error counts, latency metrics, endpoint, source model, and install source.
- Trained models: table from `rvbbit.ml_model_status` with state, latest run,
  metrics, artifact URI, backend, and generated operator.
- Training jobs: queue/history table from `rvbbit.ml_training_runs`, with
  filters for queued, running, failed, and completed runs.
- Probe panel: run `backend_probe` or `backend_probe_with_input`, display
  compact status and raw JSON.
- Operator quick test: show generated operator names and simple example SQL
  from `smoke.sql`.

### Safety Rules For UI Agents

- Never run generated SQL without showing a preview or making the action clear.
- Treat `backend_probe` and `smoke.sql` as active calls that may download/load
  models or invoke external services.
- Do not assume every installed backend came from the curated catalog.
- Do not assume a healthy Docker container means the backend is registered in
  Postgres, or vice versa.
- Do not delete generated directories, containers, or backend rows unless the
  user explicitly chooses an uninstall/delete action.

## Curated V1 Packs

- `classify/deberta-v3-base-zero-shot`
- `classify/deberta-v3-zero-shot`
- `classify/emotion-distilroberta`
- `classify/language-detection-xlm-roberta`
- `classify/toxic-bert`
- `classify/twitter-roberta-sentiment`
- `embeddings/bge-small-en-v1.5`
- `embeddings/bge-m3`
- `embeddings/e5-small-v2`
- `extract/gliner-medium-v2.1`
- `rerank/bge-reranker-base`
- `rerank/bge-reranker-v2-m3`
- `rerank/ms-marco-minilm-l6-v2`
- `runtimes/python-runtime`
- `smoke/warren-echo`
- `tabular/california-housing-sklearn`
- `tabular/wine-quality-sklearn`

These are starting points, not a claim that every model is ideal for every
workload. Pin model revisions before production use when reproducibility
matters.

## Custom Models

For unusual Hugging Face research models, set:

```yaml
runtime:
  handler: custom
```

Then edit the generated `main.py` and keep the `/predict` request/response
shape unchanged. The SQL registration and operator installation still work.
