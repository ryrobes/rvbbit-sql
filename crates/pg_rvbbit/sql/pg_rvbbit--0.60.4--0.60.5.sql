-- DuckDB/Vortex sidecar and broker observability ----------------------------

CREATE TABLE IF NOT EXISTS rvbbit.duck_sidecar_instances (
    instance_id       text PRIMARY KEY,
    hostname          text NOT NULL,
    node_id           text NOT NULL,
    pid               integer NOT NULL,
    mode              text NOT NULL,
    engine            text NOT NULL,
    layout            text NOT NULL,
    socket_path       text,
    dsn_hash          text,
    worker_count      integer,
    duck_threads      integer,
    binary_path       text,
    started_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_heartbeat_at timestamptz,
    status            text NOT NULL DEFAULT 'starting',
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (mode IN ('local_oneshot', 'local_persistent', 'shared_broker')),
    CHECK (status IN ('starting', 'online', 'offline', 'stale', 'error')),
    CHECK (worker_count IS NULL OR worker_count >= 0),
    CHECK (duck_threads IS NULL OR duck_threads >= 0)
);

CREATE INDEX IF NOT EXISTS duck_sidecar_instances_node_idx
    ON rvbbit.duck_sidecar_instances (node_id, hostname, status);

CREATE INDEX IF NOT EXISTS duck_sidecar_instances_last_heartbeat_idx
    ON rvbbit.duck_sidecar_instances (last_heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.duck_sidecar_heartbeats (
    id              bigserial PRIMARY KEY,
    observed_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    instance_id     text NOT NULL,
    hostname        text NOT NULL,
    node_id         text NOT NULL,
    pid             integer NOT NULL,
    mode            text NOT NULL,
    engine          text NOT NULL,
    layout          text NOT NULL,
    queue_depth     integer,
    active_workers  integer,
    worker_count    integer,
    duck_threads    integer,
    rss_bytes       bigint,
    pg_connections  integer,
    events_enqueued bigint,
    events_written  bigint,
    events_dropped  bigint,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (mode IN ('local_oneshot', 'local_persistent', 'shared_broker'))
);

CREATE INDEX IF NOT EXISTS duck_sidecar_heartbeats_time_idx
    ON rvbbit.duck_sidecar_heartbeats (observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_heartbeats_instance_idx
    ON rvbbit.duck_sidecar_heartbeats (instance_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_heartbeats_node_idx
    ON rvbbit.duck_sidecar_heartbeats (node_id, hostname, observed_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.duck_sidecar_query_events (
    id                  bigserial PRIMARY KEY,
    observed_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    instance_id          text NOT NULL,
    hostname             text NOT NULL,
    node_id              text NOT NULL,
    pid                  integer NOT NULL,
    mode                 text NOT NULL,
    engine               text NOT NULL,
    layout               text NOT NULL,
    worker_id            integer,
    command              text,
    query_hash           text,
    status               text NOT NULL,
    queue_wait_ms        double precision,
    elapsed_ms           double precision NOT NULL,
    execute_ms           double precision,
    route_safety_ms      double precision,
    parquet_prewarm_ms   double precision,
    row_count            bigint,
    result_format        text,
    arrow_ipc_bytes      bigint,
    repeat_count         integer,
    timeout_s            integer,
    max_rows             integer,
    error                text,
    cache                jsonb NOT NULL DEFAULT '{}'::jsonb,
    tables               jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata             jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (mode IN ('local_oneshot', 'local_persistent', 'shared_broker')),
    CHECK (elapsed_ms >= 0),
    CHECK (queue_wait_ms IS NULL OR queue_wait_ms >= 0),
    CHECK (execute_ms IS NULL OR execute_ms >= 0)
);

CREATE INDEX IF NOT EXISTS duck_sidecar_query_events_time_idx
    ON rvbbit.duck_sidecar_query_events (observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_query_events_instance_idx
    ON rvbbit.duck_sidecar_query_events (instance_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_query_events_node_idx
    ON rvbbit.duck_sidecar_query_events (node_id, hostname, observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_query_events_shape_idx
    ON rvbbit.duck_sidecar_query_events (engine, layout, query_hash, observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_query_events_status_idx
    ON rvbbit.duck_sidecar_query_events (status, observed_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.duck_sidecar_fallback_events (
    id             bigserial PRIMARY KEY,
    observed_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    hostname       text NOT NULL,
    node_id        text NOT NULL,
    backend_pid    integer NOT NULL,
    database_name  text NOT NULL,
    role_name      text NOT NULL,
    engine         text NOT NULL,
    layout         text NOT NULL,
    socket_path    text,
    reason         text NOT NULL,
    fallback_mode  text NOT NULL,
    query_hash     text,
    metadata       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS duck_sidecar_fallback_events_time_idx
    ON rvbbit.duck_sidecar_fallback_events (observed_at DESC);

CREATE INDEX IF NOT EXISTS duck_sidecar_fallback_events_node_idx
    ON rvbbit.duck_sidecar_fallback_events (node_id, hostname, observed_at DESC);

CREATE OR REPLACE VIEW rvbbit.duck_sidecar_latest AS
SELECT
    i.instance_id,
    i.hostname,
    i.node_id,
    i.pid,
    i.mode,
    i.engine,
    i.layout,
    i.socket_path,
    i.worker_count,
    i.duck_threads,
    i.binary_path,
    i.started_at,
    i.last_heartbeat_at,
    CASE
        WHEN i.last_heartbeat_at IS NULL THEN i.status
        WHEN i.last_heartbeat_at < clock_timestamp() - interval '30 seconds' THEN 'stale'
        ELSE i.status
    END AS effective_status,
    h.queue_depth,
    h.active_workers,
    h.rss_bytes,
    h.pg_connections,
    h.events_enqueued,
    h.events_written,
    h.events_dropped,
    i.metadata AS instance_metadata,
    h.metadata AS heartbeat_metadata
FROM rvbbit.duck_sidecar_instances i
LEFT JOIN LATERAL (
    SELECT *
    FROM rvbbit.duck_sidecar_heartbeats h
    WHERE h.instance_id = i.instance_id
    ORDER BY h.observed_at DESC
    LIMIT 1
) h ON true;

CREATE OR REPLACE VIEW rvbbit.duck_sidecar_query_summary AS
SELECT
    date_trunc('minute', observed_at) AS minute,
    hostname,
    node_id,
    mode,
    engine,
    layout,
    status,
    count(*) AS calls,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS p50_elapsed_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_elapsed_ms,
    max(elapsed_ms) AS max_elapsed_ms,
    sum(coalesce(row_count, 0)) AS rows_returned,
    sum(coalesce(arrow_ipc_bytes, 0)) AS arrow_ipc_bytes
FROM rvbbit.duck_sidecar_query_events
GROUP BY 1, 2, 3, 4, 5, 6, 7;
