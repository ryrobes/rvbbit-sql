# Real-World Acceptance Harness

`bench/e2e_realworld.py` is the broad end-to-end smoke harness for checking that
an Rvbbit install behaves like a usable system, not just a set of isolated unit
tests. It is deterministic by default and writes enough run data to debug
failures after the fact.

## Run Modes

```bash
make e2e-realworld
```

Starts the core stack plus deterministic sidecars, reloads the extension
non-destructively, and runs the harness.

```bash
make e2e-realworld-fresh
```

Deletes Docker volumes first. Use this before release-style install checks.

```bash
make e2e-realworld-live
```

Runs the same harness with `RVBBIT_E2E_LIVE_LLM=1`, allowing paid/provider model
calls such as `rvbbit.summarize`.

The deterministic run also includes diagnostics and a local
OpenAI-compatible provider smoke. It runs `rvbbit.doctor(false)`, registers a
temporary self-hosted chat backend, records provider/model/cost metadata, runs
`rvbbit.provider_doctor(true)`, calls the backend through a normal SQL
operator, and verifies the free cost receipt path.

```bash
make e2e-realworld-warren
```

Runs the heavier Warren deployment smoke: queue
`capabilities/manifests/smoke/warren-echo.yaml`, run `warren-agent --once` on the
host, build/start the generated sidecar container, register its backend and SQL
operator, probe the backend, and call `rvbbit.warren_smoke_echo(...)`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `RVBBIT_DSN` | `postgresql://postgres:rvbbit@pg-rvbbit:5432/bench` | Target database. |
| `RVBBIT_E2E_OUT_ROOT` | `/results/e2e` | Artifact root inside the bench container. |
| `RVBBIT_E2E_LIVE_LLM` | off | Enables live provider calls. |
| `RVBBIT_E2E_LIVE_ROWS` | `3` | Number of rows summarized in the live semantic SQL phase. |
| `RVBBIT_E2E_OPENAI_MODEL` | `gpt-5.4-mini` | Direct OpenAI smoke-test model. |
| `RVBBIT_E2E_ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Direct Anthropic smoke-test model. |
| `RVBBIT_E2E_GEMINI_MODEL` | `gemini-2.5-flash-lite` | Direct Gemini smoke-test model. |
| `RVBBIT_E2E_BIGFOOT_ROWS` | `25` | Max rows imported from the optional Bigfoot CSV sample. |
| `RVBBIT_E2E_SEMANTIC_STRESS_ROWS` | `500` | Rows used by the deterministic high-volume semantic scalar stress phase. The query makes two semantic calls per row. |
| `RVBBIT_E2E_KEEP_OBJECTS` | off | Keeps temporary SQL objects for manual inspection. |
| `RVBBIT_E2E_ECHO_BASE` | `http://rvbbit-echo:8080` | Deterministic echo sidecar URL. |

## What It Covers

The current harness checks these user-facing surfaces:

| Phase | Coverage |
|---|---|
| `catalog` | Extension presence, core catalog tables, observability views. |
| `imports` | `COPY FROM STDIN` into an Rvbbit table, compaction/export, aggregates, optional real Bigfoot CSV sample import, and full-column Bigfoot locations import for tabular model tests. |
| `storage` | `USING rvbbit` table creation, insert/update/delete, Parquet export, row-group metadata. |
| `persistence` | Non-destructive `ALTER EXTENSION ... UPDATE` reload preserving an Rvbbit table, backend/operator catalog row, and KG evidence; scoped `pg_dump`/`psql` restore of an Rvbbit table followed by Parquet metadata rebuild. |
| `routing` | `rvbbit.route_status()`, `rvbbit.route_explain(...)`, synthetic route observations, SQL profile training, profile override, and `route_eval(...)`. |
| `ml` | SQL-backed model training lifecycle: `rvbbit.train_model`, external `rvbbit-trainer`, generated tabular sidecar, `rvbbit.complete_model_training`, `ml_model_status`, and generated `predict_*` SQL operator. |
| `semantic` | Dynamic backend/operator registration, implicit prewarm with `WHERE`/`ORDER BY`/`LIMIT`, explicit batch prewarm, cache-hit behavior, high-volume scalar batching/receipt stress, and intentional backend-failure receipt audit. |
| `embeddings` | Stub embedding backend, `rvbbit.embed`, materialized embedding cache, `rvbbit.knn_text`, and optional default local CPU `embed` backend smoke coverage when it is registered. |
| `mcp` | MCP server registration, tool refresh, `rvbbit.mcp_call`, `rvbbit.mcp_rows`, invocation `query_id` audit, intentional tool-error audit, MCP-as-operator flow, and an `mcp -> code -> specialist` operator chain with receipt sub-call audit. |
| `kg` | Deterministic triples JSON, `rvbbit.kg_ingest_triples`, graph traversal, evidence `query_id`, plus a deterministic KG built from imported free-form text with context/evidence traversal. |
| `warren` | Warren node catalog, metrics ingest, capability deploy job, job claim, inventory view. The separate `make e2e-realworld-warren` target also runs the host-Docker sidecar lifecycle. |
| `costs` | Receipt cost backfill and audit views. |
| `diagnostics` | `rvbbit.doctor(false)` plus self-hosted OpenAI-compatible backend registration, live provider doctor probe, default-provider routing, and cost receipt verification. |
| `live_llm` | Optional paid/provider SQL calls: multi-row `rvbbit.summarize(...)`, direct OpenAI, Anthropic, Gemini API-key, and Gemini ADC provider routing, receipt/cost audit checks, and live `rvbbit.triples_rows(...)`. |

Optional phases log as `skip` instead of failing when their sidecar or provider
is intentionally absent.

The high-volume semantic stress phase is deterministic and uses the echo sidecar,
not a paid provider. With the default settings it runs one query over 500 rows
with two scalar semantic calls per row, then verifies that every fresh call wrote
exactly one receipt with the query id for that SQL statement and exactly one cost
audit event. Live provider cost settlement is still covered separately by
`make e2e-realworld-live`.

The default Python harness does not start Docker-managed Warren sidecars. Use
`make e2e-realworld-warren` when you want the full deployment path because that
must run `warren-agent` from the host with access to Docker.

## Artifacts

Each run gets a directory under `results/e2e/`:

```text
results/e2e/e2e_YYYYMMDDTHHMMSSZ_<id>/
  events.jsonl
  summary.json
  report.md
```

The database also receives persistent run rows:

```sql
SELECT *
FROM rvbbit_e2e.runs
ORDER BY started_at DESC;

SELECT phase, step, status, duration_ms, details, error
FROM rvbbit_e2e.events
WHERE run_id = '<run id>'
ORDER BY seq;
```

This is intentionally separate from the extension schemas so acceptance history
can survive extension reloads and can be visualized later.

## How To Extend

Add a new `phase_*` function in `bench/e2e_realworld.py`, wrap each assertion in
`with h.step("phase", "step")`, and attach structured data to the yielded
`details` dictionary. Use `optional=True` or raise `SkipStep` for external
services that are not required in hermetic mode.

Good additions should prefer:

- deterministic inputs and exact assertions when possible;
- live/provider checks only behind `RVBBIT_E2E_LIVE_LLM=1`;
- one artifact trail in JSONL plus one SQL row per meaningful step;
- non-destructive defaults, with destructive behavior only via
  `make e2e-realworld-fresh`.
