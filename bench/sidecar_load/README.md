# Rvbbit Sidecar Load Harness

This harness stress-tests the normal SQL path into the Duck/Vortex sidecar.
It is not a competitor benchmark; it is an operational check for concurrency,
latency tails, sidecar process fanout, and failure behavior.

## Default Run

```bash
./bench/sidecar_load/run_offline.sh
```

Defaults:

- candidate: `duck_vortex`
- clients: `1,2,4,8,16`
- warmup: `5s`
- measured duration per client count: `30s`
- queries: ClickBench `Q5,Q12,Q16,Q25,Q29,Q30,Q32,Q40,Q41,Q42`

The wrapper runs the Python client harness inside the `bench` container and
samples `rvbbit-duck` process count/RSS from the `pg-rvbbit` container.
The `SIDECAR_LOAD_*` environment variables shown below are forwarded into the
bench container.

## Useful Variants

```bash
SIDECAR_LOAD_CLIENTS=1,4,8,16,32 \
SIDECAR_LOAD_DURATION_S=60 \
./bench/sidecar_load/run_offline.sh
```

```bash
SIDECAR_LOAD_DUCK_THREADS=1 \
SIDECAR_LOAD_CLIENTS=1,2,4,8,16,32 \
./bench/sidecar_load/run_offline.sh
```

```bash
SIDECAR_LOAD_QUERIES=Q25,Q29,Q32 \
./bench/sidecar_load/run_offline.sh
```

```bash
./bench/sidecar_load/run_offline.sh --candidate duck_vector
```

```bash
./bench/sidecar_load/run_offline.sh --candidate auto --allow-fallback
```

## Preconditions

The selected route must be available. For the default `duck_vortex` run,
ClickBench data must be loaded into `public.hits`, and `vortex_scan` layout
variants must be ready in `rvbbit.layout_variant_status`.

The harness fails preflight if `rvbbit.route_explain()` does not choose the
forced candidate for every selected query. Use `--allow-fallback` only when
you intentionally want to measure fallback behavior.

## Output

The run writes:

- `bench/sidecar_load/results/<run_id>.json`
- `bench/sidecar_load/results/<run_id>_processes.jsonl`

The JSON contains per-client-count latency summaries, per-query summaries,
route preflight details, and PostgreSQL activity samples. The JSONL process
file contains host-wrapper samples of `rvbbit-duck` process count and RSS.
