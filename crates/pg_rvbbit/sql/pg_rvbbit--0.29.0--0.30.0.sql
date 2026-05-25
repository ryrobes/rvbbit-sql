-- pg_rvbbit 0.29.0 -> 0.30.0
-- Promote DataFusion from benchmark-only sidecar to a first-class adaptive
-- routing candidate and persisted profile metric.

CREATE FUNCTION rvbbit.datafusion_query_json(
    "query" text,
    "column_names" jsonb,
    "max_rows" integer
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'datafusion_query_json_wrapper';

ALTER TABLE rvbbit.route_observations
    DROP CONSTRAINT IF EXISTS route_observations_candidate_check;
ALTER TABLE rvbbit.route_observations
    ADD CONSTRAINT route_observations_candidate_check
    CHECK (candidate IN ('duck_vector', 'datafusion_vector', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_ms double precision;
ALTER TABLE rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'datafusion_vector', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_ms double precision;
ALTER TABLE rvbbit.route_profile_points
    DROP CONSTRAINT IF EXISTS route_profile_points_datafusion_ms_check;
ALTER TABLE rvbbit.route_profile_points
    ADD CONSTRAINT route_profile_points_datafusion_ms_check
    CHECK (datafusion_ms IS NULL OR datafusion_ms > 0);

DROP VIEW IF EXISTS rvbbit.route_profile_summary;
DROP VIEW IF EXISTS rvbbit.route_shape_summary;

CREATE OR REPLACE VIEW rvbbit.route_observation_summary AS
WITH ok AS (
    SELECT *
    FROM rvbbit.route_observations
    WHERE status = 'ok'
),
keyed AS (
    SELECT
        CASE
            WHEN shape_key LIKE 'native=%' THEN
                regexp_replace(
                    shape_key,
                    '^native=[^|]*',
                    'native_cap=' ||
                    CASE
                        WHEN coalesce((features->>'has_native_function')::boolean, false)
                        THEN '1'
                        ELSE '0'
                    END
                )
            ELSE shape_key
        END AS route_shape_key,
        candidate,
        elapsed_ms,
        observed_at,
        source
    FROM ok
)
SELECT
    route_shape_key AS shape_key,
    regexp_replace(
        regexp_replace(route_shape_key, '(^|\|)table_rows=[^|]*', '', 'g'),
        '^\|', ''
    ) AS shape_family,
    candidate,
    count(*)::bigint AS observations,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS median_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms,
    min(elapsed_ms) AS min_ms,
    max(elapsed_ms) AS max_ms,
    min(observed_at) AS first_seen,
    max(observed_at) AS last_seen,
    (array_agg(source ORDER BY observed_at DESC))[1] AS last_source
FROM keyed
GROUP BY route_shape_key, candidate;

CREATE OR REPLACE VIEW rvbbit.route_shape_summary AS
WITH candidate_stats AS (
    SELECT *
    FROM rvbbit.route_observation_summary
    WHERE candidate IN ('rvbbit_native', 'duck_vector', 'datafusion_vector')
),
shape_stats AS (
    SELECT
        shape_key,
        shape_family,
        sum(observations)::bigint AS observations,
        max(last_seen) AS last_seen,
        max(median_ms) FILTER (WHERE candidate = 'rvbbit_native') AS native_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'duck_vector') AS duck_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_median_ms,
        max(observations) FILTER (WHERE candidate = 'rvbbit_native') AS native_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vector') AS duck_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_observations
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
    ss.datafusion_median_ms,
    ss.native_observations,
    ss.duck_observations,
    ss.datafusion_observations,
    CASE
        WHEN r.median_ms IS NULL THEN NULL
        WHEN (
            SELECT max(v)
            FROM (VALUES (ss.native_median_ms), (ss.duck_median_ms), (ss.datafusion_median_ms)) AS med(v)
            WHERE v IS NOT NULL
        ) <= 0 THEN NULL
        ELSE 1.0 - r.median_ms
             / (
                SELECT max(v)
                FROM (VALUES (ss.native_median_ms), (ss.duck_median_ms), (ss.datafusion_median_ms)) AS med(v)
                WHERE v IS NOT NULL
             )
    END AS observed_gain,
    (
        coalesce(ss.native_observations, 0) = 0
        OR coalesce(ss.duck_observations, 0) = 0
        OR coalesce(ss.datafusion_observations, 0) = 0
    )
        AS needs_exploration
FROM shape_stats ss
LEFT JOIN ranked r ON r.shape_key = ss.shape_key AND r.rn = 1;

CREATE OR REPLACE VIEW rvbbit.route_profile_summary AS
SELECT
    rp.name AS profile_name,
    rp.active,
    rp.updated_at AS profile_updated_at,
    CASE
        WHEN pe.shape_key LIKE 'native=%' THEN
            regexp_replace(
                pe.shape_key,
                '^native=[^|]*',
                'native_cap=' ||
                CASE
                    WHEN coalesce((pe.entry->>'choice') = 'rvbbit_native', false)
                         OR pe.shape_key !~ '^native=none(\||$)'
                    THEN '1'
                    ELSE '0'
                END
            )
        ELSE pe.shape_key
    END AS shape_key,
    regexp_replace(
        regexp_replace(
            CASE
                WHEN pe.shape_key LIKE 'native=%' THEN
                    regexp_replace(
                        pe.shape_key,
                        '^native=[^|]*',
                        'native_cap=' ||
                        CASE
                            WHEN coalesce((pe.entry->>'choice') = 'rvbbit_native', false)
                                 OR pe.shape_key !~ '^native=none(\||$)'
                            THEN '1'
                            ELSE '0'
                        END
                    )
                ELSE pe.shape_key
            END,
            '(^|\|)table_rows=[^|]*',
            '',
            'g'
        ),
        '^\|', ''
    ) AS shape_family,
    pe.choice,
    pe.confidence,
    pe.reason,
    pe.observations,
    pe.native_ms,
    pe.duck_ms,
    pe.datafusion_ms,
    pe.pg_ms
FROM rvbbit.route_profiles rp
JOIN rvbbit.route_profile_entries pe ON pe.profile_name = rp.name;
