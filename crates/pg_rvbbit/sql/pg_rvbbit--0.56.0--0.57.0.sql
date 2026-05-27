-- pg_rvbbit 0.56.0 -> 0.57.0
-- SQL-native adaptive route training.

CREATE TABLE IF NOT EXISTS rvbbit.route_training_queries (
    id            bigserial PRIMARY KEY,
    profile_name  text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    query_sql     text NOT NULL,
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    shape_family  text NOT NULL,
    features      jsonb NOT NULL,
    label         text,
    enabled       boolean NOT NULL DEFAULT true,
    created_by    text NOT NULL DEFAULT current_user,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_name, query_hash)
);

CREATE INDEX IF NOT EXISTS route_training_queries_profile_idx
    ON rvbbit.route_training_queries (profile_name, enabled, updated_at DESC);

CREATE INDEX IF NOT EXISTS route_training_queries_shape_idx
    ON rvbbit.route_training_queries (profile_name, shape_key);

CREATE TABLE IF NOT EXISTS rvbbit.route_training_runs (
    id                 bigserial PRIMARY KEY,
    training_query_id  bigint NOT NULL REFERENCES rvbbit.route_training_queries(id) ON DELETE CASCADE,
    profile_name       text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    status             text NOT NULL DEFAULT 'running',
    repeats            integer NOT NULL DEFAULT 1,
    candidates         text[] NOT NULL DEFAULT ARRAY[]::text[],
    settings           jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary            jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (repeats > 0)
);

CREATE INDEX IF NOT EXISTS route_training_runs_profile_idx
    ON rvbbit.route_training_runs (profile_name, started_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.route_training_results (
    id                 bigserial PRIMARY KEY,
    run_id             bigint NOT NULL REFERENCES rvbbit.route_training_runs(id) ON DELETE CASCADE,
    training_query_id  bigint NOT NULL REFERENCES rvbbit.route_training_queries(id) ON DELETE CASCADE,
    profile_name       text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    observed_at        timestamptz NOT NULL DEFAULT now(),
    candidate          text NOT NULL,
    repeat_idx         integer NOT NULL DEFAULT 1,
    elapsed_ms         double precision,
    rows_returned      bigint,
    result_digest      text,
    status             text NOT NULL DEFAULT 'ok',
    validation_status  text NOT NULL DEFAULT 'unknown',
    error              text,
    route_doc          jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
    CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CHECK (rows_returned IS NULL OR rows_returned >= 0),
    CHECK (repeat_idx > 0)
);

CREATE INDEX IF NOT EXISTS route_training_results_profile_idx
    ON rvbbit.route_training_results (profile_name, observed_at DESC);

CREATE INDEX IF NOT EXISTS route_training_results_query_idx
    ON rvbbit.route_training_results (training_query_id, candidate, observed_at DESC);

CREATE OR REPLACE VIEW rvbbit.route_training_summary AS
WITH candidate_stats AS (
    SELECT
        tq.profile_name,
        tq.id AS training_query_id,
        tq.query_hash,
        tq.shape_key,
        tq.shape_family,
        tq.label,
        tq.enabled,
        tr.candidate,
        count(*) FILTER (WHERE tr.status = 'ok')::bigint AS ok_runs,
        count(*) FILTER (WHERE tr.status <> 'ok')::bigint AS error_runs,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY tr.elapsed_ms)
            FILTER (WHERE tr.status = 'ok' AND tr.elapsed_ms IS NOT NULL) AS median_ms,
        min(tr.observed_at) AS first_seen,
        max(tr.observed_at) AS last_seen,
        (array_agg(tr.validation_status ORDER BY tr.observed_at DESC))[1] AS last_validation_status,
        (array_agg(tr.error ORDER BY tr.observed_at DESC))[1] AS last_error
    FROM rvbbit.route_training_queries tq
    LEFT JOIN rvbbit.route_training_results tr ON tr.training_query_id = tq.id
    GROUP BY tq.profile_name, tq.id, tq.query_hash, tq.shape_key, tq.shape_family,
             tq.label, tq.enabled, tr.candidate
)
SELECT *
FROM candidate_stats;

