# Duck/Vortex Sidecar Modes

Rvbbit can route selected analytical SQL to DuckDB over Parquet/Vortex
accelerator files. The Duck executor lives in the `rvbbit-duck` binary, outside
the PostgreSQL backend process. This document explains how that binary is used,
what happens when no shared broker is running, and how to deploy the shared
worker mode without making the extension hard to try.

## Short Version

You do not need a broker service to use Rvbbit.

You also do not need `rvbbit-duck` for the core extension to load and run. If
the binary is absent, Duck-backed routes are skipped and Rvbbit falls back to
in-process DataFusion, native Rvbbit scans, or PostgreSQL rowstore paths when
those routes are available. Install `rvbbit-duck` when you want DuckDB execution
or the shared broker mode.

For the default "try Duck-backed acceleration" path, install the `rvbbit-duck`
binary on the PostgreSQL server. The recommended location is:

```text
/usr/local/bin/rvbbit-duck
```

The extension resolves the binary in this order:

1. `RVBBIT_DUCK_BIN`, if set in the postmaster environment.
2. `/usr/local/bin/rvbbit-duck`.
3. `rvbbit-duck` found on the postmaster `PATH`.

Use `/usr/local/bin/rvbbit-duck` or `RVBBIT_DUCK_BIN` for production. `PATH`
lookup is a convenience fallback, but Postgres service managers often run with
a minimal or surprising `PATH`.

Check the detected runtime tier with:

```sql
SELECT rvbbit.accelerator_runtime_status(false);
SELECT * FROM rvbbit.doctor(false) WHERE area = 'accelerator';
```

The default path is intentionally simple:

```text
PostgreSQL backend
  -> starts/reuses its own local rvbbit-duck child process when needed
  -> child exits when the PostgreSQL backend exits
```

The optional high-concurrency path is:

```text
PostgreSQL backends
  -> connect to a supervised Unix socket
rvbbit-duck broker
  -> fixed worker pool
  -> bounded DuckDB memory / PG connections
```

If shared mode is enabled but the broker socket is missing, Rvbbit currently
falls back to the old local sidecar path and emits a warning. That preserves
availability and keeps "try the extension" easy. Production deployments that
need hard memory limits should monitor the broker and consider disabling the
Duck route or adding a future strict mode instead of silently allowing fallback.

## Modes

### 1. Local Persistent Sidecar

This is the default operational model.

```text
rvbbit.duck_backend_shared = off
rvbbit.duck_backend_persistent = on
```

Behavior:

- No broker service is required.
- Each PostgreSQL backend that needs Duck starts one `rvbbit-duck --serve`
  child process.
- That child keeps DuckDB catalog/view state warm for that PostgreSQL backend.
- The child is killed when the PostgreSQL backend exits.
- This is simple and fast for single-user, benchmark, notebook, and moderate
  concurrency use.

Tradeoff:

- At high concurrency, N active PostgreSQL backends can mean N `rvbbit-duck`
  child processes.
- Each process can have its own DuckDB memory footprint and PG metadata/safety
  connection.

### 2. Local One-Shot Sidecar

```text
rvbbit.duck_backend_shared = off
rvbbit.duck_backend_persistent = off
```

Behavior:

- No broker service is required.
- Every Duck query launches `rvbbit-duck` once and waits for it to exit.
- This is mostly useful for debugging process lifetime issues.

Tradeoff:

- More process startup overhead.
- Catalog/view setup is repeated for each call.

### 3. Shared Broker

```text
rvbbit.duck_backend_shared = on
rvbbit.duck_backend_shared_socket = '/run/rvbbit/duck-vortex.sock'
rvbbit.duck_backend_shared_workers = 4
rvbbit.duck_threads = 4
```

Behavior:

- PostgreSQL backends connect to a Unix socket.
- A separately supervised `rvbbit-duck` broker owns a fixed worker pool.
- Each worker keeps its own DuckDB connection/catalog warm.
- Worker count bounds simultaneous Duck executions and sidecar PG connections.

Tradeoff:

