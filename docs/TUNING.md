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

Duck-backed routes use the external `rvbbit-duck` binary, but the core extension
does not require it. If the binary is absent, Duck candidates are skipped while
in-process DataFusion, native Rvbbit scans, and PostgreSQL rowstore fallback
remain available. The base Duck extension default is still the simple local
persistent sidecar path: no broker service is required to try or run Duck-backed
routes. The packaged uber compose opts into a shared broker by default for
bounded process count. Install the binary at `/usr/local/bin/rvbbit-duck`, set
`RVBBIT_DUCK_BIN`, or ensure `rvbbit-duck` is on the postmaster `PATH`.
`/usr/local/bin/rvbbit-duck` or `RVBBIT_DUCK_BIN` is preferred for production
because Postgres service `PATH` values vary by supervisor. A shared Unix-socket
broker is available for high concurrency deployments that need bounded sidecar
process count and memory.

Use `SELECT rvbbit.accelerator_runtime_status(false);` or the
`accelerator/runtime` row from `rvbbit.doctor(false)` to see which runtime tier is
active and what, if anything, is missing.

GPU/GQE is exposed as an experimental, disabled-by-default route candidate. Enable
selection with `rvbbit.route_gpu_gqe=on` or `RVBBIT_ROUTE_GPU_GQE=1`; install the
bridge at `/usr/local/bin/rvbbit-gqe`, set `RVBBIT_GQE_BIN`, set
`rvbbit.gqe_bin`, or put `rvbbit-gqe` on the postmaster `PATH`. The bridge is expected to implement the same
JSON/Arrow IPC sidecar protocol as `rvbbit-duck`.

The packaged Docker image installs `/usr/local/bin/rvbbit-gqe` as a lightweight
launcher/probe and `/opt/rvbbit/gqe/bin/rvbbit-gqe-bridge` as the RVBBIT-side
adapter. The adapter maps authoritative RVBBIT parquet row groups into GQE
`CREATE OR REPLACE EXTERNAL TABLE ... STORED AS PARQUET LOCATION ...` statements,
prepares the GQE catalog, and returns the same JSON/Arrow IPC sidecar contract as
`rvbbit-duck`. In server mode the `gpu_gqe` executor defaults to a persistent
Flight SQL client for SELECT queries, avoiding the old per-query `gqe-cli`
process cost. Set `RVBBIT_GQE_CLIENT_MODE=cli` to force the legacy path, or
`RVBBIT_GQE_FLIGHT_FALLBACK=0` to make Flight-client errors fail instead of
falling back to `gqe-cli`.
The ClickBench forced-GQE runner sets `RVBBIT_DUCK_BACKEND_SHARED_WORKERS=1` by
default because the suite is serial; this avoids measuring one-time GQE
Flight/catalog warmup across several independent worker states. Raise the worker
count explicitly when benchmarking concurrent shared-sidecar behavior.

The adapter still requires a real NVIDIA GQE install. By default it looks for
`/opt/gqe/rust/target/release/gqe-cli`,
`/opt/gqe/build/src/node_manager/gqe_node_manager`, and
`/opt/gqe/build/src/task_manager/gqe_task_manager`; override these with
`RVBBIT_GQE_CLI`, `RVBBIT_GQE_NODE_MANAGER`, and `RVBBIT_GQE_TASK_MANAGER`.
`RVBBIT_GQE_SERVER_URL` defaults to `http://127.0.0.1:50051`. If the server is
not reachable and the node/task manager binaries are present, the adapter
auto-starts a local node manager unless `RVBBIT_GQE_AUTO_START=off`. For remote
or separately managed GQE, set `RVBBIT_GQE_SERVER_URL` and
`RVBBIT_GQE_AUTO_START=off`.

For local ClickBench or TPC-H runs that include `rvbbit_gpu_gqe_forced`,
`run_offline.sh` automatically requests Docker GPU access when host `nvidia-smi`
detects a GPU and Docker exposes the NVIDIA runtime. Host-mounted GQE uses
`docker/docker-compose.gpu.yml`; the optional GQE image carries its own `gpus`
setting. Set `RVBBIT_GPU_GQE_COMPOSE=off` to suppress GPU compose
auto-selection, or pass `RVBBIT_REQUIRE_GPU_GQE=1` to fail instead of reporting
`SKIP`. The benchmark runners also default forced GQE to the shared
`rvbbit-duck` socket sidecar; set `RVBBIT_GQE_SHARED_BACKEND=off` to keep the
per-backend sidecar behavior.

GQE can be supplied two ways:

- Set `RVBBIT_GQE_HOME=/path/to/gqe` to mount a host-built GQE checkout/build at
  `/opt/gqe` inside `pg-rvbbit`.
