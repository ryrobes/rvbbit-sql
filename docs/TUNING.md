# rvbbit Postgres tuning

Vanilla Postgres ships conservative defaults from the spinning-rust era.
For analytics workloads on modern hardware those defaults leave a lot
on the table. This doc explains what rvbbit's Docker image bumps and
what to set when you run rvbbit outside the Docker image.

## What ships in the Docker image

The image at `docker/Dockerfile.rvbbit` copies
`docker/config/rvbbit-tuning.conf` to `/etc/rvbbit/tuning.conf`. The
`pg-rvbbit` service in `docker/docker-compose.yml` then starts Postgres
with:

```
postgres -c config_file=/etc/rvbbit/tuning.conf
```

This replaces the default `$PGDATA/postgresql.conf` with our tuned
file. `postgresql.auto.conf` is still read alongside, so `ALTER
SYSTEM` continues to work. Override an individual knob by appending
`-c key=value` after the config_file line — last value wins.

### Memory

| Setting | rvbbit | PG default | Why |
|---|---|---|---|
| `shared_buffers` | 1GB | 128MB | Larger PG cache for repeated reads. |
| `work_mem` | 64MB | 4MB | Per-operator hash / sort budget for analytics. |
| `maintenance_work_mem` | 256MB | 64MB | Faster ANALYZE / CREATE INDEX. |

### Planner cost model

| Setting | rvbbit | PG default | Why |
|---|---|---|---|
| `random_page_cost` | 1.1 | 4.0 | NVMe / SSD random ≈ sequential. |
| `effective_cache_size` | 4GB | 4GB | Make the planner aware of OS page cache. |
| `default_statistics_target` | 200 | 100 | Better plans on skewed analytics data. |

### Parallel query

Vanilla PG pools are sized for OLTP. rvbbit's CustomScan is not
parallel-aware yet (Phase 4.2 — see [LAKEHOUSE.md](LAKEHOUSE.md)), so
these knobs do not directly speed up rvbbit's columnar reads. They
still matter for:

- heap-side queries (un-compacted tables, hybrid workloads)
- PG's post-CustomScan operators (sort, group, gather)
- any other extension sharing the worker pool

| Setting | rvbbit | PG default |
|---|---|---|
| `max_worker_processes` | 32 | 8 |
| `max_parallel_workers` | 16 | 8 |
| `max_parallel_workers_per_gather` | 4 | 2 |
| `max_parallel_maintenance_workers` | 4 | 2 |

### Async I/O (PG18+)

PG18 introduced AIO with two backends: `worker` (always available) and
`io_uring` (Linux kernels with io_uring support). rvbbit ships
`io_method = worker` as the safe default and raises the pool size from
PG's `io_workers = 3` to 16. Cold-tier reads (object store) benefit
the most because they're latency-bound — more in-flight reads helps.

| Setting | rvbbit | PG default | Why |
|---|---|---|---|
| `io_method` | `worker` | `worker` | Explicit. Switch to `io_uring` for lower overhead. |
| `io_workers` | 16 | 3 | More in-flight reads on NVMe / object store. |
| `effective_io_concurrency` | 64 | 1 | PG default assumes spinning disk. |
| `maintenance_io_concurrency` | 16 | 10 | Higher concurrency on ANALYZE / VACUUM. |

## DataFusion worker threads

The in-process DataFusion route (`SET rvbbit.df_inprocess = on`, on by
default) runs on a per-backend tokio runtime. Sizing comes from
`RVBBIT_DF_THREADS` if set, otherwise from `min(num_cpus, 8)`. The same
value is also passed to DataFusion as `target_partitions`, so parquet
planning and execution partitioning track the runtime size instead of
using DataFusion's generic default.

| Setting | Default | Notes |
|---|---|---|
| `RVBBIT_DF_THREADS` (env var) | `min(num_cpus, 8)` | Set to `0` for single-thread. |

The 8-core cap is deliberate: each PG backend gets its own tokio pool,
so a 32-core box doesn't need 32 threads × N backends. Bump higher
only if you run few-but-large analytical queries.

## Hot columnar objects

Rvbbit can manually pin compacted Rvbbit tables as decoded Arrow batches for
the in-process DataFusion route. This is a per-backend cache with catalog
intent in `rvbbit.hot_objects`: one backend loads the table with SQL, and other
backends lazily materialize their own copy when the router chooses
`datafusion_mem`.

```sql
SELECT rvbbit.hot_load('hits'::regclass);
SELECT jsonb_pretty(rvbbit.hot_status());
SELECT rvbbit.hot_evict('hits'::regclass);
```

