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
postgres -c include_if_exists=/etc/rvbbit/tuning.conf
```

Every setting below is in that file. Override an individual knob by
appending `-c key=value` after the include line — last value wins.

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
`RVBBIT_DF_THREADS` if set, otherwise from `min(num_cpus, 8)`.

| Setting | Default | Notes |
|---|---|---|
| `RVBBIT_DF_THREADS` (env var) | `min(num_cpus, 8)` | Set to `0` for single-thread. |

The 8-core cap is deliberate: each PG backend gets its own tokio pool,
so a 32-core box doesn't need 32 threads × N backends. Bump higher
only if you run few-but-large analytical queries.

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