- Requires a managed process in production.
- Too few workers can increase queueing/median latency.
- Too many workers can recreate the original CPU/RSS pressure.

In the 5M ClickBench sidecar load test, `4 workers x 4 Duck threads` was the
best quick setting:

```text
per-backend local sidecars:
  16 clients: 16.01 qps, p95 2.66s, RSS max ~10.8 GiB
  32 clients: 15.08 qps, p95 5.22s

shared broker, 4 workers x 4 Duck threads:
  16 clients: 18.70 qps, p95 1.23s, RSS max ~2.1 GiB
  32 clients: 17.40 qps, p95 2.28s
```

Those numbers are workload-specific, but the shape is the point: the broker can
turn many sidecar processes into one process with a bounded worker pool.

## Fallback Behavior

When `rvbbit.duck_backend_shared = on`, the extension first tries the shared
socket. If the socket connect fails, the current behavior is:

```text
shared socket connect fails
  -> WARNING
  -> fallback to local persistent sidecar if persistent mode is on
  -> fallback to local one-shot sidecar if persistent mode is off
```

Example warning:

```text
WARNING: rvbbit.duck_query_json: shared rvbbit-duck failed
(connecting to shared rvbbit-duck /tmp/rvbbit-duck/missing.sock:
No such file or directory); falling back to per-backend sidecar
```

This fallback is deliberate for v1 ergonomics. A missing broker should not make
a first-time install feel broken.

Production operators who care more about resource ceilings than availability
should treat repeated warnings as an operational fault, or disable local
sidecar fallback from shared-broker routes:

```text
rvbbit.duck_backend_shared_strict = on
```

Strict mode preserves the normal default when off. When on, a shared broker
connection failure does not start a local persistent or one-shot sidecar. If
`rvbbit.duck_backend_fail_open` remains on, the query can still fall back to
native PostgreSQL/Rvbbit execution; set fail-open off as well if a missing
broker should hard-error the query.

## Why Postgres Should Not Launch The Broker

Do not use a PostgreSQL backend as the supervisor for a long-lived shared
broker.

During testing, killing a broker process that had been launched by a PostgreSQL
backend caused Postgres to log:

```text
untracked child process (...) was terminated
terminating any other active server processes
all server processes terminated; reinitializing
```

That is not an acceptable production shape.

The extension therefore does not auto-launch a shared broker by default.

This escape hatch exists only for throwaway development experiments:

```text
rvbbit.duck_backend_shared_launch = on
```

Do not turn that on in production.

## Production Deployment

Run `rvbbit-duck` under an external supervisor:

- Docker Compose service
- Kubernetes sidecar container
- systemd unit
- process manager owned by the host/database package

The broker needs:

- a Unix socket path visible to PostgreSQL backends;
- read access to accelerator files using the same path mapping that
  `rvbbit.row_groups.path` expects;
- a DSN that can connect back to the database for metadata/safety checks;
- enough PG `max_connections` headroom for broker workers;
- a restart policy and health check.

The packaged uber compose file follows this model by default: it starts a
`duck` service from the same Postgres image, enables
`RVBBIT_DUCK_BACKEND_SHARED=true` in Postgres, shares `/run/rvbbit/duck` for the
Unix socket, and shares `/tmp/rvbbit-arrow-ipc` for fast Arrow IPC result files.
The broker uses `--serve-derived-socket`, which computes the same engine/layout
socket path that the extension derives. That avoids a global socket override:
the default uber broker serves `duck` + `vortex`, while any other Duck layout can
fall back to the normal local persistent sidecar path if no matching broker is
running. `RVBBIT_DUCK_BACKEND_SHARED_TARGETS=duck:vortex` is set in the
Postgres service so non-vortex Duck routes skip the shared-broker attempt
instead of logging a missing-socket fallback first.

### Broker Command

Example command inside the same container namespace as Postgres:

```bash
rvbbit-duck \
  --serve-socket /run/rvbbit/duck-vortex.sock \
  --workers 4 \
  --engine duck \
  --layout vortex \
  --dsn "host=/var/run/postgresql dbname=bench user=postgres application_name=rvbbit-duck-sidecar" \
  --threads 4 \
  --pgdata-prefix /var/lib/postgresql/18/docker \
  --visible-pgdata-prefix /var/lib/postgresql/18/docker
```

