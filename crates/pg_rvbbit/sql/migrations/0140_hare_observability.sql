-- 0140: hare/fleet observability — the "get data before designing targeting"
-- migration. Companions to the Rust-side changes shipping with it:
--   * fleet dispatch rotates UNIFORMLY across healthy nodes (was: pinned to
--     the most-recently-probed one) — healthy workers are interchangeable by
--     design; no per-node learned scores (they'd be non-stationary noise and
--     ephemeral hares make node identity meaningless as a feature);
--   * training/optimize benches run under force_local_scope so learned curves
--     stay BRAIN-relative — fleet dispatch was quietly folding network+remote
--     cache latency into duck-candidate observations since 0137;
--   * rvbbit.hare_run() + rvbbit.brain_pressure() C bindings below.

-- Every manual/benchmark hare invocation, with the decomposition that answers
-- the design question: engine_ms (the query itself) vs server_ms (the hare's
-- whole handling incl. artifact fetch) vs wire_ms (network + platform + cold
-- start). If engine work dwarfs wire tax at medium+ scale, offload targeting
-- can be a simple size floor.
CREATE TABLE IF NOT EXISTS rvbbit.hare_invocations (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoked_at    timestamptz NOT NULL DEFAULT now(),
    endpoint      text NOT NULL,
    sql_hash      text,
    sql           text,
    ok            boolean,
    http_status   integer,
    row_count     bigint,
    n_tables      integer,
    capsule_bytes bigint,
    engine_ms     double precision,   -- the hare's engine execution alone
    server_ms     double precision,   -- hare handling: views + fetch + engine
    wire_ms       double precision,   -- total - server: network/platform/cold
    total_ms      double precision,   -- wall clock from the brain's POST
    error         text
);
CREATE INDEX IF NOT EXISTS hare_invocations_at_idx
    ON rvbbit.hare_invocations (invoked_at DESC);
CREATE INDEX IF NOT EXISTS hare_invocations_hash_idx
    ON rvbbit.hare_invocations (sql_hash, invoked_at DESC);

-- C bindings ($libdir literal — migrate() runs via SPI, 0044 precedent).
CREATE OR REPLACE FUNCTION rvbbit.hare_run(
    sql text,
    endpoint text DEFAULT NULL,
    ttl_secs integer DEFAULT 900
) RETURNS jsonb LANGUAGE c AS '$libdir/pg_rvbbit', 'hare_run_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.brain_pressure()
RETURNS jsonb LANGUAGE c AS '$libdir/pg_rvbbit', 'brain_pressure_wrapper';

-- Reap hare telemetry with the other logs (14d default) and keep its
-- autovacuum sane under benchmark bursts.
ALTER TABLE rvbbit.hare_invocations SET (
    autovacuum_vacuum_scale_factor = 0.005,
    autovacuum_vacuum_threshold = 10000,
    autovacuum_analyze_scale_factor = 0.01,
    autovacuum_analyze_threshold = 10000
);

-- One-glance distribution: where did executed work actually land, per hour —
-- the "execution distribution" lens for benchmark runs. Fleet nodes come from
-- route_executions breadcrumbs; hares from hare_invocations.
CREATE OR REPLACE VIEW rvbbit.offload_distribution AS
SELECT date_trunc('hour', executed_at) AS hour,
       coalesce(node, 'brain')         AS placement,
       candidate                       AS engine,
       count(*)                        AS executions,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms)::numeric, 1) AS median_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms)::numeric, 1) AS p95_ms
FROM rvbbit.route_executions
WHERE status = 'ok'
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('hour', invoked_at),
       'hare:' || endpoint,
       'duck_capsule',
       count(*),
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms)::numeric, 1),
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)::numeric, 1)
FROM rvbbit.hare_invocations
WHERE ok
GROUP BY 1, 2, 3;
