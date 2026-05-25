-- pg_rvbbit 0.36.0 -> 0.37.0
-- Adaptive routing hardening: effective profile selection and profile-aware
-- telemetry.

ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD COLUMN IF NOT EXISTS profile_name text;

ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD COLUMN IF NOT EXISTS profile_source text NOT NULL DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS route_decisions_profile_idx
    ON rvbbit.route_decisions (profile_name, decided_at DESC);

ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD COLUMN IF NOT EXISTS profile_name text;

ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD COLUMN IF NOT EXISTS profile_source text NOT NULL DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS route_executions_profile_idx
    ON rvbbit.route_executions (profile_name, executed_at DESC);

DROP VIEW IF EXISTS rvbbit.route_decision_summary;
CREATE VIEW rvbbit.route_decision_summary AS
SELECT
    shape_key,
    shape_family,
    profile_name,
    profile_source,
    candidate,
    route,
    route_source,
    count(*)::bigint AS decisions,
    count(*) FILTER (WHERE cache_hit)::bigint AS cache_hits,
    count(*) FILTER (WHERE rewritten)::bigint AS rewritten_count,
    min(decided_at) AS first_seen,
    max(decided_at) AS last_seen,
    (array_agg(reason ORDER BY decided_at DESC))[1] AS last_reason
FROM rvbbit.route_decisions
GROUP BY shape_key, shape_family, profile_name, profile_source, candidate, route, route_source;

DROP VIEW IF EXISTS rvbbit.route_runtime_summary;
CREATE VIEW rvbbit.route_runtime_summary AS
SELECT
    shape_key,
    shape_family,
    profile_name,
    profile_source,
    candidate,
    route,
    route_source,
    count(*)::bigint AS executions,
    count(*) FILTER (WHERE cache_hit)::bigint AS cache_hits,
    count(*) FILTER (WHERE rewritten)::bigint AS rewritten_count,
    count(*) FILTER (WHERE status = 'ok')::bigint AS ok_count,
    count(*) FILTER (WHERE status <> 'ok')::bigint AS error_count,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS median_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms,
    min(elapsed_ms) AS min_ms,
    max(elapsed_ms) AS max_ms,
    avg(elapsed_ms) AS avg_ms,
    min(executed_at) AS first_seen,
    max(executed_at) AS last_seen,
    (array_agg(reason ORDER BY executed_at DESC))[1] AS last_reason
FROM rvbbit.route_executions
GROUP BY shape_key, shape_family, profile_name, profile_source, candidate, route, route_source;

CREATE OR REPLACE FUNCTION rvbbit.route_current_profile() RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_current_profile_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_use_profile(
    profile_name text,
    local boolean DEFAULT true
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_use_profile_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_clear_profile(
    local boolean DEFAULT true
) RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_clear_profile_wrapper';