When the broker runs beside the extension and should use the extension's
derived socket path instead of an explicit global socket override, use:

```bash
rvbbit-duck \
  --serve-derived-socket \
  --workers 4 \
  --engine duck \
  --layout vortex \
  --dsn "postgresql://postgres:rvbbit@postgres:5432/rvbbit" \
  --threads 4 \
  --pgdata-prefix /var/lib/postgresql \
  --visible-pgdata-prefix /var/lib/postgresql
```

`--pgdata-prefix` is the path root stored in Rvbbit metadata. `--visible-pgdata-prefix`
is the path root visible from the broker process. They are the same when the
broker shares Postgres' filesystem view. They differ when the broker runs in a
separate container with a different mount point.

### PostgreSQL Session/Role Settings

For manual testing:

```sql
SET rvbbit.duck_backend_shared = on;
SET rvbbit.duck_backend_shared_socket = '/run/rvbbit/duck-vortex.sock';
SET rvbbit.duck_backend_shared_workers = 4;
SET rvbbit.duck_threads = 4;
```

For a database/role-level default:

```sql
ALTER DATABASE mydb SET rvbbit.duck_backend_shared = on;
ALTER DATABASE mydb SET rvbbit.duck_backend_shared_socket = '/run/rvbbit/duck-vortex.sock';
ALTER DATABASE mydb SET rvbbit.duck_backend_shared_workers = 4;
ALTER DATABASE mydb SET rvbbit.duck_threads = 4;
```

### Docker Compose Shape

The benchmark wrapper starts a broker with `docker exec -d` for convenience.
Production Compose should make it a real service/profile instead:

```yaml
services:
  pg-rvbbit:
    # normal PostgreSQL + pg_rvbbit service
    volumes:
      - rvbbit_run:/run/rvbbit
      - rvbbit_data:/var/lib/postgresql

  rvbbit-duck-vortex:
    image: your-rvbbit-image
    command:
      - rvbbit-duck
      - --serve-socket
      - /run/rvbbit/duck-vortex.sock
      - --workers
      - "4"
      - --engine
      - duck
      - --layout
      - vortex
      - --dsn
      - "host=/var/run/postgresql dbname=bench user=postgres application_name=rvbbit-duck-sidecar"
      - --threads
      - "4"
      - --pgdata-prefix
      - /var/lib/postgresql/18/docker
      - --visible-pgdata-prefix
      - /var/lib/postgresql/18/docker
    volumes:
      - rvbbit_run:/run/rvbbit
      - rvbbit_data:/var/lib/postgresql:ro
    restart: unless-stopped

volumes:
  rvbbit_run:
  rvbbit_data:
```

Exact mounts depend on how Postgres and accelerator files are packaged. The
important part is that PostgreSQL and the broker agree on the socket path and
the broker can read accelerator files.

### Kubernetes Shape

Use a same-pod sidecar when possible:

- `emptyDir` mounted at `/run/rvbbit` for the Unix socket.
- read-only volume mount for accelerator files.
- broker container runs `rvbbit-duck --serve-socket ...`.
- PostgreSQL container sets `rvbbit.duck_backend_shared_socket` to the same
  socket path.

If the broker is in a different pod, Unix sockets are not available. Do not
switch this to TCP casually; the protocol is currently local-trust JSONL over a
Unix socket. A TCP broker would need authentication and transport hardening.

## Configuration Reference

### Extension GUCs / Environment

