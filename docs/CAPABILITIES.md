# Rvbbit Capabilities

A capability pack is a portable bundle that turns an external model into a
registered Rvbbit backend and, when useful, one or more SQL operators.

V1 capability packs live in `capabilities/` and focus on Hugging Face models.
The directory is intentionally self-contained so it can move to a separate
`rvbbit-capabilities` repository later.

## What A Pack Contains

- `rvbbit.backend.yaml`: source model, runtime, endpoint, batching, and SQL
  operator definitions.
- `register.sql`: calls `rvbbit.register_backend(...)`.
- `operator.sql`: optional `rvbbit.create_operator(...)` calls.
- `smoke.sql`: active backend probe plus example operator calls.
- `Dockerfile`, `main.py`, `requirements.txt`: a FastAPI sidecar scaffold
  speaking Rvbbit's native batch transport.
- `compose.yaml`: standalone local deployment.
- `compose.gpu.yaml`: optional NVIDIA GPU overlay.

## CLI

List curated manifests:

```bash
capabilities/tools/rvbbit-capability list
```

Render SQL:

```bash
capabilities/tools/rvbbit-capability render \
  capabilities/manifests/extract/gliner-medium-v2.1.yaml
```

Scaffold a runnable sidecar:

```bash
capabilities/tools/rvbbit-capability scaffold \
  capabilities/manifests/extract/gliner-medium-v2.1.yaml \
  /tmp/rvbbit-gliner
```

Build the UI catalog:

```bash
capabilities/tools/rvbbit-capability catalog build \
  --output capabilities/catalog.json
```

Search the curated catalog:

```bash
capabilities/tools/rvbbit-capability catalog search extract
```

Install locally:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
capabilities/tools/rvbbit-capability install \
  capabilities/manifests/extract/gliner-medium-v2.1.yaml \
  --gpu
```

Queue an install through a Warren agent:

```bash
capabilities/tools/rvbbit-capability deploy \
  capabilities/manifests/smoke/warren-echo.yaml \
  --dsn "$RVBBIT_DSN" \
  --target '{"gpu":false}'