CREATE OR REPLACE FUNCTION rvbbit.route_create_profile(
    profile_name text,
    active boolean DEFAULT false
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_create_profile_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_train_query(
    profile_name text,
    query text,
    repeats integer DEFAULT 3,
    min_gain_pct double precision DEFAULT 0.05,
    activate boolean DEFAULT true,
    candidates text DEFAULT 'all',
    label text DEFAULT ''
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_train_query_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_profile_rebuild(
    profile_name text,
    min_gain_pct double precision DEFAULT 0.05,
    activate boolean DEFAULT true
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_profile_rebuild_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_training_delete_query(
    profile_name text,
    training_query_id bigint,
    rebuild boolean DEFAULT true
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_training_delete_query_wrapper';

-- Manual in-process hot columnar objects ------------------------------------

CREATE TABLE IF NOT EXISTS rvbbit.hot_objects (
    object_key       text PRIMARY KEY,
    table_oid        oid NOT NULL,
    schema_name      text NOT NULL,
    table_name       text NOT NULL,
    columns          text[] NOT NULL DEFAULT ARRAY[]::text[],
    all_columns      boolean NOT NULL DEFAULT true,
    signature        text NOT NULL,
    row_groups       bigint NOT NULL DEFAULT 0,
    row_count        bigint NOT NULL DEFAULT 0,
    parquet_bytes    bigint NOT NULL DEFAULT 0,
    cache_bytes      bigint NOT NULL DEFAULT 0,
    enabled          boolean NOT NULL DEFAULT true,
    loaded_by        text NOT NULL DEFAULT current_user,
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    last_error       text,
    CHECK (row_groups >= 0),
    CHECK (row_count >= 0),
    CHECK (parquet_bytes >= 0),
    CHECK (cache_bytes >= 0)
);

CREATE INDEX IF NOT EXISTS hot_objects_table_idx
    ON rvbbit.hot_objects (table_oid, enabled);

CREATE INDEX IF NOT EXISTS hot_objects_updated_idx
    ON rvbbit.hot_objects (updated_at DESC);

CREATE OR REPLACE FUNCTION rvbbit.df_hot_query(
    "sql" text,
    "max_rows" integer DEFAULT 100000
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'df_hot_query_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hot_load(
    "rel" oid
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hot_load_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hot_load_columns(
    "rel" oid,
    "columns" jsonb
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hot_load_columns_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hot_evict(
    "rel" oid
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hot_evict_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hot_cache_reset()
RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hot_cache_reset_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.hot_status()
RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'hot_status_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.datafusion_mem_query_json(
    "query" text,
    "column_names" jsonb,
    "max_rows" integer
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'datafusion_mem_query_json_wrapper';

ALTER TABLE IF EXISTS rvbbit.route_observations
    DROP CONSTRAINT IF EXISTS route_observations_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_observations
    ADD CONSTRAINT route_observations_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_training_results
    DROP CONSTRAINT IF EXISTS route_training_results_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_training_results
    ADD CONSTRAINT route_training_results_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_decisions
    DROP CONSTRAINT IF EXISTS route_decisions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD CONSTRAINT route_decisions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_executions
    DROP CONSTRAINT IF EXISTS route_executions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD CONSTRAINT route_executions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

DROP VIEW IF EXISTS rvbbit.route_shape_summary;

CREATE OR REPLACE VIEW rvbbit.route_shape_summary AS
WITH candidate_stats AS (
    SELECT *
    FROM rvbbit.route_observation_summary
    WHERE candidate IN ('rvbbit_native', 'duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'pg_rowstore')
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
        max(median_ms) FILTER (WHERE candidate = 'datafusion_mem') AS datafusion_mem_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'datafusion_hive') AS datafusion_hive_median_ms,
        max(median_ms) FILTER (WHERE candidate = 'pg_rowstore') AS pg_median_ms,
        max(observations) FILTER (WHERE candidate = 'rvbbit_native') AS native_observations,
        max(observations) FILTER (WHERE candidate = 'duck_vector') AS duck_observations,
        max(observations) FILTER (WHERE candidate = 'duck_hive') AS duck_hive_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_mem') AS datafusion_mem_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_vector') AS datafusion_observations,
        max(observations) FILTER (WHERE candidate = 'datafusion_hive') AS datafusion_hive_observations,
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
    ss.datafusion_mem_median_ms,
    ss.datafusion_median_ms,
    ss.datafusion_hive_median_ms,
    ss.pg_median_ms,
    ss.native_observations,
    ss.duck_observations,
    ss.duck_hive_observations,
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
                    (ss.datafusion_mem_median_ms),
                    (ss.datafusion_median_ms),
                    (ss.datafusion_hive_median_ms),
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
                        (ss.datafusion_mem_median_ms),
                        (ss.datafusion_median_ms),
                        (ss.datafusion_hive_median_ms),
                        (ss.pg_median_ms)
                ) AS med(v)
                WHERE v IS NOT NULL
             )
    END AS observed_gain,
    (
        coalesce(ss.native_observations, 0) = 0
        OR coalesce(ss.duck_observations, 0) = 0
        OR coalesce(ss.datafusion_mem_observations, 0) = 0
        OR coalesce(ss.datafusion_observations, 0) = 0
        OR coalesce(ss.pg_observations, 0) = 0
    )
        AS needs_exploration
FROM shape_stats ss
LEFT JOIN ranked r ON r.shape_key = ss.shape_key AND r.rn = 1;