| Setting | Default | Purpose |
|---|---:|---|
| `RVBBIT_DUCK_BIN` | `/usr/local/bin/rvbbit-duck`, then `PATH` | Binary used for local sidecars and broker launch experiments. |
| `rvbbit.duck_backend` / `RVBBIT_DUCK_BACKEND` | `on` | Enables Duck sidecar routes. |
| `rvbbit.duck_backend_persistent` / `RVBBIT_DUCK_BACKEND_PERSISTENT` | `on` | Reuse one local sidecar per PG backend. |
| `rvbbit.duck_backend_shared` / `RVBBIT_DUCK_BACKEND_SHARED` | `off` | Use a shared broker socket first. |
| `rvbbit.duck_backend_shared_strict` / `RVBBIT_DUCK_BACKEND_SHARED_STRICT` | `off` | If shared broker mode is enabled, do not fall back to per-backend sidecars when the broker is unavailable. |
| `rvbbit.duck_backend_shared_targets` / `RVBBIT_DUCK_BACKEND_SHARED_TARGETS` | all | Comma-separated shared broker targets such as `duck:vortex`; entries may also be just an engine or layout. Non-matching routes use the local sidecar path directly. |
| `rvbbit.duck_backend_shared_socket` / `RVBBIT_DUCK_BACKEND_SHARED_SOCKET` | derived path | Explicit broker socket path. |
| `rvbbit.duck_backend_shared_dir` / `RVBBIT_DUCK_BACKEND_SHARED_DIR` | `/tmp/rvbbit-duck` | Directory for derived socket paths. |
| `rvbbit.duck_backend_shared_workers` / `RVBBIT_DUCK_BACKEND_SHARED_WORKERS` | `4` | Expected broker worker count; part of derived socket identity. |
| `rvbbit.duck_backend_shared_launch` / `RVBBIT_DUCK_BACKEND_SHARED_LAUNCH` | `off` | Unsafe/dev-only backend auto-launch of broker. |
| `RVBBIT_DUCK_BROKER_QUEUE` | `1024` | Maximum queued shared-broker requests before new socket clients get a structured fallback response. |
| `RVBBIT_DUCK_MAX_REQUEST_BYTES` | `16777216` | Maximum JSONL request line size accepted by local persistent sidecars and shared brokers. |
| `RVBBIT_DUCK_SOCKET_IO_TIMEOUT_S` | `30` | Shared-broker socket read/write timeout for idle or abandoned client connections. |
| `rvbbit.duck_threads` / `RVBBIT_DUCK_THREADS` | `4` | DuckDB threads per worker/local sidecar. |
| `rvbbit.duck_arrow_ipc` / `RVBBIT_DUCK_ARROW_IPC` | `on` | Use Arrow IPC file transport for sidecar results. |
| `rvbbit.duck_arrow_ipc_fallback` / `RVBBIT_DUCK_ARROW_IPC_FALLBACK` | `on` | Retry JSON transport if Arrow IPC decode fails. |
| `rvbbit.duck_backend_fail_open` / `RVBBIT_DUCK_BACKEND_FAIL_OPEN` | `on` | Non-Vortex failures can fall back to native execution. |
| `RVBBIT_DUCK_BACKEND_TIMEOUT_S` | `300` | Query timeout sent to `rvbbit-duck`; local persistent reads, one-shot child processes, and shared-socket reads are aborted after this value plus a short grace window. |
| `RVBBIT_NODE_ID` / `RVBBIT_DUCK_NODE_ID` | hostname | Logical node identity recorded in sidecar telemetry. |
| `RVBBIT_DUCK_TELEMETRY` | `on` | Enables best-effort sidecar telemetry writes. |
| `RVBBIT_DUCK_TELEMETRY_QUEUE` | `8192` | Bounded sidecar telemetry event queue. |
| `RVBBIT_DUCK_TELEMETRY_BATCH` | `64` | Query events written per telemetry batch. |
| `RVBBIT_DUCK_TELEMETRY_FLUSH_MS` | `250` | Maximum query-event flush delay. |
| `RVBBIT_DUCK_TELEMETRY_HEARTBEAT_MS` | `5000` | Broker/local sidecar heartbeat cadence. |

### Broker Flags

| Flag | Purpose |
|---|---|
| `--serve-socket PATH` | Run broker over Unix socket. |
| `--workers N` | Number of persistent worker states. |
| `--engine duck\|datafusion` | Engine to expose. |
| `--layout scan\|hive\|vortex` | Accelerator layout to expose. |
| `--dsn DSN` | PostgreSQL DSN for metadata/safety checks. |
| `--threads N` | DuckDB/DataFusion threads per worker. |
| `--pgdata-prefix PATH` | Path prefix stored in Rvbbit metadata. |
| `--visible-pgdata-prefix PATH` | Path prefix visible to the broker. |