The first version is intentionally manual. `datafusion_mem` is a first-class
candidate for forced routing and SQL-native route training, but no-profile
routing does not prefer it by default. ClickBench showed that decoded memory
can lose to native rewrites and regular DataFusion scans on enough shapes that
training/profile evidence is the safer default. Projection loads via
`rvbbit.hot_load_columns(...)` are available for direct `rvbbit.df_hot_query`
experiments, but the automatic router currently requires all columns so it
cannot misroute queries that reference unloaded columns.

| Setting | Default | Notes |
|---|---|---|
| `rvbbit.hot_store_budget_mb` / `RVBBIT_HOT_STORE_BUDGET_MB` | `512` | Per-backend decoded Arrow cache budget. `0` disables loading and routing. |
| `rvbbit.hot_store_route_max_rows` / `RVBBIT_HOT_STORE_ROUTE_MAX_ROWS` | `500000` | Router ceiling for automatic `datafusion_mem` selection. |
| `RVBBIT_ROUTE_DATAFUSION_MEM` | `on` | Set to `0`/`off` to remove the memory candidate. |
| `rvbbit.route_datafusion_mem_no_profile` / `RVBBIT_ROUTE_DATAFUSION_MEM_NO_PROFILE` | `off` | Opt into no-profile hot-object preference. Leave off for benchmark-like mixed workloads. |

## Native row-group worker threads

Some native CustomScan fast paths can split independent row-group work
inside a single PostgreSQL backend without using PG parallel scans. Today
this is used for dictionary-backed text top-count paths: dictionaries and
catalog state are loaded by the leader backend, then code counting and
projected parquet reads are divided across scoped worker threads.

| Setting | Default | Notes |
|---|---|---|
| `RVBBIT_NATIVE_THREADS` (env var) | `min(num_cpus, 8)` | Clamped to row-group count. Set to `1` for serial execution. |

This knob is intentionally separate from `RVBBIT_DF_THREADS`: DataFusion
queries and native CustomScan queries are different execution paths.
Keep both values conservative on installations with many concurrent PG
backends, because the limits are per backend.

## Duck/Vortex sidecar modes

Duck-backed routes use the external `rvbbit-duck` binary. The default is still
the simple local persistent sidecar path: no broker service is required to try
or run the extension. Install the binary at `/usr/local/bin/rvbbit-duck`, set
`RVBBIT_DUCK_BIN`, or ensure `rvbbit-duck` is on the postmaster `PATH`.
`/usr/local/bin/rvbbit-duck` or `RVBBIT_DUCK_BIN` is preferred for production
because Postgres service `PATH` values vary by supervisor. A shared Unix-socket
broker is available for high concurrency deployments that need bounded sidecar
process count and memory.

See [DUCK_SIDECAR.md](DUCK_SIDECAR.md) for the full deployment model, fallback
semantics, production caveats, and load-test commands.

## Parquet writer knobs

Set as env vars on the `pg-rvbbit` container (env vars are read on each
`compact()` call, so they apply to all writes from that backend):

| Env var | Default | Effect |
|---|---|---|
| `RVBBIT_PARQUET_V2` | `on` | Parquet 2.0 writer (V2 pages, DELTA_BYTE_ARRAY). |
| `RVBBIT_PARQUET_BLOOM` | `on` | Bloom filters on text/binary columns only. |
| `RVBBIT_PARQUET_BLOOM_FPP` | `0.01` | Bloom false-positive rate. |
| `RVBBIT_PARQUET_PAGE_ROWS` | `5000` | Data-page row count limit. |

See `crates/rvbbit_storage/src/row_group.rs` for the implementation
and `docs/LAKEHOUSE.md` for the operator-facing story.

## What about pg-heap?

The `pg-heap` service in the same Docker Compose file gets the same
parallel-query, AIO, and cost-model tuning as `pg-rvbbit`, minus
`shared_preload_libraries` and the rvbbit-specific env vars. The
baseline isn't held back by vanilla defaults — when you compare
heap vs rvbbit numbers you're seeing the engine difference, not the
config difference.

## Running outside Docker

If you install pg_rvbbit on a host Postgres, copy
`docker/config/rvbbit-tuning.conf` to
`$(pg_config --sysconfdir)/conf.d/` (or any path PG can read), then
either:

- include it from `postgresql.conf`:
  ```
  include_if_exists = '/path/to/rvbbit-tuning.conf'
  ```
- or merge the settings into `postgresql.conf` directly.

Then export the rvbbit env vars (`RVBBIT_*`) in the postmaster's
environment — they're not Postgres GUCs.

## Future: parallel-aware CustomScan

The PG knobs above leave headroom for rvbbit's planned parallel-aware
CustomScan (Phase 4.2). When that lands, row groups will be partitioned
across workers and gathered at the top — and the `max_parallel_workers`
and `io_workers` pools that look unused today become the lever for
real parallel scan.