```

Or from SQL:

```sql
SELECT rvbbit.deploy_capability(
  capability_manifest => '<rendered manifest json>'::jsonb,
  target_selector => '{"gpu":true}'::jsonb
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
  capabilities/manifests/classify/deberta-v3-zero-shot.yaml \
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

## Runtime Contract

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
should be data-driven: read the catalog, inspect installed backend state from
SQL, and call the CLI for scaffold/install actions. Do not hardcode curated
pack names or generated SQL bodies.

### Primary Data Sources

- `capabilities/catalog.json`: curated pack catalog for browsing and install
  selection.
- `capabilities/manifests/**/*.yaml`: full source manifest for a selected
  catalog item.
- `rvbbit.backend_health`: passive installed backend and usage state.
- `rvbbit.backend_probe(name)`: active backend check with default sample input.
- `rvbbit.backend_probe_with_input(name, sample_jsonb)`: active backend check
  with UI-supplied sample input.
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

### Catalog JSON Shape

`capabilities/catalog.json` has this top-level shape:

```json
{
  "schema_version": 1,
  "capabilities": []
}
```

Each `capabilities[]` entry has these fields:

| Field | Type | Meaning |
|---|---:|---|
| `id` | string | Stable catalog id, relative to `capabilities/manifests/`. |
| `manifest_path` | string | Repo-relative path to the full manifest. |
| `name` | string | Capability pack name. |
| `title` | string | Human-readable title. |
| `description` | string/null | Short description. |
| `tags` | string[] | UI filters such as `embedding`, `extract`, `gpu`. |
| `kind` | string | Currently `hf_backend`. |
| `license` | string/null | Model or pack license hint. |
| `source_provider` | string/null | Currently usually `huggingface`. |
| `source_model` | string/null | Hugging Face model id. |
| `source_revision` | string/null | Optional pinned model revision. |
| `backend_name` | string | Name registered in `rvbbit.backends`. |
| `backend_transport` | string | Usually `rvbbit` for generated sidecars. |
| `runtime_template` | string | Generated runtime template. |
| `runtime_handler` | string | Handler such as `echo`, `embedding`, `gliner`, `sequence_classification`, `tabular_classification`, or `tabular_regression`. |
| `device` | string | Manifest preference: `auto`, `cpu`, or `cuda`. |
| `operators` | string[] | SQL operator functions created by `operator.sql`. |

The UI can filter by `tags`, `runtime_handler`, `source_provider`,
`source_model`, and `license`. The UI should use `backend_name` to join catalog
entries to installed backend rows.

### Manifest Shape

The full manifest is the source of truth for install details. A UI can display
or edit advanced settings from these sections:

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

The UI should treat unknown manifest keys as pass-through data. V1 only
supports `kind: hf_backend` in the CLI, but future pack kinds should not break
catalog rendering.

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

- Curated packs come from `capabilities/catalog.json`.
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

### Install State Model

For v0, use this state model:

| State | How To Infer |
|---|---|
| `catalog_only` | Entry exists in `catalog.json`, no matching backend row. |
| `registered` | `rvbbit.backend_health.name = catalog.backend_name`. |
| `used` | Registered and `n_calls > 0`. |
| `error_seen` | Registered and `n_errors > 0`. |
| `healthy` | Latest `rvbbit.backend_probe(...)` returned `{"ok": true}`. |
| `failing` | Latest probe returned `{"ok": false}` or raised a SQL/client error. |
| `external` | Backend row exists with no matching catalog entry. |

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

List curated packs:

```bash
capabilities/tools/rvbbit-capability list
```

Build or refresh catalog JSON:

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
  capabilities/manifests/embeddings/bge-small-en-v1.5.yaml \
  .rvbbit/capabilities/bge_small_en_v1_5 \
  --force
```

Install with Docker + SQL:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
capabilities/tools/rvbbit-capability install \
  capabilities/manifests/embeddings/bge-small-en-v1.5.yaml \
  --out-dir .rvbbit/capabilities/bge_small_en_v1_5 \
  --gpu \
  --force
```

Install without running Docker or SQL, useful for preview:

```bash
capabilities/tools/rvbbit-capability install \
  capabilities/manifests/embeddings/bge-small-en-v1.5.yaml \
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
| `compose.yaml` | CPU/local sidecar deployment. |
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
| `RVBBIT_CAPABILITY_PORT` | Host port for generated sidecar. Defaults to `8080`. |
| `RVBBIT_CAPABILITY_DEVICE` | Runtime device inside sidecar. GPU overlay sets `cuda`. |

GPU install should be exposed as an option, not forced. The generated GPU
overlay assumes Docker has NVIDIA GPU support configured.

### Suggested V0 UI

- Catalog browser: cards/table from `capabilities/catalog.json`, filter by
  tags, provider, runtime handler, device, and installed state.
- Capability detail: manifest summary, model/source link, operators, runtime
  settings, generated SQL preview.
- Install wizard: choose CPU/GPU, output directory, Docker network, host port,
  and whether to apply SQL immediately.
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

- `classify/deberta-v3-base-zero-shot.yaml`
- `classify/deberta-v3-zero-shot.yaml`
- `classify/emotion-distilroberta.yaml`
- `classify/language-detection-xlm-roberta.yaml`
- `classify/toxic-bert.yaml`
- `classify/twitter-roberta-sentiment.yaml`
- `embeddings/bge-small-en-v1.5.yaml`
- `embeddings/bge-m3.yaml`
- `embeddings/e5-small-v2.yaml`
- `extract/gliner-medium-v2.1.yaml`
- `rerank/bge-reranker-base.yaml`
- `rerank/bge-reranker-v2-m3.yaml`
- `rerank/ms-marco-minilm-l6-v2.yaml`
- `tabular/california-housing-sklearn.yaml`
- `tabular/wine-quality-sklearn.yaml`

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