The shared broker also honors environment-only guardrails:
`RVBBIT_DUCK_BROKER_QUEUE`, `RVBBIT_DUCK_MAX_REQUEST_BYTES`, and
`RVBBIT_DUCK_SOCKET_IO_TIMEOUT_S`.

## SQL Observability

`rvbbit-duck` writes low-overhead, best-effort telemetry back into the Rvbbit
schema. The writer is async for persistent sidecars and shared brokers: query
execution enqueues compact events into a bounded in-process queue, while a
background telemetry connection writes batches and heartbeats.

For the UI-facing table contract, field semantics, polling recipes, and
dashboard layout guidance, see [RVBBIT_DUCK_UI_CONTRACT.md](RVBBIT_DUCK_UI_CONTRACT.md).

The tables are intentionally node-aware:

```text
hostname  = physical/container host name
node_id   = logical node name, from RVBBIT_NODE_ID or RVBBIT_DUCK_NODE_ID
```

That lets a UI group today by one local sidecar and later by multiple broker
nodes reading shared Parquet/Vortex storage.

### Tables and Views

| Object | Purpose |
|---|---|
| `rvbbit.duck_sidecar_instances` | One row per sidecar/broker process instance. |
| `rvbbit.duck_sidecar_heartbeats` | Periodic liveness, RSS, queue depth, active workers, and telemetry counters. |
| `rvbbit.duck_sidecar_query_events` | Per-query sidecar execution events with timings, rows, status, cache metadata, and table summaries. |
| `rvbbit.duck_sidecar_fallback_events` | Extension-side records when shared broker mode falls back to local sidecars. |
| `rvbbit.duck_sidecar_latest` | Latest instance state joined to its newest heartbeat. |
| `rvbbit.duck_sidecar_query_summary` | Minute-level rollup by host/node/mode/engine/layout/status. |

Example UI queries:

```sql
SELECT *
FROM rvbbit.duck_sidecar_latest
ORDER BY node_id, mode, engine, layout;

SELECT minute, node_id, mode, engine, layout, status, calls,
       round(p50_elapsed_ms::numeric, 1) AS p50_ms,
       round(p95_elapsed_ms::numeric, 1) AS p95_ms
FROM rvbbit.duck_sidecar_query_summary
ORDER BY minute DESC, calls DESC
LIMIT 50;

SELECT observed_at, node_id, engine, layout, fallback_mode, reason
FROM rvbbit.duck_sidecar_fallback_events
ORDER BY observed_at DESC
LIMIT 20;
```

Raw SQL text is not stored. Query events store `query_hash` so the UI can group
repeated shapes without copying user query text into telemetry tables. Cache and
table details are stored as JSONB for inspection without schema churn.

### Telemetry Cost Model

Persistent/shared modes use:

```text
one telemetry writer thread per rvbbit-duck process
one extra PostgreSQL connection named rvbbit-duck-telemetry
bounded in-process queue; overflow drops telemetry, not queries
```

Local one-shot mode writes telemetry synchronously at process exit because the
process is about to disappear. That path is already the slow/debug path, so the
extra write is acceptable and keeps observations complete.

Telemetry is best-effort by design. If the extension schema has not been
upgraded or the telemetry connection cannot write, queries still run.

## Sizing

Start conservative:

```text
workers = 4
duck_threads = 4
```

Then benchmark with your own workload. The product `workers * duck_threads` is
a rough ceiling on Duck execution parallelism inside the broker. More is not
always better: the 5M ClickBench load sweep showed `4x4` beating `6x4`, `8x4`,
and `8x2` for the tested query mix.

Watch:

- broker RSS;
- `pg_stat_activity` rows where `application_name = 'rvbbit-duck-sidecar'`;
- p95/p99 query latency;
- warning logs for shared broker fallback;
- CPU saturation and IO wait.

