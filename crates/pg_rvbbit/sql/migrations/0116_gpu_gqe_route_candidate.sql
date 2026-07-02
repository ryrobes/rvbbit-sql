-- 0116_gpu_gqe_route_candidate
--
-- Add a disabled-by-default GPU/GQE route candidate as a first-class router
-- option. The route uses the same profile/observation surfaces as the other
-- external engines, but is gated by rvbbit.route_gpu_gqe / RVBBIT_ROUTE_GPU_GQE.

ALTER TABLE IF EXISTS rvbbit.route_observations
    DROP CONSTRAINT IF EXISTS route_observations_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_observations
    ADD CONSTRAINT route_observations_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_training_results
    DROP CONSTRAINT IF EXISTS route_training_results_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_training_results
    ADD CONSTRAINT route_training_results_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_decisions
    DROP CONSTRAINT IF EXISTS route_decisions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD CONSTRAINT route_decisions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_executions
    DROP CONSTRAINT IF EXISTS route_executions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD CONSTRAINT route_executions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS gpu_gqe_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS gpu_gqe_ms double precision;

DO $$
BEGIN
    IF to_regclass('rvbbit.route_profile_points') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM pg_constraint
           WHERE conrelid = 'rvbbit.route_profile_points'::regclass
             AND conname = 'route_profile_points_gpu_gqe_ms_check'
       ) THEN
        ALTER TABLE rvbbit.route_profile_points
            ADD CONSTRAINT route_profile_points_gpu_gqe_ms_check
            CHECK (gpu_gqe_ms IS NULL OR gpu_gqe_ms > 0);
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('rvbbit.route_overlay') IS NOT NULL THEN
        ALTER TABLE rvbbit.route_overlay
            DROP CONSTRAINT IF EXISTS route_overlay_engine_check;
        ALTER TABLE rvbbit.route_overlay
            ADD CONSTRAINT route_overlay_engine_check
            CHECK (engine IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem',
                              'datafusion_vector', 'datafusion_hive', 'datafusion_vortex',
                              'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));
    END IF;
END $$;

CREATE OR REPLACE VIEW rvbbit.route_shape_summary AS
WITH candidate_stats AS (
    SELECT *
    FROM rvbbit.route_observation_summary
    WHERE candidate IN ('rvbbit_native', 'rvbbit_native_vortex', 'duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'pg_rowstore')
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
        max(median_ms) FILTER (WHERE candidate = 'gpu_gqe') AS gpu_gqe_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'pg_rowstore') AS pg_median_ms,
        max(observations) FILTER (WHERE candidate = 'rvbbit_native') AS native_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vector') AS duck_observations,
        max(observations) FILTER (WHERE candidate = 'duck_hive') AS duck_hive_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vortex') AS duck_vortex_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_mem') AS datafusion_mem_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_hive') AS datafusion_hive_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_vortex') AS datafusion_vortex_observations,
        max(observations) FILTER (WHERE candidate = 'gpu_gqe') AS gpu_gqe_observations,
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
                    (ss.gpu_gqe_median_ms),
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
                        (ss.gpu_gqe_median_ms),
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
        OR coalesce(ss.gpu_gqe_observations, 0) = 0
        OR coalesce(ss.pg_observations, 0) = 0
    ) AS needs_exploration,
    ss.datafusion_vortex_median_ms,
    ss.datafusion_vortex_observations,
    ss.gpu_gqe_median_ms,
    ss.gpu_gqe_observations
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
    pe.datafusion_vortex_ms,
    pe.gpu_gqe_ms
FROM rvbbit.route_profiles rp
JOIN rvbbit.route_profile_entries pe ON pe.profile_name = rp.name;
