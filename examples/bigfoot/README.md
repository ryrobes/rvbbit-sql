# Bigfoot Field Notebook

This is a SQL-first mini-project for the RVBBIT docs. It loads the full
`bigfoot_sightings.csv` file with all columns, builds a cleaned reporting table,
then walks through semantic retrieval, classification, clustering, extraction,
knowledge graph context, and receipts.

No Python is used by the notebook. The loader uses `psql \copy` so the CSV can
be read from the client machine.

## Run

```bash
examples/bigfoot/run_all.sh
```

The capability-operator section (`06_capability_operators.sql`) expects these
Warren packs to be installed:

```bash
make capability-test MANIFEST=capabilities/packs/extract/gliner-medium-v2.1 TARGET='{"capability":true,"docker":true}'
make capability-test MANIFEST=capabilities/packs/rerank/bge-reranker-v2-m3 TARGET='{"capability":true,"docker":true}'
make capability-test MANIFEST=capabilities/packs/classify/emotion-distilroberta TARGET='{"capability":true,"docker":true}'
```

Set `BIGFOOT_SKIP_CAPABILITIES=1` to run only the non-capability sections.

Defaults:

- `RVBBIT_DSN=postgresql://postgres:rvbbit@localhost:55433/bench`
- `BIGFOOT_SAMPLE_ROWS=500`
- `BIGFOOT_CLASSIFY_ROWS=250`
- `BIGFOOT_EXTRACT_ROWS=12`
- `BIGFOOT_KG_ROWS=250`
- `BIGFOOT_CAPABILITY_ENTITY_ROWS=8`
- `BIGFOOT_CAPABILITY_RERANK_CANDIDATES=24`
- `BIGFOOT_CAPABILITY_CLASSIFY_ROWS=8`

The live triples/receipt section calls the configured model provider. It is
opt-in:

```bash
BIGFOOT_LIVE=1 examples/bigfoot/run_all.sh
```

The training section (`08_predict_class.sql`) trains a scikit-learn classifier
from a SQL `SELECT`. Fitting is done by the external `rvbbit-trainer` worker, so
a worker must be running. Start one in another shell, then opt in:

```bash
# 1. start a worker that claims queued runs, fits them, and serves locally
rvbbit-trainer watch --include-unmanaged --serve-local --serve-host <db-reachable-host>

# 2. run the notebook including the training section
BIGFOOT_TRAIN=1 examples/bigfoot/run_all.sh
```

In the Docker dev stack the worker lives in the `bench` container and Postgres
reaches it as host `bench`:

```bash
docker compose -f docker/docker-compose.yml exec -T bench \
  python /capabilities/tools/rvbbit-trainer \
  watch --include-unmanaged --serve-local --serve-host bench
```

`08_predict_class.sql` queues the run, waits up to `BIGFOOT_TRAIN_WAIT` seconds
for the worker to bring the model online, then predicts and evaluates. If no
worker fits it in time, it prints the command and skips the prediction step.

Re-running is supported (the script drops the prior model and clears its cached
predictions first), but a locally served sidecar uses a deterministic port per
model name (`8200 + hash(model_name)`). If you re-run before the previous
sidecar has shut down, the new worker cannot bind that port. Let the prior run
finish, or stop the old worker/sidecar, before re-running.

Training defaults:

- `BIGFOOT_TRAIN_ESTIMATORS=64`
- `BIGFOOT_TRAIN_SEED=13`
- `BIGFOOT_TRAIN_WAIT=180`

### No-CLI alternative (managed training)

`08_predict_class.sql` uses the bring-your-own-worker flow (`train_model` plus a
running `rvbbit-trainer`) so it is fully reproducible from `psql`. If a standing
Warren agent is deployed, there is a SQL-only path that needs no worker command
at all — `rvbbit.train_model_managed(...)` queues the run and a `model_training`
Warren job, the agent claims it (matched to the node's labels), trains, serves,
and registers the operator, and you watch progress with `rvbbit.training_queue`
/ `rvbbit.training_status(model)`. See the Predictive Models doc and
`docs/MODELS_UNIFIED_PLAN.md` for that path.

## Scripts

| Script | Purpose |
| --- | --- |
| `00_load.sql` | Load all CSV fields and create the cleaned notebook table. |
| `01_profile.sql` | Show dataset shape and field coverage. |
| `02_retrieval.sql` | Materialize embeddings, run KNN search, and show evidence snippets. |
| `03_semantic_map.sql` | Build semantic classifications, topics, outliers, diff, dedupe, extraction. |
| `04_knowledge_graph.sql` | Build a deterministic KG from report metadata and lexical clues. |
| `06_capability_operators.sql` | Use Warren capability operators for GLiNER spans, rerank, classification, and emotion/sentiment rollups. |
| `07_live_triples_receipts.sql` | Optional live model triples and receipt/cost inspection. |
| `08_predict_class.sql` | Optional: train a tabular classifier from SQL (needs an `rvbbit-trainer` worker), then predict and evaluate on a holdout. |

Current note: `00_load.sql` uses `psql \copy` with the local path
`/home/ryanr/csv-files/bigfoot_sightings.csv`. If this demo needs to run on
another machine, update that path or run the loader from a wrapper that expands
the path before invoking `psql`.
