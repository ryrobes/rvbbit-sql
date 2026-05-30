# Rvbbit Capabilities

Capability packs turn a model into a SQL-callable Rvbbit backend and, when
useful, one or more ergonomic operators. This directory is intentionally
self-contained so it can become a separate `rvbbit-capabilities` repository
once the manifest shape settles.

V1 is Hugging Face focused. A pack contains:

- `rvbbit-pack.yaml`: root metadata for catalog/marketplace discovery;
- `capability.yaml`: the deployable `rvbbit.capability/v1` Warren payload;
- optional source files when the pack builds its own image;
- either a prebuilt OCI image reference or a template/build fallback.

The built-ins live under `capabilities/packs/<category>/<pack>/` to mirror the
shape of a standalone capability repository. Users should be able to inspect
the pack metadata, deploy manifest, image reference, and any local source before
adding the capability to their database catalog.

## Quick Start

Render SQL only:

```bash
python capabilities/tools/rvbbit-capability render \
  capabilities/packs/extract/gliner-medium-v2.1
```

Scaffold a runnable sidecar bundle:

```bash
python capabilities/tools/rvbbit-capability scaffold \
  capabilities/packs/extract/gliner-medium-v2.1 \
  /tmp/rvbbit-gliner
```

Install locally in one command:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
python capabilities/tools/rvbbit-capability install \
  capabilities/packs/extract/gliner-medium-v2.1 \
  --gpu
```

Queue deployment through a Warren agent instead of installing on the current
machine:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
python capabilities/tools/rvbbit-capability deploy \
  capabilities/packs/smoke/warren-echo \
  --target '{"gpu":false}'
```

Run a pack's Warren acceptance tests:

```bash
RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench \
python capabilities/tools/rvbbit-capability test \
  capabilities/packs/smoke/warren-echo
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
`capabilities/packs/smoke/warren-echo`. It runs a deterministic echo
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

## Acceptance Tests

Packs may define `acceptance.tests` in `rvbbit-pack.yaml`. Each test is a named
SQL block that should raise an exception on failure. The test runner queues the
pack through Warren, runs one local Warren claim, then executes those SQL blocks
with `ON_ERROR_STOP`. This is meant for small real-world samples: create a tiny
temp table, call the installed operators/functions, and assert the expected
shape or value.

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
zero-shot classifiers, language detection, sentiment, emotion, toxicity, the
managed Python runtime, and row-oriented JSONB operators for classifying,
reranking, and predicting over tabular records.

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
