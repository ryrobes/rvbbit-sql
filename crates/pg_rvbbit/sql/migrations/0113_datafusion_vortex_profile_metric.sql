-- 0113_datafusion_vortex_profile_metric
--
-- Make DataFusion/Vortex a first-class route-profile metric so training,
-- profile export/import, and summary views can distinguish it from generic
-- DataFusion parquet/vector timings.

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS native_vortex_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS duck_vortex_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS native_vortex_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS duck_vortex_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_vortex_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS pg_ms double precision;

DO $$
BEGIN
    IF to_regclass('rvbbit.route_profile_points') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM pg_constraint
           WHERE conrelid = 'rvbbit.route_profile_points'::regclass
             AND conname = 'route_profile_points_datafusion_vortex_ms_check'
       ) THEN
        ALTER TABLE rvbbit.route_profile_points
            ADD CONSTRAINT route_profile_points_datafusion_vortex_ms_check
            CHECK (datafusion_vortex_ms IS NULL OR datafusion_vortex_ms > 0);
    END IF;
END $$;

CREATE OR REPLACE VIEW rvbbit.route_shape_summary AS
WITH candidate_stats AS (
    SELECT *
    FROM rvbbit.route_observation_summary
    WHERE candidate IN ('rvbbit_native', 'rvbbit_native_vortex', 'duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'pg_rowstore')
),
shape_stats AS (
    SELECT
        shape_key,
        shape_family,
        sum(observations)::bigint AS observations,
        max(last_seen) AS last_seen,
        max(median_ms) FILTER (WHERE candidate = 'rvbbit_native') AS native_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'duck_vector') AS duck_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'duck_hive') AS duck_hive_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'duck_vortex') AS duck_vortex_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_mem') AS datafusion_mem_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_hive') AS datafusion_hive_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_vortex') AS datafusion_vortex_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'pg_rowstore') AS pg_median_ms,
        max(observations) FILTER (WHERE candidate = 'rvbbit_native') AS native_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vector') AS duck_observations,
        max(observations) FILTER (WHERE candidate = 'duck_hive') AS duck_hive_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vortex') AS duck_vortex_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_mem') AS datafusion_mem_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_hive') AS datafusion_hive_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_vortex') AS datafusion_vortex_observations,
        max(observations) FILTER (WHERE candidate = 'pg_rowstore') AS pg_observations
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
    ss.duck_hive_median_ms,
    ss.duck_vortex_median_ms,
    ss.datafusion_mem_median_ms,
    ss.datafusion_median_ms,
    ss.datafusion_hive_median_ms,
    ss.pg_median_ms,
    ss.native_observations,
    ss.duck_observations,
    ss.duck_hive_observations,
    ss.duck_vortex_observations,
    ss.datafusion_mem_observations,
    ss.datafusion_observations,
    ss.datafusion_hive_observations,
    ss.pg_observations,
    CASE
        WHEN r.median_ms IS NULL THEN NULL
        WHEN (
            SELECT max(v)
            FROM (
                VALUES
                    (ss.native_median_ms),
                    (ss.duck_median_ms),
                    (ss.duck_hive_median_ms),
                    (ss.duck_vortex_median_ms),
                    (ss.datafusion_mem_median_ms),
                    (ss.datafusion_median_ms),
                    (ss.datafusion_hive_median_ms),
                    (ss.datafusion_vortex_median_ms),
                    (ss.pg_median_ms)
            ) AS med(v)
            WHERE v IS NOT NULL
        ) <= 0 THEN NULL
        ELSE 1.0 - r.median_ms
             / (
                SELECT max(v)
                FROM (
                    VALUES
                        (ss.native_median_ms),
                        (ss.duck_median_ms),
                        (ss.duck_hive_median_ms),
                        (ss.duck_vortex_median_ms),
                        (ss.datafusion_mem_median_ms),
                        (ss.datafusion_median_ms),
                        (ss.datafusion_hive_median_ms),
                        (ss.datafusion_vortex_median_ms),
                        (ss.pg_median_ms)
                ) AS med(v)
                WHERE v IS NOT NULL
             )
    END AS observed_gain,
    (
        coalesce(ss.native_observations, 0) = 0
        OR coalesce(ss.duck_observations, 0) = 0
        OR coalesce(ss.duck_vortex_observations, 0) = 0
        OR coalesce(ss.datafusion_mem_observations, 0) = 0
        OR coalesce(ss.datafusion_observations, 0) = 0
        OR coalesce(ss.datafusion_vortex_observations, 0) = 0
        OR coalesce(ss.pg_observations, 0) = 0
    ) AS needs_exploration,
    ss.datafusion_vortex_median_ms,
    ss.datafusion_vortex_observations
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
    pe.duck_hive_ms,
    pe.duck_vortex_ms,
    pe.datafusion_ms,
    pe.datafusion_hive_ms,
    pe.pg_ms,
    pe.native_vortex_ms,
    pe.datafusion_vortex_ms
FROM rvbbit.route_profiles rp
JOIN rvbbit.route_profile_entries pe ON pe.profile_name = rp.name;
