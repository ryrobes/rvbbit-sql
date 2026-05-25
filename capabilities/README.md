# Rvbbit Capabilities

Capability packs turn a model into a SQL-callable Rvbbit backend and, when
useful, one or more ergonomic operators. This directory is intentionally
self-contained so it can become a separate `rvbbit-capabilities` repository
once the manifest shape settles.

V1 is Hugging Face focused. A pack contains:

- a manifest under `manifests/`;
- generated `register.sql` for `rvbbit.backends`;
- optional generated `operator.sql` for `rvbbit.create_operator(...)`;
- a FastAPI sidecar scaffold that speaks Rvbbit's native batch transport.

## Quick Start

Render SQL only:

```bash
python capabilities/tools/rvbbit-capability render \
  capabilities/manifests/extract/gliner-medium-v2.1.yaml
```

Scaffold a runnable sidecar bundle:

```bash
python capabilities/tools/rvbbit-capability scaffold \
  capabilities/manifests/extract/gliner-medium-v2.1.yaml \
  /tmp/rvbbit-gliner
```

Install locally in one command:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
python capabilities/tools/rvbbit-capability install \
  capabilities/manifests/extract/gliner-medium-v2.1.yaml \
  --gpu
```

Queue deployment through a Warren agent instead of installing on the current
machine:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
python capabilities/tools/rvbbit-capability deploy \
  capabilities/manifests/smoke/warren-echo.yaml \
  --target '{"gpu":false}'
```

The command above is equivalent to:

```sql
SELECT rvbbit.deploy_capability(
  capability_manifest => '<rendered manifest json>'::jsonb,
  target_selector => '{"gpu":false}'::jsonb
);
```

Warren agents poll the database, deploy matching jobs, then register the
resulting backend/operator. See `docs/WARREN.md`.

For a zero-download end-to-end smoke test, use
`capabilities/manifests/smoke/warren-echo.yaml`. It runs a deterministic echo
sidecar from `python:3.12-slim` and validates deployment mechanics without a
Hugging Face model pull.

The scaffold writes:

- `Dockerfile`
- `main.py`
- `requirements.txt`
- `rvbbit.backend.yaml`
- `register.sql`
- `operator.sql`
- `smoke.sql`
- `compose.yaml`
- `compose.gpu.yaml`

## Catalog

Build the UI-friendly catalog index:

```bash
python capabilities/tools/rvbbit-capability catalog build \
  --output capabilities/catalog.json
```

Search curated packs:

```bash
python capabilities/tools/rvbbit-capability catalog search extract
```

The default catalog includes embeddings, rerankers, entity/PII extraction,
zero-shot classifiers, language detection, sentiment, emotion, toxicity, and
row-oriented JSONB operators for classifying, reranking, and predicting over
tabular records.

## Install Pattern

1. Build/run the generated sidecar.
2. Apply `register.sql`.
3. Apply `operator.sql` if the pack includes operators.
4. Run `SELECT rvbbit.reload_backends();`.
5. Run `smoke.sql`.

The generated SQL uses only public Rvbbit APIs:

- `rvbbit.register_backend(...)`
- `rvbbit.create_operator(...)`
- `rvbbit.reload_backends()`

## Design Notes

The manifest includes source metadata and operator wiring because a backend
alone is plumbing. The user-facing primitive should be a normal SQL function
such as `rvbbit.extract_entities(text, labels)` or `rvbbit.rerank(query, text)`.

The runtime template implements a small subset of common Hugging Face model
families. For research models that need custom preprocessing, use
`runtime.handler: custom` and edit the generated `predict_batch` function.