- Build the optional `pg-rvbbit` GQE image with
  `docker/docker-compose.gqe-image.yml`. This extends the normal `pg-rvbbit`
  image with NVIDIA GQE's CUDA/RAPIDS build environment, source-builds libcudf
  and MLIR, builds GQE, and installs `gqe-cli` plus the node/task managers under
  `/opt/gqe`.

The GPU compose overlays set `shm_size` to `RVBBIT_GQE_SHM_SIZE` (default
`8gb`) plus `memlock`/stack ulimits. This matters for GQE because its node/task
manager uses NVSHMEM during startup; Docker's default 64 MB `/dev/shm` is too
small and can leave every forced GQE query waiting for the server startup
timeout. The benchmark runners preflight `/dev/shm`, start the GQE node
manager once, and mark `rvbbit_gpu_gqe_forced` as `SKIP` before the measured
queries if the server cannot listen. Set `RVBBIT_GQE_PREFLIGHT_START=off` to
disable that startup probe.

The overlays also default `NVSHMEM_DISABLE_CUDA_VMM=1`,
`NVSHMEM_SYMMETRIC_SIZE=6G`, and `GQE_MAX_QUERY_MEMORY=6442450944`. On the
local RTX 3090 Ti test host, default NVSHMEM CUDA VMM allocation failed during
node-manager startup; disabling CUDA VMM allowed the GQE server to listen on
`127.0.0.1:50051`. The packaged default now gives GQE a 6 GiB query pool under
the default 8 GB `/dev/shm` container setting. Keep `NVSHMEM_SYMMETRIC_SIZE` at
least as large as `GQE_MAX_QUERY_MEMORY`: with the old 512 MB symmetric heap,
2/4/6 GiB query pools failed during `pgas_memory_resource` allocation. Lower
both values if startup fails on a constrained host, or raise both plus
`RVBBIT_GQE_SHM_SIZE` on larger GPUs.

The ClickBench and TPC-H runners default `RVBBIT_GQE_PREWARM=auto`: after
loading data and refreshing layout variants, they run an explain-only GQE query
against a benchmark table (`hits` for ClickBench, `lineitem` for TPC-H) so the
GQE catalog and adapter sidecar files are ready before measured query timing.
Set `RVBBIT_GQE_PREWARM=off` when intentionally measuring cold catalog/setup
overhead.

The current GQE bridge avoids shapes this GQE/Substrait path does not execute
correctly by building a GQE-specific parquet sidecar when needed. Date columns
are exposed as ISO text, timestamp columns are exposed as ISO text plus derived
minute helpers, and text columns get derived character-length helpers. The
bridge rewrites simple `SELECT *`, `length(...)`/`character_length(...)`,
`extract(minute FROM ...)`, `date_trunc('minute', ...)`, and literal
`GROUP BY 1, ...` shapes onto those safe columns before sending SQL to GQE.
PostgreSQL regex semantics are still routed away from GQE.

The GQE route has explicit shape gates in both the Postgres router and the
GQE bridge. Simple multi-table inner/left/cross joins with explicit predicates
are allowed, but the route is rejected for right/full/natural/lateral/USING
joins, schema-qualified table references, qualified star projections,
multi-table `SELECT *`, and wide `SELECT *` + text filter + order/limit row
retrieval. The wide row retrieval gate is intentionally defensive after the
10M-row ClickBench Q23 RMM allocation failure. For controlled benchmark
experiments only, set `RVBBIT_GQE_ALLOW_RISKY_SHAPES=1` or
`SET rvbbit.gqe_allow_risky_shapes = on` to bypass these gates.

The benchmark runner controls image selection with `RVBBIT_GPU_GQE_INSTALL`:
`auto` is the default and selects the optional image only when
`rvbbit_gpu_gqe_forced` is in `BENCH_SYSTEMS`, no `RVBBIT_GQE_HOME` is set, host
`nvidia-smi` reports a GPU, Docker exposes the NVIDIA runtime, and
`RVBBIT_GPU_GQE_COMPOSE` is not disabled; `image` forces the optional image;
`off` leaves the normal image in place so the GQE path reports `SKIP`; `host`
expects `RVBBIT_GQE_HOME` to be provided. With
`--rebuild` and image mode selected, the runner first rebuilds the normal
`pg-rvbbit` image. If an existing GQE image is present, the default
`RVBBIT_GPU_GQE_REBUILD_MODE=refresh` then overlays the current extension,
`rvbbit-duck`, and `rvbbit-gqe` bridge into that image without rebuilding
libcudf, MLIR, or GQE. Set `RVBBIT_GPU_GQE_REBUILD_MODE=full` when the GPU
toolchain itself needs to be rebuilt.

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