`max_connections` must leave room for:

```text
application clients
+ benchmark/admin connections
+ broker workers
+ Postgres reserved connections
```

The Rvbbit Docker image currently sets `max_connections = 300` for benchmark
headroom.

## Testing

### Confirm Default Local Mode

No broker required:

```sql
SET rvbbit.route_force_candidate = 'duck_vortex';
SET rvbbit.duck_backend_shared = off;
SET rvbbit.duck_threads = 4;

SELECT COUNT(DISTINCT "SearchPhrase") FROM hits;
```

Expected:

- query succeeds;
- no shared broker process is required.

### Confirm Missing Broker Fallback

```sql
SET rvbbit.route_force_candidate = 'duck_vortex';
SET rvbbit.duck_backend_shared = on;
SET rvbbit.duck_backend_shared_socket = '/tmp/rvbbit-duck/missing.sock';
SET rvbbit.duck_threads = 4;

SELECT COUNT(DISTINCT "SearchPhrase") FROM hits;
```

Expected:

- query succeeds;
- warning says shared broker connect failed;
- execution falls back to local per-backend sidecar.

Observed smoke-test result on 5M ClickBench:

```text
610809
```

### Confirm Shared Broker Path

Start a broker:

```bash
rvbbit-duck \
  --serve-socket /tmp/rvbbit-duck/doc-test.sock \
  --workers 2 \
  --engine duck \
  --layout vortex \
  --dsn "host=/var/run/postgresql dbname=bench user=postgres application_name=rvbbit-duck-sidecar" \
  --threads 4 \
  --pgdata-prefix /var/lib/postgresql/18/docker \
  --visible-pgdata-prefix /var/lib/postgresql/18/docker
```

Run SQL:

```sql
SET rvbbit.route_force_candidate = 'duck_vortex';
SET rvbbit.duck_backend_shared = on;
SET rvbbit.duck_backend_shared_socket = '/tmp/rvbbit-duck/doc-test.sock';
SET rvbbit.duck_threads = 4;

SELECT COUNT(DISTINCT "SearchPhrase") FROM hits;
SELECT count(*) FROM pg_stat_activity
WHERE application_name = 'rvbbit-duck-sidecar';
```

Expected:

- query returns the same result as local mode;
- sidecar PG connection count matches broker workers.

Observed smoke-test result with `--workers 2`:

```text
COUNT(DISTINCT "SearchPhrase") = 610809
rvbbit-duck-sidecar connections = 2
```

### Load Harness

Default/local per-backend path:

```bash
SIDECAR_LOAD_DUCK_THREADS=4 \
SIDECAR_LOAD_CLIENTS=16,32 \
SIDECAR_LOAD_DURATION_S=30 \
./bench/sidecar_load/run_offline.sh
```

Shared broker path:

```bash
SIDECAR_LOAD_SHARED=on \
SIDECAR_LOAD_SHARED_WORKERS=4 \
SIDECAR_LOAD_DUCK_THREADS=4 \
SIDECAR_LOAD_CLIENTS=16,32 \
SIDECAR_LOAD_DURATION_S=30 \
./bench/sidecar_load/run_offline.sh
```

The wrapper starts the broker externally for the test run and cleans it up.

## Security Notes

The shared socket protocol is intended for local trusted processes. Do not
expose it over TCP without adding authentication and authorization.

The broker creates its Unix socket with broad permissions so PostgreSQL
backends running as the `postgres` OS user can connect even if the supervisor
starts the broker as another user. Prefer a private directory such as
`/run/rvbbit` with controlled ownership/mode rather than a globally writable
directory in production.

The broker runs guarded read-only SQL and performs route-safety checks before
registering Rvbbit accelerator files. It still needs read access to those files
and a PostgreSQL role capable of reading Rvbbit metadata.

## Recommended V1 Product Stance

Keep the easy path easy:

```text
shared broker off by default
local persistent sidecar on by default
```

Document shared mode as a high-concurrency / bounded-memory deployment option,
not a requirement. A new user should be able to install the extension and run
queries without learning about broker supervision.
