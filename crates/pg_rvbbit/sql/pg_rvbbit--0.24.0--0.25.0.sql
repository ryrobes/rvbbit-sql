-- pg_rvbbit 0.24.0 -> 0.25.0
-- Adaptive routing summaries, evaluation, and safer profile training.

CREATE OR REPLACE VIEW rvbbit.route_observation_summary AS
WITH ok AS (
    SELECT *
    FROM rvbbit.route_observations
    WHERE status = 'ok'
)
SELECT
    shape_key,
    shape_family,
    candidate,
    count(*)::bigint AS observations,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS median_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms,
    min(elapsed_ms) AS min_ms,
    max(elapsed_ms) AS max_ms,
    min(observed_at) AS first_seen,
    max(observed_at) AS last_seen,
    (array_agg(source ORDER BY observed_at DESC))[1] AS last_source
FROM ok
GROUP BY shape_key, shape_family, candidate;

CREATE OR REPLACE VIEW rvbbit.route_shape_summary AS
WITH candidate_stats AS (
    SELECT *
    FROM rvbbit.route_observation_summary
    WHERE candidate IN ('rvbbit_native', 'duck_vector')
),
shape_stats AS (
    SELECT
        shape_key,
        shape_family,
        sum(observations)::bigint AS observations,
        max(last_seen) AS last_seen,
        max(median_ms) FILTER (WHERE candidate = 'rvbbit_native') AS native_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'duck_vector') AS duck_median_ms,
        max(observations) FILTER (WHERE candidate = 'rvbbit_native') AS native_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vector') AS duck_observations
    FROM candidate_stats
    GROUP BY shape_key, shape_family
),
ranked AS (
    SELECT
        cs.*,
        row_number() OVER (PARTITION BY shape_key ORDER BY median_ms ASC, observations DESC) AS rn
    FROM candidate_stats cs
)
SELECT
    ss.shape_key,
    ss.shape_family,
    ss.observations,
    ss.last_seen,
    r.candidate AS best_candidate,
    r.median_ms AS best_median_ms,
    ss.native_median_ms,
    ss.duck_median_ms,
    ss.native_observations,
    ss.duck_observations,
    CASE
        WHEN ss.native_median_ms IS NULL OR ss.duck_median_ms IS NULL THEN NULL
        WHEN greatest(ss.native_median_ms, ss.duck_median_ms) <= 0 THEN NULL
        ELSE 1.0 - least(ss.native_median_ms, ss.duck_median_ms)
             / greatest(ss.native_median_ms, ss.duck_median_ms)
    END AS observed_gain,
    (coalesce(ss.native_observations, 0) = 0 OR coalesce(ss.duck_observations, 0) = 0)
        AS needs_exploration
FROM shape_stats ss
LEFT JOIN ranked r ON r.shape_key = ss.shape_key AND r.rn = 1;

CREATE OR REPLACE VIEW rvbbit.route_profile_summary AS
SELECT
    rp.name AS profile_name,
    rp.active,
    rp.updated_at AS profile_updated_at,
    pe.shape_key,
    regexp_replace(
        regexp_replace(pe.shape_key, '(^|\|)table_rows=[^|]*', '', 'g'),
        '^\|', ''
    ) AS shape_family,
    pe.choice,
    pe.confidence,
    pe.reason,
    pe.observations,
    pe.native_ms,
    pe.duck_ms,
    pe.pg_ms
FROM rvbbit.route_profiles rp
JOIN rvbbit.route_profile_entries pe ON pe.profile_name = rp.name;

CREATE FUNCTION rvbbit.route_eval(
    profile_name text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_eval_wrapper';
