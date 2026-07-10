//! Native Rvbbit query router control plane.
//!
//! This module is deliberately execution-engine agnostic. It owns the route
//! vocabulary, feature extraction, adaptive profile catalog, and explainable
//! decisions. The DuckDB executor can later consume these decisions directly.

use std::cell::Cell;
use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap};
use std::ffi::{c_void, CStr, CString};
use std::panic::AssertUnwindSafe;
use std::time::Instant;

use pgrx::extension_sql;
use pgrx::prelude::*;
use pgrx::JsonB;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const NATIVE_FUNCTION_MARKERS: &[&str] = &[
    "vector_float_agg",
    "top_searchphrase_ordered",
    "count_text_contains",
    "top_phrase_min_url_for_url_contains",
    "top_phrase_url_title_rollup",
    "top_rows_text_contains_ordered_json",
    "top_text_transform_avg_len",
    "any_count_int_text",
    "top_count_1col",
    "count_distinct_int",
    "top_count_distinct_1col",
    "top_count_distinct_int_text",
    "top_rollup_2int",
    "top_rollup_1int_distinct",
    "top_count_int_minute_text",
    "top_count_filtered",
    "agg_groupby_count",
    "top_avg_len_by_int_col",
    "top_count_int_text",
];

extension_sql!(
    r#"
-- Adaptive query routing -----------------------------------------------------

CREATE TABLE IF NOT EXISTS rvbbit.route_profiles (
    name          text PRIMARY KEY,
    active        boolean NOT NULL DEFAULT false,
    profile       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.route_observations (
    id            bigserial PRIMARY KEY,
    observed_at   timestamptz NOT NULL DEFAULT now(),
    source        text NOT NULL DEFAULT 'manual',
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    shape_family  text NOT NULL,
    features      jsonb NOT NULL,
    candidate     text NOT NULL,
    elapsed_ms    double precision NOT NULL,
    status        text NOT NULL DEFAULT 'ok',
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore')),
    CHECK (elapsed_ms >= 0)
);

CREATE INDEX IF NOT EXISTS route_observations_shape_idx
    ON rvbbit.route_observations (shape_key, candidate, observed_at DESC);

CREATE INDEX IF NOT EXISTS route_observations_family_idx
    ON rvbbit.route_observations (shape_family, candidate, observed_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.route_shadow_decisions (
    id                bigserial PRIMARY KEY,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    query_hash        text NOT NULL,
    shape_key         text NOT NULL,
    shape_family      text NOT NULL,
    chosen_candidate  text,
    shadow_candidate  text,
    shadow_source     text,
    confidence        double precision,
    table_rows        bigint NOT NULL DEFAULT 0,
    features          jsonb NOT NULL,
    decision          jsonb NOT NULL,
    CHECK (confidence IS NULL OR confidence >= 0)
);

CREATE INDEX IF NOT EXISTS route_shadow_decisions_shape_idx
    ON rvbbit.route_shadow_decisions (shape_key, observed_at DESC);

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
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore')),
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

CREATE TABLE IF NOT EXISTS rvbbit.route_decisions (
    id            bigserial PRIMARY KEY,
    decided_at    timestamptz NOT NULL DEFAULT now(),
    backend_pid   integer NOT NULL,
    database_name text NOT NULL,
    role_name     text NOT NULL,
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    shape_family  text NOT NULL,
    route         text NOT NULL,
    candidate     text,
    profile_name  text,
    profile_source text NOT NULL DEFAULT 'unknown',
    route_source  text NOT NULL,
    reason        text NOT NULL DEFAULT '',
    confidence    double precision,
    cache_hit     boolean NOT NULL DEFAULT false,
    rewritten     boolean NOT NULL DEFAULT false,
    features      jsonb NOT NULL DEFAULT '{}'::jsonb,
    route_doc     jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Fleet identity: which node/endpoint executed the candidate (NULL = the
    -- brain's local engines). Populated once remote warren candidates exist.
    node          text,
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore')),
    CHECK (confidence IS NULL OR confidence >= 0)
);

CREATE INDEX IF NOT EXISTS route_decisions_time_idx
    ON rvbbit.route_decisions (decided_at DESC);

CREATE INDEX IF NOT EXISTS route_decisions_shape_idx
    ON rvbbit.route_decisions (shape_key, candidate, decided_at DESC);

CREATE INDEX IF NOT EXISTS route_decisions_source_idx
    ON rvbbit.route_decisions (route_source, decided_at DESC);

CREATE INDEX IF NOT EXISTS route_decisions_profile_idx
    ON rvbbit.route_decisions (profile_name, decided_at DESC);

ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD COLUMN IF NOT EXISTS profile_name text;

ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD COLUMN IF NOT EXISTS profile_source text NOT NULL DEFAULT 'unknown';

CREATE TABLE IF NOT EXISTS rvbbit.route_executions (
    id            bigserial PRIMARY KEY,
    executed_at   timestamptz NOT NULL DEFAULT now(),
    backend_pid   integer NOT NULL,
    database_name text NOT NULL,
    role_name     text NOT NULL,
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    shape_family  text NOT NULL,
    route         text NOT NULL,
    candidate     text,
    profile_name  text,
    profile_source text NOT NULL DEFAULT 'unknown',
    route_source  text NOT NULL,
    reason        text NOT NULL DEFAULT '',
    confidence    double precision,
    cache_hit     boolean NOT NULL DEFAULT false,
    rewritten     boolean NOT NULL DEFAULT false,
    elapsed_ms    double precision NOT NULL,
    rows_returned bigint NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'ok',
    features      jsonb NOT NULL DEFAULT '{}'::jsonb,
    route_doc     jsonb NOT NULL DEFAULT '{}'::jsonb,
    node          text,
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore')),
    CHECK (confidence IS NULL OR confidence >= 0),
    CHECK (elapsed_ms >= 0),
    CHECK (rows_returned >= 0)
);

CREATE INDEX IF NOT EXISTS route_executions_time_idx
    ON rvbbit.route_executions (executed_at DESC);

CREATE INDEX IF NOT EXISTS route_executions_shape_idx
    ON rvbbit.route_executions (shape_key, candidate, executed_at DESC);

CREATE INDEX IF NOT EXISTS route_executions_source_idx
    ON rvbbit.route_executions (route_source, executed_at DESC);

CREATE INDEX IF NOT EXISTS route_executions_profile_idx
    ON rvbbit.route_executions (profile_name, executed_at DESC);

ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD COLUMN IF NOT EXISTS profile_name text;

ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD COLUMN IF NOT EXISTS profile_source text NOT NULL DEFAULT 'unknown';

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

CREATE UNIQUE INDEX IF NOT EXISTS route_profiles_one_active_idx
    ON rvbbit.route_profiles ((active))
    WHERE active;

CREATE TABLE IF NOT EXISTS rvbbit.route_profile_entries (
    profile_name  text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    shape_key     text NOT NULL,
    choice        text NOT NULL,
    confidence    double precision NOT NULL DEFAULT 0,
    reason        text NOT NULL DEFAULT '',
    observations  bigint NOT NULL DEFAULT 0,
    native_ms     double precision,
    native_vortex_ms double precision,
    duck_ms       double precision,
    duck_hive_ms  double precision,
    duck_vortex_ms double precision,
    datafusion_ms double precision,
    datafusion_hive_ms double precision,
    datafusion_vortex_ms double precision,
    gpu_gqe_ms double precision,
    pg_ms         double precision,
    entry         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_name, shape_key),
    CHECK (choice IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore')),
    CHECK (confidence >= 0)
);

CREATE INDEX IF NOT EXISTS route_profile_entries_choice_idx
    ON rvbbit.route_profile_entries (choice, confidence DESC);

CREATE TABLE IF NOT EXISTS rvbbit.route_profile_points (
    id            bigserial PRIMARY KEY,
    profile_name  text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    shape_family  text NOT NULL,
    table_rows    bigint NOT NULL,
    native_ms     double precision NOT NULL,
    native_vortex_ms double precision,
    duck_ms       double precision NOT NULL,
    duck_hive_ms  double precision,
    duck_vortex_ms double precision,
    datafusion_ms double precision,
    datafusion_hive_ms double precision,
    datafusion_vortex_ms double precision,
    gpu_gqe_ms double precision,
    pg_ms         double precision,
    point         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (table_rows >= 0),
    CHECK (native_ms > 0),
    CHECK (native_vortex_ms IS NULL OR native_vortex_ms > 0),
    CHECK (duck_ms > 0),
    CHECK (duck_hive_ms IS NULL OR duck_hive_ms > 0),
    CHECK (duck_vortex_ms IS NULL OR duck_vortex_ms > 0),
    CHECK (datafusion_ms IS NULL OR datafusion_ms > 0),
    CHECK (datafusion_hive_ms IS NULL OR datafusion_hive_ms > 0),
    CHECK (datafusion_vortex_ms IS NULL OR datafusion_vortex_ms > 0),
    CHECK (gpu_gqe_ms IS NULL OR gpu_gqe_ms > 0),
    CHECK (pg_ms IS NULL OR pg_ms > 0)
);

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS native_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS native_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS duck_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS gpu_gqe_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS pg_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS duck_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_vortex_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS gpu_gqe_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

CREATE INDEX IF NOT EXISTS route_profile_points_family_idx
    ON rvbbit.route_profile_points (profile_name, shape_family, table_rows);

CREATE OR REPLACE VIEW rvbbit.route_decision_summary AS
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

CREATE OR REPLACE VIEW rvbbit.route_runtime_summary AS
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

CREATE OR REPLACE FUNCTION rvbbit.route_profiles_touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS route_profiles_touch_updated_at ON rvbbit.route_profiles;
CREATE TRIGGER route_profiles_touch_updated_at
    BEFORE UPDATE ON rvbbit.route_profiles
    FOR EACH ROW EXECUTE FUNCTION rvbbit.route_profiles_touch_updated_at();

DROP TRIGGER IF EXISTS route_profile_entries_touch_updated_at ON rvbbit.route_profile_entries;
CREATE TRIGGER route_profile_entries_touch_updated_at
    BEFORE UPDATE ON rvbbit.route_profile_entries
    FOR EACH ROW EXECUTE FUNCTION rvbbit.route_profiles_touch_updated_at();

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
    )
        AS needs_exploration,
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
"#,
    name = "create_route_catalog",
    requires = ["rvbbit_bootstrap"]
);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Candidate {
    DuckVector,
    DuckHive,
    DuckVortex,
    DataFusionMem,
    DataFusionVector,
    DataFusionHive,
    DataFusionVortex,
    GpuGqe,
    RvbbitNative,
    RvbbitNativeVortex,
    PgRowstore,
}

impl Candidate {
    fn all() -> [Self; 11] {
        [
            Candidate::DuckVector,
            Candidate::DuckHive,
            Candidate::DuckVortex,
            Candidate::DataFusionMem,
            Candidate::DataFusionVector,
            Candidate::DataFusionHive,
            Candidate::DataFusionVortex,
            Candidate::GpuGqe,
            Candidate::RvbbitNative,
            Candidate::RvbbitNativeVortex,
            Candidate::PgRowstore,
        ]
    }

    fn as_str(self) -> &'static str {
        match self {
            Candidate::DuckVector => "duck_vector",
            Candidate::DuckHive => "duck_hive",
            Candidate::DuckVortex => "duck_vortex",
            Candidate::DataFusionMem => "datafusion_mem",
            Candidate::DataFusionVector => "datafusion_vector",
            Candidate::DataFusionHive => "datafusion_hive",
            Candidate::DataFusionVortex => "datafusion_vortex",
            Candidate::GpuGqe => "gpu_gqe",
            Candidate::RvbbitNative => "rvbbit_native",
            Candidate::RvbbitNativeVortex => "rvbbit_native_vortex",
            Candidate::PgRowstore => "pg_rowstore",
        }
    }

    fn route(self) -> &'static str {
        match self {
            Candidate::DuckVector => "duck",
            Candidate::DuckHive => "duck_hive",
            Candidate::DuckVortex => "duck_vortex",
            Candidate::DataFusionMem => "datafusion_mem",
            Candidate::DataFusionVector => "datafusion",
            Candidate::DataFusionHive => "datafusion_hive",
            Candidate::DataFusionVortex => "datafusion_vortex",
            Candidate::GpuGqe => "gpu_gqe",
            Candidate::RvbbitNative => "native",
            // Falls through to the native CustomScan like RvbbitNative; the
            // NATIVE_VORTEX_ROUTE_SELECTED flag tells the scan to read vortex.
            Candidate::RvbbitNativeVortex => "native",
            Candidate::PgRowstore => "postgres_rowstore",
        }
    }

    fn from_str(s: &str) -> Option<Self> {
        match s {
            "duck_vector" | "duck" => Some(Candidate::DuckVector),
            "duck_hive" | "duck-hive" => Some(Candidate::DuckHive),
            "duck_vortex" | "duck-vortex" => Some(Candidate::DuckVortex),
            "datafusion_mem" | "datafusion-memory" | "df_mem" => Some(Candidate::DataFusionMem),
            "datafusion_vector" | "datafusion" | "df" => Some(Candidate::DataFusionVector),
            "datafusion_hive" | "datafusion-hive" | "df_hive" => Some(Candidate::DataFusionHive),
            "datafusion_vortex" | "datafusion-vortex" | "df_vortex" | "vortex" => {
                Some(Candidate::DataFusionVortex)
            }
            "gpu_gqe" | "gpu-gqe" | "gqe" | "gqe_parquet" => Some(Candidate::GpuGqe),
            "rvbbit_native" | "native" => Some(Candidate::RvbbitNative),
            "rvbbit_native_vortex" | "native_vortex" => Some(Candidate::RvbbitNativeVortex),
            "pg_rowstore" | "postgres_rowstore" => Some(Candidate::PgRowstore),
            _ => None,
        }
    }

    /// Execution engine family — the unit a per-table policy denies. native and
    /// pg_rowstore are the correctness floor and are never gated.
    fn engine(self) -> &'static str {
        match self {
            Candidate::DuckVector | Candidate::DuckHive | Candidate::DuckVortex => "duck",
            Candidate::DataFusionMem
            | Candidate::DataFusionVector
            | Candidate::DataFusionHive
            | Candidate::DataFusionVortex => "datafusion",
            Candidate::GpuGqe => "gpu_gqe",
            Candidate::RvbbitNative => "native",
            Candidate::RvbbitNativeVortex => "native",
            Candidate::PgRowstore => "pg_rowstore",
        }
    }

    /// Physical layout this candidate reads. Empty for the row-oriented paths
    /// (native / pg_rowstore), which have no columnar layout to deny.
    fn layout(self) -> &'static str {
        match self {
            Candidate::DuckVector | Candidate::DataFusionVector | Candidate::GpuGqe => "vector",
            Candidate::DuckHive | Candidate::DataFusionHive => "hive",
            Candidate::DuckVortex | Candidate::DataFusionVortex | Candidate::RvbbitNativeVortex => {
                "vortex"
            }
            Candidate::DataFusionMem => "mem",
            Candidate::RvbbitNative | Candidate::PgRowstore => "",
        }
    }
}

/// The physical storage actually read for this decision — a logging/observability
/// label only, NOT a routing input (the chosen `candidate` is unchanged).
///
/// `engine()`/`route()` collapse the whole native family to "native", which hides
/// three very different reads: a heap-only table (no row groups) plans a plain
/// PostgreSQL heap SeqScan (the custom scan bails at `n_rgs == 0`, planner.rs),
/// a compacted table uses the native vector scan over parquet, and the vortex
/// route reads `.vortex`. This splits them so the route log/UI shows
/// `native · heap` vs `native · parquet` vs `native · vortex`. Columnar engines
/// are gated on `row_groups > 0`, so they never see the heap case.
fn physical_path(candidate: Candidate, tables: &[RvbbitTableMetric]) -> &'static str {
    match candidate {
        Candidate::RvbbitNativeVortex | Candidate::DuckVortex | Candidate::DataFusionVortex => {
            "vortex"
        }
        Candidate::DuckHive | Candidate::DataFusionHive => "hive",
        Candidate::GpuGqe => "gpu_parquet",
        Candidate::DataFusionMem => "mem",
        Candidate::DuckVector | Candidate::DataFusionVector => "parquet",
        // pg_rowstore reads the retained shadow heap; native with no row groups
        // falls through to a plain PG heap SeqScan.
        Candidate::PgRowstore => "heap",
        Candidate::RvbbitNative => {
            let with_rg = tables.iter().filter(|t| t.row_groups > 0).count();
            if with_rg == 0 {
                "heap"
            } else if with_rg == tables.len() {
                "parquet"
            } else {
                // some referenced tables are compacted, some are heap-only —
                // each plans independently, so the query mixes both reads.
                "mixed"
            }
        }
    }
}

thread_local! {
    static PG_ROWSTORE_ROUTE_SELECTED: Cell<bool> = const { Cell::new(false) };
}

pub(crate) fn set_pg_rowstore_route_selected(selected: bool) {
    PG_ROWSTORE_ROUTE_SELECTED.with(|flag| flag.set(selected));
}

pub(crate) fn pg_rowstore_route_selected() -> bool {
    PG_ROWSTORE_ROUTE_SELECTED.with(|flag| flag.get())
}

thread_local! {
    /// Set by the rewriter when the router chooses `RvbbitNativeVortex` for this
    /// query, read by custom_scan's `fetch_best_row_group_paths` to read the
    /// vortex layout instead of canonical parquet — the per-query analogue of the
    /// global `rvbbit.native_vortex` GUC. Mirrors `PG_ROWSTORE_ROUTE_SELECTED`.
    static NATIVE_VORTEX_ROUTE_SELECTED: Cell<bool> = const { Cell::new(false) };
}

pub(crate) fn set_native_vortex_route_selected(selected: bool) {
    NATIVE_VORTEX_ROUTE_SELECTED.with(|flag| flag.set(selected));
}

pub(crate) fn native_vortex_route_selected() -> bool {
    // The rewriter sets the thread-local flag for profile/observation-driven
    // selection. We ALSO honor route_force_candidate directly here because (a) the
    // training harness forces each candidate via that GUC (execute_candidate_once),
    // and (b) it's a stable session GUC immune to the parse/plan re-computation that
    // can clobber the thread-local before the scan opens.
    NATIVE_VORTEX_ROUTE_SELECTED.with(|flag| flag.get())
        || forced_candidate_setting() == Some(Candidate::RvbbitNativeVortex)
}

thread_local! {
    /// Snapshot of NATIVE_VORTEX_ROUTE_SELECTED taken at planner-hook entry. The
    /// rewriter's flag survives parse but can be clobbered by route re-computation
    /// inside standard_planner before the CustomScan path is built; capturing into
    /// this separate cell at planner entry (then stashing into the scan node) is
    /// immune to those mid-plan resets.
    static NATIVE_VORTEX_PLAN_CAPTURED: Cell<bool> = const { Cell::new(false) };
}

pub(crate) fn set_native_vortex_plan_captured(selected: bool) {
    NATIVE_VORTEX_PLAN_CAPTURED.with(|flag| flag.set(selected));
}

pub(crate) fn native_vortex_plan_captured() -> bool {
    NATIVE_VORTEX_PLAN_CAPTURED.with(|flag| flag.get())
}

#[derive(Clone, Debug)]
struct RvbbitTableMetric {
    schema: String,
    relname: String,
    oid: u32,
    row_groups: i64,
    rows: i64,
    bytes: i64,
    heap_bytes: i64,
    shadow_heap_retained: bool,
    shadow_heap_dirty: bool,
    native_overlay_readable: bool,
    delete_count: i64,
    text_columns: Vec<String>,
    temporal_columns: Vec<String>,
    date_columns: Vec<String>,
    timestamp_columns: Vec<String>,
    /// Per-table engine/layout deny-sets from rvbbit.accel_policy. A candidate is
    /// gated out for this table if its engine() ∈ denied_engines or its layout()
    /// ∈ denied_layouts. Empty for tables with no policy row.
    denied_engines: Vec<String>,
    denied_layouts: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
struct RouteFeatures {
    normalized_sql: String,
    sql_hash: String,
    shape_key: String,
    shape_family: String,
    legacy_shape_key: String,
    legacy_shape_family: String,
    starts_with_with: bool,
    is_select: bool,
    select_star: bool,
    from_count: i64,
    join_count: i64,
    #[serde(rename = "where")]
    where_present: bool,
    group_by: bool,
    order_by: bool,
    having: bool,
    distinct: bool,
    count_distinct_count: i64,
    aggregate_count: i64,
    sum_count: i64,
    avg_count: i64,
    count_count: i64,
    min_count: i64,
    max_count: i64,
    referenced_text_col_count: i64,
    group_text_col_count: i64,
    order_text_col_count: i64,
    count_distinct_text_count: i64,
    exists_count: i64,
    in_count: i64,
    between_count: i64,
    or_count: i64,
    and_count: i64,
    comparison_count: i64,
    like_count: i64,
    not_like_count: i64,
    fixed_contains_like_count: i64,
    regex_count: i64,
    limit_bucket: String,
    offset_present: bool,
    group_expr_count_bucket: String,
    group_expr_signature: String,
    order_expr_count_bucket: String,
    order_expr_signature: String,
    count_distinct_signature: String,
    plan_available: bool,
    plan_has_group: bool,
    plan_has_hash: bool,
    plan_has_join: bool,
    plan_has_sort: bool,
    plan_has_subplan: bool,
    native_function: Option<String>,
    has_native_function: bool,
    plan_width_bucket: String,
    table_rows: i64,
    table_rows_bucket: String,
    table_bytes: i64,
    table_bytes_bucket: String,
    row_group_count: i64,
    row_group_count_bucket: String,
}

impl RouteFeatures {
    fn to_json(&self) -> Value {
        serde_json::to_value(self).unwrap_or_else(|_| json!({}))
    }
}

#[derive(Clone, Debug)]
struct RouteDecision {
    route: &'static str,
    candidate: Option<Candidate>,
    source: &'static str,
    reason: String,
    confidence: Option<f64>,
    profile_entry: Option<Value>,
}

#[derive(Clone, Debug)]
struct RouteProfileSelection {
    requested: Option<String>,
    effective: Option<String>,
    source: &'static str,
    warning: Option<String>,
    updated_epoch: Option<String>,
}

#[derive(Clone, Copy, Debug, Default)]
struct RouteCurveSample {
    native_ms: Option<f64>,
    native_vortex_ms: Option<f64>,
    duck_ms: Option<f64>,
    duck_hive_ms: Option<f64>,
    duck_vortex_ms: Option<f64>,
    datafusion_ms: Option<f64>,
    datafusion_hive_ms: Option<f64>,
    datafusion_vortex_ms: Option<f64>,
    gpu_gqe_ms: Option<f64>,
    pg_ms: Option<f64>,
}

#[derive(Default)]
struct CandidateBuckets {
    native: Vec<f64>,
    native_vortex: Vec<f64>,
    duck: Vec<f64>,
    duck_hive: Vec<f64>,
    duck_vortex: Vec<f64>,
    datafusion: Vec<f64>,
    datafusion_hive: Vec<f64>,
    datafusion_vortex: Vec<f64>,
    gpu_gqe: Vec<f64>,
    pg: Vec<f64>,
}

#[derive(Clone, Debug)]
struct CandidateExecution {
    elapsed_ms: f64,
    rows_returned: i64,
    result_digest: String,
    route_doc: Value,
}

#[derive(Clone, Debug)]
struct TrainingRunResult {
    candidate: Candidate,
    repeat_idx: i32,
    elapsed_ms: Option<f64>,
    rows_returned: Option<i64>,
    result_digest: Option<String>,
    status: String,
    validation_status: String,
    error: Option<String>,
    route_doc: Value,
}

#[derive(Clone, Debug)]
struct TrainingObservation {
    shape_key: String,
    shape_family: String,
    features: Value,
    candidate: Candidate,
    elapsed_ms: f64,
}

impl RouteCurveSample {
    fn has_at_least_two(self) -> bool {
        [
            self.native_ms,
            self.native_vortex_ms,
            self.duck_ms,
            self.duck_hive_ms,
            self.duck_vortex_ms,
            self.datafusion_ms,
            self.datafusion_hive_ms,
            self.datafusion_vortex_ms,
            self.gpu_gqe_ms,
            self.pg_ms,
        ]
        .into_iter()
        .flatten()
        .count()
            >= 2
    }
}

#[pg_extern(volatile)]
fn route_explain(query: &str) -> JsonB {
    JsonB(route_explain_value(query, true))
}

#[pg_extern(volatile)]
fn route_shadow_explain(query: &str, log: default!(bool, "false")) -> JsonB {
    JsonB(route_explain_value_inner(query, true, true, log))
}

#[pg_extern(volatile)]
fn route_explain_text(query: &str) -> String {
    format_route_explain_text(&route_explain_value(query, true))
}

#[pg_extern(volatile)]
fn route_features(query: &str) -> JsonB {
    let safe = safe_select(query);
    let plan = if safe.is_ok() {
        explain_sql(query).ok()
    } else {
        None
    };
    let tables = referenced_rvbbit_tables(query, plan.as_deref());
    let features = build_features(query, plan.as_deref(), &tables);
    JsonB(features.to_json())
}

#[pg_extern(volatile)]
fn route_record_observation(
    query: &str,
    candidate: &str,
    elapsed_ms: f64,
    status: &str,
    source: &str,
) -> JsonB {
    let Some(candidate) = Candidate::from_str(candidate) else {
        pgrx::error!(
            "rvbbit.route_record_observation: unknown candidate '{}'",
            candidate
        );
    };
    if !elapsed_ms.is_finite() || elapsed_ms < 0.0 {
        pgrx::error!("rvbbit.route_record_observation: elapsed_ms must be finite and >= 0");
    }
    if let Err(reason) = safe_select(query) {
        pgrx::error!("rvbbit.route_record_observation: {reason}");
    }

    let plan = explain_sql(query).ok();
    let tables = referenced_rvbbit_tables(query, plan.as_deref());
    let features = build_features(query, plan.as_deref(), &tables);
    let features_json = features.to_json();
    let features_lit = sql_json_lit(&features_json);
    let status_lit = sql_lit(status);
    let source_lit = sql_lit(source);
    let candidate_lit = sql_lit(candidate.as_str());
    let hash_lit = sql_lit(&features.sql_hash);
    let shape_lit = sql_lit(&features.shape_key);
    let family_lit = sql_lit(&features.shape_family);
    let inserted: i64 = Spi::get_one(&format!(
        "INSERT INTO rvbbit.route_observations \
         (source, query_hash, shape_key, shape_family, features, candidate, elapsed_ms, status) \
         VALUES ({source_lit}, {hash_lit}, {shape_lit}, {family_lit}, {features_lit}::jsonb, \
                 {candidate_lit}, {elapsed_ms}, {status_lit}) \
         RETURNING id"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);

    JsonB(json!({
        "observation_id": inserted,
        "candidate": candidate.as_str(),
        "shape_key": features.shape_key,
        "shape_family": features.shape_family,
        "query_hash": features.sql_hash,
        "features": features_json,
    }))
}

#[pg_extern(volatile)]
fn route_train(profile_name: &str, min_observations: i64, min_gain_pct: f64) -> JsonB {
    let profile = train_profile(min_observations.max(1), min_gain_pct.max(0.0));
    let entries = profile
        .get("entries")
        .and_then(Value::as_object)
        .map(|o| o.len())
        .unwrap_or(0);
    let rejected = profile
        .get("rejected")
        .and_then(Value::as_object)
        .map(|o| o.len())
        .unwrap_or(0);
    let activate = entries > 0;
    if !activate {
        return JsonB(json!({
            "profile": profile_name,
            "entries": entries,
            "rejected": rejected,
            "active": false,
            "reason": "profile not activated: no accepted shapes",
            "profile_json": compact_profile_for_storage(&profile),
        }));
    }
    let (stored_entries, stored_points, stored_profile) =
        store_route_profile(profile_name, &profile, activate, "route_train");

    JsonB(json!({
        "profile": profile_name,
        "entries": stored_entries,
        "rejected": rejected,
        "points": stored_points,
        "active": activate,
        "reason": if activate { "profile activated" } else { "profile not activated: no accepted shapes" },
        "profile_json": stored_profile,
    }))
}

#[pg_extern(volatile)]
fn route_create_profile(profile_name: &str, active: default!(bool, "false")) -> JsonB {
    let name = validate_route_profile_name(profile_name, "route_create_profile");
    ensure_route_profile_row(name, active, "route_create_profile");
    if active {
        route_activate_profile(name);
    }
    JsonB(profile_lifecycle_summary(name, "created"))
}

#[pg_extern(volatile)]
fn route_train_query(
    profile_name: &str,
    query: &str,
    repeats: default!(i32, "3"),
    min_gain_pct: default!(f64, "0.05"),
    activate: default!(bool, "true"),
    candidates: default!(&str, "'all'"),
    label: default!(&str, "''"),
) -> JsonB {
    let profile = validate_route_profile_name(profile_name, "route_train_query");
    if let Err(reason) = safe_select(query) {
        pgrx::error!("rvbbit.route_train_query: {reason}");
    }
    if !min_gain_pct.is_finite() || min_gain_pct < 0.0 {
        pgrx::error!("rvbbit.route_train_query: min_gain_pct must be finite and >= 0");
    }
    let repeats = repeats.max(1).min(100);
    let plan = explain_sql(query).ok();
    let tables = referenced_rvbbit_tables(query, plan.as_deref());
    if tables.is_empty() {
        pgrx::error!("rvbbit.route_train_query: query does not reference an rvbbit table");
    }
    let features = build_features(query, plan.as_deref(), &tables);
    let features_json = features.to_json();
    let candidates = parse_training_candidates(candidates, "route_train_query");
    ensure_route_profile_row(profile, false, "route_train_query");

    let training_query_id = upsert_training_query(profile, query, &features, &features_json, label);
    let run_id = insert_training_run(
        profile,
        training_query_id,
        repeats,
        &candidates,
        json!({
            "min_gain_pct": min_gain_pct,
            "activate": activate,
            "candidate_request": candidates.iter().map(|c| c.as_str()).collect::<Vec<_>>(),
        }),
    );

    let mut run_results = Vec::new();
    let mut baseline_digest: Option<String> = None;
    let mut baseline_rows: Option<i64> = None;
    for candidate in &candidates {
        let (available, reason) = candidate_availability(*candidate, &features, &tables);
        if !available {
            let skipped = TrainingRunResult {
                candidate: *candidate,
                repeat_idx: 1,
                elapsed_ms: None,
                rows_returned: None,
                result_digest: None,
                status: "skipped".to_string(),
                validation_status: "skipped".to_string(),
                error: Some(reason),
                route_doc: json!({}),
            };
            insert_training_result(profile, training_query_id, run_id, &skipped);
            run_results.push(skipped);
            continue;
        }

        for repeat_idx in 1..=repeats {
            let execution = execute_candidate_once(query, *candidate, features.order_by);
            let result = match execution {
                Ok(outcome) => {
                    let validation_status =
                        if *candidate == Candidate::RvbbitNative && baseline_digest.is_none() {
                            baseline_digest = Some(outcome.result_digest.clone());
                            baseline_rows = Some(outcome.rows_returned);
                            "baseline".to_string()
                        } else if let Some(baseline) = baseline_digest.as_deref() {
                            if baseline == outcome.result_digest
                                && baseline_rows == Some(outcome.rows_returned)
                            {
                                "ok".to_string()
                            } else {
                                "mismatch".to_string()
                            }
                        } else if *candidate == Candidate::RvbbitNative {
                            "baseline".to_string()
                        } else {
                            "no_baseline".to_string()
                        };
                    TrainingRunResult {
                        candidate: *candidate,
                        repeat_idx,
                        elapsed_ms: Some(outcome.elapsed_ms),
                        rows_returned: Some(outcome.rows_returned),
                        result_digest: Some(outcome.result_digest),
                        status: "ok".to_string(),
                        validation_status,
                        error: None,
                        route_doc: outcome.route_doc,
                    }
                }
                Err(error) => TrainingRunResult {
                    candidate: *candidate,
                    repeat_idx,
                    elapsed_ms: None,
                    rows_returned: None,
                    result_digest: None,
                    status: "error".to_string(),
                    validation_status: "error".to_string(),
                    error: Some(error),
                    route_doc: json!({}),
                },
            };

            insert_training_result(profile, training_query_id, run_id, &result);
            if result.status == "ok"
                && matches!(result.validation_status.as_str(), "baseline" | "ok")
                && result.elapsed_ms.is_some()
            {
                insert_route_observation(
                    query,
                    &features,
                    &features_json,
                    result.candidate,
                    result.elapsed_ms.unwrap_or(0.0),
                    "ok",
                    &format!("sql-train:{profile}:{run_id}"),
                );
            }
            run_results.push(result);
        }
    }

    let rebuild = route_profile_rebuild_inner(profile, min_gain_pct, activate);
    let summary = training_run_summary(profile, training_query_id, run_id, &run_results, rebuild);
    finish_training_run(run_id, "finished", &summary);
    JsonB(summary)
}

#[pg_extern(volatile)]
fn route_profile_rebuild(
    profile_name: &str,
    min_gain_pct: default!(f64, "0.05"),
    activate: default!(bool, "true"),
) -> JsonB {
    let profile = validate_route_profile_name(profile_name, "route_profile_rebuild");
    if !min_gain_pct.is_finite() || min_gain_pct < 0.0 {
        pgrx::error!("rvbbit.route_profile_rebuild: min_gain_pct must be finite and >= 0");
    }
    ensure_route_profile_row(profile, false, "route_profile_rebuild");
    JsonB(route_profile_rebuild_inner(profile, min_gain_pct, activate))
}

#[pg_extern(volatile)]
fn route_training_delete_query(
    profile_name: &str,
    training_query_id: i64,
    rebuild: default!(bool, "true"),
) -> JsonB {
    let profile = validate_route_profile_name(profile_name, "route_training_delete_query");
    let name_lit = sql_lit(profile);
    let id = training_query_id.max(0);
    let deleted: i64 = Spi::get_one(&format!(
        "WITH deleted AS ( \
             DELETE FROM rvbbit.route_training_queries \
             WHERE profile_name = {name_lit} AND id = {id} \
             RETURNING 1 \
         ) SELECT count(*)::bigint FROM deleted"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    if deleted == 0 {
        pgrx::error!(
            "rvbbit.route_training_delete_query: training query {} was not found in profile '{}'",
            training_query_id,
            profile
        );
    }
    let rebuild_summary = rebuild.then(|| route_profile_rebuild_inner(profile, 0.05, true));
    JsonB(json!({
        "action": "deleted",
        "profile": profile,
        "training_query_id": training_query_id,
        "deleted": deleted,
        "rebuild": rebuild_summary,
    }))
}

#[pg_extern(volatile)]
fn route_eval(profile_name: &str) -> JsonB {
    let name_lit = sql_lit(profile_name);
    let active: bool = Spi::get_one(&format!(
        "SELECT coalesce((SELECT active FROM rvbbit.route_profiles WHERE name = {name_lit}), false)"
    ))
    .ok()
    .flatten()
    .unwrap_or(false);
    let entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let duck_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'duck_vector'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let duck_hive_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'duck_hive'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let duck_vortex_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'duck_vortex'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let datafusion_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'datafusion_vector'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let datafusion_mem_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'datafusion_mem'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let datafusion_hive_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'datafusion_hive'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let datafusion_vortex_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'datafusion_vortex'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let gpu_gqe_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'gpu_gqe'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let native_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND choice = 'rvbbit_native'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let avg_confidence: f64 = Spi::get_one(&format!(
        "SELECT coalesce(avg(confidence), 0)::double precision \
         FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))
    .ok()
    .flatten()
    .unwrap_or(0.0);
    let low_confidence_entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries \
         WHERE profile_name = {name_lit} AND confidence < 0.10"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let observed_shapes: i64 =
        Spi::get_one("SELECT count(*)::bigint FROM rvbbit.route_shape_summary")
            .ok()
            .flatten()
            .unwrap_or(0);
    let shapes_needing_exploration: i64 = Spi::get_one(
        "SELECT count(*)::bigint FROM rvbbit.route_shape_summary WHERE needs_exploration",
    )
    .ok()
    .flatten()
    .unwrap_or(0);
    let observations: i64 =
        Spi::get_one("SELECT count(*)::bigint FROM rvbbit.route_observations WHERE status = 'ok'")
            .ok()
            .flatten()
            .unwrap_or(0);
    let explore_observations: i64 = Spi::get_one(
        "SELECT count(*)::bigint FROM rvbbit.route_observations \
         WHERE status = 'ok' AND source LIKE 'explore:%'",
    )
    .ok()
    .flatten()
    .unwrap_or(0);

    JsonB(json!({
        "profile": profile_name,
        "active": active,
        "entries": entries,
        "duck_entries": duck_entries,
        "duck_hive_entries": duck_hive_entries,
        "duck_vortex_entries": duck_vortex_entries,
        "datafusion_mem_entries": datafusion_mem_entries,
        "datafusion_entries": datafusion_entries,
        "datafusion_hive_entries": datafusion_hive_entries,
        "datafusion_vortex_entries": datafusion_vortex_entries,
        "gpu_gqe_entries": gpu_gqe_entries,
        "native_entries": native_entries,
        "avg_confidence": avg_confidence,
        "low_confidence_entries": low_confidence_entries,
        "observed_shapes": observed_shapes,
        "shapes_needing_exploration": shapes_needing_exploration,
        "observations": observations,
        "explore_observations": explore_observations,
    }))
}

#[pg_extern]
fn route_current_profile() -> JsonB {
    JsonB(route_profile_selection_json(&route_profile_selection()))
}

#[pg_extern]
fn route_profiles() -> JsonB {
    JsonB(route_profiles_json())
}

#[pg_extern]
fn route_status() -> JsonB {
    let profile = route_profile_selection();
    JsonB(json!({
        "current_profile": route_profile_selection_json(&profile),
        "profiles": route_profiles_json(),
        "candidate_gates": Candidate::all()
            .into_iter()
            .map(|candidate| json!({
                "candidate": candidate.as_str(),
                "route": candidate.route(),
                "enabled": candidate_gate_enabled(candidate),
                "min_confidence": min_confidence_for_candidate(candidate),
            }))
            .collect::<Vec<_>>(),
        "runtime": {
            "accelerator": crate::duck_backend::accelerator_runtime_status_value(false),
            "duck_backend_enabled": crate::duck_backend::backend_enabled(),
            "duck_backend_fail_open": crate::duck_backend::fail_open_enabled(),
        },
        "catalog": route_catalog_counts_json(),
    }))
}

#[pg_extern]
fn route_use_profile(profile_name: &str, local: default!(bool, "true")) -> JsonB {
    let trimmed = profile_name.trim();
    if trimmed.is_empty() {
        pgrx::error!("rvbbit.route_use_profile: profile_name must not be empty");
    }
    ensure_profile_exists(trimmed, "route_use_profile");
    let is_local = if local { "true" } else { "false" };
    Spi::run(&format!(
        "SELECT pg_catalog.set_config('rvbbit.route_profile', {}, {is_local})",
        sql_lit(trimmed)
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_use_profile: {e}"));
    JsonB(route_profile_selection_json(&route_profile_selection()))
}

#[pg_extern]
fn route_clear_profile(local: default!(bool, "true")) -> JsonB {
    let is_local = if local { "true" } else { "false" };
    Spi::run(&format!(
        "SELECT pg_catalog.set_config('rvbbit.route_profile', '', {is_local})"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_clear_profile: {e}"));
    JsonB(route_profile_selection_json(&route_profile_selection()))
}

#[pg_extern(volatile)]
fn route_set_profile(profile_name: &str, profile: JsonB, active: bool) -> JsonB {
    let (stored_entries, stored_points, _) =
        store_route_profile(profile_name, &profile.0, active, "route_set_profile");
    JsonB(json!({
        "profile": profile_name,
        "active": active,
        "stored_entries": stored_entries,
        "stored_points": stored_points,
    }))
}

#[pg_extern(volatile)]
fn route_export_profile(profile_name: &str) -> JsonB {
    ensure_profile_exists(profile_name, "route_export_profile");
    JsonB(export_route_profile_value(
        profile_name,
        "route_export_profile",
    ))
}

#[pg_extern(volatile)]
fn route_import_profile(profile_name: &str, profile: JsonB, active: bool) -> JsonB {
    let mut imported = profile.0;
    if !imported.is_object() {
        pgrx::error!("rvbbit.route_import_profile: profile must be a JSON object");
    }
    if let Value::Object(map) = &mut imported {
        if let Some(source_name) = map.get("name").and_then(Value::as_str) {
            if source_name != profile_name {
                map.insert("imported_from_name".into(), json!(source_name));
            }
        }
        map.insert("imported_as".into(), json!(profile_name));
        map.insert(
            "imported_by".into(),
            json!("pg_rvbbit.route_import_profile"),
        );
    }
    let (stored_entries, stored_points, _) =
        store_route_profile(profile_name, &imported, active, "route_import_profile");
    JsonB(json!({
        "action": "imported",
        "profile": profile_name,
        "active": active,
        "stored_entries": stored_entries,
        "stored_points": stored_points,
    }))
}

#[pg_extern(volatile)]
fn route_activate_profile(profile_name: &str) -> JsonB {
    ensure_profile_exists(profile_name, "route_activate_profile");
    let name_lit = sql_lit(profile_name);
    Spi::run("UPDATE rvbbit.route_profiles SET active = false WHERE active")
        .unwrap_or_else(|e| pgrx::error!("rvbbit.route_activate_profile: {e}"));
    Spi::run(&format!(
        "UPDATE rvbbit.route_profiles SET active = true WHERE name = {name_lit}"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_activate_profile: {e}"));
    JsonB(profile_lifecycle_summary(profile_name, "activated"))
}

#[pg_extern(volatile)]
fn route_retire_profile(profile_name: &str) -> JsonB {
    ensure_profile_exists(profile_name, "route_retire_profile");
    let name_lit = sql_lit(profile_name);
    let was_active: bool = Spi::get_one(&format!(
        "SELECT coalesce((SELECT active FROM rvbbit.route_profiles WHERE name = {name_lit}), false)"
    ))
    .ok()
    .flatten()
    .unwrap_or(false);
    Spi::run(&format!(
        "UPDATE rvbbit.route_profiles SET active = false WHERE name = {name_lit}"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_retire_profile: {e}"));
    let mut summary = profile_lifecycle_summary(profile_name, "retired");
    if let Value::Object(map) = &mut summary {
        map.insert("was_active".into(), json!(was_active));
    }
    JsonB(summary)
}

#[pg_extern(volatile)]
fn route_clone_profile(source_profile: &str, target_profile: &str, active: bool) -> JsonB {
    if source_profile == target_profile {
        pgrx::error!("rvbbit.route_clone_profile: source and target must differ");
    }
    ensure_profile_exists(source_profile, "route_clone_profile");
    let source_lit = sql_lit(source_profile);
    let target_lit = sql_lit(target_profile);
    let mut metadata = Spi::get_one::<JsonB>(&format!(
        "SELECT profile FROM rvbbit.route_profiles WHERE name = {source_lit}"
    ))
    .ok()
    .flatten()
    .map(|j| j.0)
    .unwrap_or_else(|| json!({}));
    if let Value::Object(map) = &mut metadata {
        map.insert("generated_by".into(), json!("pg_rvbbit.router_lifecycle"));
        map.insert("cloned_from".into(), json!(source_profile));
    }
    if active {
        Spi::run("UPDATE rvbbit.route_profiles SET active = false WHERE active")
            .unwrap_or_else(|e| pgrx::error!("rvbbit.route_clone_profile: {e}"));
    }
    let metadata_lit = sql_json_lit(&compact_profile_for_storage(&metadata));
    Spi::run(&format!(
        "INSERT INTO rvbbit.route_profiles (name, active, profile) \
         VALUES ({target_lit}, {active}, {metadata_lit}::jsonb) \
         ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active, profile = EXCLUDED.profile"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_clone_profile: {e}"));
    replace_profile_entries_from_source(target_profile, source_profile, "route_clone_profile");
    replace_profile_points_from_source(target_profile, source_profile, "route_clone_profile");
    let (entries, points) =
        refresh_profile_json_from_tables(target_profile, metadata, "route_clone_profile");
    JsonB(json!({
        "action": "cloned",
        "source_profile": source_profile,
        "profile": target_profile,
        "active": active,
        "entries": entries,
        "points": points,
    }))
}

#[pg_extern(volatile)]
fn route_merge_profiles(target_profile: &str, source_profiles: JsonB, active: bool) -> JsonB {
    let sources = parse_profile_list(&source_profiles.0, "route_merge_profiles");
    if sources.is_empty() {
        pgrx::error!("rvbbit.route_merge_profiles: source_profiles must not be empty");
    }
    if sources.iter().any(|source| source == target_profile) {
        pgrx::error!("rvbbit.route_merge_profiles: target profile cannot also be a source");
    }
    for source in &sources {
        ensure_profile_exists(source, "route_merge_profiles");
    }
    if active {
        Spi::run("UPDATE rvbbit.route_profiles SET active = false WHERE active")
            .unwrap_or_else(|e| pgrx::error!("rvbbit.route_merge_profiles: {e}"));
    }
    let target_lit = sql_lit(target_profile);
    let metadata = json!({
        "version": 1,
        "kind": "rvbbit_route_profile",
        "generated_by": "pg_rvbbit.router_lifecycle",
        "merged_from": sources,
    });
    let metadata_lit = sql_json_lit(&metadata);
    Spi::run(&format!(
        "INSERT INTO rvbbit.route_profiles (name, active, profile) \
         VALUES ({target_lit}, {active}, {metadata_lit}::jsonb) \
         ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active, profile = EXCLUDED.profile"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_merge_profiles: {e}"));
    clear_profile_entries(target_profile, "route_merge_profiles");
    clear_profile_points(target_profile, "route_merge_profiles");
    for source in &sources {
        copy_profile_entries(target_profile, source, "route_merge_profiles");
        copy_profile_points(target_profile, source, "route_merge_profiles");
    }
    let (entries, points) =
        refresh_profile_json_from_tables(target_profile, metadata, "route_merge_profiles");
    JsonB(json!({
        "action": "merged",
        "profile": target_profile,
        "sources": sources,
        "active": active,
        "entries": entries,
        "points": points,
    }))
}

pub(crate) fn route_explain_value(query: &str, include_plan: bool) -> Value {
    route_explain_value_inner(query, include_plan, false, false)
}

fn route_explain_value_inner(
    query: &str,
    include_plan: bool,
    include_shadow: bool,
    log_shadow: bool,
) -> Value {
    let mut out = Map::new();
    out.insert("route".into(), json!("none"));
    out.insert("chosen_candidate".into(), Value::Null);
    out.insert("route_source".into(), json!("none"));
    out.insert("reason".into(), Value::Null);
    out.insert("safe_select".into(), json!(false));
    out.insert("fallback".into(), json!("postgres"));
    let profile = route_profile_selection();
    insert_profile_selection_json(&mut out, &profile);

    if let Err(reason) = safe_select(query) {
        out.insert("reason".into(), json!(reason));
        out.insert("candidates".into(), json!([]));
        return Value::Object(out);
    }
    out.insert("safe_select".into(), json!(true));

    let plan = explain_sql(query).ok();
    if include_plan {
        out.insert(
            "postgres_explain".into(),
            plan.clone().map_or(Value::Null, Value::String),
        );
    }

    let tables = referenced_rvbbit_tables(query, plan.as_deref());
    out.insert(
        "rvbbit_tables".into(),
        Value::Array(tables.iter().map(table_metric_json).collect()),
    );
    if tables.is_empty() {
        out.insert(
            "reason".into(),
            json!("query does not reference Rvbbit tables"),
        );
        out.insert("candidates".into(), json!([]));
        return Value::Object(out);
    }

    let features = build_features(query, plan.as_deref(), &tables);
    let decision = choose_route(&features, &tables, &profile);
    let candidate = decision.candidate;
    out.insert("route".into(), json!(decision.route));
    out.insert(
        "chosen_candidate".into(),
        candidate.map_or(Value::Null, |c| json!(c.as_str())),
    );
    out.insert(
        "physical_path".into(),
        candidate.map_or(Value::Null, |c| json!(physical_path(c, &tables))),
    );
    out.insert("route_source".into(), json!(decision.source));
    out.insert("reason".into(), json!(decision.reason));
    out.insert(
        "confidence".into(),
        decision.confidence.map_or(Value::Null, |v| json!(v)),
    );
    if let Some(entry) = decision.profile_entry {
        out.insert("route_entry".into(), entry);
    }
    out.insert("features".into(), features.to_json());
    out.insert("table_metrics".into(), aggregate_metrics_json(&tables));
    out.insert(
        "candidates".into(),
        candidate_list_json(candidate, &features, &tables),
    );
    if include_shadow {
        let shadow = shadow_learned_route_json(query, &features, &tables, candidate, log_shadow);
        out.insert("shadow_learned_route".into(), shadow);
    }
    Value::Object(out)
}

pub(crate) fn route_rewrite_value(query: &str) -> Value {
    if let Some(fast) = route_rewrite_value_fast(query) {
        return fast;
    }
    route_explain_value(query, false)
}

fn route_rewrite_value_fast(query: &str) -> Option<Value> {
    if safe_select(query).is_err() {
        return None;
    }
    let tables = referenced_rvbbit_tables(query, None);
    if tables.is_empty() {
        return None;
    }
    let features = build_features(query, None, &tables);
    let profile = route_profile_selection();
    let decision = choose_route_fast(&features, &tables, &profile)?;
    Some(route_doc_from_decision(
        decision, &features, &tables, &profile, false,
    ))
}

fn route_doc_from_decision(
    decision: RouteDecision,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
    include_candidates: bool,
) -> Value {
    let candidate = decision.candidate;
    let mut out = Map::new();
    out.insert("route".into(), json!(decision.route));
    out.insert(
        "chosen_candidate".into(),
        candidate.map_or(Value::Null, |c| json!(c.as_str())),
    );
    out.insert(
        "physical_path".into(),
        candidate.map_or(Value::Null, |c| json!(physical_path(c, tables))),
    );
    out.insert("route_source".into(), json!(decision.source));
    out.insert("reason".into(), json!(decision.reason));
    out.insert("safe_select".into(), json!(true));
    out.insert("fallback".into(), json!("postgres"));
    insert_profile_selection_json(&mut out, profile);
    out.insert(
        "confidence".into(),
        decision.confidence.map_or(Value::Null, |v| json!(v)),
    );
    if let Some(entry) = decision.profile_entry {
        out.insert("route_entry".into(), entry);
    }
    out.insert("features".into(), features.to_json());
    out.insert("table_metrics".into(), aggregate_metrics_json(tables));
    out.insert(
        "rvbbit_tables".into(),
        Value::Array(tables.iter().map(table_metric_json).collect()),
    );
    if include_candidates {
        out.insert(
            "candidates".into(),
            candidate_list_json(candidate, features, tables),
        );
    }
    Value::Object(out)
}

fn shadow_learned_route_json(
    query: &str,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    chosen_candidate: Option<Candidate>,
    log_shadow: bool,
) -> Value {
    let Some(decision) = choose_shadow_learned_route(features, tables) else {
        return json!({
            "available": false,
            "reason": "insufficient route observations for this shape"
        });
    };
    let value = route_decision_json(&decision);
    if log_shadow {
        log_shadow_decision(query, features, chosen_candidate, &decision, &value);
    }
    value
}

fn route_decision_json(decision: &RouteDecision) -> Value {
    json!({
        "available": true,
        "route": decision.route,
        "candidate": decision.candidate.map(|c| c.as_str()),
        "source": decision.source,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "entry": decision.profile_entry.clone(),
    })
}

fn log_shadow_decision(
    _query: &str,
    features: &RouteFeatures,
    chosen_candidate: Option<Candidate>,
    decision: &RouteDecision,
    decision_json: &Value,
) {
    if !relations_present(&["rvbbit.route_shadow_decisions"]) {
        return;
    }
    let chosen_sql = chosen_candidate
        .map(|c| format!("{}::text", sql_lit(c.as_str())))
        .unwrap_or_else(|| "NULL::text".to_string());
    let shadow_sql = decision
        .candidate
        .map(|c| format!("{}::text", sql_lit(c.as_str())))
        .unwrap_or_else(|| "NULL::text".to_string());
    let confidence_sql = decision
        .confidence
        .filter(|v| v.is_finite() && *v >= 0.0)
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NULL::double precision".to_string());
    let _ = Spi::run(&format!(
        "INSERT INTO rvbbit.route_shadow_decisions \
             (query_hash, shape_key, shape_family, chosen_candidate, shadow_candidate, \
              shadow_source, confidence, table_rows, features, decision) \
         VALUES ({}, {}, {}, {chosen_sql}, {shadow_sql}, {}, {confidence_sql}, {}, {}::jsonb, {}::jsonb)",
        sql_lit(&features.sql_hash),
        sql_lit(&features.shape_key),
        sql_lit(&features.shape_family),
        sql_lit(decision.source),
        features.table_rows,
        sql_json_lit(&features.to_json()),
        sql_json_lit(decision_json),
    ));
}

fn insert_profile_selection_json(out: &mut Map<String, Value>, profile: &RouteProfileSelection) {
    out.insert(
        "profile_name".into(),
        profile.effective.clone().map_or(Value::Null, Value::String),
    );
    out.insert("profile_source".into(), json!(profile.source));
    out.insert(
        "requested_profile".into(),
        profile.requested.clone().map_or(Value::Null, Value::String),
    );
    out.insert(
        "profile_updated_epoch".into(),
        profile
            .updated_epoch
            .clone()
            .map_or(Value::Null, Value::String),
    );
    if let Some(warning) = &profile.warning {
        out.insert("profile_warning".into(), json!(warning));
    }
}

thread_local! {
    // routing-overlay: shape_key -> tested engine pin (enabled rows only), memoized per-backend
    // under the same TTL contract as the other route memos (RVBBIT_ROUTE_STAMP_TTL_MS). The
    // overlay changes only on an explicit train, so this is a hashmap hit per query. A
    // <=TTL-stale map only affects which engine is CHOSEN — the pin still passes
    // candidate_availability in overlay_decision, so it never changes correctness.
    static OVERLAY_MEMO: std::cell::RefCell<Option<(Instant, std::collections::HashMap<String, OverlayPin>)>> =
        const { std::cell::RefCell::new(None) };

    // gpu-gqe warm-prior: is the GQE server confirmed warm & functional (fresh
    // rvbbit.gqe_warm_state)? Memoized per-backend under the same TTL contract as
    // OVERLAY_MEMO. Only read when rvbbit.route_gpu_gqe_prior is enabled, so it
    // adds nothing for the default (prior-off) config.
    static GQE_WARM_MEMO: std::cell::RefCell<Option<(Instant, bool)>> =
        const { std::cell::RefCell::new(None) };

    // ML routing layer: engine-name -> latency model, loaded from
    // rvbbit.route_model and memoized per-backend under the route memo TTL. Only
    // touched when rvbbit.route_ml_enabled is on. None until first load; an empty
    // map (no trained models) means the ML layer is a no-op.
    static ML_MODEL_MEMO: std::cell::RefCell<Option<(Instant, std::rc::Rc<std::collections::HashMap<String, crate::route_model::EngineModel>>)>> =
        const { std::cell::RefCell::new(None) };

    // Set by the trainer while it computes the TRUE base decision (what base rules would pick
    // with no pin present). Never set on the normal query path.
    static OVERLAY_BYPASS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

#[derive(Clone, Debug)]
struct OverlayPin {
    engine: Candidate,
    sample_order: Vec<Candidate>,
}

fn overlay_sample_order(sample_ms: &str) -> Vec<Candidate> {
    let Ok(Value::Object(samples)) = serde_json::from_str::<Value>(sample_ms) else {
        return Vec::new();
    };
    let mut ordered: Vec<(Candidate, f64)> = samples
        .iter()
        .filter_map(|(name, value)| {
            let candidate = Candidate::from_str(name)?;
            let ms = value.as_f64()?;
            ms.is_finite().then_some((candidate, ms))
        })
        .collect();
    ordered.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(Ordering::Equal));
    ordered
        .into_iter()
        .map(|(candidate, _)| candidate)
        .collect()
}

/// All enabled overlay pins as shape_key -> pin, refreshed at most once per TTL. Empty (no
/// behavior change) until the route_overlay table exists and has rows.
fn overlay_map() -> std::collections::HashMap<String, OverlayPin> {
    let ttl = route_stamp_ttl();
    let stale = ttl.is_zero()
        || OVERLAY_MEMO.with(|m| {
            m.borrow()
                .as_ref()
                .map(|(at, _)| at.elapsed() >= ttl)
                .unwrap_or(true)
        });
    if stale {
        let mut map: std::collections::HashMap<String, OverlayPin> =
            std::collections::HashMap::new();
        if relations_present(&["rvbbit.route_overlay"]) {
            let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
                let rows = client.select(
                    "SELECT shape_key, engine, COALESCE(sample_ms, '{}'::jsonb)::text \
                     FROM rvbbit.route_overlay WHERE enabled",
                    None,
                    &[],
                )?;
                for row in rows {
                    let key: String = row.get(1)?.unwrap_or_default();
                    let eng: String = row.get(2)?.unwrap_or_default();
                    let sample_ms: String = row.get(3)?.unwrap_or_else(|| "{}".to_string());
                    if !key.is_empty() {
                        if let Some(c) = Candidate::from_str(&eng) {
                            map.insert(
                                key,
                                OverlayPin {
                                    engine: c,
                                    sample_order: overlay_sample_order(&sample_ms),
                                },
                            );
                        }
                    }
                }
                Ok(())
            });
        }
        OVERLAY_MEMO.with(|m| *m.borrow_mut() = Some((Instant::now(), map.clone())));
        return map;
    }
    OVERLAY_MEMO.with(|m| {
        m.borrow()
            .as_ref()
            .map(|(_, x)| x.clone())
            .unwrap_or_default()
    })
}

/// The tested-pin layer: a decision iff this shape is pinned AND the pinned engine is
/// available for this query. Otherwise None -> fall through to base rules. A pin can never
/// force an unavailable/unsafe engine, so it never affects correctness — only engine choice.
fn overlay_decision(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    if OVERLAY_BYPASS.with(|b| b.get()) {
        return None;
    }
    let pin = overlay_map().get(&features.shape_key)?.clone();
    let mut candidates = Vec::with_capacity(pin.sample_order.len() + 1);
    candidates.push(pin.engine);
    for candidate in pin.sample_order {
        if !candidates.contains(&candidate) {
            candidates.push(candidate);
        }
    }
    for cand in candidates {
        let (available, reason) = candidate_availability(cand, features, tables);
        if available {
            let reason = if cand == pin.engine {
                format!("tested overlay pin; {reason}")
            } else {
                format!(
                    "tested overlay fallback from {} to {}; {reason}",
                    pin.engine.as_str(),
                    cand.as_str()
                )
            };
            return Some(decision(cand, "overlay", &reason, Some(1.0), None));
        }
    }
    None
}

/// Drop the per-backend overlay memo so the next lookup re-reads the table — called by the
/// trainer right after it writes/deletes a pin so the change takes effect immediately.
fn overlay_invalidate() {
    OVERLAY_MEMO.with(|m| *m.borrow_mut() = None);
}

/// Server-side execution time (ms) of one `EXPLAIN (ANALYZE, TIMING OFF) <sql>` run, isolated
/// in a subtransaction so a slow/erroring engine (statement_timeout, an un-pushable function,
/// …) yields None instead of poisoning the trainer's transaction. EXPLAIN ANALYZE executes the
/// plan, so it routes per the currently-forced candidate; it's a utility statement, hence the
/// mutable SPI path (mirrors explain_sql).
fn explain_exec_ms(sql: &str) -> Option<f64> {
    let probe = format!("EXPLAIN (ANALYZE, TIMING OFF) {sql}");
    pgrx::PgTryBuilder::new(std::panic::AssertUnwindSafe(|| {
        let mut out: Option<f64> = None;
        let _ = Spi::connect_mut(|client| -> Result<(), pgrx::spi::Error> {
            let table = client.update(&probe, None, &[])?;
            for row in table {
                let line: String = row.get(1)?.unwrap_or_default();
                if let Some(rest) = line.trim().strip_prefix("Execution Time:") {
                    out = rest
                        .trim()
                        .trim_end_matches("ms")
                        .trim()
                        .parse::<f64>()
                        .ok();
                }
            }
            Ok(())
        });
        out
    }))
    .catch_others(|_| None)
    .catch_rust_panic(|_| None)
    .execute()
}

/// Force `cand`, then benchmark `sql`: one warmup + `samples` measured runs, median ms. Returns
/// None if the engine couldn't be exercised (unavailable / timed out / errored every run).
/// Always restores the prior `route_force_candidate`.
/// True when a caught bench error left the transaction wedged in parallel
/// mode. When a benched query runs a parallel plan (EXPLAIN ANALYZE with
/// Gather workers) and statement_timeout fires mid-execution, the caught
/// error can leave the leader's parallel-mode counter non-zero — after which
/// every GUC write in the TRANSACTION fails with 25000 "parameter ... cannot
/// be set during a parallel operation". The counter is transaction-local, so
/// the correct response is to STOP the optimizer pass gracefully (the next
/// call runs in a fresh transaction and is clean) — force-unwinding it with
/// ExitParallelMode() crashes the backend (verified: SIGSEGV) because live
/// parallel contexts may still be registered.
fn parallel_mode_wedged() -> bool {
    unsafe { pg_sys::IsInParallelMode() }
}

fn bench_candidate(sql: &str, cand: Candidate, samples: i32) -> Option<f64> {
    let prev = guc_setting("rvbbit.route_force_candidate").unwrap_or_default();
    // Any PG error while benching ONE candidate — statement timeout on a heavy
    // shape, an engine hard-error (fail_open is off during optimize), or a bad
    // logged SQL — must cost only this candidate's timing, never unwind the
    // whole optimizer pass. The subtransaction rollback also reverts the
    // force-candidate GUC (GUC assignments are transactional).
    let sql_owned = sql.to_string();
    let mut times: Vec<f64> = pgrx::PgTryBuilder::new(move || {
        if set_route_force_candidate(cand.as_str()).is_err() {
            return Vec::new();
        }
        let _ = explain_exec_ms(&sql_owned); // warmup
        let mut times = Vec::new();
        for _ in 0..samples.max(1) {
            if let Some(ms) = explain_exec_ms(&sql_owned) {
                times.push(ms);
            }
        }
        times
    })
    .catch_others(|_| Vec::new())
    .execute();
    // NOTE: if the caught error left the txn in parallel mode (see
    // parallel_mode_wedged), the restore below raises 25000 and unwinds to the
    // caller's per-shape catch; the optimizer loop then stops gracefully.
    if !parallel_mode_wedged() {
        let _ = set_route_force_candidate(&prev);
    }
    if times.is_empty() {
        return None;
    }
    times.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    Some(times[times.len() / 2])
}

/// Benchmark one read-only query on every available engine and, if a non-base engine wins by
/// >= `min_margin_pct`, pin that shape -> engine in rvbbit.route_overlay. If the base engine is
/// already best (or the win is sub-threshold), any existing pin for the shape is removed
/// (self-pruning). Read-only and side-effect-free apart from the overlay row; each bench run is
/// subtransaction-isolated and bounded by a local statement_timeout.
#[pg_extern]
fn route_optimize_query(
    sql: &str,
    samples: default!(i32, "3"),
    min_margin_pct: default!(f64, "15.0"),
) -> JsonB {
    if safe_select(sql).is_err() {
        return JsonB(json!({"ok": false, "reason": "not a read-only SELECT"}));
    }
    // Backstop so a pathological query can't pin the box during benchmarking.
    // Configurable because heavy warehouse shapes (e.g. TPC-DS Q4-class multi-
    // fact CTEs) legitimately need ~40s+ on their BEST engine — a too-small
    // bound means the optimizer can never learn them at all.
    let bench_timeout_s = guc_setting("rvbbit.route_optimize_timeout_s")
        .and_then(|v| v.trim().parse::<i64>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(60);
    let _ = Spi::run(&format!(
        "SET LOCAL statement_timeout = '{bench_timeout_s}s'"
    ));
    // statement_timeout can't interrupt a blocking sidecar wait — pin the
    // sidecar's own timeout to the same budget so one heavy candidate can't
    // hold a bench slot for the sidecar default (300s) per execution.
    let _ = Spi::run(&format!(
        "SET LOCAL rvbbit.duck_backend_timeout_s = '{bench_timeout_s}'"
    ));
    // NO PG-parallel plans while benching. A statement_timeout firing inside a
    // parallel EXPLAIN ANALYZE, caught by the per-candidate subtransaction, can
    // deadlock leader+worker on LWLock/ParallelQueryDSA — uninterruptible
    // (observed live twice; survives pg_terminate_backend, needs a restart).
    // Cost: native/pg heap benches lose PG parallel workers (a conservative
    // bias toward the vectorized engines); duck/DF/GQE parallelize internally
    // and are unaffected.
    let _ = Spi::run("SET LOCAL max_parallel_workers_per_gather = 0");
    // Force-fail (no fail-open) while benching: if a forced engine can't actually
    // run this query it must return no timing, not silently fall back to native
    // and record native's latency under the forced engine's name — that would
    // poison the per-engine training labels (e.g. a mislabeled fast "gpu_gqe").
    let _ = Spi::run("SET LOCAL rvbbit.duck_backend_fail_open = off");

    let plan = explain_sql(sql).ok();
    let tables = referenced_rvbbit_tables(sql, plan.as_deref());
    if tables.is_empty() {
        return JsonB(json!({"ok": true, "pinned": false,
            "skipped": "query does not reference rvbbit tables"}));
    }
    let features = build_features(sql, plan.as_deref(), &tables);
    let profile = route_profile_selection();

    // True base decision: what base rules pick with NO pin present.
    OVERLAY_BYPASS.with(|b| b.set(true));
    let base = choose_route(&features, &tables, &profile);
    OVERLAY_BYPASS.with(|b| b.set(false));
    let base_cand = base.candidate.unwrap_or(Candidate::RvbbitNative);

    // Bench every AVAILABLE candidate (forcing an unavailable one would silently fall back).
    // Log each replay's timing into route_observations (source 'optimize') so it
    // becomes ML training data. This replay is UNBIASED — it times every engine,
    // not just the router's pick — so it also feeds engines the live router
    // currently avoids (e.g. GQE), breaking the auto-run feedback loop.
    let features_json = features.to_json();
    let log_obs = relations_present(&["rvbbit.route_observations"]);
    let mut results: std::collections::BTreeMap<String, f64> = std::collections::BTreeMap::new();
    for cand in Candidate::all() {
        // Once a caught bench error wedges the txn's parallel state, running
        // ANOTHER parallel query in it can deadlock on ParallelQueryDSA
        // (leader+worker stuck on an uninterruptible LWLock — statement_timeout
        // can't fire; observed live as a 5h hang). Stop benching this shape.
        if parallel_mode_wedged() {
            break;
        }
        if candidate_availability(cand, &features, &tables).0 {
            if let Some(ms) = bench_candidate(sql, cand, samples) {
                results.insert(cand.as_str().to_string(), ms);
                if log_obs && ms.is_finite() && ms >= 0.0 {
                    let _ = Spi::run(&format!(
                        "INSERT INTO rvbbit.route_observations \
                             (source, query_hash, shape_key, shape_family, features, candidate, elapsed_ms, status) \
                         VALUES ('optimize', {}, {}, {}, {}::jsonb, {}, {}, 'ok')",
                        sql_lit(&features.sql_hash),
                        sql_lit(&features.shape_key),
                        sql_lit(&features.shape_family),
                        sql_json_lit(&features_json),
                        sql_lit(cand.as_str()),
                        ms,
                    ));
                }
            }
        }
    }

    let winner = results
        .iter()
        .min_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
        .map(|(k, _)| k.clone());
    let base_ms = results.get(base_cand.as_str()).copied();

    let (pinned, margin) = match (&winner, base_ms) {
        (Some(w), Some(bms)) if bms > 0.0 => {
            let margin = (bms - results[w]) / bms * 100.0;
            if w != base_cand.as_str() && margin >= min_margin_pct {
                let _ = Spi::run(&format!(
                    "INSERT INTO rvbbit.route_overlay \
                       (shape_key, shape_family, engine, base_engine, margin_pct, sample_ms, n_samples, source) \
                     VALUES ({sk}, {sf}, {eng}, {base}, {margin}, {sms}::jsonb, {n}, 'tested') \
                     ON CONFLICT (shape_key) DO UPDATE SET \
                       shape_family = EXCLUDED.shape_family, engine = EXCLUDED.engine, \
                       base_engine = EXCLUDED.base_engine, margin_pct = EXCLUDED.margin_pct, \
                       sample_ms = EXCLUDED.sample_ms, n_samples = EXCLUDED.n_samples, \
                       source = 'tested', tested_at = now()",
                    sk = sql_lit(&features.shape_key),
                    sf = sql_lit(&features.shape_family),
                    eng = sql_lit(w),
                    base = sql_lit(base_cand.as_str()),
                    margin = margin,
                    sms = sql_lit(&json!(results).to_string()),
                    n = samples.max(1),
                ));
                (true, margin)
            } else {
                // base wins or sub-threshold → ensure no stale pin lingers
                let _ = Spi::run(&format!(
                    "DELETE FROM rvbbit.route_overlay WHERE shape_key = {}",
                    sql_lit(&features.shape_key)
                ));
                (false, margin)
            }
        }
        _ => (false, 0.0),
    };
    overlay_invalidate();

    JsonB(json!({
        "ok": true,
        "shape_key": features.shape_key,
        "shape_family": features.shape_family,
        "base_engine": base_cand.as_str(),
        "winner": winner,
        "margin_pct": margin,
        "pinned": pinned,
        "samples_ms": results,
    }))
}

/// Batch form: optimize many queries (intended use: one representative SQL per hot shape).
#[pg_extern]
fn route_optimize_queries(sqls: Vec<String>, samples: default!(i32, "3")) -> JsonB {
    let mut pinned = 0i64;
    let mut out: Vec<serde_json::Value> = Vec::with_capacity(sqls.len());
    for s in &sqls {
        let r = route_optimize_query(s, samples, 15.0).0;
        if r.get("pinned").and_then(|v| v.as_bool()).unwrap_or(false) {
            pinned += 1;
        }
        out.push(r);
    }
    JsonB(json!({"queries": sqls.len(), "pinned": pinned, "results": out}))
}

/// Auto-optimizer pass: benchmark the hottest shapes still on base rules
/// (route_optimization_candidates) that have a captured representative SQL (route_shape_samples),
/// writing divergent pins. Bounded by `top_k` shapes and a `max_seconds` wall-clock budget; logs
/// the pass to route_optimize_runs. Built for a nightly pg_cron job, safe to run manually.
#[pg_extern]
fn route_optimize_auto(
    top_k: default!(i32, "20"),
    max_seconds: default!(i32, "600"),
    samples: default!(i32, "3"),
) -> JsonB {
    if !relations_present(&[
        "rvbbit.route_shape_samples",
        "rvbbit.route_optimize_runs",
        "rvbbit.route_optimization_candidates",
    ]) {
        return JsonB(
            json!({"ok": false, "reason": "auto-optimizer tables not present (run migrate)"}),
        );
    }
    let started = Instant::now();

    // open a run row (write path + RETURNING)
    let mut run_id: i64 = 0;
    let _ = Spi::connect_mut(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.update(
            "INSERT INTO rvbbit.route_optimize_runs (trigger) VALUES ('auto') RETURNING run_id",
            None,
            &[],
        )?;
        for row in rows {
            run_id = row.get::<i64>(1)?.unwrap_or(0);
        }
        Ok(())
    });

    // hot, base-routed shapes that have a captured sample SQL, ranked by potential (freq × latency)
    // search_path (0128) and last_tested_at/last_result (0129) are added by
    // migrations; probe so a not-yet-migrated install still optimizes.
    let extra_cols = Spi::get_one::<i64>(
        "SELECT count(*) FROM information_schema.columns \
         WHERE table_schema='rvbbit' AND table_name='route_shape_samples' \
           AND column_name IN ('search_path','last_tested_at')",
    )
    .ok()
    .flatten()
    .unwrap_or(0);
    let has_search_path_col = extra_cols >= 1;
    let has_test_memory = extra_cols >= 2;
    let sp_col = if has_search_path_col {
        "coalesce(s.search_path, '')"
    } else {
        "''"
    };
    // Test-memory: don't re-bench a shape on every pass. A shape qualifies when
    // it has never been tested, or its retest cooldown elapsed AND it has
    // executed again since the last test (c.last_seen is max(executed_at) from
    // the candidates view). Dormant shapes are never replayed again; active
    // shapes are revalidated at most once per cooldown window.
    let retest_hours = guc_setting("rvbbit.route_optimize_retest_hours")
        .and_then(|v| v.trim().parse::<i64>().ok())
        .filter(|v| *v >= 0)
        .unwrap_or(24);
    let test_filter = if has_test_memory {
        format!(
            "WHERE s.last_tested_at IS NULL \
                OR (s.last_tested_at < now() - make_interval(hours => {retest_hours}) \
                    AND c.last_seen > s.last_tested_at)"
        )
    } else {
        String::new()
    };
    let mut candidates: Vec<(String, String, String)> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(
            &format!(
                "SELECT c.shape_key, s.sql, {sp_col} \
                 FROM rvbbit.route_optimization_candidates c \
                 JOIN rvbbit.route_shape_samples s ON s.shape_key = c.shape_key \
                 {test_filter} \
                 ORDER BY c.potential_ms DESC NULLS LAST \
                 LIMIT {}",
                top_k.max(1)
            ),
            None,
            &[],
        )?;
        for row in rows {
            let sk: String = row.get(1)?.unwrap_or_default();
            let sql: String = row.get(2)?.unwrap_or_default();
            let sp: String = row.get(3)?.unwrap_or_default();
            if !sk.is_empty() && !sql.is_empty() {
                candidates.push((sk, sql, sp));
            }
        }
        Ok(())
    });

    let mut tested = 0i32;
    let mut pinned = 0i32;
    let mut errors = 0i32;
    let mut detail: Vec<Value> = Vec::new();
    let prev_search_path = guc_setting("search_path").unwrap_or_default();
    for (sk, sql, sample_sp) in candidates {
        if started.elapsed().as_secs() as i32 >= max_seconds {
            break;
        }
        // A prior caught bench error can wedge this transaction in parallel
        // mode (see parallel_mode_wedged) — every further GUC write would fail,
        // so stop cleanly with partial results; the next pass starts a fresh
        // transaction and picks up where the test-memory left off.
        if parallel_mode_wedged() {
            detail.push(json!({
                "note": "pass stopped early: transaction wedged in parallel mode by a caught bench error; remaining shapes deferred to the next pass"
            }));
            break;
        }
        // Replay each shape under the search_path it was CAPTURED with, so
        // unqualified table names (e.g. a tpcds-schema workload) resolve like
        // they did for the original caller instead of 42P01-ing here.
        let apply_sp = !sample_sp.is_empty() && sample_sp != prev_search_path;
        if apply_sp {
            let _ = Spi::run(&format!(
                "SELECT set_config('search_path', {}, false)",
                sql_lit(&sample_sp)
            ));
        }
        // One bad logged query (dropped table, changed schema, syntax the
        // engines reject) must be counted + skipped — never abort the pass.
        let sql_cl = sql.clone();
        let r: Value = pgrx::PgTryBuilder::new(move || route_optimize_query(&sql_cl, samples, 15.0).0)
            .catch_others(|caught| {
                json!({"ok": false, "reason": format!("shape replay failed: {caught:?}")})
            })
            .execute();
        if apply_sp && !parallel_mode_wedged() {
            let _ = Spi::run(&format!(
                "SELECT set_config('search_path', {}, false)",
                sql_lit(&prev_search_path)
            ));
        }
        tested += 1;
        let was_pinned = r.get("pinned").and_then(Value::as_bool).unwrap_or(false);
        if was_pinned {
            pinned += 1;
        }
        let was_ok = r.get("ok").and_then(Value::as_bool) == Some(true);
        if !was_ok {
            errors += 1;
        }
        // If this shape's bench wedged the txn in parallel mode, NO further
        // SQL (the UPDATE below, the run-row UPDATE, train) can run — stop.
        if parallel_mode_wedged() {
            detail.push(json!({
                "shape_key": sk,
                "error": "bench left transaction in parallel mode; pass stopped early",
            }));
            break;
        }
        if has_test_memory {
            let result = if was_pinned {
                format!(
                    "pinned:{}",
                    r.get("winner").and_then(Value::as_str).unwrap_or("?")
                )
            } else if was_ok {
                "base_ok".to_string()
            } else {
                format!(
                    "error:{}",
                    r.get("reason")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown")
                        .chars()
                        .take(200)
                        .collect::<String>()
                )
            };
            let _ = Spi::run(&format!(
                "UPDATE rvbbit.route_shape_samples \
                 SET last_tested_at = now(), last_result = {} WHERE shape_key = {}",
                sql_lit(&result),
                sql_lit(&sk),
            ));
        }
        detail.push(json!({
            "shape_key": sk,
            "pinned": was_pinned,
            "winner": r.get("winner").cloned().unwrap_or(Value::Null),
            "margin_pct": r.get("margin_pct").cloned().unwrap_or(Value::Null),
            "error": r.get("reason").cloned().unwrap_or(Value::Null),
        }));
    }

    let wedged = parallel_mode_wedged();
    if !wedged {
        let _ = Spi::run(&format!(
        "UPDATE rvbbit.route_optimize_runs \
         SET finished_at = now(), shapes_tested = {}, pinned = {}, errors = {}, elapsed_sec = {}, \
             detail = {}::jsonb \
         WHERE run_id = {}",
        tested,
        pinned,
        errors,
        started.elapsed().as_secs(),
        sql_lit(&json!(detail).to_string()),
        run_id,
    ));
    }

    JsonB(json!({
        "ok": true,
        "run_id": run_id,
        "shapes_tested": tested,
        "pinned": pinned,
        "errors": errors,
        "elapsed_sec": started.elapsed().as_secs(),
        "parallel_mode_wedged": wedged,
    }))
}

#[derive(Clone, Debug)]
struct WorkloadTableRef {
    oid: u32,
    schema: String,
    relname: String,
    qualified: String,
}

#[derive(Clone, Debug)]
struct WorkloadColumn {
    name: String,
    lower_name: String,
    pg_type: i32,
    n_distinct: f64,
    correlation_abs: f64,
    primary: bool,
    unique: bool,
}

#[derive(Clone, Debug)]
struct WorkloadSample {
    shape_key: String,
    sql: String,
    executions: i64,
    avg_ms: f64,
}

#[derive(Default, Clone, Debug)]
struct WorkloadColumnRoles {
    observations: i64,
    weighted_ms: f64,
    where_refs: i64,
    group_refs: i64,
    order_refs: i64,
    count_distinct_refs: i64,
    sample_shapes: Vec<String>,
}

#[derive(Clone, Debug)]
struct WorkloadLayoutRecommendation {
    table_oid: u32,
    table_name: String,
    layout_kind: String,
    column_name: String,
    layout: String,
    score: f64,
    observations: i64,
    weighted_ms: f64,
    role_counts: Value,
    sample_shapes: Vec<String>,
    existing_status: Option<String>,
    recommendation_status: Option<String>,
    reason: String,
    details: Value,
}

/// Recommend per-table layout variants from routed workload samples. This is a
/// shadow/advisor pass: it writes candidate rows by default, but accepted rows
/// are the only ones consumed by the variant builder.
#[pg_extern]
fn recommend_workload_layouts(
    rel: pg_sys::Oid,
    lookback_hours: default!(i32, "24"),
    min_observations: default!(i32, "2"),
    max_recommendations: default!(i32, "8"),
    persist: default!(bool, "true"),
) -> JsonB {
    if !relations_present(&[
        "rvbbit.tables",
        "rvbbit.route_shape_samples",
        "rvbbit.route_executions",
        "rvbbit.layout_variant_status",
        "rvbbit.workload_layout_recommendations",
    ]) {
        return JsonB(json!({
            "ok": false,
            "reason": "workload layout advisor catalog is not present; run rvbbit.migrate()"
        }));
    }

    let rel_oid = rel.to_u32();
    let table = match workload_table_ref(rel_oid) {
        Some(table) => table,
        None => pgrx::error!(
            "rvbbit.recommend_workload_layouts: relation {rel_oid} is not an enabled rvbbit table"
        ),
    };
    let columns = workload_columns_for_table(rel_oid);
    if columns.is_empty() {
        return JsonB(json!({
            "ok": true,
            "table": table.qualified,
            "recommendations": [],
            "reason": "table has no recommendable columns"
        }));
    }

    let samples = workload_samples(lookback_hours.max(1), 1000);
    let mut roles: BTreeMap<String, WorkloadColumnRoles> = BTreeMap::new();
    let mut matched_samples = 0_i64;
    for sample in &samples {
        let stringless = sql_stringless(&sample.sql).to_lowercase();
        if !sql_mentions_relation(&stringless, &table.schema, &table.relname) {
            continue;
        }
        matched_samples += 1;
        merge_workload_roles(sample, &columns, &mut roles);
    }

    let mut recommendations = score_workload_layouts(
        &table,
        &columns,
        &roles,
        min_observations.max(1) as i64,
        max_recommendations.max(1) as usize,
    );
    if persist {
        persist_workload_layout_recommendations(&recommendations);
    }

    JsonB(json!({
        "ok": true,
        "table": table.qualified,
        "lookback_hours": lookback_hours.max(1),
        "sample_shapes_seen": samples.len(),
        "sample_shapes_matched": matched_samples,
        "persisted": persist,
        "recommendations": recommendations
            .drain(..)
            .map(workload_recommendation_json)
            .collect::<Vec<_>>(),
    }))
}

/// Mark a workload recommendation as accepted. Accepted rows are explicit
/// per-table layout hints consumed by refresh_acceleration/refresh_layout_variants.
#[pg_extern]
fn accept_workload_layout(rel: pg_sys::Oid, layout_kind: &str, column_name: &str) -> JsonB {
    set_workload_layout_status(rel, layout_kind, column_name, "accepted")
}

#[pg_extern]
fn reject_workload_layout(rel: pg_sys::Oid, layout_kind: &str, column_name: &str) -> JsonB {
    set_workload_layout_status(rel, layout_kind, column_name, "rejected")
}

fn set_workload_layout_status(
    rel: pg_sys::Oid,
    layout_kind: &str,
    column_name: &str,
    status: &str,
) -> JsonB {
    if !relations_present(&["rvbbit.workload_layout_recommendations"]) {
        return JsonB(json!({
            "ok": false,
            "reason": "workload layout advisor catalog is not present; run rvbbit.migrate()"
        }));
    }
    let rel_oid = rel.to_u32();
    let Some(table) = workload_table_ref(rel_oid) else {
        pgrx::error!(
            "rvbbit.{status}_workload_layout: relation {rel_oid} is not an enabled rvbbit table"
        );
    };
    let kind = normalize_workload_layout_kind(layout_kind).unwrap_or_else(|| {
        pgrx::error!("rvbbit.workload_layout: layout_kind must be cluster or hive")
    });
    let Some(column) = workload_columns_for_table(rel_oid)
        .into_iter()
        .find(|c| c.name.eq_ignore_ascii_case(column_name))
    else {
        pgrx::error!(
            "rvbbit.workload_layout: column {} does not exist on {}",
            column_name,
            table.qualified
        );
    };
    let layout = workload_layout_name(&kind, &column.name);
    let details = json!({
        "source": "manual_status_update",
        "status": status,
    });
    let _ = Spi::run(&format!(
        "INSERT INTO rvbbit.workload_layout_recommendations \
             (table_oid, layout_kind, column_name, layout, status, reason, details, updated_at) \
         VALUES ({rel_oid}::oid, {}, {}, {}, {}, 'manual workload layout status', {}::jsonb, now()) \
         ON CONFLICT (table_oid, layout_kind, column_name) DO UPDATE SET \
             status = EXCLUDED.status, \
             layout = EXCLUDED.layout, \
             reason = EXCLUDED.reason, \
             details = rvbbit.workload_layout_recommendations.details || EXCLUDED.details, \
             updated_at = now()",
        sql_lit(&kind),
        sql_lit(&column.name),
        sql_lit(&layout),
        sql_lit(status),
        sql_json_lit(&details)
    ));
    JsonB(json!({
        "ok": true,
        "table": table.qualified,
        "layout_kind": kind,
        "column_name": column.name,
        "layout": layout,
        "status": status,
    }))
}

fn workload_table_ref(rel_oid: u32) -> Option<WorkloadTableRef> {
    let mut out = None;
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(
            &format!(
                "SELECT lower(n.nspname), lower(c.relname), c.oid::regclass::text \
                 FROM rvbbit.tables t \
                 JOIN pg_class c ON c.oid = t.table_oid \
                 JOIN pg_namespace n ON n.oid = c.relnamespace \
                 WHERE t.table_oid = {rel_oid}::oid \
                   AND coalesce(t.acceleration_enabled, true)"
            ),
            Some(1),
            &[],
        )?;
        for row in rows {
            out = Some(WorkloadTableRef {
                oid: rel_oid,
                schema: row.get::<String>(1)?.unwrap_or_default(),
                relname: row.get::<String>(2)?.unwrap_or_default(),
                qualified: row.get::<String>(3)?.unwrap_or_else(|| rel_oid.to_string()),
            });
        }
        Ok(())
    });
    out
}

fn workload_columns_for_table(rel_oid: u32) -> Vec<WorkloadColumn> {
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(
            &format!(
                "SELECT a.attname::text, a.atttypid::oid::int4, \
                        coalesce(s.n_distinct::float8, 0), \
                        abs(coalesce(s.correlation, 0))::float8, \
                        coalesce(ix.primary_col, false), \
                        coalesce(ix.unique_col, false) \
                 FROM pg_attribute a \
                 JOIN pg_class c ON c.oid = a.attrelid \
                 JOIN pg_namespace n ON n.oid = c.relnamespace \
                 LEFT JOIN pg_stats s \
                   ON s.schemaname = n.nspname \
                  AND s.tablename = c.relname \
                  AND s.attname = a.attname::text \
                 LEFT JOIN LATERAL ( \
                     SELECT bool_or(i.indisprimary) AS primary_col, \
                            bool_or(i.indisunique) AS unique_col \
                     FROM pg_index i \
                     JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true \
                     WHERE i.indrelid = a.attrelid \
                       AND i.indisvalid \
                       AND i.indisready \
                       AND k.ord <= i.indnkeyatts \
                       AND k.attnum = a.attnum \
                 ) ix ON true \
                 WHERE a.attrelid = {rel_oid}::oid \
                   AND a.attnum > 0 \
                   AND NOT a.attisdropped \
                 ORDER BY a.attnum"
            ),
            None,
            &[],
        )?;
        for row in rows {
            let Some(name) = row.get::<String>(1)? else {
                continue;
            };
            out.push(WorkloadColumn {
                lower_name: name.to_ascii_lowercase(),
                name,
                pg_type: row.get::<i32>(2)?.unwrap_or_default(),
                n_distinct: row.get::<f64>(3)?.unwrap_or(0.0),
                correlation_abs: row.get::<f64>(4)?.unwrap_or(0.0),
                primary: row.get::<bool>(5)?.unwrap_or(false),
                unique: row.get::<bool>(6)?.unwrap_or(false),
            });
        }
        Ok(())
    });
    out
}

fn workload_samples(lookback_hours: i32, limit: i32) -> Vec<WorkloadSample> {
    let mut out = Vec::new();
    let hours = lookback_hours.clamp(1, 24 * 365);
    let limit = limit.clamp(1, 10_000);
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(
            &format!(
                "SELECT s.shape_key, s.sql, \
                        count(e.*)::bigint AS executions, \
                        avg(e.elapsed_ms)::float8 AS avg_ms \
                 FROM rvbbit.route_shape_samples s \
                 JOIN rvbbit.route_executions e ON e.shape_key = s.shape_key \
                 WHERE e.executed_at > now() - make_interval(hours => {hours}) \
                   AND e.status = 'ok' \
                   AND s.sql <> '' \
                 GROUP BY s.shape_key, s.sql \
                 ORDER BY count(e.*) * avg(e.elapsed_ms) DESC NULLS LAST \
                 LIMIT {limit}"
            ),
            None,
            &[],
        )?;
        for row in rows {
            let shape_key: String = row.get(1)?.unwrap_or_default();
            let sql: String = row.get(2)?.unwrap_or_default();
            if shape_key.is_empty() || sql.is_empty() {
                continue;
            }
            out.push(WorkloadSample {
                shape_key,
                sql,
                executions: row.get::<i64>(3)?.unwrap_or(0).max(0),
                avg_ms: row.get::<f64>(4)?.unwrap_or(0.0).max(0.0),
            });
        }
        Ok(())
    });
    out
}

fn merge_workload_roles(
    sample: &WorkloadSample,
    columns: &[WorkloadColumn],
    roles: &mut BTreeMap<String, WorkloadColumnRoles>,
) {
    let stringless = sql_stringless(&sample.sql).to_lowercase();
    let where_clause = top_level_clause(
        &stringless,
        "where",
        &["group by", "order by", "having", "limit", "offset", "union"],
    );
    let group_clause = top_level_clause(
        &stringless,
        "group by",
        &["order by", "having", "limit", "offset", "union"],
    );
    let order_clause = top_level_clause(&stringless, "order by", &["limit", "offset", "union"]);
    let count_distinct = count_distinct_expr(&stringless).unwrap_or_default();
    for column in columns {
        let in_where = contains_column_identifier(&where_clause, &column.lower_name);
        let in_group = contains_column_identifier(&group_clause, &column.lower_name);
        let in_order = contains_column_identifier(&order_clause, &column.lower_name);
        let in_count_distinct = contains_column_identifier(&count_distinct, &column.lower_name);
        if !(in_where || in_group || in_order || in_count_distinct) {
            continue;
        }
        let entry = roles.entry(column.name.clone()).or_default();
        entry.observations += sample.executions.max(1);
        entry.weighted_ms += sample.executions.max(1) as f64 * sample.avg_ms.max(1.0);
        if in_where {
            entry.where_refs += sample.executions.max(1);
        }
        if in_group {
            entry.group_refs += sample.executions.max(1);
        }
        if in_order {
            entry.order_refs += sample.executions.max(1);
        }
        if in_count_distinct {
            entry.count_distinct_refs += sample.executions.max(1);
        }
        if !entry
            .sample_shapes
            .iter()
            .any(|shape| shape == &sample.shape_key)
        {
            entry.sample_shapes.push(sample.shape_key.clone());
        }
    }
}

fn score_workload_layouts(
    table: &WorkloadTableRef,
    columns: &[WorkloadColumn],
    roles: &BTreeMap<String, WorkloadColumnRoles>,
    min_observations: i64,
    max_recommendations: usize,
) -> Vec<WorkloadLayoutRecommendation> {
    let mut out = Vec::new();
    for column in columns {
        let Some(role) = roles.get(&column.name) else {
            continue;
        };
        if role.observations < min_observations {
            continue;
        }
        if workload_clusterable_type(column.pg_type) {
            if let Some(rec) = score_cluster_layout(table, column, role) {
                out.push(rec);
            }
        }
        if workload_hive_partitionable_type(column.pg_type) {
            if let Some(rec) = score_hive_layout(table, column, role) {
                out.push(rec);
            }
        }
    }
    out.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.layout.cmp(&b.layout))
    });
    out.truncate(max_recommendations);
    for rec in &mut out {
        rec.existing_status = layout_variant_status(rec.table_oid, &rec.layout);
        rec.recommendation_status =
            workload_recommendation_status(rec.table_oid, &rec.layout_kind, &rec.column_name);
    }
    out
}

fn score_cluster_layout(
    table: &WorkloadTableRef,
    column: &WorkloadColumn,
    role: &WorkloadColumnRoles,
) -> Option<WorkloadLayoutRecommendation> {
    let mut score = role.where_refs as f64 * 120.0
        + role.order_refs as f64 * 90.0
        + role.group_refs as f64 * 25.0
        + role.count_distinct_refs as f64 * 20.0
        + role.weighted_ms * 0.05;
    if matches!(column.pg_type, 1082 | 1114 | 1184) {
        score += 500.0;
    }
    if column.n_distinct < 0.0 || column.n_distinct >= 1_000.0 {
        score += 250.0;
    } else if column.n_distinct > 0.0 && column.n_distinct <= 8.0 {
        score -= 200.0;
    }
    if column.primary || column.unique {
        score += 150.0;
    }
    if column.correlation_abs >= 0.95 {
        score += 50.0;
    }
    if matches!(column.pg_type, 25 | 1042 | 1043) && role.where_refs == 0 && role.order_refs == 0 {
        return None;
    }
    if score <= 0.0 {
        return None;
    }
    Some(workload_recommendation(
        table,
        "cluster",
        column,
        role,
        score,
        "range/order pruning candidate",
    ))
}

fn score_hive_layout(
    table: &WorkloadTableRef,
    column: &WorkloadColumn,
    role: &WorkloadColumnRoles,
) -> Option<WorkloadLayoutRecommendation> {
    if column.primary || column.unique {
        return None;
    }
    let n_distinct = column.n_distinct;
    if !(2.0..=256.0).contains(&n_distinct) {
        return None;
    }
    let mut score = role.group_refs as f64 * 120.0
        + role.where_refs as f64 * 90.0
        + role.count_distinct_refs as f64 * 50.0
        + role.weighted_ms * 0.05;
    if n_distinct <= 32.0 {
        score += 350.0;
    } else if n_distinct <= 128.0 {
        score += 175.0;
    }
    if score <= 0.0 {
        return None;
    }
    Some(workload_recommendation(
        table,
        "hive",
        column,
        role,
        score,
        "partition-pruning/grouping candidate",
    ))
}

fn workload_recommendation(
    table: &WorkloadTableRef,
    layout_kind: &str,
    column: &WorkloadColumn,
    role: &WorkloadColumnRoles,
    score: f64,
    reason: &str,
) -> WorkloadLayoutRecommendation {
    let role_counts = json!({
        "where": role.where_refs,
        "group_by": role.group_refs,
        "order_by": role.order_refs,
        "count_distinct": role.count_distinct_refs,
    });
    let details = json!({
        "pg_type_oid": column.pg_type,
        "n_distinct": column.n_distinct,
        "correlation_abs": column.correlation_abs,
        "primary": column.primary,
        "unique": column.unique,
    });
    WorkloadLayoutRecommendation {
        table_oid: table.oid,
        table_name: table.qualified.clone(),
        layout_kind: layout_kind.to_string(),
        column_name: column.name.clone(),
        layout: workload_layout_name(layout_kind, &column.name),
        score,
        observations: role.observations,
        weighted_ms: role.weighted_ms,
        role_counts,
        sample_shapes: role.sample_shapes.iter().take(8).cloned().collect(),
        existing_status: None,
        recommendation_status: None,
        reason: reason.to_string(),
        details,
    }
}

fn workload_recommendation_json(rec: WorkloadLayoutRecommendation) -> Value {
    json!({
        "table": rec.table_name,
        "layout_kind": rec.layout_kind,
        "column_name": rec.column_name,
        "layout": rec.layout,
        "score": rec.score,
        "observations": rec.observations,
        "weighted_ms": rec.weighted_ms,
        "role_counts": rec.role_counts,
        "sample_shapes": rec.sample_shapes,
        "layout_status": rec.existing_status,
        "recommendation_status": rec.recommendation_status,
        "reason": rec.reason,
        "details": rec.details,
        "accept_sql": format!(
            "SELECT rvbbit.accept_workload_layout('{}'::regclass, '{}', '{}')",
            rec.table_name.replace('\'', "''"),
            rec.layout_kind.replace('\'', "''"),
            rec.column_name.replace('\'', "''"),
        ),
    })
}

fn persist_workload_layout_recommendations(recommendations: &[WorkloadLayoutRecommendation]) {
    for rec in recommendations {
        let details = rec.details.clone();
        let _ = Spi::run(&format!(
            "INSERT INTO rvbbit.workload_layout_recommendations \
                 (table_oid, layout_kind, column_name, layout, score, observations, weighted_ms, \
                  role_counts, sample_shapes, reason, details, status, recommended_at, updated_at) \
             VALUES ({oid}::oid, {kind}, {col}, {layout}, {score}, {obs}, {weighted}, \
                     {roles}::jsonb, {shapes}, {reason}, {details}::jsonb, 'candidate', now(), now()) \
             ON CONFLICT (table_oid, layout_kind, column_name) DO UPDATE SET \
                 layout = EXCLUDED.layout, \
                 score = EXCLUDED.score, \
                 observations = EXCLUDED.observations, \
                 weighted_ms = EXCLUDED.weighted_ms, \
                 role_counts = EXCLUDED.role_counts, \
                 sample_shapes = EXCLUDED.sample_shapes, \
                 reason = EXCLUDED.reason, \
                 details = EXCLUDED.details, \
                 status = CASE \
                     WHEN rvbbit.workload_layout_recommendations.status IN ('accepted', 'rejected') \
                     THEN rvbbit.workload_layout_recommendations.status \
                     ELSE 'candidate' \
                 END, \
                 updated_at = now()",
            oid = rec.table_oid,
            kind = sql_lit(&rec.layout_kind),
            col = sql_lit(&rec.column_name),
            layout = sql_lit(&rec.layout),
            score = rec.score,
            obs = rec.observations,
            weighted = rec.weighted_ms,
            roles = sql_json_lit(&rec.role_counts),
            shapes = sql_text_array_lit(&rec.sample_shapes),
            reason = sql_lit(&rec.reason),
            details = sql_json_lit(&details),
        ));
    }
}

fn layout_variant_status(table_oid: u32, layout: &str) -> Option<String> {
    Spi::get_one::<String>(&format!(
        "SELECT status FROM rvbbit.layout_variant_status \
         WHERE table_oid = {table_oid}::oid AND layout = {}",
        sql_lit(layout)
    ))
    .ok()
    .flatten()
}

fn workload_recommendation_status(
    table_oid: u32,
    layout_kind: &str,
    column_name: &str,
) -> Option<String> {
    Spi::get_one::<String>(&format!(
        "SELECT status FROM rvbbit.workload_layout_recommendations \
         WHERE table_oid = {table_oid}::oid AND layout_kind = {} AND column_name = {}",
        sql_lit(layout_kind),
        sql_lit(column_name)
    ))
    .ok()
    .flatten()
}

fn normalize_workload_layout_kind(raw: &str) -> Option<String> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "cluster" | "clustered" => Some("cluster".to_string()),
        "hive" | "partition" | "partitioned" => Some("hive".to_string()),
        _ => None,
    }
}

fn workload_layout_name(layout_kind: &str, column_name: &str) -> String {
    match layout_kind {
        "hive" => format!("hive:{column_name}"),
        _ => format!("cluster:{column_name}"),
    }
}

fn workload_clusterable_type(pg_type: i32) -> bool {
    matches!(
        pg_type,
        20 | 21 | 23 | 25 | 700 | 701 | 1042 | 1043 | 1082 | 1114 | 1184
    )
}

fn workload_hive_partitionable_type(pg_type: i32) -> bool {
    matches!(pg_type, 16 | 20 | 21 | 23 | 25 | 1042 | 1043)
}

fn choose_route_fast(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    _profile: &RouteProfileSelection, // profile layer retired; param kept for signature parity
) -> Option<RouteDecision> {
    if features.regex_count > 0 {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "postgres regex semantics",
            None,
            None,
        ));
    }
    if let Some(decision) = forced_route_decision(features, tables) {
        return Some(decision);
    }
    if let Some(d) = overlay_decision(features, tables) {
        return Some(d);
    }
    if let Some(decision) = native_metadata_hard_rule(features, tables, "hard-rule-fast") {
        return Some(decision);
    }
    let (_vector_available, vector_reason) = duck_availability(features, tables);
    let (pg_available, pg_reason) = pg_rowstore_availability(tables);
    let external_available = default_external_candidate(features, tables).is_some();
    if !external_available && !pg_available {
        return Some(decision(
            Candidate::RvbbitNative,
            "eligibility-fast",
            &format!(
                "parquet vector ineligible: {vector_reason}; pg rowstore ineligible: {pg_reason}"
            ),
            None,
            None,
        ));
    }
    if !external_available && pg_available {
        if all_dirty_tables_native_overlay_readable(tables) {
            return Some(decision(
                Candidate::RvbbitNative,
                "eligibility-fast",
                "parquet vector path ineligible; native read-time overlay is available",
                None,
                None,
            ));
        }
        return Some(decision(
            Candidate::PgRowstore,
            "eligibility-fast",
            &format!("parquet vector path ineligible; {pg_reason}"),
            None,
            None,
        ));
    }
    if features.count_count > 0
        && features.aggregate_count == features.count_count
        && !features.where_present
        && !features.group_by
        && features.count_distinct_count == 0
    {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "native count metadata",
            None,
            None,
        ));
    }
    if simple_metadata_aggregate_should_stay_native(features)
        && native_simple_metadata_available(tables)
    {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "native simple aggregate metadata",
            None,
            None,
        ));
    }
    if filtered_count_should_stay_native(features)
        && native_filtered_count_metadata_available(tables)
    {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "native filtered count metadata",
            None,
            None,
        ));
    }
    if features.min_count > 0
        && features.max_count > 0
        && !features.where_present
        && native_minmax_metadata_available(tables)
    {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "native min/max metadata",
            None,
            None,
        ));
    }
    if features.sum_count >= 16 && !features.where_present {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "native wide aggregate rewrite",
            None,
            None,
        ));
    }
    // Profile/observation layer retired — the routing overlay (tested pins) supersedes it.
    // The fast path defers any non-hard-rule shape to choose_route (base rules + overlay).
    None
}

fn choose_route(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
) -> RouteDecision {
    if features.regex_count > 0 {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "postgres regex semantics",
            None,
            None,
        );
    }
    if let Some(decision) = forced_route_decision(features, tables) {
        return decision;
    }
    if let Some(d) = overlay_decision(features, tables) {
        return d;
    }
    if let Some(decision) = native_metadata_hard_rule(features, tables, "hard-rule") {
        return decision;
    }
    let (_vector_available, vector_reason) = duck_availability(features, tables);
    let (pg_available, pg_reason) = pg_rowstore_availability(tables);
    let external_available = default_external_candidate(features, tables).is_some();
    if !external_available && !pg_available {
        return decision(
            Candidate::RvbbitNative,
            "eligibility",
            &format!(
                "parquet vector ineligible: {vector_reason}; pg rowstore ineligible: {pg_reason}"
            ),
            None,
            None,
        );
    }
    if !external_available && pg_available {
        if all_dirty_tables_native_overlay_readable(tables) {
            return decision(
                Candidate::RvbbitNative,
                "eligibility",
                "parquet vector path ineligible; native read-time overlay is available",
                None,
                None,
            );
        }
        return decision(
            Candidate::PgRowstore,
            "eligibility",
            &format!("parquet vector path ineligible; {pg_reason}"),
            None,
            None,
        );
    }
    if features.count_count > 0
        && features.aggregate_count == features.count_count
        && !features.where_present
        && !features.group_by
        && features.count_distinct_count == 0
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native count metadata",
            None,
            None,
        );
    }
    if features.fixed_contains_like_count > 0
        && features.count_count > 0
        && features.aggregate_count == features.count_count
        && !features.group_by
        && !features.plan_has_join
        && !features.plan_has_subplan
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native fixed LIKE count rewrite",
            None,
            None,
        );
    }
    if simple_metadata_aggregate_should_stay_native(features)
        && native_simple_metadata_available(tables)
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native simple aggregate metadata",
            None,
            None,
        );
    }
    if filtered_count_should_stay_native(features)
        && native_filtered_count_metadata_available(tables)
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native filtered count metadata",
            None,
            None,
        );
    }
    if features.min_count > 0
        && features.max_count > 0
        && !features.where_present
        && native_minmax_metadata_available(tables)
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native min/max metadata",
            None,
            None,
        );
    }
    if features.sum_count >= 16 && !features.where_present {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native wide aggregate rewrite",
            None,
            None,
        );
    }
    if native_function_should_stay_native(features) {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native vector function rewrite",
            None,
            None,
        );
    }
    if features.native_function.as_deref() == Some("top_count_1col")
        && features.group_by
        && features.normalized_sql.contains(" - ")
        && features.table_rows < no_profile_variant_min_rows()
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native expression-key top count",
            None,
            None,
        );
    }

    // Profile/observation layer retired — the routing overlay supersedes it (it ran above,
    // right after force_candidate). With no active profile this always takes the base route.
    if profile.effective.is_none() {
        return choose_no_profile_route(features, tables, profile);
    }

    if hot_store_no_profile_enabled() && hot_store_prefers_mem(features) {
        if let Some(candidate) =
            first_available_candidate(&[Candidate::DataFusionMem], features, tables)
        {
            return decision(
                candidate,
                "default-hot",
                "route profile miss; manually loaded hot columnar object uses in-memory DataFusion",
                None,
                None,
            );
        }
    }

    if let Some(reason) = fallback_native_reason_for_tables(features, tables) {
        return decision(
            Candidate::RvbbitNative,
            "default-native",
            &format!("route profile miss; {reason}"),
            None,
            None,
        );
    }
    if let Some(candidates) = fallback_external_candidate_order(features) {
        if let Some(candidate) = first_available_candidate(candidates, features, tables) {
            let reason = if matches!(
                candidate,
                Candidate::DataFusionHive | Candidate::DuckHive | Candidate::DuckVortex
            ) {
                "route profile miss; variant-friendly analytical shape uses a parquet variant path"
            } else if candidate == Candidate::DuckVector {
                "route profile miss; complex analytical shape uses DuckDB vector execution"
            } else {
                "route profile miss; analytical parquet shape uses vector execution"
            };
            return decision(candidate, "default-fallback", reason, None, None);
        }
    }
    let default_candidate =
        default_external_candidate(features, tables).unwrap_or(Candidate::RvbbitNative);
    decision(
        default_candidate,
        "default",
        "default parquet candidate",
        None,
        None,
    )
}

fn native_metadata_hard_rule(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    source: &'static str,
) -> Option<RouteDecision> {
    if !native_metadata_hard_rule_safe(tables) {
        return None;
    }
    if features.count_count > 0
        && features.aggregate_count == features.count_count
        && !features.where_present
        && !features.group_by
        && features.count_distinct_count == 0
    {
        return Some(decision(
            Candidate::RvbbitNative,
            source,
            "native count metadata",
            None,
            None,
        ));
    }
    if simple_metadata_aggregate_should_stay_native(features)
        && native_simple_metadata_available(tables)
    {
        return Some(decision(
            Candidate::RvbbitNative,
            source,
            "native simple aggregate metadata",
            None,
            None,
        ));
    }
    if filtered_count_should_stay_native(features)
        && native_filtered_count_metadata_available(tables)
    {
        return Some(decision(
            Candidate::RvbbitNative,
            source,
            "native filtered count metadata",
            None,
            None,
        ));
    }
    if features.min_count > 0
        && features.max_count > 0
        && !features.where_present
        && native_minmax_metadata_available(tables)
    {
        return Some(decision(
            Candidate::RvbbitNative,
            source,
            "native min/max metadata",
            None,
            None,
        ));
    }
    if features.sum_count >= 16 && !features.where_present {
        return Some(decision(
            Candidate::RvbbitNative,
            source,
            "native wide aggregate rewrite",
            None,
            None,
        ));
    }
    None
}

fn native_metadata_hard_rule_safe(tables: &[RvbbitTableMetric]) -> bool {
    !tables.is_empty()
        && tables
            .iter()
            .all(|table| !table.shadow_heap_dirty && table.delete_count == 0)
}

fn native_simple_metadata_available(tables: &[RvbbitTableMetric]) -> bool {
    native_metadata_hard_rule_safe(tables)
        && tables
            .iter()
            .all(|table| table.row_groups > 0 && table_has_complete_column_stats(table.oid))
}

fn native_minmax_metadata_available(tables: &[RvbbitTableMetric]) -> bool {
    native_simple_metadata_available(tables)
}

fn native_filtered_count_metadata_available(tables: &[RvbbitTableMetric]) -> bool {
    native_metadata_hard_rule_safe(tables)
        && tables
            .iter()
            .all(|table| table.row_groups > 0 && table_has_complete_column_bitmaps(table.oid))
}

fn table_has_complete_column_stats(table_oid: u32) -> bool {
    if table_oid == 0 || !relations_present(&["rvbbit.row_groups_visible"]) {
        return false;
    }
    let variant_sql = if relations_present(&[
        "rvbbit.row_group_variants",
        "rvbbit.layout_variant_status",
    ]) {
        format!(
                " OR EXISTS ( \
                 SELECT 1 \
                 FROM rvbbit.row_group_variants v \
                 JOIN visible rg ON rg.rg_id = v.rg_id \
                 JOIN rvbbit.layout_variant_status lvs \
                   ON lvs.table_oid = v.table_oid AND lvs.layout = v.layout \
                 WHERE v.table_oid = {table_oid}::oid \
                   AND lvs.status = 'ready' \
                   AND jsonb_array_length(coalesce(v.stats, '[]'::jsonb)) >= (SELECT n FROM att_count) \
                 GROUP BY v.layout \
                 HAVING count(DISTINCT v.rg_id)::bigint = (SELECT row_groups FROM total) \
             )"
            )
    } else {
        String::new()
    };
    let sql = format!(
        "WITH visible AS ( \
             SELECT rg_id, stats \
             FROM rvbbit.row_groups_visible \
             WHERE table_oid = {table_oid}::oid \
         ), att_count AS ( \
             SELECT count(*)::bigint AS n \
             FROM pg_attribute \
             WHERE attrelid = {table_oid}::oid \
               AND attnum > 0 \
               AND NOT attisdropped \
         ), total AS ( \
             SELECT count(*)::bigint AS row_groups FROM visible \
         ) \
         SELECT (SELECT row_groups FROM total) > 0 \
            AND ( \
                EXISTS ( \
                    SELECT 1 \
                    FROM visible \
                    WHERE jsonb_array_length(coalesce(stats, '[]'::jsonb)) >= (SELECT n FROM att_count) \
                    HAVING count(*)::bigint = (SELECT row_groups FROM total) \
                ) \
                {variant_sql} \
            )"
    );
    Spi::get_one::<bool>(&sql).ok().flatten().unwrap_or(false)
}

fn table_has_complete_column_bitmaps(table_oid: u32) -> bool {
    if table_oid == 0 || !relations_present(&["rvbbit.row_groups_visible", "rvbbit.column_bitmaps"])
    {
        return false;
    }
    let sql = format!(
        "WITH visible AS ( \
             SELECT rg_id \
             FROM rvbbit.row_groups_visible \
             WHERE table_oid = {table_oid}::oid \
         ), total AS ( \
             SELECT count(*)::bigint AS row_groups FROM visible \
         ) \
         SELECT (SELECT row_groups FROM total) > 0 \
            AND EXISTS ( \
                SELECT 1 \
                FROM rvbbit.column_bitmaps cb \
                JOIN visible rg ON rg.rg_id = cb.rg_id \
                WHERE cb.table_oid = {table_oid}::oid \
                GROUP BY cb.column_name \
                HAVING count(DISTINCT cb.rg_id)::bigint = (SELECT row_groups FROM total) \
            )"
    );
    Spi::get_one::<bool>(&sql).ok().flatten().unwrap_or(false)
}

fn decision(
    candidate: Candidate,
    source: &'static str,
    reason: &str,
    confidence: Option<f64>,
    profile_entry: Option<Value>,
) -> RouteDecision {
    RouteDecision {
        route: candidate.route(),
        candidate: Some(candidate),
        source,
        reason: reason.to_string(),
        confidence,
        profile_entry,
    }
}

fn choose_shadow_learned_route(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    choose_from_observation_exact(features, tables)
        .or_else(|| choose_from_observation_curve(features, tables))
}

fn choose_from_observation_exact(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    if !relations_present(&["rvbbit.route_observations"]) {
        return None;
    }
    let shape_lit = sql_lit(&features.shape_key);
    let legacy_shape_lit = sql_lit(&features.legacy_shape_key);
    let mut by_candidate: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT candidate, elapsed_ms \
                 FROM rvbbit.route_observations \
                 WHERE shape_key IN ({shape_lit}, {legacy_shape_lit}) \
                   AND status = 'ok' \
                   AND elapsed_ms > 0 \
                 ORDER BY observed_at DESC \
                 LIMIT 1000"
            ),
            None,
            &[],
        )?;
        for row in table {
            let candidate: String = row.get(1)?.unwrap_or_default();
            let elapsed_ms: f64 = row.get(2)?.unwrap_or_default();
            if Candidate::from_str(&candidate).is_some() && elapsed_ms > 0.0 {
                by_candidate.entry(candidate).or_default().push(elapsed_ms);
            }
        }
        Ok(())
    });

    let mut medians = by_candidate
        .into_iter()
        .filter_map(|(candidate, values)| {
            let candidate = Candidate::from_str(&candidate)?;
            if !candidate_availability(candidate, features, tables).0 {
                return None;
            }
            let n = values.len();
            (n > 0).then(|| (candidate, median_f64(values), n))
        })
        .collect::<Vec<_>>();
    medians.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(Ordering::Equal));
    if medians.len() < 2 {
        return None;
    }
    let (candidate, best_ms, observations) = medians[0];
    let (_, second_ms, _) = medians[1];
    let confidence = if second_ms > 0.0 {
        (1.0 - best_ms / second_ms).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let source = if confidence >= min_confidence_for_candidate(candidate) {
        "shadow-observation-exact"
    } else {
        "shadow-observation-low-confidence"
    };
    let entry = json!({
        "choice": candidate.as_str(),
        "confidence": confidence,
        "median_ms": best_ms,
        "next_best_ms": second_ms,
        "observations": observations,
        "candidate_medians": medians.iter().map(|(candidate, ms, n)| {
            json!({"candidate": candidate.as_str(), "median_ms": ms, "observations": n})
        }).collect::<Vec<_>>(),
    });
    Some(decision(
        candidate,
        source,
        &format!(
            "shadow exact observations: {}",
            ratio_text_many(candidate, best_ms, second_ms)
        ),
        Some(confidence),
        Some(entry),
    ))
}

fn choose_from_observation_curve(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    if !relations_present(&["rvbbit.route_observations"]) {
        return None;
    }
    let family_lit = sql_lit(&features.shape_family);
    let legacy_family_lit = sql_lit(&features.legacy_shape_family);
    let mut by_rows: BTreeMap<i64, CandidateBuckets> = BTreeMap::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT coalesce((features->>'table_rows')::bigint, 0), candidate, elapsed_ms \
                 FROM rvbbit.route_observations \
                 WHERE shape_family IN ({family_lit}, {legacy_family_lit}) \
                   AND status = 'ok' \
                   AND candidate IN ('rvbbit_native', 'rvbbit_native_vortex', 'duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'gpu_gqe', 'pg_rowstore') \
                   AND features ? 'table_rows' \
                 ORDER BY observed_at DESC \
                 LIMIT 2000"
            ),
            None,
            &[],
        )?;
        for row in table {
            let rows: i64 = row.get(1)?.unwrap_or_default();
            let candidate: String = row.get(2)?.unwrap_or_default();
            let elapsed_ms: f64 = row.get(3)?.unwrap_or_default();
            if rows <= 0 || elapsed_ms <= 0.0 {
                continue;
            }
            let entry = by_rows.entry(rows).or_default();
            match candidate.as_str() {
                "rvbbit_native" => entry.native.push(elapsed_ms),
                "rvbbit_native_vortex" => entry.native_vortex.push(elapsed_ms),
                "duck_vector" => entry.duck.push(elapsed_ms),
                "duck_hive" => entry.duck_hive.push(elapsed_ms),
                "duck_vortex" => entry.duck_vortex.push(elapsed_ms),
                "datafusion_vector" => entry.datafusion.push(elapsed_ms),
                "datafusion_hive" => entry.datafusion_hive.push(elapsed_ms),
                "datafusion_vortex" => entry.datafusion_vortex.push(elapsed_ms),
                "gpu_gqe" => entry.gpu_gqe.push(elapsed_ms),
                "pg_rowstore" => entry.pg.push(elapsed_ms),
                _ => {}
            }
        }
        Ok(())
    });
    let mut anchors: BTreeMap<i64, Vec<RouteCurveSample>> = BTreeMap::new();
    for (rows, values) in by_rows {
        let sample = RouteCurveSample {
            native_ms: (!values.native.is_empty()).then(|| median_f64(values.native)),
            native_vortex_ms: (!values.native_vortex.is_empty())
                .then(|| median_f64(values.native_vortex)),
            duck_ms: (!values.duck.is_empty()).then(|| median_f64(values.duck)),
            duck_hive_ms: (!values.duck_hive.is_empty()).then(|| median_f64(values.duck_hive)),
            duck_vortex_ms: (!values.duck_vortex.is_empty())
                .then(|| median_f64(values.duck_vortex)),
            datafusion_ms: (!values.datafusion.is_empty()).then(|| median_f64(values.datafusion)),
            datafusion_hive_ms: (!values.datafusion_hive.is_empty())
                .then(|| median_f64(values.datafusion_hive)),
            datafusion_vortex_ms: (!values.datafusion_vortex.is_empty())
                .then(|| median_f64(values.datafusion_vortex)),
            gpu_gqe_ms: (!values.gpu_gqe.is_empty()).then(|| median_f64(values.gpu_gqe)),
            pg_ms: (!values.pg.is_empty()).then(|| median_f64(values.pg)),
        };
        if !sample.has_at_least_two() {
            continue;
        }
        anchors.entry(rows).or_default().push(sample);
    }
    route_curve_from_anchors(anchors, features, tables, "observation-curve")
}

fn route_curve_from_anchors(
    anchors: BTreeMap<i64, Vec<RouteCurveSample>>,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    source: &'static str,
) -> Option<RouteDecision> {
    if anchors.len() < 3 || features.table_rows <= 0 {
        return None;
    }

    let points: Vec<(i64, RouteCurveSample)> = anchors
        .into_iter()
        .map(|(rows, vals)| {
            let sample = RouteCurveSample {
                native_ms: median_option(vals.iter().filter_map(|v| v.native_ms).collect()),
                native_vortex_ms: median_option(
                    vals.iter().filter_map(|v| v.native_vortex_ms).collect(),
                ),
                duck_ms: median_option(vals.iter().filter_map(|v| v.duck_ms).collect()),
                duck_hive_ms: median_option(vals.iter().filter_map(|v| v.duck_hive_ms).collect()),
                duck_vortex_ms: median_option(
                    vals.iter().filter_map(|v| v.duck_vortex_ms).collect(),
                ),
                datafusion_ms: median_option(vals.iter().filter_map(|v| v.datafusion_ms).collect()),
                datafusion_hive_ms: median_option(
                    vals.iter().filter_map(|v| v.datafusion_hive_ms).collect(),
                ),
                datafusion_vortex_ms: median_option(
                    vals.iter().filter_map(|v| v.datafusion_vortex_ms).collect(),
                ),
                gpu_gqe_ms: median_option(vals.iter().filter_map(|v| v.gpu_gqe_ms).collect()),
                pg_ms: median_option(vals.iter().filter_map(|v| v.pg_ms).collect()),
            };
            (rows, sample)
        })
        .collect();

    for pair in points.windows(2) {
        let (r1, lower) = pair[0];
        let (r2, upper) = pair[1];
        if features.table_rows < r1 || features.table_rows > r2 {
            continue;
        }
        let t = if r2 == r1 {
            0.0
        } else {
            (features.table_rows - r1) as f64 / (r2 - r1) as f64
        };
        let predictions = interpolate_predictions(lower, upper, t);
        let (candidate, best_ms, second_ms) =
            fastest_routable_prediction(&predictions, features, tables)?;
        let confidence = if second_ms > 0.0 {
            (1.0 - best_ms / second_ms).clamp(0.0, 1.0)
        } else {
            0.0
        };
        if confidence < min_confidence_for_candidate(candidate) {
            return None;
        }
        let entry = json!({
            "choice": candidate.as_str(),
            "confidence": confidence,
            "candidate_ms_predicted": predictions.iter().map(|(candidate, ms)| {
                json!({"candidate": candidate.as_str(), "ms": ms})
            }).collect::<Vec<_>>(),
            "native_ms_predicted": predicted_ms(&predictions, Candidate::RvbbitNative),
            "duck_ms_predicted": predicted_ms(&predictions, Candidate::DuckVector),
            "duck_hive_ms_predicted": predicted_ms(&predictions, Candidate::DuckHive),
            "duck_vortex_ms_predicted": predicted_ms(&predictions, Candidate::DuckVortex),
            "datafusion_ms_predicted": predicted_ms(&predictions, Candidate::DataFusionVector),
            "datafusion_hive_ms_predicted": predicted_ms(&predictions, Candidate::DataFusionHive),
            "datafusion_vortex_ms_predicted": predicted_ms(&predictions, Candidate::DataFusionVortex),
            "gpu_gqe_ms_predicted": predicted_ms(&predictions, Candidate::GpuGqe),
            "pg_ms_predicted": predicted_ms(&predictions, Candidate::PgRowstore),
            "lower_anchor_rows": r1,
            "upper_anchor_rows": r2,
        });
        return Some(decision(
            candidate,
            source,
            &format!(
                "route curve: predicted {} between {} and {} rows",
                ratio_text_many(candidate, best_ms, second_ms),
                r1,
                r2
            ),
            Some(confidence),
            Some(entry),
        ));
    }
    None
}

fn train_profile(min_observations: i64, min_gain_pct: f64) -> Value {
    let observations = load_route_observations();
    let mut by_shape: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for obs in &observations {
        if let Some(shape) = obs.get("shape_key").and_then(Value::as_str) {
            by_shape
                .entry(shape.to_string())
                .or_default()
                .push(obs.clone());
        }
    }

    let mut entries = Map::new();
    let mut rejected = Map::new();
    for (shape, rows) in by_shape {
        let mut by_candidate: HashMap<String, Vec<f64>> = HashMap::new();
        for row in &rows {
            if row.get("status").and_then(Value::as_str) != Some("ok") {
                continue;
            }
            let Some(candidate) = row.get("candidate").and_then(Value::as_str) else {
                continue;
            };
            if Candidate::from_str(candidate).is_none() {
                continue;
            }
            let Some(ms) = row.get("elapsed_ms").and_then(Value::as_f64) else {
                continue;
            };
            by_candidate
                .entry(candidate.to_string())
                .or_default()
                .push(ms);
        }
        let mut medians: Vec<(String, f64, usize)> = by_candidate
            .into_iter()
            .map(|(candidate, values)| {
                let n = values.len();
                (candidate, median_f64(values), n)
            })
            .collect();
        medians.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(Ordering::Equal));
        if medians.len() < 2 {
            rejected.insert(
                shape,
                json!({"reason": "need at least two candidate timings"}),
            );
            continue;
        }
        let total_obs: usize = medians.iter().map(|m| m.2).sum();
        if total_obs < min_observations as usize {
            rejected.insert(
                shape,
                json!({"reason": "not enough observations", "observations": total_obs}),
            );
            continue;
        }
        let best = &medians[0];
        let second = &medians[1];
        let Some(best_candidate) = Candidate::from_str(&best.0) else {
            continue;
        };
        let gain = if second.1 > 0.0 {
            1.0 - (best.1 / second.1)
        } else {
            0.0
        };
        let required_gain = min_gain_pct.max(min_confidence_for_candidate(best_candidate));
        if gain < required_gain {
            rejected.insert(
                shape,
                json!({"reason": "gain below threshold", "gain": gain, "required_gain": required_gain}),
            );
            continue;
        }
        entries.insert(
            shape,
            json!({
                "choice": best.0,
                "confidence": gain,
                "reason": format!("{} {:.2}x faster than next candidate over {} observation(s)", best.0, second.1 / best.1, total_obs),
                "median_ms": best.1,
                "next_best_ms": second.1,
                "observations": total_obs,
                "candidate_medians": medians.iter().map(|(c, ms, n)| json!({"candidate": c, "median_ms": ms, "observations": n})).collect::<Vec<_>>(),
            }),
        );
    }

    json!({
        "version": 1,
        "kind": "rvbbit_route_profile",
        "generated_by": "pg_rvbbit.router",
        "entries": entries,
        "rejected": rejected,
        "observation_count": observations.len(),
    })
}

fn load_route_observations() -> Vec<Value> {
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            "SELECT query_hash, shape_key, shape_family, features, candidate, elapsed_ms, status, source \
             FROM rvbbit.route_observations \
             ORDER BY observed_at DESC \
             LIMIT 20000",
            None,
            &[],
        )?;
        for row in table {
            let query_hash: String = row.get(1)?.unwrap_or_default();
            let shape_key: String = row.get(2)?.unwrap_or_default();
            let shape_family: String = row.get(3)?.unwrap_or_default();
            let features: JsonB = row.get(4)?.unwrap_or_else(|| JsonB(json!({})));
            let (shape_key, shape_family, features) =
                canonical_observation_shape(shape_key, shape_family, features.0);
            let candidate: String = row.get(5)?.unwrap_or_default();
            let elapsed_ms: f64 = row.get(6)?.unwrap_or_default();
            let status: String = row.get(7)?.unwrap_or_default();
            let source: String = row.get(8)?.unwrap_or_default();
            out.push(json!({
                "query_hash": query_hash,
                "shape_key": shape_key,
                "shape_family": shape_family,
                "features": features,
                "candidate": candidate,
                "elapsed_ms": elapsed_ms,
                "status": status,
                "source": source,
            }));
        }
        Ok(())
    });
    out
}

fn route_profile_rebuild_inner(profile_name: &str, min_gain_pct: f64, activate: bool) -> Value {
    let observations = load_route_training_observations(profile_name);
    let mut by_shape: BTreeMap<String, Vec<TrainingObservation>> = BTreeMap::new();
    for obs in observations.iter().cloned() {
        by_shape.entry(obs.shape_key.clone()).or_default().push(obs);
    }

    let mut entries = Map::new();
    let mut rejected = Map::new();
    let mut profile_points = Vec::new();
    for (shape, rows) in by_shape {
        let mut by_candidate: HashMap<&'static str, Vec<f64>> = HashMap::new();
        let mut shape_family = String::new();
        let mut features = json!({});
        for row in &rows {
            if shape_family.is_empty() {
                shape_family = row.shape_family.clone();
            }
            if features == json!({}) {
                features = row.features.clone();
            }
            by_candidate
                .entry(row.candidate.as_str())
                .or_default()
                .push(row.elapsed_ms);
        }
        let mut medians: Vec<(String, f64, usize)> = by_candidate
            .into_iter()
            .map(|(candidate, values)| {
                let n = values.len();
                (candidate.to_string(), median_f64(values), n)
            })
            .collect();
        medians.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(Ordering::Equal));
        if medians.len() < 2 {
            rejected.insert(
                shape,
                json!({"reason": "need at least two validated candidate timings"}),
            );
            continue;
        }
        let best = &medians[0];
        let second = &medians[1];
        let Some(best_candidate) = Candidate::from_str(&best.0) else {
            rejected.insert(shape, json!({"reason": "unknown best candidate"}));
            continue;
        };
        let gain = if second.1 > 0.0 {
            (1.0 - best.1 / second.1).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let required_gain = min_gain_pct.max(min_confidence_for_candidate(best_candidate));
        if gain < required_gain {
            rejected.insert(
                shape,
                json!({
                    "reason": "gain below threshold",
                    "gain": gain,
                    "required_gain": required_gain,
                    "candidate_medians": medians.iter().map(|(c, ms, n)| {
                        json!({"candidate": c, "median_ms": ms, "observations": n})
                    }).collect::<Vec<_>>(),
                }),
            );
            continue;
        }

        let total_obs: usize = medians.iter().map(|m| m.2).sum();
        let mut entry = Map::new();
        entry.insert("choice".into(), json!(best.0));
        entry.insert("confidence".into(), json!(gain));
        entry.insert(
            "reason".into(),
            json!(format!(
                "{} {:.2}x faster than next candidate over {} validated run(s)",
                best.0,
                second.1 / best.1,
                total_obs
            )),
        );
        entry.insert("median_ms".into(), json!(best.1));
        entry.insert("next_best_ms".into(), json!(second.1));
        entry.insert("observations".into(), json!(total_obs));
        entry.insert(
            "candidate_medians".into(),
            Value::Array(
                medians
                    .iter()
                    .map(|(c, ms, n)| json!({"candidate": c, "median_ms": ms, "observations": n}))
                    .collect(),
            ),
        );
        for (candidate, ms, _) in &medians {
            if let Some(field) = candidate_median_field(candidate) {
                entry.insert(field.to_string(), json!(ms));
            }
        }
        entries.insert(shape.clone(), Value::Object(entry));

        let point = profile_point_from_medians(&shape_family, &features, &medians);
        if !point.is_null() {
            profile_points.push(point);
        }
    }

    let active = if entries.is_empty() {
        false
    } else {
        activate || route_profile_is_active(profile_name)
    };
    let profile = json!({
        "version": 2,
        "kind": "rvbbit_route_profile",
        "generated_by": "pg_rvbbit.route_profile_rebuild",
        "source": "sql-training",
        "entries": entries,
        "rejected": rejected,
        "profile_points": profile_points,
        "training_observation_count": observations.len(),
        "min_gain_pct": min_gain_pct,
    });
    let (stored_entries, stored_points, stored_profile) =
        store_route_profile(profile_name, &profile, active, "route_profile_rebuild");
    json!({
        "profile": profile_name,
        "active": active,
        "entries": stored_entries,
        "rejected": profile.get("rejected").and_then(Value::as_object).map(|m| m.len()).unwrap_or(0),
        "points": stored_points,
        "training_observation_count": observations.len(),
        "profile_json": stored_profile,
    })
}

fn load_route_training_observations(profile_name: &str) -> Vec<TrainingObservation> {
    let name_lit = sql_lit(profile_name);
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT tq.shape_key, tq.shape_family, tq.features, tr.candidate, tr.elapsed_ms \
                 FROM rvbbit.route_training_queries tq \
                 JOIN rvbbit.route_training_results tr ON tr.training_query_id = tq.id \
                 WHERE tq.profile_name = {name_lit} \
                   AND tq.enabled \
                   AND tr.status = 'ok' \
                   AND tr.validation_status IN ('baseline', 'ok') \
                   AND tr.elapsed_ms IS NOT NULL \
                   AND tr.elapsed_ms > 0 \
                 ORDER BY tr.observed_at DESC \
                 LIMIT 20000"
            ),
            None,
            &[],
        )?;
        for row in table {
            let stored_shape_key: String = row.get(1)?.unwrap_or_default();
            let stored_shape_family: String = row.get(2)?.unwrap_or_default();
            let features: JsonB = row.get(3)?.unwrap_or_else(|| JsonB(json!({})));
            let (shape_key, shape_family, features) =
                canonical_observation_shape(stored_shape_key, stored_shape_family, features.0);
            let candidate_name: String = row.get(4)?.unwrap_or_default();
            let Some(candidate) = Candidate::from_str(&candidate_name) else {
                continue;
            };
            let elapsed_ms: f64 = row.get(5)?.unwrap_or_default();
            if elapsed_ms <= 0.0 || !elapsed_ms.is_finite() {
                continue;
            }
            out.push(TrainingObservation {
                shape_key,
                shape_family,
                features,
                candidate,
                elapsed_ms,
            });
        }
        Ok(())
    });
    out
}

fn profile_point_from_medians(
    shape_family: &str,
    features: &Value,
    medians: &[(String, f64, usize)],
) -> Value {
    let get = |candidate: Candidate| -> Option<f64> {
        medians
            .iter()
            .find_map(|(name, ms, _)| (name == candidate.as_str()).then_some(*ms))
    };
    let Some(native_ms) = get(Candidate::RvbbitNative) else {
        return Value::Null;
    };
    let Some(duck_ms) = get(Candidate::DuckVector) else {
        return Value::Null;
    };
    let table_rows = features
        .get("table_rows")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if table_rows <= 0 {
        return Value::Null;
    }
    json!({
        "shape_family": shape_family,
        "table_rows": table_rows,
        "native_ms": native_ms,
        "native_vortex_ms": get(Candidate::RvbbitNativeVortex),
        "duck_ms": duck_ms,
        "duck_hive_ms": get(Candidate::DuckHive),
        "duck_vortex_ms": get(Candidate::DuckVortex),
        "datafusion_ms": get(Candidate::DataFusionVector),
        "datafusion_hive_ms": get(Candidate::DataFusionHive),
        "datafusion_vortex_ms": get(Candidate::DataFusionVortex),
        "gpu_gqe_ms": get(Candidate::GpuGqe),
        "pg_ms": get(Candidate::PgRowstore),
        "point": {
            "shape_family": shape_family,
            "table_rows": table_rows,
            "features": features,
            "candidate_medians": medians.iter().map(|(candidate, ms, n)| {
                json!({"candidate": candidate, "median_ms": ms, "observations": n})
            }).collect::<Vec<_>>(),
        }
    })
}

fn candidate_median_field(candidate: &str) -> Option<&'static str> {
    match Candidate::from_str(candidate)? {
        Candidate::RvbbitNative => Some("native_ms_median"),
        Candidate::RvbbitNativeVortex => Some("native_vortex_ms_median"),
        Candidate::DuckVector => Some("duck_ms_median"),
        Candidate::DuckHive => Some("duck_hive_ms_median"),
        Candidate::DuckVortex => Some("duck_vortex_ms_median"),
        Candidate::DataFusionMem => None,
        Candidate::DataFusionVector => Some("datafusion_ms_median"),
        Candidate::DataFusionHive => Some("datafusion_hive_ms_median"),
        Candidate::DataFusionVortex => Some("datafusion_vortex_ms_median"),
        Candidate::GpuGqe => Some("gpu_gqe_ms_median"),
        Candidate::PgRowstore => Some("pg_ms_median"),
    }
}

fn parse_training_candidates(value: &str, caller: &str) -> Vec<Candidate> {
    let trimmed = value.trim();
    let requested = if trimmed.is_empty() { "all" } else { trimmed };
    let raw = if requested.eq_ignore_ascii_case("all") {
        vec![
            Candidate::RvbbitNative,
            Candidate::RvbbitNativeVortex,
            Candidate::DataFusionMem,
            Candidate::DataFusionVector,
            Candidate::DuckVector,
            Candidate::DuckVortex,
            Candidate::PgRowstore,
            Candidate::DataFusionHive,
            Candidate::DataFusionVortex,
            Candidate::GpuGqe,
            Candidate::DuckHive,
        ]
    } else {
        requested
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|candidate| {
                Candidate::from_str(&candidate.to_ascii_lowercase()).unwrap_or_else(|| {
                    pgrx::error!("rvbbit.{caller}: unknown candidate '{candidate}'")
                })
            })
            .collect::<Vec<_>>()
    };
    let mut out = Vec::with_capacity(raw.len() + 1);
    out.push(Candidate::RvbbitNative);
    for candidate in raw {
        if !out.contains(&candidate) {
            out.push(candidate);
        }
    }
    out
}

fn validate_route_profile_name<'a>(profile_name: &'a str, caller: &str) -> &'a str {
    let trimmed = profile_name.trim();
    if trimmed.is_empty() {
        pgrx::error!("rvbbit.{caller}: profile_name must not be empty");
    }
    if trimmed.len() > 128 {
        pgrx::error!("rvbbit.{caller}: profile_name must be 128 bytes or less");
    }
    trimmed
}

fn ensure_route_profile_row(profile_name: &str, active: bool, caller: &str) {
    let name_lit = sql_lit(profile_name);
    if active {
        Spi::run("UPDATE rvbbit.route_profiles SET active = false WHERE active")
            .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
    }
    let profile = json!({
        "version": 2,
        "kind": "rvbbit_route_profile",
        "generated_by": format!("pg_rvbbit.{caller}"),
        "source": "sql-training",
        "entries": {},
    });
    let profile_lit = sql_json_lit(&profile);
    Spi::run(&format!(
        "INSERT INTO rvbbit.route_profiles (name, active, profile) \
         VALUES ({name_lit}, {active}, {profile_lit}::jsonb) \
         ON CONFLICT (name) DO NOTHING"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
}

fn route_profile_is_active(profile_name: &str) -> bool {
    let name_lit = sql_lit(profile_name);
    Spi::get_one(&format!(
        "SELECT coalesce((SELECT active FROM rvbbit.route_profiles WHERE name = {name_lit}), false)"
    ))
    .ok()
    .flatten()
    .unwrap_or(false)
}

fn upsert_training_query(
    profile_name: &str,
    query: &str,
    features: &RouteFeatures,
    features_json: &Value,
    label: &str,
) -> i64 {
    let label_sql = sql_nullable_text(label.trim());
    Spi::get_one(&format!(
        "INSERT INTO rvbbit.route_training_queries \
             (profile_name, query_sql, query_hash, shape_key, shape_family, features, label, enabled) \
         VALUES ({}, {}, {}, {}, {}, {}::jsonb, {label_sql}, true) \
         ON CONFLICT (profile_name, query_hash) DO UPDATE SET \
             query_sql = EXCLUDED.query_sql, \
             shape_key = EXCLUDED.shape_key, \
             shape_family = EXCLUDED.shape_family, \
             features = EXCLUDED.features, \
             label = EXCLUDED.label, \
             enabled = true, \
             updated_at = now() \
         RETURNING id",
        sql_lit(profile_name),
        sql_lit(query.trim()),
        sql_lit(&features.sql_hash),
        sql_lit(&features.shape_key),
        sql_lit(&features.shape_family),
        sql_json_lit(features_json),
    ))
    .ok()
    .flatten()
    .unwrap_or_else(|| pgrx::error!("rvbbit.route_train_query: failed to persist training query"))
}

fn insert_training_run(
    profile_name: &str,
    training_query_id: i64,
    repeats: i32,
    candidates: &[Candidate],
    settings: Value,
) -> i64 {
    let candidate_names = candidates
        .iter()
        .map(|candidate| candidate.as_str().to_string())
        .collect::<Vec<_>>();
    Spi::get_one(&format!(
        "INSERT INTO rvbbit.route_training_runs \
             (training_query_id, profile_name, repeats, candidates, settings) \
         VALUES ({training_query_id}, {}, {repeats}, {}, {}::jsonb) \
         RETURNING id",
        sql_lit(profile_name),
        sql_text_array_lit(&candidate_names),
        sql_json_lit(&settings),
    ))
    .ok()
    .flatten()
    .unwrap_or_else(|| pgrx::error!("rvbbit.route_train_query: failed to create training run"))
}

fn finish_training_run(run_id: i64, status: &str, summary: &Value) {
    Spi::run(&format!(
        "UPDATE rvbbit.route_training_runs \
         SET status = {}, finished_at = now(), summary = {}::jsonb \
         WHERE id = {run_id}",
        sql_lit(status),
        sql_json_lit(summary),
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_train_query: {e}"));
}

fn insert_training_result(
    profile_name: &str,
    training_query_id: i64,
    run_id: i64,
    result: &TrainingRunResult,
) {
    Spi::run(&format!(
        "INSERT INTO rvbbit.route_training_results \
             (run_id, training_query_id, profile_name, candidate, repeat_idx, elapsed_ms, \
              rows_returned, result_digest, status, validation_status, error, route_doc) \
         VALUES ({run_id}, {training_query_id}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}::jsonb)",
        sql_lit(profile_name),
        sql_lit(result.candidate.as_str()),
        result.repeat_idx,
        sql_nullable_f64(result.elapsed_ms),
        sql_nullable_i64(result.rows_returned),
        sql_nullable_text(result.result_digest.as_deref().unwrap_or("")),
        sql_lit(&result.status),
        sql_lit(&result.validation_status),
        sql_nullable_text(result.error.as_deref().unwrap_or("")),
        sql_json_lit(&result.route_doc),
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_train_query: {e}"));
}

fn insert_route_observation(
    query: &str,
    features: &RouteFeatures,
    features_json: &Value,
    candidate: Candidate,
    elapsed_ms: f64,
    status: &str,
    source: &str,
) {
    if !elapsed_ms.is_finite() || elapsed_ms < 0.0 {
        return;
    }
    let _ = query;
    Spi::run(&format!(
        "INSERT INTO rvbbit.route_observations \
             (source, query_hash, shape_key, shape_family, features, candidate, elapsed_ms, status) \
         VALUES ({}, {}, {}, {}, {}::jsonb, {}, {}, {})",
        sql_lit(source),
        sql_lit(&features.sql_hash),
        sql_lit(&features.shape_key),
        sql_lit(&features.shape_family),
        sql_json_lit(features_json),
        sql_lit(candidate.as_str()),
        elapsed_ms,
        sql_lit(status),
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.route_train_query: {e}"));
}

fn execute_candidate_once(
    query: &str,
    candidate: Candidate,
    ordered_digest: bool,
) -> Result<CandidateExecution, String> {
    let previous = guc_setting("rvbbit.route_force_candidate").unwrap_or_default();
    let restore = previous.clone();
    PgTryBuilder::new(AssertUnwindSafe(|| {
        set_route_force_candidate(candidate.as_str())?;
        let started = Instant::now();
        let (rows_returned, result_digest) =
            Spi::connect(|client| -> Result<(i64, String), pgrx::spi::Error> {
                let mut table = client.select(query, None, &[])?;
                digest_spi_table(&mut table, ordered_digest)
            })
            .map_err(|e| e.to_string())?;
        let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        let route_doc = route_rewrite_value(query);
        Ok(CandidateExecution {
            elapsed_ms,
            rows_returned,
            result_digest,
            route_doc,
        })
    }))
    .catch_others(|cause| Err(caught_error_message(cause)))
    .catch_rust_panic(|cause| Err(caught_error_message(cause)))
    .finally(move || {
        let _ = set_route_force_candidate(&restore);
    })
    .execute()
}

fn set_route_force_candidate(value: &str) -> Result<(), String> {
    Spi::run(&format!(
        "SELECT pg_catalog.set_config('rvbbit.route_force_candidate', {}, true)",
        sql_lit(value)
    ))
    .map_err(|e| e.to_string())
}

fn digest_spi_table(
    table: &mut pgrx::spi::SpiTupleTable<'_>,
    ordered: bool,
) -> Result<(i64, String), pgrx::spi::Error> {
    let columns = table.columns().unwrap_or(0);
    let mut row_hashes = Vec::with_capacity(table.len());
    while table.next().is_some() {
        let mut row_hasher = Sha256::new();
        row_hasher.update((columns as u64).to_le_bytes());
        for ordinal in 1..=columns {
            let typoid = table.column_type_oid(ordinal)?.value();
            row_hasher.update(typoid.to_u32().to_le_bytes());
            let datum = table.get_datum_by_ordinal(ordinal)?;
            match datum {
                Some(datum) => {
                    row_hasher.update([1]);
                    let text = unsafe { datum_output_text(datum, typoid) };
                    row_hasher.update((text.len() as u64).to_le_bytes());
                    row_hasher.update(text.as_bytes());
                }
                None => row_hasher.update([0]),
            }
        }
        row_hashes.push(format!("{:x}", row_hasher.finalize()));
    }
    if !ordered {
        row_hashes.sort();
    }
    let mut final_hasher = Sha256::new();
    final_hasher.update((columns as u64).to_le_bytes());
    final_hasher.update((row_hashes.len() as u64).to_le_bytes());
    for row_hash in &row_hashes {
        final_hasher.update(row_hash.as_bytes());
    }
    Ok((
        row_hashes.len() as i64,
        format!("{:x}", final_hasher.finalize()),
    ))
}

unsafe fn datum_output_text(datum: pg_sys::Datum, typoid: pg_sys::Oid) -> String {
    let mut typoutput = pg_sys::InvalidOid;
    let mut typisvarlena = false;
    pg_sys::getTypeOutputInfo(typoid, &mut typoutput, &mut typisvarlena);
    let cstr = pg_sys::OidOutputFunctionCall(typoutput, datum);
    if cstr.is_null() {
        return String::new();
    }
    let out = CStr::from_ptr(cstr).to_string_lossy().into_owned();
    pg_sys::pfree(cstr as *mut c_void);
    out
}

pub(crate) fn caught_error_message(cause: pg_sys::panic::CaughtError) -> String {
    match cause {
        pg_sys::panic::CaughtError::PostgresError(report)
        | pg_sys::panic::CaughtError::ErrorReport(report) => {
            if let Some(detail) = report.detail() {
                format!("{}: {}", report.message(), detail)
            } else {
                report.message().to_string()
            }
        }
        pg_sys::panic::CaughtError::RustPanic { ereport, .. } => {
            if let Some(detail) = ereport.detail() {
                format!("{}: {}", ereport.message(), detail)
            } else {
                ereport.message().to_string()
            }
        }
    }
}

fn training_run_summary(
    profile_name: &str,
    training_query_id: i64,
    run_id: i64,
    results: &[TrainingRunResult],
    rebuild: Value,
) -> Value {
    let mut by_candidate: BTreeMap<String, Vec<&TrainingRunResult>> = BTreeMap::new();
    for result in results {
        by_candidate
            .entry(result.candidate.as_str().to_string())
            .or_default()
            .push(result);
    }
    let candidates = by_candidate
        .into_iter()
        .map(|(candidate, rows)| {
            let timings = rows
                .iter()
                .filter(|r| r.status == "ok")
                .filter_map(|r| r.elapsed_ms)
                .collect::<Vec<_>>();
            json!({
                "candidate": candidate,
                "runs": rows.len(),
                "ok_runs": rows.iter().filter(|r| r.status == "ok").count(),
                "error_runs": rows.iter().filter(|r| r.status == "error").count(),
                "skipped_runs": rows.iter().filter(|r| r.status == "skipped").count(),
                "median_ms": (!timings.is_empty()).then(|| median_f64(timings)),
                "last_validation_status": rows.last().map(|r| r.validation_status.as_str()),
                "last_error": rows.iter().rev().find_map(|r| r.error.as_deref()),
            })
        })
        .collect::<Vec<_>>();
    json!({
        "profile": profile_name,
        "training_query_id": training_query_id,
        "run_id": run_id,
        "results": candidates,
        "rebuild": rebuild,
    })
}

fn canonical_observation_shape(
    stored_shape_key: String,
    stored_shape_family: String,
    mut features: Value,
) -> (String, String, Value) {
    let feature_shape = features
        .get("shape_key")
        .and_then(Value::as_str)
        .unwrap_or(&stored_shape_key)
        .to_string();
    let canonical = canonical_shape_key(&feature_shape, Some(&features));
    let family = shape_family_key(&canonical);
    if let Value::Object(map) = &mut features {
        if canonical != feature_shape {
            map.entry("legacy_shape_key")
                .or_insert_with(|| json!(feature_shape.clone()));
            map.entry("legacy_shape_family")
                .or_insert_with(|| json!(shape_family_key(&feature_shape)));
        } else if !stored_shape_family.is_empty() && stored_shape_family != family {
            map.entry("legacy_shape_family")
                .or_insert_with(|| json!(stored_shape_family));
        }
        map.insert("shape_key".into(), json!(canonical.clone()));
        map.insert("shape_family".into(), json!(family.clone()));
    }
    (canonical, family, features)
}

fn route_profile_selection_json(selection: &RouteProfileSelection) -> Value {
    json!({
        "requested_profile": selection.requested,
        "profile_name": selection.effective,
        "profile_source": selection.source,
        "profile_warning": selection.warning,
        "profile_updated_epoch": selection.updated_epoch,
    })
}

fn route_profiles_json() -> Value {
    if !relations_present(&["rvbbit.route_profiles"]) {
        return json!([]);
    }
    Spi::get_one::<JsonB>(
        r#"
        SELECT coalesce(jsonb_agg(
            jsonb_build_object(
                'name', name,
                'active', active,
                'created_at', created_at,
                'updated_at', updated_at,
                'entries', entries,
                'points', points,
                'duck_entries', duck_entries,
                'duck_hive_entries', duck_hive_entries,
                'duck_vortex_entries', duck_vortex_entries,
                'datafusion_mem_entries', datafusion_mem_entries,
                'datafusion_entries', datafusion_entries,
                'datafusion_hive_entries', datafusion_hive_entries,
                'datafusion_vortex_entries', datafusion_vortex_entries,
                'gpu_gqe_entries', gpu_gqe_entries,
                'native_entries', native_entries,
                'pg_rowstore_entries', pg_rowstore_entries,
                'avg_confidence', avg_confidence,
                'generated_by', generated_by,
                'imported_from_name', imported_from_name
            )
            ORDER BY active DESC, updated_at DESC, name
        ), '[]'::jsonb)
        FROM (
            SELECT
                rp.name,
                rp.active,
                rp.created_at,
                rp.updated_at,
                coalesce(e.entries, 0) AS entries,
                coalesce(p.points, 0) AS points,
                coalesce(e.duck_entries, 0) AS duck_entries,
                coalesce(e.duck_hive_entries, 0) AS duck_hive_entries,
                coalesce(e.duck_vortex_entries, 0) AS duck_vortex_entries,
                coalesce(e.datafusion_mem_entries, 0) AS datafusion_mem_entries,
                coalesce(e.datafusion_entries, 0) AS datafusion_entries,
                coalesce(e.datafusion_hive_entries, 0) AS datafusion_hive_entries,
                coalesce(e.datafusion_vortex_entries, 0) AS datafusion_vortex_entries,
                coalesce(e.gpu_gqe_entries, 0) AS gpu_gqe_entries,
                coalesce(e.native_entries, 0) AS native_entries,
                coalesce(e.pg_rowstore_entries, 0) AS pg_rowstore_entries,
                coalesce(e.avg_confidence, 0)::double precision AS avg_confidence,
                rp.profile->>'generated_by' AS generated_by,
                rp.profile->>'imported_from_name' AS imported_from_name
            FROM rvbbit.route_profiles rp
            LEFT JOIN (
                SELECT
                    profile_name,
                    count(*)::bigint AS entries,
                    count(*) FILTER (WHERE choice = 'duck_vector')::bigint AS duck_entries,
                    count(*) FILTER (WHERE choice = 'duck_hive')::bigint AS duck_hive_entries,
                    count(*) FILTER (WHERE choice = 'duck_vortex')::bigint AS duck_vortex_entries,
                    count(*) FILTER (WHERE choice = 'datafusion_mem')::bigint AS datafusion_mem_entries,
                    count(*) FILTER (WHERE choice = 'datafusion_vector')::bigint AS datafusion_entries,
                    count(*) FILTER (WHERE choice = 'datafusion_hive')::bigint AS datafusion_hive_entries,
                    count(*) FILTER (WHERE choice = 'datafusion_vortex')::bigint AS datafusion_vortex_entries,
                    count(*) FILTER (WHERE choice = 'gpu_gqe')::bigint AS gpu_gqe_entries,
                    count(*) FILTER (WHERE choice = 'rvbbit_native')::bigint AS native_entries,
                    count(*) FILTER (WHERE choice = 'pg_rowstore')::bigint AS pg_rowstore_entries,
                    avg(confidence) AS avg_confidence
                FROM rvbbit.route_profile_entries
                GROUP BY profile_name
            ) e ON e.profile_name = rp.name
            LEFT JOIN (
                SELECT profile_name, count(*)::bigint AS points
                FROM rvbbit.route_profile_points
                GROUP BY profile_name
            ) p ON p.profile_name = rp.name
        ) s
        "#,
    )
    .ok()
    .flatten()
    .map(|j| j.0)
    .unwrap_or_else(|| json!([]))
}

fn route_catalog_counts_json() -> Value {
    let count = |relation: &str| -> i64 {
        if !relations_present(&[relation]) {
            return 0;
        }
        Spi::get_one::<i64>(&format!("SELECT count(*)::bigint FROM {relation}"))
            .ok()
            .flatten()
            .unwrap_or(0)
    };
    json!({
        "profiles": count("rvbbit.route_profiles"),
        "profile_entries": count("rvbbit.route_profile_entries"),
        "profile_points": count("rvbbit.route_profile_points"),
        "observations": count("rvbbit.route_observations"),
        "decisions": count("rvbbit.route_decisions"),
        "executions": count("rvbbit.route_executions"),
    })
}

fn route_profile_selection() -> RouteProfileSelection {
    if !relations_present(&["rvbbit.route_profiles"]) {
        return RouteProfileSelection {
            requested: None,
            effective: None,
            source: "catalog-missing",
            warning: Some("rvbbit.route_profiles is unavailable".to_string()),
            updated_epoch: None,
        };
    }

    let requested = guc_setting("rvbbit.route_profile")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty() && !value.eq_ignore_ascii_case("default"));
    if let Some(name) = requested.clone() {
        return route_profile_selection_by_name(name, "guc");
    }

    let mut selection = RouteProfileSelection {
        requested: None,
        effective: None,
        source: "active",
        warning: None,
        updated_epoch: None,
    };
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            "SELECT name, extract(epoch FROM updated_at)::text \
             FROM rvbbit.route_profiles \
             WHERE active \
             ORDER BY updated_at DESC \
             LIMIT 1",
            None,
            &[],
        )?;
        for row in table {
            selection.effective = row.get(1)?;
            selection.updated_epoch = row.get(2)?;
        }
        Ok(())
    });
    if selection.effective.is_none() {
        selection.source = "none";
        selection.warning = Some("no active route profile".to_string());
    }
    selection
}

fn route_profile_selection_by_name(name: String, source: &'static str) -> RouteProfileSelection {
    let requested = Some(name.clone());
    let mut selection = RouteProfileSelection {
        requested,
        effective: None,
        source,
        warning: None,
        updated_epoch: None,
    };
    let name_lit = sql_lit(&name);
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT name, extract(epoch FROM updated_at)::text \
                 FROM rvbbit.route_profiles \
                 WHERE name = {name_lit} \
                 LIMIT 1"
            ),
            None,
            &[],
        )?;
        for row in table {
            selection.effective = row.get(1)?;
            selection.updated_epoch = row.get(2)?;
        }
        Ok(())
    });
    if selection.effective.is_none() {
        selection.source = "guc-missing";
        selection.warning = Some(format!("route profile '{name}' does not exist"));
    }
    selection
}

thread_local! {
    /// Per-backend memo of the EXPENSIVE half of the route runtime stamp — the
    /// full-catalog `string_agg` over every rvbbit table's size/rows/bytes/deletes.
    /// The cheap half (route_force_candidate + active profile) is recomputed fresh on
    /// every call so explicit control — including the training harness's per-candidate
    /// `route_force_candidate` — takes effect immediately. The stamp is used ONLY as the
    /// route-cache key (rewriter::duck_route_doc_for_probe); the aggregation ran on every
    /// routable query, BEFORE the cache lookup, so even cache hits paid for it (and
    /// embedding it in the key thrashed the cache on any data change). Memoizing it with a
    /// short TTL collapses that cost for long-lived/pooled connections. Routing is
    /// correctness-neutral (never changes query results), so a <=TTL-stale table
    /// fingerprint at worst delays a re-route by TTL after a data-size change.
    static ROUTE_TABLE_STATE_MEMO: std::cell::RefCell<Option<(Instant, String)>> =
        const { std::cell::RefCell::new(None) };
}

/// TTL for the table-state memo. `RVBBIT_ROUTE_STAMP_TTL_MS` overrides the 1000ms
/// default; `0` disables memoization (recompute every call) for strict freshness.
fn route_stamp_ttl() -> std::time::Duration {
    let ms = std::env::var("RVBBIT_ROUTE_STAMP_TTL_MS")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(1000);
    std::time::Duration::from_millis(ms)
}

pub(crate) fn route_runtime_stamp() -> String {
    if !relations_present(&[
        "rvbbit.tables",
        "rvbbit.route_profiles",
        "rvbbit.table_dirty_state",
        "rvbbit.acceleration_state",
        "rvbbit.row_groups",
        "rvbbit.row_groups_visible",
        "rvbbit.delete_log",
    ]) {
        return "route-runtime-stamp-unavailable".to_string();
    }
    // Fresh every call (cheap, no big SPI) so route_force_candidate (set per-candidate by
    // the training harness) and the active profile take effect immediately.
    let profile = route_profile_selection();
    let profile_stamp = format!(
        "profile:{}:{}@{}|force:{}",
        profile.source,
        profile.effective.as_deref().unwrap_or("none"),
        profile.updated_epoch.as_deref().unwrap_or("unknown"),
        guc_setting("rvbbit.route_force_candidate")
            .map(|v| v.trim().to_ascii_lowercase())
            .unwrap_or_default()
    );
    format!(
        "{profile_stamp}|runtime={}|tables={}",
        crate::duck_backend::accelerator_route_runtime_stamp(),
        route_table_state_stamp()
    )
}

/// The expensive full-catalog table-state aggregation, memoized per-backend with a TTL.
/// Same shape as the inline form it replaced (`string_agg(...)` or `none`), but using
/// `relpages` instead of `pg_relation_size` for the heap-fork size — see the note in the
/// body for why (the fs-stat version was the catalog-crawl slowness).
fn route_table_state_stamp() -> String {
    let ttl = route_stamp_ttl();
    if !ttl.is_zero() {
        if let Some(cached) = ROUTE_TABLE_STATE_MEMO.with(|memo| {
            memo.borrow()
                .as_ref()
                .filter(|(at, _)| at.elapsed() < ttl)
                .map(|(_, ts)| ts.clone())
        }) {
            return cached;
        }
    }
    // `c.relpages * block_size` (catalog estimate, refreshed by (auto)vacuum/analyze)
    // replaces `pg_relation_size(c.oid)` here. pg_relation_size stat()s every fork of
    // every rvbbit-AM relation on EACH call; on a real warehouse (2800+ such relations,
    // incl. toast) that is ~8s of syscalls, and with this stamp memoized at only a 1s TTL
    // it recomputed ~10x across a single multi-query statement — turning every routed
    // catalog-crawl sub-query into an 8s tax (90s to fingerprint a 71-row table). relpages
    // is free (already in the catalog) and good enough: this string is ONLY a route-cache
    // key, the precise rvbbit data size is still captured by rg.bytes, and routing is
    // correctness-neutral so a slightly-stale heap-fork estimate at worst delays a re-route.
    let table_state = Spi::get_one::<String>(
        "SELECT coalesce(string_agg( \
                    c.oid::text || ':' || (c.relpages::bigint * current_setting('block_size')::bigint)::text || ':' || \
                    coalesce(rg.rows, 0)::text || ':' || coalesce(rg.bytes, 0)::text || ':' || \
                    coalesce(dl.deletes, 0)::text || ':' || \
                    coalesce(ds.shadow_heap_retained, false)::text || ':' || \
                    coalesce(ds.shadow_heap_dirty, false)::text || ':' || \
                    coalesce(ds.dirty_has_insert, false)::text || ':' || \
                    coalesce(ds.dirty_has_update, false)::text || ':' || \
                    coalesce(ds.dirty_has_delete, false)::text || ':' || \
                    coalesce(ds.dirty_has_truncate, false)::text || ':' || \
                    coalesce(a.last_refresh_xid, 0)::text || ':' || \
                    pg_relation_filenode(c.oid)::text, \
                    ',' ORDER BY c.oid \
                ), 'none') \
	         FROM rvbbit.tables t \
	         JOIN pg_class c ON c.oid = t.table_oid \
	         JOIN rvbbit.table_dirty_state ds ON ds.table_oid = c.oid \
	         LEFT JOIN rvbbit.acceleration_state a ON a.table_oid = c.oid \
	         LEFT JOIN ( \
	             SELECT table_oid, sum(n_rows)::bigint AS rows, sum(n_bytes)::bigint AS bytes \
             FROM rvbbit.row_groups_visible \
             GROUP BY table_oid \
         ) rg ON rg.table_oid = c.oid \
         LEFT JOIN ( \
             SELECT dl.table_oid, count(*)::bigint AS deletes \
             FROM rvbbit.delete_log dl \
             JOIN rvbbit.row_groups_visible rg \
               ON rg.table_oid = dl.table_oid \
              AND rg.rg_id = dl.rg_id \
             GROUP BY dl.table_oid \
         ) dl ON dl.table_oid = c.oid \
	         WHERE coalesce(t.acceleration_enabled, true)",
    )
    .ok()
    .flatten()
    .unwrap_or_else(|| "none".to_string());
    if !ttl.is_zero() {
        ROUTE_TABLE_STATE_MEMO
            .with(|memo| *memo.borrow_mut() = Some((Instant::now(), table_state.clone())));
    }
    table_state
}

fn referenced_rvbbit_tables(sql: &str, plan_text: Option<&str>) -> Vec<RvbbitTableMetric> {
    if !relations_present(&[
        "rvbbit.tables",
        "rvbbit.row_groups_visible",
        "rvbbit.delete_log",
    ]) {
        return Vec::new();
    }
    let stringless = sql_stringless(sql).to_lowercase();
    let plan_lower = plan_text.map(str::to_lowercase);
    // Resolve which rvbbit-registered relations this query references (Step 1, memoized candidate
    // list) and gather full metrics only for those (Step 2, per-oid memo). Both are O(refs),
    // not O(catalog): see referenced_am_oids / table_metrics_for.
    let ref_oids = referenced_am_oids(&stringless, plan_lower.as_deref());
    if ref_oids.is_empty() {
        return Vec::new();
    }
    table_metrics_for(&ref_oids)
}

/// `(oid, lower(schema), lower(relname))` for one rvbbit-registered relation — the memoized
/// candidate row that the per-query name match filters against.
type AmRelation = (i64, String, String);

thread_local! {
    // perf-router-09: the rvbbit-registered relation list (oid/schema/relname) memoized per-backend
    // under the same TTL + correctness-neutral contract as ROUTE_TABLE_STATE_MEMO /
    // VARIANT_READY_MEMO. This is the O(catalog) candidate scan that ran on every routed
    // query; a <=TTL-stale list only delays seeing a just-created/dropped rvbbit-AM table by
    // one TTL, and an unseen new table simply routes to the postgres heap fallback (correct,
    // just not yet accelerated) — never wrong results.
    static RVBBIT_AM_RELATIONS_MEMO: std::cell::RefCell<Option<(Instant, Vec<AmRelation>)>> =
        const { std::cell::RefCell::new(None) };

    // perf-router-09: per-table routing metrics memoized by oid, same contract. A
    // <=TTL-stale metric only changes which engine is CHOSEN (every candidate returns
    // identical results + falls back); the correctness-sensitive metadata fast-path keeps its
    // own fresh tombstone check (metadata_rewrites_unsafe_for_correctness), so this never
    // affects results. Bounded to one small entry per rvbbit-AM table per backend.
    static TABLE_METRIC_MEMO: std::cell::RefCell<std::collections::HashMap<u32, (Instant, RvbbitTableMetric)>> =
        std::cell::RefCell::new(std::collections::HashMap::new());
}

/// OIDs of the rvbbit-registered relations this query references — name-matched against the memoized
/// relation list (refreshed at most once per TTL). The match is plain string work; memoizing
/// the list is what removes the per-query O(catalog) scan that previously cost ~4.5s at ~2900
/// tables (and which a bare `SELECT 1` paid too).
fn referenced_am_oids(stringless: &str, plan_lower: Option<&str>) -> Vec<i64> {
    let ttl = route_stamp_ttl();
    let stale = ttl.is_zero()
        || RVBBIT_AM_RELATIONS_MEMO.with(|m| {
            m.borrow()
                .as_ref()
                .map(|(at, _)| at.elapsed() >= ttl)
                .unwrap_or(true)
        });
    if stale {
        let list = fetch_rvbbit_am_relations();
        RVBBIT_AM_RELATIONS_MEMO.with(|m| *m.borrow_mut() = Some((Instant::now(), list)));
    }
    let match_refs = |list: &[AmRelation]| -> Vec<i64> {
        list.iter()
            .filter(|(oid, schema, relname)| {
                *oid > 0
                    && (sql_mentions_relation(stringless, schema, relname)
                        || plan_lower
                            .map(|plan| plan_mentions_relation(plan, schema, relname))
                            .unwrap_or(false))
            })
            .map(|(oid, _, _)| *oid)
            .collect()
    };
    let refs = RVBBIT_AM_RELATIONS_MEMO.with(|m| {
        m.borrow()
            .as_ref()
            .map(|(_, list)| match_refs(list))
            .unwrap_or_default()
    });
    if refs.is_empty() && !stale && !ttl.is_zero() {
        // A training/query backend may create a new rvbbit table and route it
        // immediately after another route call populated the memo. An empty
        // match is cheap to re-check once so brand-new tables do not look like
        // non-rvbbit relations until the TTL expires.
        let list = fetch_rvbbit_am_relations();
        let refreshed = match_refs(&list);
        RVBBIT_AM_RELATIONS_MEMO.with(|m| *m.borrow_mut() = Some((Instant::now(), list)));
        refreshed
    } else {
        refs
    }
}

/// One cheap catalog scan: (oid, schema, relname) of every enabled rvbbit relation. No metrics,
/// no per-relation subqueries or fs stat()s — those are gathered per referenced oid below.
fn fetch_rvbbit_am_relations() -> Vec<AmRelation> {
    let mut list: Vec<AmRelation> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(
            "SELECT c.oid::bigint, lower(n.nspname), lower(c.relname) \
	             FROM rvbbit.tables t \
	             JOIN pg_class c ON c.oid = t.table_oid \
	             JOIN pg_namespace n ON n.oid = c.relnamespace \
	             WHERE coalesce(t.acceleration_enabled, true)",
            None,
            &[],
        )?;
        for row in rows {
            let oid: i64 = row.get(1)?.unwrap_or_default();
            let schema: String = row.get(2)?.unwrap_or_default();
            let relname: String = row.get(3)?.unwrap_or_default();
            list.push((oid, schema, relname));
        }
        Ok(())
    });
    list
}

/// Metrics for the referenced oids, served from the per-oid memo where fresh; the misses are
/// fetched in one batched query and cached. Same result as an uncached gather, minus the SPI
/// round-trips for tables already seen this TTL window.
fn table_metrics_for(ref_oids: &[i64]) -> Vec<RvbbitTableMetric> {
    let ttl = route_stamp_ttl();
    let mut out: Vec<RvbbitTableMetric> = Vec::with_capacity(ref_oids.len());
    let mut misses: Vec<i64> = Vec::new();
    for &oid in ref_oids {
        let cached = if ttl.is_zero() {
            None
        } else {
            TABLE_METRIC_MEMO.with(|m| {
                m.borrow()
                    .get(&(oid.max(0) as u32))
                    .filter(|(at, _)| at.elapsed() < ttl)
                    .map(|(_, v)| v.clone())
            })
        };
        match cached {
            Some(v) => out.push(v),
            None => misses.push(oid),
        }
    }
    if !misses.is_empty() {
        for m in fetch_table_metrics(&misses) {
            if !ttl.is_zero() {
                let key = m.oid;
                let entry = m.clone();
                TABLE_METRIC_MEMO.with(|c| {
                    c.borrow_mut().insert(key, (Instant::now(), entry));
                });
            }
            out.push(m);
        }
    }
    out
}

/// The expensive per-relation metric gather, scoped to the given oids (trusted catalog
/// integers, so the inlined IN-list is injection-safe). heap_bytes uses relpages*block_size,
/// not pg_relation_size(c.oid): a routing heuristic, so the catalog estimate is precise
/// enough and avoids a per-relation fs stat().
fn fetch_table_metrics(ref_oids: &[i64]) -> Vec<RvbbitTableMetric> {
    let oid_list = ref_oids
        .iter()
        .map(|o| o.to_string())
        .collect::<Vec<_>>()
        .join(", ");
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
            "SELECT lower(n.nspname), lower(c.relname), c.oid::bigint, \
                    count(rg.*)::bigint, coalesce(sum(rg.n_rows), 0)::bigint, \
                    coalesce(sum(rg.n_bytes), 0)::bigint, \
                    (c.relpages::bigint * current_setting('block_size')::bigint)::bigint, \
                    coalesce(t.shadow_heap_retained, false), \
                    rvbbit.shadow_heap_dirty_effective(c.oid), \
                    (coalesce(t.shadow_heap_retained, false) \
                     AND rvbbit.shadow_heap_dirty_effective(c.oid) \
                     AND coalesce(a.last_refresh_xid, 0) > 0 \
                     AND ( \
                          (NOT coalesce(ds.dirty_has_update, false) \
                           AND NOT coalesce(ds.dirty_has_delete, false) \
                           AND NOT coalesce(ds.dirty_has_truncate, false)) \
                          OR rvbbit.accel_overlay_ready(c.oid) \
                     )), \
                    (SELECT count(*)::bigint \
                       FROM rvbbit.delete_log dl \
                       JOIN rvbbit.row_groups_visible rgv \
                         ON rgv.table_oid = dl.table_oid \
                        AND rgv.rg_id = dl.rg_id \
                      WHERE dl.table_oid = c.oid), \
                    coalesce(( \
                        SELECT string_agg(lower(a.attname::text), ',' ORDER BY a.attnum) \
                        FROM pg_attribute a \
                        WHERE a.attrelid = c.oid \
                          AND a.attnum > 0 \
                          AND NOT a.attisdropped \
                          AND a.atttypid IN ('text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, 'name'::regtype) \
                    ), ''), \
                    coalesce(( \
                        SELECT string_agg(lower(a.attname::text), ',' ORDER BY a.attnum) \
                        FROM pg_attribute a \
                        WHERE a.attrelid = c.oid \
                          AND a.attnum > 0 \
                          AND NOT a.attisdropped \
                          AND a.atttypid IN ( \
                              'date'::regtype, \
                              'timestamp without time zone'::regtype, \
                              'timestamp with time zone'::regtype, \
                              'time without time zone'::regtype, \
                              'time with time zone'::regtype \
                          ) \
                    ), ''), \
                    coalesce(( \
                        SELECT string_agg(lower(a.attname::text), ',' ORDER BY a.attnum) \
                        FROM pg_attribute a \
                        WHERE a.attrelid = c.oid \
                          AND a.attnum > 0 \
                          AND NOT a.attisdropped \
                          AND a.atttypid = 'date'::regtype \
                    ), ''), \
                    coalesce(( \
                        SELECT string_agg(lower(a.attname::text), ',' ORDER BY a.attnum) \
                        FROM pg_attribute a \
                        WHERE a.attrelid = c.oid \
                          AND a.attnum > 0 \
                          AND NOT a.attisdropped \
                          AND a.atttypid IN ( \
                              'timestamp without time zone'::regtype, \
                              'timestamp with time zone'::regtype \
                          ) \
                    ), ''), \
                    array_to_string(coalesce(p.denied_engines, '{{}}'), ','), \
                    array_to_string(coalesce(p.denied_layouts, '{{}}'), ',') \
	             FROM rvbbit.tables t \
	             JOIN pg_class c ON c.oid = t.table_oid \
	             JOIN pg_namespace n ON n.oid = c.relnamespace \
	             JOIN rvbbit.table_dirty_state ds ON ds.table_oid = c.oid \
	             LEFT JOIN rvbbit.acceleration_state a ON a.table_oid = c.oid \
	             LEFT JOIN rvbbit.accel_policy p ON p.table_oid = c.oid \
	             LEFT JOIN rvbbit.row_groups_visible rg ON rg.table_oid = c.oid \
	             WHERE coalesce(t.acceleration_enabled, true) AND c.oid IN ({oid_list}) \
             GROUP BY n.nspname, c.relname, c.oid, t.shadow_heap_retained, \
                      ds.shadow_heap_dirty, ds.dirty_has_update, ds.dirty_has_delete, \
                      ds.dirty_has_truncate, a.last_refresh_xid, \
                      p.denied_engines, p.denied_layouts"
            ),
            None,
            &[],
        )?;
        for row in table {
            let schema: String = row.get(1)?.unwrap_or_default();
            let relname: String = row.get(2)?.unwrap_or_default();
            let oid_i64: i64 = row.get(3)?.unwrap_or_default();
            out.push(RvbbitTableMetric {
                schema,
                relname,
                oid: oid_i64.max(0) as u32,
                row_groups: row.get(4)?.unwrap_or_default(),
                rows: row.get(5)?.unwrap_or_default(),
                bytes: row.get(6)?.unwrap_or_default(),
                heap_bytes: row.get(7)?.unwrap_or_default(),
                shadow_heap_retained: row.get(8)?.unwrap_or_default(),
                shadow_heap_dirty: row.get(9)?.unwrap_or_default(),
                native_overlay_readable: row.get(10)?.unwrap_or_default(),
                delete_count: row.get(11)?.unwrap_or_default(),
                text_columns: row
                    .get::<String>(12)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|col| !col.is_empty())
                    .map(str::to_string)
                    .collect(),
                temporal_columns: row
                    .get::<String>(13)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|col| !col.is_empty())
                    .map(str::to_string)
                    .collect(),
                date_columns: row
                    .get::<String>(14)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|col| !col.is_empty())
                    .map(str::to_string)
                    .collect(),
                timestamp_columns: row
                    .get::<String>(15)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|col| !col.is_empty())
                    .map(str::to_string)
                    .collect(),
                denied_engines: row
                    .get::<String>(16)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|v| !v.is_empty())
                    .map(str::to_string)
                    .collect(),
                denied_layouts: row
                    .get::<String>(17)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|v| !v.is_empty())
                    .map(str::to_string)
                    .collect(),
            });
        }
        Ok(())
    });
    out
}

fn relations_present(names: &[&str]) -> bool {
    if names.is_empty() {
        return true;
    }
    let names_sql = names
        .iter()
        .map(|name| sql_lit(name))
        .collect::<Vec<_>>()
        .join(", ");
    Spi::get_one::<bool>(&format!(
        "SELECT count(*) = {} FROM unnest(ARRAY[{names_sql}]::text[]) AS rel(name) WHERE to_regclass(rel.name) IS NOT NULL",
        names.len()
    ))
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn build_features(
    sql: &str,
    plan_text: Option<&str>,
    tables: &[RvbbitTableMetric],
) -> RouteFeatures {
    let stringless = sql_stringless(sql);
    let lowered = stringless.to_lowercase();
    let normalized = normalize_sql(&stringless);
    let group_clause = top_level_clause(
        &lowered,
        "group by",
        &["order by", "having", "limit", "offset", "union"],
    );
    let order_clause = top_level_clause(&lowered, "order by", &["limit", "offset", "union"]);
    let group_exprs = split_top_level_commas(&group_clause)
        .into_iter()
        .filter(|s| !s.trim().is_empty())
        .count() as i64;
    let order_exprs = split_top_level_commas(&order_clause)
        .into_iter()
        .filter(|s| !s.trim().is_empty())
        .count() as i64;
    let count_distinct_sig = count_distinct_signature(&lowered);
    let plan = plan_features(plan_text);
    let table_rows: i64 = tables.iter().map(|t| t.rows).sum();
    let table_bytes: i64 = tables.iter().map(|t| t.bytes).sum();
    let row_groups: i64 = tables.iter().map(|t| t.row_groups).sum();
    let count_count = count_word_fn(&lowered, "count");
    let sum_count = count_word_fn(&lowered, "sum");
    let avg_count = count_word_fn(&lowered, "avg");
    let min_count = count_word_fn(&lowered, "min");
    let max_count = count_word_fn(&lowered, "max");
    let aggregate_count = count_count + sum_count + avg_count + min_count + max_count;
    let text_columns = text_columns_for_tables(tables);
    let referenced_text_col_count = text_column_ref_count(&lowered, &text_columns);
    let group_text_col_count = text_column_ref_count(&group_clause, &text_columns);
    let order_text_col_count = text_column_ref_count(&order_clause, &text_columns);
    let count_distinct_text_count = count_distinct_expr(&lowered)
        .map(|expr| text_column_ref_count(&expr, &text_columns))
        .unwrap_or(0);

    let mut f = RouteFeatures {
        normalized_sql: normalized.clone(),
        sql_hash: hash_short(&normalized),
        shape_key: String::new(),
        shape_family: String::new(),
        legacy_shape_key: String::new(),
        legacy_shape_family: String::new(),
        starts_with_with: normalized.starts_with("with "),
        is_select: normalized.starts_with("select ") || normalized.starts_with("with "),
        select_star: normalized.starts_with("select * from "),
        from_count: tables.len() as i64,
        join_count: count_word(&lowered, "join"),
        where_present: has_word(&lowered, "where"),
        group_by: lowered.contains("group") && lowered.contains("by") && !group_clause.is_empty(),
        order_by: lowered.contains("order") && lowered.contains("by") && !order_clause.is_empty(),
        having: has_word(&lowered, "having"),
        distinct: has_word(&lowered, "distinct"),
        count_distinct_count: count_substr(&lowered, "count(distinct")
            + count_substr(&lowered, "count (distinct"),
        aggregate_count,
        sum_count,
        avg_count,
        count_count,
        min_count,
        max_count,
        referenced_text_col_count,
        group_text_col_count,
        order_text_col_count,
        count_distinct_text_count,
        exists_count: count_substr(&lowered, "exists(") + count_substr(&lowered, "exists ("),
        in_count: count_substr(&lowered, " in(") + count_substr(&lowered, " in ("),
        between_count: count_word(&lowered, "between"),
        or_count: count_word(&lowered, "or"),
        and_count: count_word(&lowered, "and"),
        comparison_count: lowered.matches('=').count() as i64
            + count_substr(&lowered, "<")
            + count_substr(&lowered, ">"),
        like_count: count_word(&lowered, "like"),
        not_like_count: count_substr(&lowered, "not like"),
        fixed_contains_like_count: fixed_contains_like_count(sql),
        // mvcc-06: PG regex has POSIX semantics (backrefs, [[:class:]], locale
        // case-folding) that DuckDB/DataFusion (RE2) do not match. Count the
        // whole regexp_* function family AND the POSIX match operators
        // (~ ~* !~ !~* all contain '~') so duck_availability's regex veto pins
        // any regex query to the native, heap-equivalent path. Overcounting only
        // makes routing more conservative, never wrong.
        regex_count: count_substr(&lowered, "regexp_")
            + count_substr(&lowered, "regex_replace")
            + count_substr(&lowered, "~"),
        limit_bucket: limit_bucket(&lowered),
        offset_present: has_word(&lowered, "offset"),
        group_expr_count_bucket: bucket(group_exprs, &[0, 1, 2, 4, 8, 16]),
        group_expr_signature: clause_expr_signature(&group_clause),
        order_expr_count_bucket: bucket(order_exprs, &[0, 1, 2, 4, 8, 16]),
        order_expr_signature: clause_expr_signature(&order_clause),
        count_distinct_signature: count_distinct_sig,
        plan_available: plan.available,
        plan_has_group: plan.has_group,
        plan_has_hash: plan.has_hash,
        plan_has_join: plan.has_join,
        plan_has_sort: plan.has_sort,
        plan_has_subplan: plan.has_subplan,
        native_function: plan.native_function.clone(),
        has_native_function: plan.has_native_function,
        plan_width_bucket: plan.width_bucket,
        table_rows,
        table_rows_bucket: metric_bucket(table_rows),
        table_bytes,
        table_bytes_bucket: metric_bucket(table_bytes),
        row_group_count: row_groups,
        row_group_count_bucket: bucket(row_groups, &[1, 4, 16, 64, 256]),
    };
    f.legacy_shape_key = legacy_shape_key(&f);
    f.legacy_shape_family = shape_family_key(&f.legacy_shape_key);
    f.shape_key = shape_key(&f);
    f.shape_family = shape_family_key(&f.shape_key);
    f
}

fn text_columns_for_tables(tables: &[RvbbitTableMetric]) -> Vec<String> {
    let mut out = Vec::new();
    for table in tables {
        for column in &table.text_columns {
            if !out.iter().any(|seen| seen == column) {
                out.push(column.clone());
            }
        }
    }
    out
}

fn text_column_ref_count(sql: &str, text_columns: &[String]) -> i64 {
    text_columns
        .iter()
        .filter(|column| contains_column_identifier(sql, column))
        .count() as i64
}

fn contains_column_identifier(sql: &str, column: &str) -> bool {
    contains_identifier(sql, column) || sql.contains(&format!("\"{column}\""))
}

struct PlanFeatures {
    available: bool,
    has_group: bool,
    has_hash: bool,
    has_join: bool,
    has_sort: bool,
    has_subplan: bool,
    native_function: Option<String>,
    has_native_function: bool,
    width_bucket: String,
}

fn plan_features(plan_text: Option<&str>) -> PlanFeatures {
    let Some(plan_text) = plan_text else {
        return PlanFeatures {
            available: false,
            has_group: false,
            has_hash: false,
            has_join: false,
            has_sort: false,
            has_subplan: false,
            native_function: None,
            has_native_function: false,
            width_bucket: "unknown".to_string(),
        };
    };
    let lowered = plan_text.to_lowercase();
    let width = max_plan_number(plan_text, "width=").unwrap_or(0);
    let native_function = function_scan_name(plan_text);
    let has_native_function = native_function
        .as_deref()
        .is_some_and(|name| NATIVE_FUNCTION_MARKERS.contains(&name));
    PlanFeatures {
        available: true,
        has_group: lowered.contains("group") || lowered.contains("aggregate"),
        has_hash: lowered.contains("hash"),
        has_join: lowered.contains("join"),
        has_sort: lowered.contains("sort"),
        has_subplan: lowered.contains("subplan") || lowered.contains("initplan"),
        native_function,
        has_native_function,
        width_bucket: if width > 0 {
            bucket(width, &[16, 64, 256, 1024, 4096])
        } else {
            "unknown".to_string()
        },
    }
}

fn shape_key(f: &RouteFeatures) -> String {
    [
        format!("native_cap={}", f.has_native_function as i32),
        shape_key_tail(f),
    ]
    .concat()
}

fn legacy_shape_key(f: &RouteFeatures) -> String {
    let native_function = if f.has_native_function {
        f.native_function.as_deref().unwrap_or("none")
    } else {
        "none"
    };
    [format!("native={native_function}"), shape_key_tail(f)].concat()
}

fn shape_key_tail(f: &RouteFeatures) -> String {
    [
        format!("tables={}", bucket(f.from_count, &[1, 2, 4, 8])),
        format!("joins={}", bucket(f.join_count, &[0, 1, 2, 4, 8])),
        format!("agg={}", bucket(f.aggregate_count, &[0, 1, 2, 4, 16, 64])),
        format!("cd={}", bucket(f.count_distinct_count, &[0, 1, 2, 4])),
        format!("group={}", f.group_by as i32),
        format!("where={}", f.where_present as i32),
        format!("order={}", f.order_by as i32),
        format!("limit={}", f.limit_bucket),
        format!("offset={}", f.offset_present as i32),
        format!("star={}", f.select_star as i32),
        format!("like={}", bucket(f.like_count, &[0, 1, 2, 4])),
        format!(
            "fixed_like={}",
            bucket(f.fixed_contains_like_count, &[0, 1, 2, 4])
        ),
        format!("regex={}", bucket(f.regex_count, &[0, 1, 2])),
        format!("exists={}", bucket(f.exists_count, &[0, 1, 2])),
        format!("in={}", bucket(f.in_count, &[0, 1, 4])),
        format!("between={}", bucket(f.between_count, &[0, 1, 4])),
        format!("or={}", bucket(f.or_count, &[0, 1, 4, 16])),
        format!("group_keys={}", f.group_expr_count_bucket),
        format!("group_sig={}", f.group_expr_signature),
        format!("order_keys={}", f.order_expr_count_bucket),
        format!("order_sig={}", f.order_expr_signature),
        format!("cd_sig={}", f.count_distinct_signature),
        format!("width={}", f.plan_width_bucket),
        format!("table_rows={}", f.table_rows_bucket),
        format!("plan_join={}", f.plan_has_join as i32),
        format!("subplan={}", f.plan_has_subplan as i32),
    ]
    .into_iter()
    .map(|part| format!("|{part}"))
    .collect::<String>()
}

fn explain_sql(query: &str) -> Result<String, String> {
    let explain = format!("EXPLAIN {query}");
    crate::rewriter::with_duck_rewrite_disabled(|| {
        Spi::connect_mut(|client| -> Result<String, pgrx::spi::Error> {
            // EXPLAIN is a utility statement. PostgreSQL rejects it when SPI runs
            // in read-only mode, even without ANALYZE, so use pgrx's mutable path.
            let table = client.update(&explain, None, &[])?;
            let mut lines = Vec::new();
            for row in table {
                let line: String = row.get(1)?.unwrap_or_default();
                lines.push(line);
            }
            Ok(lines.join("\n"))
        })
    })
    .map_err(|e| e.to_string())
}

fn safe_select(sql: &str) -> Result<(), String> {
    let stripped = sql.trim();
    let lowered = sql_stringless(stripped).to_lowercase();
    if !(lowered.starts_with("select") || lowered.starts_with("with")) {
        return Err("not a read-only SELECT".into());
    }
    if lowered.trim_end_matches(';').contains(';') {
        return Err("multiple statements".into());
    }
    let blacklist = [
        "insert",
        "update",
        "delete",
        "merge",
        "copy",
        "create",
        "alter",
        "drop",
        "truncate",
        "vacuum",
        "grant",
        "revoke",
        "call",
        "do",
        "refresh",
        "listen",
        "notify",
        "nextval",
        "setval",
        "currval",
        "set_config",
        "current_setting",
        "random",
        "now",
        "clock_timestamp",
        "statement_timestamp",
        "transaction_timestamp",
        "timeofday",
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
        "generate_series",
        "pg_sleep",
        "gen_random_uuid",
        "uuid_generate_v4",
    ];
    for token in blacklist {
        if has_word(&lowered, token) {
            return Err(format!("unsupported token: {token}"));
        }
    }
    for token in [
        "rvbbit.", "pg_", " means ", " about ", "::json", "::jsonb", "->", "$$",
    ] {
        if lowered.contains(token) {
            return Err(format!("unsupported token: {token}"));
        }
    }
    Ok(())
}

fn candidate_list_json(
    selected: Option<Candidate>,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Value {
    Value::Array(
        Candidate::all()
            .into_iter()
            .map(|candidate| {
                let (available, reason) = candidate_availability(candidate, features, tables);
                json!({
                    "name": candidate.as_str(),
                    "route": candidate.route(),
                    "available": available,
                    "selected": selected == Some(candidate),
                    "reason": reason,
                })
            })
            .collect(),
    )
}

fn table_metric_json(t: &RvbbitTableMetric) -> Value {
    json!({
        "schema": t.schema,
        "table": t.relname,
        "oid": t.oid,
        "row_groups": t.row_groups,
        "rows": t.rows,
        "bytes": t.bytes,
        "heap_bytes": t.heap_bytes,
        "shadow_heap_retained": t.shadow_heap_retained,
        "shadow_heap_dirty": t.shadow_heap_dirty,
        "native_overlay_readable": t.native_overlay_readable,
        "text_columns": t.text_columns,
        "temporal_columns": t.temporal_columns,
        "date_columns": t.date_columns,
        "timestamp_columns": t.timestamp_columns,
        "parquet_authoritative": t.delete_count == 0
            && (t.heap_bytes == 0 || (t.shadow_heap_retained && !t.shadow_heap_dirty)),
        "delete_count": t.delete_count,
    })
}

fn aggregate_metrics_json(tables: &[RvbbitTableMetric]) -> Value {
    json!({
        "rows": tables.iter().map(|t| t.rows).sum::<i64>(),
        "row_groups": tables.iter().map(|t| t.row_groups).sum::<i64>(),
        "bytes": tables.iter().map(|t| t.bytes).sum::<i64>(),
        "heap_bytes": tables.iter().map(|t| t.heap_bytes).sum::<i64>(),
        "delete_count": tables.iter().map(|t| t.delete_count).sum::<i64>(),
        "native_overlay_readable_tables": tables.iter().filter(|t| t.native_overlay_readable).count(),
    })
}

fn native_availability(tables: &[RvbbitTableMetric]) -> (bool, String) {
    if tables.is_empty() {
        return (true, "Rvbbit native PostgreSQL path available".to_string());
    }
    let dirty = tables.iter().filter(|t| t.shadow_heap_dirty).count();
    if dirty == 0 {
        return (
            true,
            "Rvbbit native parquet path available for clean accelerated row groups".to_string(),
        );
    }
    let overlay_readable = tables
        .iter()
        .filter(|t| t.shadow_heap_dirty && t.native_overlay_readable)
        .count();
    if overlay_readable == dirty {
        return (
            true,
            format!("Rvbbit native read-time overlay available for {dirty} dirty table(s)"),
        );
    }
    (
        true,
        format!(
            "Rvbbit native path available; {dirty} dirty table(s), {overlay_readable} overlay-readable"
        ),
    )
}

fn duck_availability(features: &RouteFeatures, tables: &[RvbbitTableMetric]) -> (bool, String) {
    if features.regex_count > 0 {
        return (false, "Postgres regex semantics required".to_string());
    }
    if tables.is_empty() {
        return (false, "query does not reference Rvbbit tables".to_string());
    }
    if tables.iter().any(|t| t.row_groups <= 0) {
        return (
            false,
            "no compacted parquet row groups are available".to_string(),
        );
    }
    let dirty_heap_bytes: i64 = tables
        .iter()
        .filter(|t| !(t.shadow_heap_retained && !t.shadow_heap_dirty))
        .map(|t| t.heap_bytes)
        .sum();
    if dirty_heap_bytes > 0 {
        let overlay_readable = tables
            .iter()
            .filter(|t| t.shadow_heap_dirty && t.native_overlay_readable)
            .count();
        let overlay_note = if overlay_readable > 0 {
            format!("; native read-time overlay is available for {overlay_readable} table(s)")
        } else {
            String::new()
        };
        return (
            false,
            format!(
                "parquet is not authoritative; heap tail has {dirty_heap_bytes} byte(s){overlay_note}"
            ),
        );
    }
    let delete_count: i64 = tables.iter().map(|t| t.delete_count).sum();
    if delete_count > 0 {
        return (
            false,
            format!("parquet is not authoritative; delete log has {delete_count} row(s)"),
        );
    }
    (
        true,
        "DuckDB vector execution over authoritative Rvbbit parquet row groups".to_string(),
    )
}

fn pg_rowstore_availability(tables: &[RvbbitTableMetric]) -> (bool, String) {
    if tables.is_empty() {
        return (false, "query does not reference Rvbbit tables".to_string());
    }
    let missing = tables
        .iter()
        .filter(|t| !t.shadow_heap_retained)
        .map(|t| format!("{}.{}", t.schema, t.relname))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return (
            false,
            format!("shadow heap not retained for {}", missing.join(", ")),
        );
    }
    let dirty = tables.iter().filter(|t| t.shadow_heap_dirty).count();
    if dirty > 0 {
        (
            true,
            format!("retained shadow heap available; {dirty} table(s) contain post-compaction mutations"),
        )
    } else {
        (true, "retained shadow heap available and clean".to_string())
    }
}

fn hive_availability(features: &RouteFeatures, tables: &[RvbbitTableMetric]) -> (bool, String) {
    let (base_available, base_reason) = duck_availability(features, tables);
    if !base_available {
        return (false, base_reason);
    }
    if !relations_present(&["rvbbit.row_group_variants", "rvbbit.layout_variant_status"]) {
        return (
            false,
            "hive parquet variants catalog is not available".to_string(),
        );
    }
    let variant_count = tables
        .iter()
        .filter(|table| table_has_hive_variant(table.oid))
        .count();
    if variant_count == 0 {
        return (
            false,
            "no referenced table has a hive parquet variant".to_string(),
        );
    }
    let canonical_count = tables.len().saturating_sub(variant_count);
    if canonical_count > 0 {
        (
            true,
            format!(
                "Hive-partitioned parquet variant available for {variant_count} table(s); canonical parquet used for {canonical_count} table(s)"
            ),
        )
    } else {
        (
            true,
            "Hive-partitioned parquet variants available and authoritative".to_string(),
        )
    }
}

thread_local! {
    // perf-router-08: per-table variant readiness (vortex_scan, hive) cached with
    // the same TTL + correctness-neutral contract as ROUTE_TABLE_STATE_MEMO. The
    // availability checks otherwise issue a fresh EXISTS round-trip per table per
    // vortex/hive candidate on the routing hot path (3x redundant per query). A
    // <=TTL-stale readiness only affects which engine is CHOSEN — every candidate
    // returns identical results and falls back — so it never changes correctness,
    // at worst delaying use of a just-built variant by one TTL.
    static VARIANT_READY_MEMO: std::cell::RefCell<std::collections::HashMap<u32, (Instant, (bool, bool))>> =
        std::cell::RefCell::new(std::collections::HashMap::new());
}

/// `(vortex_scan_ready, hive_ready)` for one table — one SPI query for both,
/// memoized per (oid, TTL). On any error (e.g. variant catalog absent) returns
/// `(false, false)`, matching the prior per-probe `.unwrap_or(false)`.
fn variant_readiness(table_oid: u32) -> (bool, bool) {
    let ttl = route_stamp_ttl();
    if !ttl.is_zero() {
        if let Some(v) = VARIANT_READY_MEMO.with(|m| {
            m.borrow()
                .get(&table_oid)
                .filter(|(at, _)| at.elapsed() < ttl)
                .map(|(_, v)| *v)
        }) {
            return v;
        }
    }
    let sql = format!(
        "SELECT coalesce(bool_or(rg.layout = 'vortex_scan'), false), \
                coalesce(bool_or(rg.layout LIKE 'hive:%'), false) \
         FROM rvbbit.row_group_variants rg \
         JOIN rvbbit.row_groups_visible rgv \
           ON rgv.table_oid = rg.table_oid AND rgv.rg_id = rg.rg_id \
         JOIN rvbbit.layout_variant_status s \
           ON s.table_oid = rg.table_oid AND s.layout = rg.layout \
         WHERE rg.table_oid = {table_oid}::oid AND s.status = 'ready'"
    );
    let v = match Spi::get_two::<bool, bool>(&sql) {
        Ok((vx, hv)) => (vx.unwrap_or(false), hv.unwrap_or(false)),
        Err(_) => (false, false),
    };
    if !ttl.is_zero() {
        VARIANT_READY_MEMO.with(|m| {
            m.borrow_mut().insert(table_oid, (Instant::now(), v));
        });
    }
    v
}

fn table_has_hive_variant(table_oid: u32) -> bool {
    variant_readiness(table_oid).1
}

fn duck_vortex_availability(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> (bool, String) {
    let (base_available, base_reason) = duck_availability(features, tables);
    if !base_available {
        return (false, base_reason);
    }
    if crate::time_travel::active_as_of_enabled() {
        return (
            false,
            "DuckDB Vortex accelerator is not used for AS OF queries".to_string(),
        );
    }
    if !relations_present(&["rvbbit.row_group_variants", "rvbbit.layout_variant_status"]) {
        return (
            false,
            "Vortex accelerator catalog is not available".to_string(),
        );
    }
    let missing = tables
        .iter()
        .filter(|table| !table_has_vortex_scan(table.oid))
        .map(|table| format!("{}.{}", table.schema, table.relname))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return (
            false,
            format!("Vortex accelerator is missing for {}", missing.join(", ")),
        );
    }
    (
        true,
        "DuckDB Vortex accelerator files available and authoritative".to_string(),
    )
}

/// Native CustomScan reading the vortex columnar variant. Mirrors
/// `duck_vortex_availability`: parquet must be authoritative (which also means no
/// pending deletes — so the scan's tombstone→parquet fallback never triggers), no
/// AS OF (vortex variants carry no per-rg generations), and a ready `vortex_scan`
/// variant for every referenced table. Shares the "native" engine family but the
/// "vortex" layout, so a per-table vortex-layout deny still gates it.
fn native_vortex_candidate_availability(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> (bool, String) {
    let (base_available, base_reason) = duck_availability(features, tables);
    if !base_available {
        return (false, base_reason);
    }
    if crate::time_travel::active_as_of_enabled() {
        return (
            false,
            "Native Vortex accelerator is not used for AS OF queries".to_string(),
        );
    }
    if !relations_present(&["rvbbit.row_group_variants", "rvbbit.layout_variant_status"]) {
        return (
            false,
            "Vortex accelerator catalog is not available".to_string(),
        );
    }
    let missing = tables
        .iter()
        .filter(|table| !table_has_vortex_scan(table.oid))
        .map(|table| format!("{}.{}", table.schema, table.relname))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return (
            false,
            format!(
                "Native Vortex accelerator is missing for {}",
                missing.join(", ")
            ),
        );
    }
    (
        true,
        "Native CustomScan Vortex accelerator files available and authoritative".to_string(),
    )
}

fn vortex_availability(features: &RouteFeatures, tables: &[RvbbitTableMetric]) -> (bool, String) {
    let (base_available, base_reason) = duck_availability(features, tables);
    if !base_available {
        return (false, base_reason);
    }
    if features.regex_count > 0 {
        return (false, "Postgres regex semantics required".to_string());
    }
    if !vortex_temporal_allowed() {
        if let Some(reason) = vortex_temporal_reference_reason(features, tables) {
            return (false, reason);
        }
    }
    if !relations_present(&["rvbbit.row_group_variants", "rvbbit.layout_variant_status"]) {
        return (
            false,
            "Vortex accelerator catalog is not available".to_string(),
        );
    }
    let missing = tables
        .iter()
        .filter(|table| !table_has_vortex_scan(table.oid))
        .map(|table| format!("{}.{}", table.schema, table.relname))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return (
            false,
            format!("Vortex accelerator is missing for {}", missing.join(", ")),
        );
    }
    (
        true,
        "DataFusion Vortex accelerator files available and authoritative".to_string(),
    )
}

fn vortex_temporal_reference_reason(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<String> {
    let referenced = tables
        .iter()
        .flat_map(|table| {
            table.temporal_columns.iter().filter_map(|column| {
                if contains_column_identifier(&features.normalized_sql, column) {
                    Some(format!("{}.{}.{}", table.schema, table.relname, column))
                } else {
                    None
                }
            })
        })
        .collect::<Vec<_>>();
    if referenced.is_empty() {
        None
    } else {
        Some(format!(
            "Vortex/DataFusion temporal pruning is disabled for referenced date/time column(s): {}",
            referenced.join(", ")
        ))
    }
}

fn vortex_temporal_allowed() -> bool {
    route_enabled(
        "RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL",
        "rvbbit.route_datafusion_vortex_allow_temporal",
        false,
    )
}

fn table_has_vortex_scan(table_oid: u32) -> bool {
    variant_readiness(table_oid).0
}

fn candidate_gate_enabled(candidate: Candidate) -> bool {
    match candidate {
        Candidate::DuckVector => {
            route_enabled("RVBBIT_ROUTE_DUCK_VECTOR", "rvbbit.route_duck_vector", true)
        }
        Candidate::DataFusionMem => route_enabled(
            "RVBBIT_ROUTE_DATAFUSION_MEM",
            "rvbbit.route_datafusion_mem",
            true,
        ),
        Candidate::DataFusionVector => route_enabled(
            "RVBBIT_ROUTE_DATAFUSION_VECTOR",
            "rvbbit.route_datafusion_vector",
            true,
        ),
        Candidate::DuckHive => {
            route_enabled("RVBBIT_ROUTE_HIVE", "rvbbit.route_hive", true)
                && route_enabled("RVBBIT_ROUTE_DUCK_HIVE", "rvbbit.route_duck_hive", true)
        }
        Candidate::DuckVortex => {
            route_enabled("RVBBIT_ROUTE_DUCK_VORTEX", "rvbbit.route_duck_vortex", true)
        }
        Candidate::DataFusionHive => {
            route_enabled("RVBBIT_ROUTE_HIVE", "rvbbit.route_hive", true)
                && route_enabled(
                    "RVBBIT_ROUTE_DATAFUSION_HIVE",
                    "rvbbit.route_datafusion_hive",
                    true,
                )
        }
        Candidate::DataFusionVortex => route_enabled(
            "RVBBIT_ROUTE_DATAFUSION_VORTEX",
            "rvbbit.route_datafusion_vortex",
            true,
        ),
        // Default ON: GQE is eligible wherever it can actually run. On a box with
        // no GPU/GQE binary the downstream runtime check (gqe_routes_available ->
        // gqe_binary() == None) makes it ineligible anyway, so an on-by-default
        // gate is inert there — it just lets GQE-capable machines route to and
        // (self-)train on GQE without a manual opt-in. Set rvbbit.route_gpu_gqe =
        // off to disable it even where a GPU is present.
        Candidate::GpuGqe => route_enabled("RVBBIT_ROUTE_GPU_GQE", "rvbbit.route_gpu_gqe", true),
        Candidate::RvbbitNative => route_enabled(
            "RVBBIT_ROUTE_RVBBIT_NATIVE",
            "rvbbit.route_rvbbit_native",
            true,
        ),
        Candidate::RvbbitNativeVortex => route_enabled(
            "RVBBIT_ROUTE_NATIVE_VORTEX",
            "rvbbit.route_native_vortex",
            true,
        ),
        Candidate::PgRowstore => {
            route_enabled("RVBBIT_ROUTE_PG_ROWSTORE", "rvbbit.route_pg_rowstore", true)
        }
    }
}

fn route_enabled(env_name: &str, guc_name: &str, default: bool) -> bool {
    guc_setting(guc_name)
        .map(|value| setting_enabled(&value, default))
        .unwrap_or_else(|| env_enabled(env_name, default))
}

fn env_enabled(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => setting_enabled(&value, default),
        Err(_) => default,
    }
}

fn setting_enabled(value: &str, default: bool) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return default;
    }
    !matches!(
        trimmed.to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off" | "disabled"
    )
}

fn env_f64(env_name: &str, guc_name: &str, default: f64) -> f64 {
    guc_setting(guc_name)
        .or_else(|| std::env::var(env_name).ok())
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite() && *value >= 0.0)
        .unwrap_or(default)
}

fn guc_setting(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() })
    }
}

/// Per-table engine/layout policy (rvbbit.accel_policy deny-sets). Any touched
/// table can veto a candidate — most-restrictive wins for multi-table queries.
/// native + pg_rowstore are the correctness floor and are never gated, so a
/// fully-denied table simply downgrades to the native path.
fn candidate_denied_by_table_policy(
    candidate: Candidate,
    tables: &[RvbbitTableMetric],
) -> Option<String> {
    if matches!(candidate, Candidate::RvbbitNative | Candidate::PgRowstore) {
        return None;
    }
    let engine = candidate.engine();
    let layout = candidate.layout();
    for t in tables {
        if t.denied_engines.iter().any(|e| e == engine) {
            return Some(format!(
                "{} disabled for {}.{} (engine '{}' denied by table policy)",
                candidate.as_str(),
                t.schema,
                t.relname,
                engine
            ));
        }
        if !layout.is_empty() && t.denied_layouts.iter().any(|l| l == layout) {
            return Some(format!(
                "{} disabled for {}.{} (layout '{}' denied by table policy)",
                candidate.as_str(),
                t.schema,
                t.relname,
                layout
            ));
        }
    }
    None
}

fn candidate_availability(
    candidate: Candidate,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> (bool, String) {
    if !candidate_gate_enabled(candidate) {
        return (
            false,
            format!("{} route disabled by configuration", candidate.as_str()),
        );
    }
    if let Some(reason) = candidate_denied_by_table_policy(candidate, tables) {
        return (false, reason);
    }
    if let Some((available, reason)) = candidate_runtime_availability(candidate) {
        if !available {
            return (false, reason);
        }
    }
    // mvcc-02: AS OF time-travel is only correct on the native parquet path,
    // which filters row groups by the resolved generation and applies tombstones
    // generation-aware. The duck/datafusion/hive/mem engines and pg_rowstore
    // (the shadow heap) all read CURRENT data, so they would silently return
    // latest rows for a historical query. Force AS OF onto the native path.
    if crate::time_travel::active_as_of_enabled() && candidate != Candidate::RvbbitNative {
        return (
            false,
            "AS OF time-travel is served only by the native parquet path".to_string(),
        );
    }
    match candidate {
        Candidate::DuckVector => vector_availability("DuckDB", features, tables),
        Candidate::DataFusionMem => hot_mem_availability(features, tables),
        Candidate::DataFusionVector => vector_availability("DataFusion", features, tables),
        Candidate::DuckHive | Candidate::DataFusionHive => hive_availability(features, tables),
        Candidate::DuckVortex => duck_vortex_availability(features, tables),
        Candidate::DataFusionVortex => vortex_availability(features, tables),
        Candidate::GpuGqe => gpu_gqe_availability(features, tables),
        Candidate::RvbbitNative => native_availability(tables),
        Candidate::RvbbitNativeVortex => native_vortex_candidate_availability(features, tables),
        Candidate::PgRowstore => pg_rowstore_availability(tables),
    }
}

fn candidate_runtime_availability(candidate: Candidate) -> Option<(bool, String)> {
    match candidate {
        Candidate::DuckVector | Candidate::DuckHive | Candidate::DuckVortex => {
            Some(crate::duck_backend::duck_routes_available())
        }
        Candidate::DataFusionMem
        | Candidate::DataFusionVector
        | Candidate::DataFusionHive
        | Candidate::DataFusionVortex => Some(crate::duck_backend::datafusion_routes_available()),
        Candidate::GpuGqe => Some(crate::duck_backend::gqe_routes_available()),
        Candidate::RvbbitNative | Candidate::RvbbitNativeVortex | Candidate::PgRowstore => None,
    }
}

fn vector_availability(
    engine_name: &str,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> (bool, String) {
    let (available, reason) = duck_availability(features, tables);
    if available {
        (
            true,
            format!("{engine_name} vector execution over authoritative Rvbbit parquet row groups"),
        )
    } else {
        (false, reason)
    }
}

fn gpu_gqe_availability(features: &RouteFeatures, tables: &[RvbbitTableMetric]) -> (bool, String) {
    if let Some(reason) = gpu_gqe_unsupported_shape_reason(features, tables) {
        return (false, reason);
    }
    if let Some(reason) = gpu_gqe_unsupported_function_reason(features) {
        return (false, reason);
    }
    if let Some(reason) = gpu_gqe_unsupported_grouping_reason(features) {
        return (false, reason);
    }
    if let Some(reason) = gpu_gqe_temporal_reference_reason(features, tables) {
        return (false, reason);
    }
    let (available, reason) = duck_availability(features, tables);
    if available {
        (
            true,
            "GPU/GQE execution over authoritative Rvbbit parquet row groups".to_string(),
        )
    } else {
        (false, reason)
    }
}

fn gpu_gqe_allow_risky_shapes() -> bool {
    route_enabled(
        "RVBBIT_GQE_ALLOW_RISKY_SHAPES",
        "rvbbit.gqe_allow_risky_shapes",
        false,
    )
}

/// Warm-prior: when enabled (default off), the cold/no-profile router prefers
/// GPU/GQE for large analytical shapes on machines that support it — the engine
/// is otherwise never chosen cold (it's absent from the FALLBACK_* orders) and so
/// never gathers timings. Gated additionally on GQE eligibility AND a fresh
/// rvbbit.gqe_warm_state, so a user query never pays a GQE cold-start.
fn route_gpu_gqe_prior_enabled() -> bool {
    route_enabled(
        "RVBBIT_ROUTE_GPU_GQE_PRIOR",
        "rvbbit.route_gpu_gqe_prior",
        false,
    )
}

fn route_gpu_gqe_prior_min_rows() -> i64 {
    let configured = {
        #[cfg(not(test))]
        {
            guc_setting("rvbbit.route_gpu_gqe_prior_min_rows")
                .or_else(|| std::env::var("RVBBIT_ROUTE_GPU_GQE_PRIOR_MIN_ROWS").ok())
        }
        #[cfg(test)]
        {
            std::env::var("RVBBIT_ROUTE_GPU_GQE_PRIOR_MIN_ROWS").ok()
        }
    };
    configured
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value >= 0)
        .unwrap_or(1_000_000)
}

/// Pure shape test for the GQE prior: a large, scan-heavy analytical shape
/// (aggregate or GROUP BY over >= min_rows) — the regime GPU query engines win.
/// Eligibility (join limits, unsupported functions, etc.) is enforced separately
/// by candidate_availability; this only screens the shape.
fn gpu_gqe_prior_shape_applies(features: &RouteFeatures, min_rows: i64) -> bool {
    features.table_rows >= min_rows && (features.aggregate_count > 0 || features.group_by)
}

/// Is the GQE server confirmed warm & functional (fresh rvbbit.gqe_warm_state,
/// written only by a successful forced-GQE probe)? Memoized per-backend under the
/// route memo TTL. Returns false when the state table is absent or stale, so the
/// prior deactivates automatically if GQE goes down.
fn gqe_prior_warm() -> bool {
    let ttl = route_stamp_ttl();
    let cached = if ttl.is_zero() {
        None
    } else {
        GQE_WARM_MEMO.with(|m| {
            m.borrow()
                .as_ref()
                .filter(|(at, _)| at.elapsed() < ttl)
                .map(|(_, warm)| *warm)
        })
    };
    if let Some(warm) = cached {
        return warm;
    }
    let warm = if relations_present(&["rvbbit.gqe_warm_state"]) {
        Spi::get_one::<bool>(
            "SELECT coalesce(max(warm_at) > clock_timestamp() - interval '3 minutes', false) \
             FROM rvbbit.gqe_warm_state",
        )
        .ok()
        .flatten()
        .unwrap_or(false)
    } else {
        false
    };
    GQE_WARM_MEMO.with(|m| *m.borrow_mut() = Some((Instant::now(), warm)));
    warm
}

// ── ML routing layer ────────────────────────────────────────────────────────
// A per-engine latency model (rvbbit.route_model) ranks the ELIGIBLE candidates
// and picks the predicted-fastest, inserted at the top of the cold/no-profile
// path. Off by default; a no-op when no models are loaded, so it cannot regress
// routing. Eligibility stays deterministic (candidate_availability) — the model
// only ranks safe candidates, so a misprediction costs latency, never a wrong
// answer. See docs/ML_ROUTING_PLAN.md.

fn route_ml_enabled() -> bool {
    route_enabled("RVBBIT_ROUTE_ML_ENABLED", "rvbbit.route_ml_enabled", false)
}

/// Fraction faster than native a non-native winner must be predicted to be before
/// the ML layer leaves the native default (default 0.15). Keeps the layer
/// conservative: it only moves off native when confidently faster.
fn route_ml_min_margin() -> f64 {
    let raw = {
        #[cfg(not(test))]
        {
            guc_setting("rvbbit.route_ml_min_margin")
                .or_else(|| std::env::var("RVBBIT_ROUTE_ML_MIN_MARGIN").ok())
        }
        #[cfg(test)]
        {
            std::env::var("RVBBIT_ROUTE_ML_MIN_MARGIN").ok()
        }
    };
    raw.and_then(|v| v.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0 && *v < 1.0)
        .unwrap_or(0.15)
}

fn engine_name_to_candidate(name: &str) -> Option<Candidate> {
    Some(match name {
        "native" => Candidate::RvbbitNative,
        "native_vortex" => Candidate::RvbbitNativeVortex,
        "duck" | "duck_vector" => Candidate::DuckVector,
        "duck_hive" => Candidate::DuckHive,
        "duck_vortex" => Candidate::DuckVortex,
        "datafusion" | "datafusion_vector" => Candidate::DataFusionVector,
        "datafusion_hive" => Candidate::DataFusionHive,
        "datafusion_vortex" => Candidate::DataFusionVortex,
        "gpu_gqe" => Candidate::GpuGqe,
        "pg" | "pg_rowstore" | "postgres_rowstore" => Candidate::PgRowstore,
        _ => return None,
    })
}

/// Resolve one model feature name to a numeric value from RouteFeatures. Unknown
/// names → 0.0 (the trainer and evaluator share this vocabulary; a name the
/// evaluator doesn't know is treated as a zeroed feature, never a panic).
fn feature_value(f: &RouteFeatures, name: &str) -> f64 {
    let b = |x: bool| if x { 1.0 } else { 0.0 };
    match name {
        "ln_table_rows" => ((f.table_rows.max(0) as f64) + 1.0).ln(),
        "ln_table_bytes" => ((f.table_bytes.max(0) as f64) + 1.0).ln(),
        "ln_row_group_count" => ((f.row_group_count.max(0) as f64) + 1.0).ln(),
        "table_rows" => f.table_rows as f64,
        "table_bytes" => f.table_bytes as f64,
        "aggregate_count" => f.aggregate_count as f64,
        "count_count" => f.count_count as f64,
        "count_distinct_count" => f.count_distinct_count as f64,
        "sum_count" => f.sum_count as f64,
        "avg_count" => f.avg_count as f64,
        "min_count" => f.min_count as f64,
        "max_count" => f.max_count as f64,
        "from_count" => f.from_count as f64,
        "join_count" => f.join_count as f64,
        "in_count" => f.in_count as f64,
        "between_count" => f.between_count as f64,
        "or_count" => f.or_count as f64,
        "and_count" => f.and_count as f64,
        "comparison_count" => f.comparison_count as f64,
        "like_count" => f.like_count as f64,
        "not_like_count" => f.not_like_count as f64,
        "fixed_contains_like_count" => f.fixed_contains_like_count as f64,
        "regex_count" => f.regex_count as f64,
        "exists_count" => f.exists_count as f64,
        "referenced_text_col_count" => f.referenced_text_col_count as f64,
        "group_text_col_count" => f.group_text_col_count as f64,
        "order_text_col_count" => f.order_text_col_count as f64,
        "count_distinct_text_count" => f.count_distinct_text_count as f64,
        "row_group_count" => f.row_group_count as f64,
        "group_by" => b(f.group_by),
        "order_by" => b(f.order_by),
        "having" => b(f.having),
        "distinct" => b(f.distinct),
        "where" => b(f.where_present),
        "select_star" => b(f.select_star),
        "offset_present" => b(f.offset_present),
        "starts_with_with" => b(f.starts_with_with),
        "has_native_function" => b(f.has_native_function),
        "plan_has_group" => b(f.plan_has_group),
        "plan_has_hash" => b(f.plan_has_hash),
        "plan_has_join" => b(f.plan_has_join),
        "plan_has_sort" => b(f.plan_has_sort),
        "plan_has_subplan" => b(f.plan_has_subplan),
        _ => 0.0,
    }
}

/// Load per-engine models from rvbbit.route_model, memoized per-backend under the
/// route memo TTL. Empty map when the table is absent or has no rows.
fn ml_models() -> std::rc::Rc<std::collections::HashMap<String, crate::route_model::EngineModel>> {
    let ttl = route_stamp_ttl();
    if !ttl.is_zero() {
        if let Some(m) = ML_MODEL_MEMO.with(|c| {
            c.borrow()
                .as_ref()
                .filter(|(at, _)| at.elapsed() < ttl)
                .map(|(_, m)| m.clone())
        }) {
            return m;
        }
    }
    let mut map: std::collections::HashMap<String, crate::route_model::EngineModel> =
        std::collections::HashMap::new();
    if relations_present(&["rvbbit.route_model"]) {
        let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
            let rows = client.select("SELECT engine, params::text FROM rvbbit.route_model", None, &[])?;
            for row in rows {
                let engine: String = row.get(1)?.unwrap_or_default();
                let params: String = row.get(2)?.unwrap_or_default();
                if engine.is_empty() || params.is_empty() {
                    continue;
                }
                if let Ok(model) = serde_json::from_str::<crate::route_model::EngineModel>(&params) {
                    map.insert(engine, model);
                }
            }
            Ok(())
        });
    }
    let rc = std::rc::Rc::new(map);
    ML_MODEL_MEMO.with(|c| *c.borrow_mut() = Some((Instant::now(), rc.clone())));
    rc
}

/// The ML decision layer. Predicts log-latency for every eligible engine that has
/// a model, and returns the predicted-fastest — but only leaves native when a
/// non-native engine is predicted at least `route_ml_min_margin` faster. Returns
/// None (fall through to heuristics) when disabled, unmodeled, or no eligible
/// modeled engine exists.
fn ml_route_decision(features: &RouteFeatures, tables: &[RvbbitTableMetric]) -> Option<RouteDecision> {
    if !route_ml_enabled() {
        return None;
    }
    let models = ml_models();
    if models.is_empty() {
        return None;
    }
    let mut best: Option<(Candidate, f64)> = None;
    let mut native_pred: Option<f64> = None;
    for (engine_name, model) in models.iter() {
        let Some(cand) = engine_name_to_candidate(engine_name) else {
            continue;
        };
        if !candidate_availability(cand, features, tables).0 {
            continue;
        }
        let x: Vec<f64> = model
            .feature_names
            .iter()
            .map(|n| feature_value(features, n))
            .collect();
        let Some(pred) = model.predict(&x) else {
            continue;
        };
        if cand == Candidate::RvbbitNative {
            native_pred = Some(pred);
        }
        if best.map(|(_, b)| pred < b).unwrap_or(true) {
            best = Some((cand, pred));
        }
    }
    let (cand, pred) = best?;
    // log-space margin: exp(pred - native) is the winner/native latency ratio;
    // require it below (1 - margin) to leave native.
    if cand != Candidate::RvbbitNative {
        if let Some(np) = native_pred {
            let leave_native = pred < np + (1.0 - route_ml_min_margin()).ln();
            if !leave_native && candidate_availability(Candidate::RvbbitNative, features, tables).0 {
                return Some(decision(
                    Candidate::RvbbitNative,
                    "ml",
                    "ML latency model: no eligible engine confidently beats native",
                    None,
                    None,
                ));
            }
        }
    }
    Some(decision(
        cand,
        "ml",
        &format!("ML latency model: predicted-fastest eligible engine (score {pred:.3})"),
        None,
        None,
    ))
}

/// The canonical feature vocabulary for route models. Must stay in sync with
/// `feature_value` above (every name here must resolve) and with the FEATURE_NAMES
/// list in scripts/train_route_model.py.
const ROUTE_FEATURE_NAMES: &[&str] = &[
    "ln_table_rows", "ln_table_bytes", "ln_row_group_count",
    "aggregate_count", "count_count", "count_distinct_count",
    "sum_count", "avg_count", "min_count", "max_count",
    "join_count", "from_count", "in_count", "between_count", "or_count",
    "and_count", "comparison_count", "like_count", "not_like_count",
    "regex_count", "exists_count",
    "referenced_text_col_count", "group_text_col_count",
    "order_text_col_count", "count_distinct_text_count",
    "group_by", "order_by", "having", "distinct", "where", "select_star",
    "offset_present", "plan_has_join", "plan_has_sort", "plan_has_group",
    "plan_has_subplan", "plan_has_hash",
];

/// Build the SQL that yields (engine, features_json, median_ms) training rows from
/// whichever sources are present:
///   * route_observations — unbiased per-engine timings from the optimizer
///     replaying real logged queries across ALL engines (the preferred source).
///   * bench_history forced runs — per-engine timings from rvbbit_<engine>_forced
///     bench sweeps (features joined from the query's sibling rows).
///   * bench_history auto runs (when `include_auto`) — the one engine the router
///     chose per query (biased; a bootstrap supplement).
/// Returns None when no source table exists.
fn training_query(obs_present: bool, bench_present: bool, include_auto: bool) -> Option<String> {
    let engine_allowlist = "'native','native_vortex','duck','duck_hive','duck_vortex',\
         'datafusion','datafusion_hive','datafusion_vortex','gpu_gqe','pg'";
    let mut parts: Vec<String> = Vec::new();

    if obs_present {
        // route_observations.candidate uses Candidate::as_str() names; map to the
        // model engine names.
        parts.push(
            "SELECT (CASE candidate \
                   WHEN 'rvbbit_native' THEN 'native' \
                   WHEN 'rvbbit_native_vortex' THEN 'native_vortex' \
                   WHEN 'duck_vector' THEN 'duck' \
                   WHEN 'duck_hive' THEN 'duck_hive' \
                   WHEN 'duck_vortex' THEN 'duck_vortex' \
                   WHEN 'datafusion_vector' THEN 'datafusion' \
                   WHEN 'datafusion_hive' THEN 'datafusion_hive' \
                   WHEN 'datafusion_vortex' THEN 'datafusion_vortex' \
                   WHEN 'gpu_gqe' THEN 'gpu_gqe' \
                   WHEN 'pg_rowstore' THEN 'pg' END) AS engine, \
                 features::text AS features, elapsed_ms AS median_ms \
             FROM rvbbit.route_observations WHERE status='ok' AND elapsed_ms > 0"
                .to_string(),
        );
    }
    if bench_present {
        parts.push(
            "SELECT (CASE q.system \
                   WHEN 'rvbbit_native_forced' THEN 'native' \
                   WHEN 'rvbbit_native_vortex_forced' THEN 'native_vortex' \
                   WHEN 'rvbbit_duck_forced' THEN 'duck' \
                   WHEN 'rvbbit_duck_hive_forced' THEN 'duck_hive' \
                   WHEN 'rvbbit_duck_vortex_forced' THEN 'duck_vortex' \
                   WHEN 'rvbbit_datafusion_forced' THEN 'datafusion' \
                   WHEN 'rvbbit_datafusion_hive_forced' THEN 'datafusion_hive' \
                   WHEN 'rvbbit_datafusion_vortex_forced' THEN 'datafusion_vortex' \
                   WHEN 'rvbbit_gpu_gqe_forced' THEN 'gpu_gqe' \
                   WHEN 'rvbbit_pg_heap_forced' THEN 'pg' END) AS engine, \
                 f.features::text AS features, q.median_ms \
             FROM bench_history.query_results q JOIN feats f USING (run_id, qid) \
             WHERE q.status='ok' AND q.median_ms > 0 AND q.system LIKE 'rvbbit%\\_forced'"
                .to_string(),
        );
        if include_auto {
            parts.push(
                "SELECT (CASE q.detail->'route'->>'route' \
                       WHEN 'postgres_rowstore' THEN 'pg' WHEN 'pg_rowstore' THEN 'pg' \
                       WHEN 'duck_vector' THEN 'duck' WHEN 'datafusion_vector' THEN 'datafusion' \
                       ELSE q.detail->'route'->>'route' END) AS engine, \
                     (q.detail->'route'->'features')::text AS features, q.median_ms \
                 FROM bench_history.query_results q \
                 WHERE q.status='ok' AND q.median_ms > 0 \
                   AND q.system LIKE 'rvbbit%' AND q.system NOT LIKE '%\\_forced' \
                   AND q.detail->'route'->'features' IS NOT NULL"
                    .to_string(),
            );
        }
    }

    if parts.is_empty() {
        return None;
    }
    // The forced/auto branches reference the feats CTE; only emit it when bench
    // sources are in play.
    let feats_cte = if bench_present {
        "WITH feats AS (\
           SELECT DISTINCT ON (run_id, qid) run_id, qid, detail->'route'->'features' AS features \
           FROM bench_history.query_results WHERE detail->'route'->'features' IS NOT NULL) "
    } else {
        ""
    };
    Some(format!(
        "{feats_cte}SELECT engine, features, median_ms FROM ({}) rows \
         WHERE engine IN ({engine_allowlist})",
        parts.join(" UNION ALL ")
    ))
}

/// rvbbit.train_route_model(min_samples, include_auto) — SQL-native trainer for
/// the ML routing layer. Fits a per-engine gradient-boosted latency model from
/// bench_history and writes rvbbit.route_model. Returns a per-engine summary.
/// The Python script (scripts/train_route_model.py) is an offline equivalent.
#[pg_extern]
fn train_route_model(
    min_samples: default!(i32, "20"),
    include_auto: default!(bool, "true"),
) -> JsonB {
    if !relations_present(&["rvbbit.route_model"]) {
        return JsonB(json!({
            "status": "no_table",
            "detail": "rvbbit.route_model missing — run rvbbit.migrate()"
        }));
    }
    let obs_present = relations_present(&["rvbbit.route_observations"]);
    let bench_present = relations_present(&["bench_history.query_results"]);
    let min_samples = min_samples.max(1) as usize;
    let sql = match training_query(obs_present, bench_present, include_auto) {
        Some(s) => s,
        None => {
            return JsonB(json!({
                "status": "no_data",
                "detail": "no training source — populate rvbbit.route_observations \
                           (run rvbbit.route_optimize_auto) or run a benchmark first"
            }))
        }
    };

    let mut per_engine: std::collections::HashMap<String, Vec<crate::route_model::Sample>> =
        std::collections::HashMap::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(&sql, None, &[])?;
        for row in rows {
            let engine: String = row.get(1)?.unwrap_or_default();
            let features_txt: String = row.get(2)?.unwrap_or_default();
            let ms: f64 = row.get::<f64>(3)?.unwrap_or(0.0);
            if engine.is_empty() || features_txt.is_empty() || !(ms > 0.0) {
                continue;
            }
            let rf: RouteFeatures = match serde_json::from_str(&features_txt) {
                Ok(v) => v,
                Err(_) => continue,
            };
            let x: Vec<f64> = ROUTE_FEATURE_NAMES
                .iter()
                .map(|n| feature_value(&rf, n))
                .collect();
            per_engine
                .entry(engine)
                .or_default()
                .push(crate::route_model::Sample { x, y: ms.ln() });
        }
        Ok(())
    });

    let feature_names: Vec<String> = ROUTE_FEATURE_NAMES.iter().map(|s| s.to_string()).collect();
    let cfg = crate::route_model::GbmConfig::default();
    let mut summary = Map::new();
    let mut written = 0i64;
    for (engine, samples) in per_engine.iter() {
        let n = samples.len();
        if n < min_samples {
            summary.insert(engine.clone(), json!({"n": n, "status": "skipped_low_samples"}));
            continue;
        }
        let model = crate::route_model::train_gbm(samples, feature_names.clone(), &cfg);
        let r2 = crate::route_model::r2(&model, samples);
        let params = serde_json::to_string(&model).unwrap_or_else(|_| "{}".to_string());
        let esc = |s: &str| s.replace('\'', "''");
        let notes = format!("rvbbit.train_route_model n={n} r2={r2:.3}");
        let up = format!(
            "INSERT INTO rvbbit.route_model (engine, params, feature_schema, n_samples, trained_at, notes) \
             VALUES ('{}', '{}'::jsonb, 1, {}, clock_timestamp(), '{}') \
             ON CONFLICT (engine) DO UPDATE \
               SET params=EXCLUDED.params, n_samples=EXCLUDED.n_samples, \
                   trained_at=EXCLUDED.trained_at, notes=EXCLUDED.notes",
            esc(engine), esc(&params), n, esc(&notes)
        );
        if Spi::run(&up).is_ok() {
            written += 1;
            summary.insert(
                engine.clone(),
                json!({"n": n, "r2": (r2 * 1000.0).round() / 1000.0, "trees": model.trees.len()}),
            );
        } else {
            summary.insert(engine.clone(), json!({"n": n, "status": "write_failed"}));
        }
    }
    let mut sources: Vec<&str> = Vec::new();
    if obs_present {
        sources.push("route_observations");
    }
    if bench_present {
        sources.push("bench_history(forced)");
        if include_auto {
            sources.push("bench_history(auto)");
        }
    }
    JsonB(json!({
        "status": "ok",
        "written": written,
        "min_samples": min_samples as i64,
        "include_auto": include_auto,
        "sources": sources,
        "engines": Value::Object(summary),
    }))
}

/// rvbbit.route_self_train(...) — the closed self-improving loop in one call,
/// intended for a nightly pg_cron job: replay the hottest real logged query
/// shapes across every eligible engine (route_optimize_auto — read-only,
/// timeout- and budget-bounded, logging each engine's timing to
/// route_observations), then refit the per-engine latency models
/// (train_route_model). No forced bench sweeps, no Python — the router learns
/// from real traffic on its own.
#[pg_extern]
fn route_self_train(
    top_k: default!(i32, "20"),
    max_seconds: default!(i32, "600"),
    samples: default!(i32, "3"),
    min_samples: default!(i32, "20"),
) -> JsonB {
    let optimize = route_optimize_auto(top_k, max_seconds, samples).0;
    // A wedged (parallel-mode) transaction can't run the trainer's SQL either;
    // skip it — the next (fresh-transaction) pass trains on the accumulated data.
    let train = if parallel_mode_wedged() {
        json!({"status": "skipped", "reason": "transaction wedged in parallel mode by a caught bench error; next pass will train"})
    } else {
        train_route_model(min_samples, true).0
    };
    JsonB(json!({ "status": "ok", "optimize": optimize, "train": train }))
}

fn gpu_gqe_unsupported_shape_reason(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<String> {
    gpu_gqe_unsupported_shape_reason_inner(features, tables, gpu_gqe_allow_risky_shapes())
}

fn gpu_gqe_unsupported_shape_reason_inner(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    allow_risky: bool,
) -> Option<String> {
    if allow_risky {
        return None;
    }

    if gpu_gqe_unsupported_join_shape(features) {
        return Some(
            "GPU/GQE supports only simple inner/left/cross joins with explicit ON predicates"
                .to_string(),
        );
    }
    if gpu_gqe_schema_qualified_table_ref(features, tables) {
        return Some(
            "GPU/GQE does not safely support schema-qualified table references yet".to_string(),
        );
    }
    if gpu_gqe_qualified_star_projection(features) {
        return Some("GPU/GQE does not support qualified SELECT * projections".to_string());
    }
    if features.select_star
        && (features.from_count > 1 || features.join_count > 0 || features.plan_has_join)
    {
        return Some("GPU/GQE does not support SELECT * over multiple tables".to_string());
    }
    if gpu_gqe_wide_row_retrieval_shape(features) {
        return Some(
            "GPU/GQE wide SELECT * text-filter/order/limit shapes are disabled to avoid high RMM memory pressure"
                .to_string(),
        );
    }
    None
}

fn gpu_gqe_unsupported_join_shape(features: &RouteFeatures) -> bool {
    let sql = &features.normalized_sql;
    [
        "full join",
        "full outer join",
        "right join",
        "right outer join",
        "natural join",
        " lateral ",
        " using ",
    ]
    .iter()
    .any(|needle| sql.contains(needle))
}

fn gpu_gqe_schema_qualified_table_ref(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> bool {
    tables.iter().any(|table| {
        let schema = table.schema.to_ascii_lowercase();
        let relname = table.relname.to_ascii_lowercase();
        features
            .normalized_sql
            .contains(&format!("{schema}.{relname}"))
            || features
                .normalized_sql
                .contains(&format!("\"{schema}\".\"{relname}\""))
    })
}

fn gpu_gqe_qualified_star_projection(features: &RouteFeatures) -> bool {
    features.normalized_sql.contains(".*") || features.normalized_sql.contains(". *")
}

fn gpu_gqe_wide_row_retrieval_shape(features: &RouteFeatures) -> bool {
    features.select_star
        && features.where_present
        && features.like_count > 0
        && features.order_by
        && features.normalized_sql.contains(" limit ")
}

fn gpu_gqe_unsupported_function_reason(features: &RouteFeatures) -> Option<String> {
    let _ = features;
    None
}

fn gpu_gqe_unsupported_grouping_reason(features: &RouteFeatures) -> Option<String> {
    if sql_group_by_ordinal_present(&features.normalized_sql) {
        if gpu_gqe_literal_first_group_by_rewriteable(&features.normalized_sql) {
            return None;
        }
        return Some("GPU/GQE does not safely support GROUP BY ordinal expressions".to_string());
    }
    None
}

fn gpu_gqe_literal_first_group_by_rewriteable(sql: &str) -> bool {
    let trimmed = sql.trim_start();
    if !(trimmed.starts_with("select ?,") || trimmed.starts_with("select ? ,")) {
        return false;
    }
    top_level_clause(
        sql,
        "group by",
        &["having", "order by", "limit", "offset", "union"],
    )
    .split(',')
    .next()
    .is_some_and(|expr| matches!(expr.trim(), "?" | "1"))
}

fn gpu_gqe_temporal_reference_reason(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<String> {
    let timestamp_columns = tables
        .iter()
        .flat_map(|table| {
            table
                .timestamp_columns
                .iter()
                .map(|column| format!("{}.{}.{}", table.schema, table.relname, column))
        })
        .collect::<Vec<_>>();
    if timestamp_columns.is_empty() {
        return None;
    }
    let referenced = timestamp_columns
        .iter()
        .filter(|qualified| {
            let column = qualified.rsplit('.').next().unwrap_or(qualified.as_str());
            contains_column_identifier(&features.normalized_sql, column)
        })
        .cloned()
        .collect::<Vec<_>>();
    if referenced.is_empty() {
        None
    } else if gpu_gqe_timestamp_references_order_only(features, &referenced)
        || gpu_gqe_timestamp_references_rewriteable(features)
    {
        None
    } else {
        Some(format!(
            "GPU/GQE does not support referenced timestamp column(s): {}",
            referenced.join(", ")
        ))
    }
}

fn gpu_gqe_timestamp_references_rewriteable(features: &RouteFeatures) -> bool {
    sql_function_call_present(&features.normalized_sql, "extract")
        || sql_function_call_present(&features.normalized_sql, "date_trunc")
}

fn gpu_gqe_timestamp_references_order_only(
    features: &RouteFeatures,
    referenced: &[String],
) -> bool {
    let sql = &features.normalized_sql;
    let select_clause = normalized_select_clause(sql);
    let where_clause = top_level_clause(
        sql,
        "where",
        &["group by", "order by", "having", "limit", "offset", "union"],
    );
    let group_clause = top_level_clause(
        sql,
        "group by",
        &["having", "order by", "limit", "offset", "union"],
    );
    let having_clause = top_level_clause(sql, "having", &["order by", "limit", "offset", "union"]);
    let order_clause = top_level_clause(sql, "order by", &["limit", "offset", "union"]);
    if order_clause.is_empty() {
        return false;
    }
    referenced.iter().all(|qualified| {
        let column = qualified.rsplit('.').next().unwrap_or(qualified.as_str());
        contains_column_identifier(&order_clause, column)
            && !contains_column_identifier(&select_clause, column)
            && !contains_column_identifier(&where_clause, column)
            && !contains_column_identifier(&group_clause, column)
            && !contains_column_identifier(&having_clause, column)
    })
}

fn normalized_select_clause(sql: &str) -> String {
    let trimmed = sql.trim_start();
    let Some(after_select) = trimmed.strip_prefix("select") else {
        return String::new();
    };
    let Some(from_pos) = find_top_level_keyword(after_select, "from") else {
        return String::new();
    };
    after_select[..from_pos].trim().to_string()
}

fn sql_function_call_present(sql: &str, function_name: &str) -> bool {
    let mut start = 0usize;
    while let Some(pos) = sql[start..].find(function_name) {
        let abs = start + pos;
        let before = if abs == 0 {
            None
        } else {
            sql[..abs].chars().next_back()
        };
        let after_name = abs + function_name.len();
        let after_name_char = sql[after_name..].chars().next();
        if !before.is_some_and(is_ident_char)
            && !after_name_char.is_some_and(is_ident_char)
            && sql[after_name..].trim_start().starts_with('(')
        {
            return true;
        }
        start = after_name;
    }
    false
}

fn sql_group_by_ordinal_present(sql: &str) -> bool {
    let Some(group_pos) = sql.find("group by") else {
        return false;
    };
    let after_group = &sql[group_pos + "group by".len()..];
    let end = [
        " having ",
        " order by ",
        " limit ",
        " offset ",
        " union ",
        " except ",
        " intersect ",
    ]
    .iter()
    .filter_map(|marker| after_group.find(marker))
    .min()
    .unwrap_or(after_group.len());
    after_group[..end].split(',').any(|expr| {
        let expr = expr.trim();
        expr == "?" || (!expr.is_empty() && expr.chars().all(|ch| ch.is_ascii_digit()))
    })
}

fn hot_mem_availability(features: &RouteFeatures, tables: &[RvbbitTableMetric]) -> (bool, String) {
    if features.regex_count > 0 {
        return (false, "Postgres regex semantics required".to_string());
    }
    if features.table_rows > crate::df::hot_store_route_max_rows() {
        return (
            false,
            format!(
                "table rows {} exceed rvbbit.hot_store_route_max_rows {}",
                features.table_rows,
                crate::df::hot_store_route_max_rows()
            ),
        );
    }
    let table_ids = tables
        .iter()
        .map(|table| (table.oid, format!("{}.{}", table.schema, table.relname)))
        .collect::<Vec<_>>();
    crate::df::hot_tables_available(&table_ids)
}

fn min_confidence_for_candidate(candidate: Candidate) -> f64 {
    match candidate {
        Candidate::PgRowstore => 0.25,
        Candidate::DataFusionMem => 0.05,
        Candidate::DuckHive | Candidate::DataFusionHive => env_f64(
            "RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE",
            "rvbbit.route_hive_min_confidence",
            0.08,
        ),
        Candidate::DuckVortex => env_f64(
            "RVBBIT_ROUTE_DUCK_VORTEX_MIN_CONFIDENCE",
            "rvbbit.route_duck_vortex_min_confidence",
            0.05,
        ),
        Candidate::GpuGqe => env_f64(
            "RVBBIT_ROUTE_GPU_GQE_MIN_CONFIDENCE",
            "rvbbit.route_gpu_gqe_min_confidence",
            0.05,
        ),
        _ => 0.05,
    }
}

const FALLBACK_VECTOR_FIRST: [Candidate; 3] = [
    Candidate::DataFusionVector,
    Candidate::DuckVector,
    Candidate::DuckHive,
];

const FALLBACK_VORTEX_VECTOR_FIRST: [Candidate; 4] = [
    Candidate::DuckVortex,
    Candidate::DataFusionVector,
    Candidate::DuckVector,
    Candidate::DuckHive,
];

const FALLBACK_VORTEX_DUCK_FIRST: [Candidate; 4] = [
    Candidate::DuckVortex,
    Candidate::DuckVector,
    Candidate::DataFusionVector,
    Candidate::DuckHive,
];

const FALLBACK_VORTEX_VARIANT_FIRST: [Candidate; 4] = [
    Candidate::DuckVortex,
    Candidate::DuckHive,
    Candidate::DataFusionVector,
    Candidate::DuckVector,
];

const FALLBACK_MEM_FIRST: [Candidate; 4] = [
    Candidate::DataFusionMem,
    Candidate::DataFusionVector,
    Candidate::DuckVector,
    Candidate::DuckHive,
];

const FALLBACK_VARIANT_FIRST: [Candidate; 3] = [
    Candidate::DuckHive,
    Candidate::DataFusionVector,
    Candidate::DuckVector,
];

const FALLBACK_DUCK_FIRST: [Candidate; 3] = [
    Candidate::DuckVector,
    Candidate::DuckHive,
    Candidate::DataFusionVector,
];

const FALLBACK_COMPLEX_HIVE_FIRST: [Candidate; 3] = [
    Candidate::DuckHive,
    Candidate::DuckVector,
    Candidate::DataFusionVector,
];

fn all_dirty_tables_native_overlay_readable(tables: &[RvbbitTableMetric]) -> bool {
    let dirty = tables.iter().filter(|t| t.shadow_heap_dirty).count();
    dirty > 0
        && tables
            .iter()
            .filter(|t| t.shadow_heap_dirty)
            .all(|t| t.native_overlay_readable)
}

fn default_external_candidate(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<Candidate> {
    if hot_store_no_profile_enabled() && hot_store_prefers_mem(features) {
        first_available_candidate(&FALLBACK_MEM_FIRST, features, tables)
    } else if fallback_prefers_vortex_default(features) {
        first_available_candidate(vortex_first_candidate_order(features), features, tables)
    } else {
        first_available_candidate(&FALLBACK_VECTOR_FIRST, features, tables)
    }
}

fn vortex_first_candidate_order(features: &RouteFeatures) -> &'static [Candidate] {
    if fallback_prefers_complex_duck_hive(features) || fallback_prefers_variant(features) {
        &FALLBACK_VORTEX_VARIANT_FIRST
    } else if fallback_prefers_duck_vector(features) {
        &FALLBACK_VORTEX_DUCK_FIRST
    } else {
        &FALLBACK_VORTEX_VECTOR_FIRST
    }
}

fn first_available_candidate(
    candidates: &[Candidate],
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<Candidate> {
    candidates
        .iter()
        .copied()
        .find(|candidate| candidate_availability(*candidate, features, tables).0)
}

fn forced_candidate_setting() -> Option<Candidate> {
    guc_setting("rvbbit.route_force_candidate")
        .map(|value| value.trim().to_ascii_lowercase())
        .filter(|value| !value.is_empty() && value != "default" && value != "auto")
        .and_then(|value| Candidate::from_str(&value))
}

fn forced_route_decision(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    let candidate = forced_candidate_setting()?;
    let (available, reason) = candidate_availability(candidate, features, tables);
    if available {
        Some(decision(
            candidate,
            "forced",
            &format!("forced by rvbbit.route_force_candidate; {reason}"),
            Some(1.0),
            None,
        ))
    } else {
        Some(decision(
            Candidate::RvbbitNative,
            "forced-unavailable",
            &format!(
                "forced candidate {} unavailable: {reason}; using native path",
                candidate.as_str()
            ),
            None,
            None,
        ))
    }
}

fn simple_metadata_aggregate_should_stay_native(features: &RouteFeatures) -> bool {
    features.aggregate_count > 0
        && features.aggregate_count
            == features.count_count
                + features.sum_count
                + features.avg_count
                + features.min_count
                + features.max_count
        && features.count_distinct_count == 0
        && !features.where_present
        && !features.group_by
        && !features.having
        && !features.distinct
        && features.from_count == 1
        && features.join_count == 0
        && !features.plan_has_join
        && !features.plan_has_subplan
}

fn filtered_count_should_stay_native(features: &RouteFeatures) -> bool {
    features.count_count > 0
        && features.aggregate_count == features.count_count
        && features.count_distinct_count == 0
        && features.where_present
        && !features.group_by
        && !features.having
        && !features.distinct
        && features.from_count == 1
        && features.join_count == 0
        && !features.plan_has_join
        && !features.plan_has_subplan
}

#[cfg(test)]
fn fallback_native_reason(features: &RouteFeatures) -> Option<&'static str> {
    fallback_native_reason_inner(features, true)
}

fn fallback_native_reason_for_tables(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<&'static str> {
    let allow_filtered_count = !filtered_count_should_stay_native(features)
        || native_filtered_count_metadata_available(tables);
    fallback_native_reason_inner(features, allow_filtered_count)
}

fn fallback_native_reason_inner(
    features: &RouteFeatures,
    allow_filtered_count: bool,
) -> Option<&'static str> {
    if allow_filtered_count && filtered_count_should_stay_native(features) {
        return Some("filtered count metadata stays on native path");
    }
    if selective_single_table_topk_should_stay_native(features)
        && features.has_native_function
        && !native_function_prefers_external_at_scale(features)
    {
        return Some("selective single-table top-k rewrite stays on native path");
    }
    if features.has_native_function && !fallback_prefers_external_analytical_shape(features) {
        return Some("native PostgreSQL plan rewrite is available");
    }
    if features.table_rows > 0
        && features.table_rows <= no_profile_native_max_rows()
        && (features.aggregate_count > 0 || features.group_by || features.distinct)
        && !fallback_prefers_external_analytical_shape(features)
    {
        return Some("small/simple analytical table stays on native path");
    }
    if features.aggregate_count == 0
        && !features.group_by
        && !features.distinct
        && !features.having
        && features.join_count == 0
        && features.from_count <= 1
        && !features.plan_has_join
        && !features.plan_has_subplan
    {
        return Some("row-returning query stays on native path");
    }
    None
}

#[cfg(test)]
fn no_profile_native_reason(features: &RouteFeatures) -> Option<&'static str> {
    match fallback_native_reason(features)? {
        "native PostgreSQL plan rewrite is available" => {
            Some("no active route profile; native PostgreSQL plan rewrite is available")
        }
        "small/simple analytical table stays on native path" => {
            Some("no active route profile; small/simple analytical table stays on native path")
        }
        "row-returning query stays on native path" => {
            Some("no active route profile; row-returning query stays on native path")
        }
        "selective single-table top-k rewrite stays on native path" => Some(
            "no active route profile; selective single-table top-k rewrite stays on native path",
        ),
        "filtered count metadata stays on native path" => {
            Some("no active route profile; filtered count metadata stays on native path")
        }
        _ => Some("no active route profile; using native path"),
    }
}

fn no_profile_native_reason_for_tables(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<&'static str> {
    match fallback_native_reason_for_tables(features, tables)? {
        "native PostgreSQL plan rewrite is available" => {
            Some("no active route profile; native PostgreSQL plan rewrite is available")
        }
        "small/simple analytical table stays on native path" => {
            Some("no active route profile; small/simple analytical table stays on native path")
        }
        "row-returning query stays on native path" => {
            Some("no active route profile; row-returning query stays on native path")
        }
        "selective single-table top-k rewrite stays on native path" => Some(
            "no active route profile; selective single-table top-k rewrite stays on native path",
        ),
        "filtered count metadata stays on native path" => {
            Some("no active route profile; filtered count metadata stays on native path")
        }
        _ => Some("no active route profile; using native path"),
    }
}

fn no_profile_native_max_rows() -> i64 {
    let configured = {
        #[cfg(not(test))]
        {
            guc_setting("rvbbit.route_no_profile_native_max_rows")
                .or_else(|| std::env::var("RVBBIT_ROUTE_NO_PROFILE_NATIVE_MAX_ROWS").ok())
        }
        #[cfg(test)]
        {
            std::env::var("RVBBIT_ROUTE_NO_PROFILE_NATIVE_MAX_ROWS").ok()
        }
    };
    configured
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value >= 0)
        .unwrap_or(500_000)
}

fn no_profile_prefers_datafusion(features: &RouteFeatures) -> bool {
    features.aggregate_count > 0
        || features.group_by
        || features.distinct
        || features.having
        || features.join_count > 0
        || features.from_count > 1
}

fn no_profile_variant_min_rows() -> i64 {
    let configured = {
        #[cfg(not(test))]
        {
            guc_setting("rvbbit.route_no_profile_variant_min_rows")
                .or_else(|| std::env::var("RVBBIT_ROUTE_NO_PROFILE_VARIANT_MIN_ROWS").ok())
        }
        #[cfg(test)]
        {
            std::env::var("RVBBIT_ROUTE_NO_PROFILE_VARIANT_MIN_ROWS").ok()
        }
    };
    configured
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value >= 0)
        .unwrap_or(250_000)
}

fn fallback_prefers_external_analytical_shape(features: &RouteFeatures) -> bool {
    if filtered_count_should_stay_native(features) {
        return false;
    }
    if native_function_prefers_external_at_scale(features) {
        return true;
    }
    if selective_single_table_topk_should_stay_native(features) {
        return false;
    }
    if native_function_prefers_vector_external(features) {
        return true;
    }
    if native_function_should_stay_native(features)
        || simple_metadata_aggregate_should_stay_native(features)
    {
        return false;
    }
    if complex_analytical_shape(features) {
        return true;
    }
    if large_wide_grouped_aggregate_prefers_vector(features) {
        return true;
    }
    if features.count_distinct_count > 0
        && (!features.group_by
            || features.count_distinct_text_count > 0
            || features.table_rows >= no_profile_variant_min_rows())
    {
        return true;
    }
    if has_time_bucket_group(features) {
        return true;
    }
    if features.group_by
        && features.order_by
        && has_limit(features)
        && features.group_text_col_count > 0
        && features.table_rows >= 100_000
        && (has_multiple_group_keys(features) || features.count_distinct_count > 0)
    {
        return true;
    }
    if features.group_by
        && features.order_by
        && has_limit(features)
        && has_multiple_group_keys(features)
        && features.table_rows >= no_profile_native_max_rows()
    {
        return true;
    }
    if features.fixed_contains_like_count > 0
        && features.group_by
        && features.table_rows >= no_profile_variant_min_rows()
    {
        return true;
    }
    false
}

fn fallback_prefers_variant(features: &RouteFeatures) -> bool {
    if !fallback_prefers_external_analytical_shape(features) {
        return false;
    }
    if features.table_rows < no_profile_variant_min_rows() {
        return false;
    }
    if features.count_distinct_text_count > 0 {
        return true;
    }
    if features.fixed_contains_like_count > 0 && features.group_by {
        return true;
    }
    if features.group_by
        && features.order_by
        && has_limit(features)
        && features.group_text_col_count > 0
        && (has_multiple_group_keys(features)
            || features.count_distinct_count > 0
            || features.table_rows >= 1_000_000)
    {
        return true;
    }
    if features.group_by
        && features.order_by
        && has_limit(features)
        && features.where_present
        && has_multiple_group_keys(features)
    {
        return true;
    }
    false
}

fn fallback_external_candidate_order(features: &RouteFeatures) -> Option<&'static [Candidate]> {
    if fallback_prefers_vortex_default(features) {
        Some(vortex_first_candidate_order(features))
    } else if native_function_prefers_vector_external(features) {
        Some(&FALLBACK_VECTOR_FIRST)
    } else if single_table_text_distinct_prefers_vector(features) {
        Some(&FALLBACK_VECTOR_FIRST)
    } else if fallback_prefers_complex_duck_hive(features) {
        Some(&FALLBACK_COMPLEX_HIVE_FIRST)
    } else if fallback_prefers_duck_vector(features) {
        Some(&FALLBACK_DUCK_FIRST)
    } else if fallback_prefers_variant(features) {
        Some(&FALLBACK_VARIANT_FIRST)
    } else if fallback_prefers_external_analytical_shape(features)
        || no_profile_prefers_datafusion(features)
    {
        Some(&FALLBACK_VECTOR_FIRST)
    } else {
        None
    }
}

fn fallback_prefers_vortex_default(features: &RouteFeatures) -> bool {
    features.table_rows >= no_profile_variant_min_rows()
        && (fallback_prefers_external_analytical_shape(features)
            || fallback_prefers_duck_vector(features)
            || no_profile_prefers_datafusion(features))
}

fn fallback_prefers_duck_vector(features: &RouteFeatures) -> bool {
    if features.table_rows < 1_000_000 || simple_metadata_aggregate_should_stay_native(features) {
        return false;
    }
    if features.normalized_sql.contains("full outer join")
        || features.normalized_sql.contains(" over (")
    {
        return true;
    }
    if features.join_count >= 4 {
        return true;
    }
    if features.from_count >= 5 && features.aggregate_count >= 3 {
        return true;
    }
    if features.starts_with_with
        && features.plan_has_subplan
        && features.from_count >= 4
        && features.aggregate_count >= 2
    {
        return true;
    }
    false
}

fn single_table_text_distinct_prefers_vector(features: &RouteFeatures) -> bool {
    features.from_count <= 1
        && features.join_count == 0
        && !features.plan_has_join
        && !features.plan_has_subplan
        && !features.group_by
        && features.count_distinct_text_count > 0
}

fn fallback_prefers_complex_duck_hive(features: &RouteFeatures) -> bool {
    complex_analytical_shape(features)
        && features.table_rows >= no_profile_variant_min_rows()
        && !duck_hive_known_unsupported(features)
}

fn complex_analytical_shape(features: &RouteFeatures) -> bool {
    let complex_from = features.join_count > 0
        || features.from_count > 1
        || features.plan_has_join
        || features.plan_has_subplan
        || features.exists_count > 0
        || features.starts_with_with;
    let analytical = features.aggregate_count > 0
        || features.group_by
        || features.order_by
        || features.distinct
        || features.having
        || features.exists_count > 0
        || features.in_count > 0;
    complex_from && analytical
}

fn duck_hive_known_unsupported(features: &RouteFeatures) -> bool {
    features.regex_count > 0 || features.normalized_sql.contains(") at_")
}

fn selective_single_table_topk_should_stay_native(features: &RouteFeatures) -> bool {
    features.from_count <= 1
        && features.join_count == 0
        && !features.plan_has_join
        && !features.plan_has_subplan
        && features.where_present
        && features.group_by
        && features.order_by
        && has_limit(features)
        && features.count_distinct_count == 0
        && features.aggregate_count <= 1
        && features.and_count >= 3
        && features.comparison_count >= 4
        && features.fixed_contains_like_count == 0
}

fn large_wide_grouped_aggregate_prefers_vector(features: &RouteFeatures) -> bool {
    features.from_count <= 1
        && features.join_count == 0
        && !features.plan_has_join
        && !features.plan_has_subplan
        && features.group_by
        && features.aggregate_count >= 4
        && features.table_rows >= no_profile_native_max_rows()
        && !selective_single_table_topk_should_stay_native(features)
}

fn hot_store_prefers_mem(features: &RouteFeatures) -> bool {
    features.table_rows > 0
        && features.table_rows <= crate::df::hot_store_route_max_rows()
        && !simple_metadata_aggregate_should_stay_native(features)
        && !native_function_should_stay_native(features)
        && (features.aggregate_count > 0
            || features.group_by
            || features.distinct
            || features.having)
        && !(features.select_star && features.aggregate_count == 0 && !features.group_by)
}

fn hot_store_no_profile_enabled() -> bool {
    route_enabled(
        "RVBBIT_ROUTE_DATAFUSION_MEM_NO_PROFILE",
        "rvbbit.route_datafusion_mem_no_profile",
        false,
    )
}

fn has_limit(features: &RouteFeatures) -> bool {
    features.limit_bucket != "unknown"
}

fn has_multiple_group_keys(features: &RouteFeatures) -> bool {
    !matches!(features.group_expr_count_bucket.as_str(), "<=0" | "<=1")
}

fn has_time_bucket_group(features: &RouteFeatures) -> bool {
    features.group_by
        && (features.normalized_sql.contains("date_trunc")
            || features.normalized_sql.contains("extract("))
}

fn choose_no_profile_route(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
) -> RouteDecision {
    // ML routing layer (docs/ML_ROUTING_PLAN.md). When enabled and trained, a
    // per-engine latency model ranks the eligible candidates and picks the
    // predicted-fastest, superseding the hand rules + GQE prior below. No-op
    // (returns None) when disabled or untrained, so this is a pure addition.
    if let Some(ml) = ml_route_decision(features, tables) {
        return ml;
    }
    if hot_store_no_profile_enabled() && hot_store_prefers_mem(features) {
        if let Some(candidate) =
            first_available_candidate(&[Candidate::DataFusionMem], features, tables)
        {
            return decision(
                candidate,
                "no-profile-hot",
                "no active route profile; manually loaded hot columnar object uses in-memory DataFusion",
                None,
                None,
            );
        }
    }
    // GPU/GQE warm-prior. GQE is absent from every FALLBACK_* order, so the cold
    // router never picks it and it never gathers timings. When the operator opts
    // in (route_gpu_gqe_prior) on a GQE-capable machine, prefer GQE for large
    // analytical shapes — but only when GQE is actually eligible for THIS query
    // AND confirmed warm, so no user query pays a cold-start. Trained shapes never
    // reach here (overlay/profile layers run first), so this only affects the
    // untrained cold path.
    if route_gpu_gqe_prior_enabled()
        && gpu_gqe_prior_shape_applies(features, route_gpu_gqe_prior_min_rows())
        && gqe_prior_warm()
    {
        if let Some(candidate) =
            first_available_candidate(&[Candidate::GpuGqe], features, tables)
        {
            return decision(
                candidate,
                "no-profile-gpu-gqe-prior",
                "no active route profile; large analytical shape routed to warm GPU/GQE (route_gpu_gqe_prior)",
                None,
                None,
            );
        }
    }
    if let Some(reason) = no_profile_native_reason_for_tables(features, tables) {
        return decision(
            Candidate::RvbbitNative,
            "no-profile-native",
            reason,
            None,
            None,
        );
    }
    if let Some(candidates) = fallback_external_candidate_order(features) {
        if let Some(candidate) = first_available_candidate(candidates, features, tables) {
            let (source, reason) = if matches!(
                candidate,
                Candidate::DataFusionHive | Candidate::DuckHive | Candidate::DuckVortex
            ) {
                (
                    "no-profile-variant",
                    "no active route profile; variant-friendly analytical shape uses a parquet variant path",
                )
            } else if candidate == Candidate::DuckVector {
                (
                    "no-profile-duck",
                    "no active route profile; complex analytical shape uses DuckDB vector execution",
                )
            } else {
                (
                    "no-profile-datafusion",
                    "no active route profile; analytical parquet shape uses vector execution",
                )
            };
            return decision(candidate, source, reason, None, None);
        }
        return decision(
            Candidate::RvbbitNative,
            "no-profile-native",
            "no active route profile; external parquet path is unavailable",
            None,
            None,
        );
    }
    decision(
        Candidate::RvbbitNative,
        "no-profile-native",
        profile
            .warning
            .as_deref()
            .unwrap_or("no active route profile; using native path"),
        None,
        None,
    )
}

fn native_function_should_stay_native(features: &RouteFeatures) -> bool {
    let Some(name) = features.native_function.as_deref() else {
        return false;
    };

    if !single_table_native_rewrite(features) {
        return false;
    }

    if native_function_prefers_external_at_scale(features) {
        return false;
    }

    if native_function_prefers_vector_external(features) {
        return false;
    }

    if name == "agg_groupby_count" || name == "top_rollup_1int_distinct" {
        return true;
    }

    if scalable_native_topk_threshold(name).is_some() {
        return true;
    }

    if !matches!(
        name,
        "vector_float_agg"
            | "count_text_contains"
            | "top_phrase_min_url_for_url_contains"
            | "top_phrase_url_title_rollup"
            | "top_rows_text_contains_ordered_json"
            | "top_text_transform_avg_len"
            | "any_count_int_text"
            | "top_count_filtered"
    ) {
        return false;
    }

    !(features.plan_has_sort
        || features.plan_has_group
        || features.plan_has_join
        || features.plan_has_subplan)
}

fn native_function_prefers_external_at_scale(features: &RouteFeatures) -> bool {
    if features.table_rows < no_profile_variant_min_rows() {
        return false;
    }
    matches!(
        features.native_function.as_deref(),
        Some("top_rollup_1int_distinct" | "any_count_int_text" | "top_count_filtered")
    )
}

fn native_function_prefers_vector_external(features: &RouteFeatures) -> bool {
    let Some(name) = features.native_function.as_deref() else {
        return false;
    };
    let Some(threshold) = scalable_native_topk_threshold(name) else {
        return false;
    };
    single_table_native_rewrite(features) && features.table_rows > threshold
}

fn scalable_native_topk_threshold(name: &str) -> Option<i64> {
    match name {
        "top_avg_len_by_int_col" | "top_rollup_2int" | "top_count_int_minute_text" => {
            Some(no_profile_native_max_rows())
        }
        "top_count_1col"
        | "top_count_distinct_1col"
        | "top_count_distinct_int_text"
        | "top_count_int_text"
        | "top_searchphrase_ordered" => Some(1_000_000),
        _ => None,
    }
}

fn single_table_native_rewrite(features: &RouteFeatures) -> bool {
    features.from_count <= 1
        && features.join_count == 0
        && !features.plan_has_join
        && !features.plan_has_subplan
}

fn format_route_explain_text(doc: &Value) -> String {
    let get = |key: &str| doc.get(key).and_then(Value::as_str).unwrap_or("none");
    let mut lines = Vec::new();
    lines.push("Rvbbit Route".to_string());
    lines.push(format!("  Route       : {}", get("route")));
    lines.push(format!(
        "  Candidate   : {}",
        doc.get("chosen_candidate")
            .and_then(Value::as_str)
            .unwrap_or("none")
    ));
    lines.push(format!(
        "  Profile     : {} ({})",
        doc.get("profile_name")
            .and_then(Value::as_str)
            .unwrap_or("none"),
        doc.get("profile_source")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
    ));
    lines.push(format!("  Source      : {}", get("route_source")));
    lines.push(format!(
        "  Reason      : {}",
        doc.get("reason").and_then(Value::as_str).unwrap_or("none")
    ));
    if let Some(confidence) = doc.get("confidence").and_then(Value::as_f64) {
        lines.push(format!("  Confidence  : {confidence:.3}"));
    }
    lines.push(format!(
        "  Safe SELECT : {}",
        doc.get("safe_select")
            .and_then(Value::as_bool)
            .map(|v| v.to_string())
            .unwrap_or_else(|| "false".to_string())
    ));
    if let Some(metrics) = doc.get("table_metrics") {
        lines.push(format!(
            "  Tables      : rows={} row_groups={} bytes={} heap_bytes={} deletes={}",
            metrics.get("rows").and_then(Value::as_i64).unwrap_or(0),
            metrics
                .get("row_groups")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            metrics.get("bytes").and_then(Value::as_i64).unwrap_or(0),
            metrics
                .get("heap_bytes")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            metrics
                .get("delete_count")
                .and_then(Value::as_i64)
                .unwrap_or(0),
        ));
    }
    lines.push("".to_string());
    lines.push("Candidates".to_string());
    match doc.get("candidates").and_then(Value::as_array) {
        Some(candidates) if !candidates.is_empty() => {
            for candidate in candidates {
                let marker = if candidate
                    .get("selected")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    "*"
                } else {
                    "-"
                };
                let available = candidate
                    .get("available")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                lines.push(format!(
                    "  {marker} {:<14} available={} {}",
                    candidate
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown"),
                    available,
                    candidate
                        .get("reason")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                ));
            }
        }
        _ => lines.push("  none".to_string()),
    }
    lines.join("\n")
}

fn compact_profile_for_storage(profile: &Value) -> Value {
    let mut compact = profile.clone();
    if let Value::Object(map) = &mut compact {
        if let Some(observations) = map.remove("observations") {
            map.insert(
                "observation_count".into(),
                json!(observations.as_array().map(|a| a.len()).unwrap_or(0)),
            );
            map.insert("observations_persisted".into(), json!(true));
        }
        if let Some(points) = map.remove("profile_points") {
            map.insert(
                "profile_point_count".into(),
                json!(points.as_array().map(|a| a.len()).unwrap_or(0)),
            );
            map.insert("profile_points_persisted".into(), json!(true));
        }
    }
    compact
}

fn store_route_profile(
    profile_name: &str,
    profile: &Value,
    active: bool,
    caller: &str,
) -> (i64, i64, Value) {
    if !profile.is_object() {
        pgrx::error!("rvbbit.{caller}: profile must be a JSON object");
    }
    let name_lit = sql_lit(profile_name);
    let storage_profile = compact_profile_for_storage(profile);
    let profile_lit = sql_json_lit(&storage_profile);
    if active {
        Spi::run("UPDATE rvbbit.route_profiles SET active = false WHERE active")
            .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
    }
    Spi::run(&format!(
        "INSERT INTO rvbbit.route_profiles (name, active, profile) \
         VALUES ({name_lit}, {active}, {profile_lit}::jsonb) \
         ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active, profile = EXCLUDED.profile"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
    persist_profile_tables(profile_name, profile)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
    let (entries, points) = refresh_profile_json_from_tables(profile_name, profile.clone(), caller);
    let stored_profile = Spi::get_one::<JsonB>(&format!(
        "SELECT profile FROM rvbbit.route_profiles WHERE name = {name_lit}"
    ))
    .ok()
    .flatten()
    .map(|j| j.0)
    .unwrap_or(storage_profile);
    (entries, points, stored_profile)
}

fn export_route_profile_value(profile_name: &str, caller: &str) -> Value {
    let name_lit = sql_lit(profile_name);
    Spi::get_one::<JsonB>(&format!(
        r#"
        WITH rp AS (
            SELECT name, active, profile
            FROM rvbbit.route_profiles
            WHERE name = {name_lit}
        ),
        entries AS (
            SELECT coalesce(jsonb_object_agg(shape_key, entry ORDER BY shape_key), '{{}}'::jsonb) AS entries,
                   count(*)::bigint AS entry_count
            FROM rvbbit.route_profile_entries
            WHERE profile_name = {name_lit}
        ),
        points AS (
            SELECT coalesce(jsonb_agg(
                       jsonb_build_object(
                           'shape_family', shape_family,
                           'table_rows', table_rows,
                           'native_ms', native_ms,
                           'native_vortex_ms', native_vortex_ms,
                           'duck_ms', duck_ms,
                           'duck_hive_ms', duck_hive_ms,
                           'duck_vortex_ms', duck_vortex_ms,
                           'datafusion_ms', datafusion_ms,
                           'datafusion_hive_ms', datafusion_hive_ms,
                           'datafusion_vortex_ms', datafusion_vortex_ms,
                           'gpu_gqe_ms', gpu_gqe_ms,
                           'pg_ms', pg_ms,
                           'point', point
                       )
                       ORDER BY shape_family, table_rows, id
                   ), '[]'::jsonb) AS profile_points,
                   count(*)::bigint AS profile_point_count
            FROM rvbbit.route_profile_points
            WHERE profile_name = {name_lit}
        )
        SELECT rp.profile || jsonb_build_object(
            'name', rp.name,
            'active', rp.active,
            'entries', entries.entries,
            'entry_count', entries.entry_count,
            'profile_points', points.profile_points,
            'profile_point_count', points.profile_point_count,
            'exported_at', to_jsonb(clock_timestamp()),
            'exported_by', 'pg_rvbbit.route_export_profile'
        )
        FROM rp, entries, points
        "#
    ))
    .ok()
    .flatten()
    .map(|j| j.0)
    .unwrap_or_else(|| pgrx::error!("rvbbit.{caller}: profile '{profile_name}' could not be exported"))
}

fn persist_profile_tables(
    profile_name: &str,
    profile: &Value,
) -> Result<(i64, i64), pgrx::spi::Error> {
    let name_lit = sql_lit(profile_name);
    let profile_lit = sql_json_lit(profile);
    Spi::run(&format!(
        "DELETE FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))?;
    Spi::run(&format!(
        "DELETE FROM rvbbit.route_profile_points WHERE profile_name = {name_lit}"
    ))?;
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.route_profile_entries
            (profile_name, shape_key, choice, confidence, reason, observations,
             native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, entry)
        SELECT {name_lit},
               e.key,
               CASE e.value->>'choice'
                   WHEN 'native' THEN 'rvbbit_native'
                   WHEN 'duck' THEN 'duck_vector'
                   WHEN 'df_mem' THEN 'datafusion_mem'
                   WHEN 'datafusion' THEN 'datafusion_vector'
                   WHEN 'df_hive' THEN 'datafusion_hive'
                   WHEN 'gqe' THEN 'gpu_gqe'
                   WHEN 'pg_heap' THEN 'pg_rowstore'
                   ELSE e.value->>'choice'
               END,
               coalesce(nullif(e.value->>'confidence', '')::double precision, 0),
               coalesce(e.value->>'reason', ''),
               coalesce(nullif(e.value->>'observations', '')::bigint, 0),
               coalesce(nullif(e.value->>'native_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'rvbbit_native'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'native_vortex_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'rvbbit_native_vortex'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'duck_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'duck_vector'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'duck_hive_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'duck_hive'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'duck_vortex_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'duck_vortex'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'datafusion_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'datafusion_vector'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'datafusion_hive_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'datafusion_hive'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'datafusion_vortex_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'datafusion_vortex'
                   LIMIT 1
               )),
               coalesce(nullif(e.value->>'gpu_gqe_ms_median', '')::double precision, (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'gpu_gqe'
                   LIMIT 1
               )),
               (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'pg_rowstore'
                   LIMIT 1
               ),
               e.value
        FROM jsonb_each(coalesce({profile_lit}::jsonb->'entries', '{{}}'::jsonb)) AS e(key, value)
        WHERE e.value ? 'choice'
          AND e.value->>'choice' IN ('duck', 'duck_hive', 'duck_vortex', 'native', 'datafusion_mem', 'df_mem', 'datafusion', 'datafusion_hive', 'datafusion_vortex', 'df_hive', 'gpu_gqe', 'gqe', 'pg_heap', 'duck_vector', 'datafusion_vector', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore')
        "#
    ))?;
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.route_profile_points
            (profile_name, shape_family, table_rows, native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, point)
        SELECT {name_lit},
               regexp_replace(
                   regexp_replace(coalesce(obs->'features'->>'shape_key', ''),
                                  '(^|\|)table_rows=[^|]*', '', 'g'),
                   '^\|', ''
               ),
               coalesce(nullif(obs->'features'->>'table_rows', '')::bigint, 0),
               nullif(obs->>'native_ms', '')::double precision,
               nullif(obs->>'native_vortex_ms', '')::double precision,
               nullif(obs->>'duck_ms', '')::double precision,
               nullif(obs->>'duck_hive_ms', '')::double precision,
               nullif(obs->>'duck_vortex_ms', '')::double precision,
               nullif(obs->>'datafusion_ms', '')::double precision,
               nullif(obs->>'datafusion_hive_ms', '')::double precision,
               nullif(obs->>'datafusion_vortex_ms', '')::double precision,
               nullif(obs->>'gpu_gqe_ms', '')::double precision,
               nullif(obs->>'pg_ms', '')::double precision,
               obs
        FROM jsonb_array_elements(coalesce({profile_lit}::jsonb->'observations', '[]'::jsonb)) AS obs
        WHERE obs ? 'features'
          AND obs ? 'native_ms'
          AND obs ? 'duck_ms'
          AND coalesce(nullif(obs->'features'->>'table_rows', '')::bigint, 0) > 0
          AND nullif(obs->>'native_ms', '')::double precision > 0
          AND nullif(obs->>'duck_ms', '')::double precision > 0
        "#
    ))?;
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.route_profile_points
            (profile_name, shape_family, table_rows, native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, point)
        SELECT {name_lit},
               shape_family,
               table_rows,
               native_ms,
               native_vortex_ms,
               duck_ms,
               duck_hive_ms,
               duck_vortex_ms,
               datafusion_ms,
               datafusion_hive_ms,
               datafusion_vortex_ms,
               gpu_gqe_ms,
               pg_ms,
               point
        FROM (
            SELECT coalesce(
                       nullif(pp->>'shape_family', ''),
                       nullif(pp->'point'->>'shape_family', ''),
                       regexp_replace(
                           regexp_replace(coalesce(
                               pp->'point'->'features'->>'shape_key',
                               pp->'features'->>'shape_key',
                               ''
                           ), '(^|\|)table_rows=[^|]*', '', 'g'),
                           '^\|', ''
                       )
                   ) AS shape_family,
                   coalesce(
                       nullif(pp->>'table_rows', '')::bigint,
                       nullif(pp->'point'->>'table_rows', '')::bigint,
                       nullif(pp->'point'->'features'->>'table_rows', '')::bigint,
                       0
                   ) AS table_rows,
                   coalesce(
                       nullif(pp->>'native_ms', '')::double precision,
                       nullif(pp->'point'->>'native_ms', '')::double precision
                   ) AS native_ms,
                   coalesce(
                       nullif(pp->>'native_vortex_ms', '')::double precision,
                       nullif(pp->'point'->>'native_vortex_ms', '')::double precision
                   ) AS native_vortex_ms,
                   coalesce(
                       nullif(pp->>'duck_ms', '')::double precision,
                       nullif(pp->'point'->>'duck_ms', '')::double precision
                   ) AS duck_ms,
                   coalesce(
                       nullif(pp->>'duck_hive_ms', '')::double precision,
                       nullif(pp->'point'->>'duck_hive_ms', '')::double precision
                   ) AS duck_hive_ms,
                   coalesce(
                       nullif(pp->>'duck_vortex_ms', '')::double precision,
                       nullif(pp->'point'->>'duck_vortex_ms', '')::double precision
                   ) AS duck_vortex_ms,
                   coalesce(
                       nullif(pp->>'datafusion_ms', '')::double precision,
                       nullif(pp->'point'->>'datafusion_ms', '')::double precision
                   ) AS datafusion_ms,
                   coalesce(
                       nullif(pp->>'datafusion_hive_ms', '')::double precision,
                       nullif(pp->'point'->>'datafusion_hive_ms', '')::double precision
                   ) AS datafusion_hive_ms,
                   coalesce(
                       nullif(pp->>'datafusion_vortex_ms', '')::double precision,
                       nullif(pp->'point'->>'datafusion_vortex_ms', '')::double precision
                   ) AS datafusion_vortex_ms,
                   coalesce(
                       nullif(pp->>'gpu_gqe_ms', '')::double precision,
                       nullif(pp->'point'->>'gpu_gqe_ms', '')::double precision
                   ) AS gpu_gqe_ms,
                   coalesce(
                       nullif(pp->>'pg_ms', '')::double precision,
                       nullif(pp->'point'->>'pg_ms', '')::double precision
                   ) AS pg_ms,
                   coalesce(pp->'point', pp) AS point
            FROM jsonb_array_elements(coalesce({profile_lit}::jsonb->'profile_points', '[]'::jsonb)) AS pp
        ) AS imported_points
        WHERE shape_family <> ''
          AND table_rows > 0
          AND native_ms > 0
          AND duck_ms > 0
        "#
    ))?;
    let entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))?
    .unwrap_or(0);
    let points: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_points WHERE profile_name = {name_lit}"
    ))?
    .unwrap_or(0);
    Ok((entries, points))
}

fn ensure_profile_exists(profile_name: &str, caller: &str) {
    let name_lit = sql_lit(profile_name);
    let exists: bool = Spi::get_one(&format!(
        "SELECT EXISTS (SELECT 1 FROM rvbbit.route_profiles WHERE name = {name_lit})"
    ))
    .ok()
    .flatten()
    .unwrap_or(false);
    if !exists {
        pgrx::error!("rvbbit.{caller}: profile '{profile_name}' does not exist");
    }
}

fn parse_profile_list(value: &Value, caller: &str) -> Vec<String> {
    let Some(items) = value.as_array() else {
        pgrx::error!("rvbbit.{caller}: source_profiles must be a JSON string array");
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let Some(name) = item.as_str() else {
            pgrx::error!("rvbbit.{caller}: source_profiles must contain only strings");
        };
        let name = name.trim();
        if name.is_empty() {
            pgrx::error!("rvbbit.{caller}: source profile names must not be empty");
        }
        if !out.iter().any(|existing| existing == name) {
            out.push(name.to_string());
        }
    }
    out
}

fn profile_lifecycle_summary(profile_name: &str, action: &str) -> Value {
    let name_lit = sql_lit(profile_name);
    let active: bool = Spi::get_one(&format!(
        "SELECT coalesce((SELECT active FROM rvbbit.route_profiles WHERE name = {name_lit}), false)"
    ))
    .ok()
    .flatten()
    .unwrap_or(false);
    let entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let points: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_points WHERE profile_name = {name_lit}"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    json!({
        "action": action,
        "profile": profile_name,
        "active": active,
        "entries": entries,
        "points": points,
    })
}

fn clear_profile_entries(profile_name: &str, caller: &str) {
    let name_lit = sql_lit(profile_name);
    Spi::run(&format!(
        "DELETE FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
}

fn clear_profile_points(profile_name: &str, caller: &str) {
    let name_lit = sql_lit(profile_name);
    Spi::run(&format!(
        "DELETE FROM rvbbit.route_profile_points WHERE profile_name = {name_lit}"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
}

fn copy_profile_entries(target_profile: &str, source_profile: &str, caller: &str) {
    let target_lit = sql_lit(target_profile);
    let source_lit = sql_lit(source_profile);
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.route_profile_entries
            (profile_name, shape_key, choice, confidence, reason, observations,
             native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, entry)
        SELECT {target_lit}, shape_key, choice, confidence, reason, observations,
               native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, entry
        FROM rvbbit.route_profile_entries
        WHERE profile_name = {source_lit}
        ON CONFLICT (profile_name, shape_key) DO UPDATE SET
            choice = EXCLUDED.choice,
            confidence = EXCLUDED.confidence,
            reason = EXCLUDED.reason,
            observations = EXCLUDED.observations,
            native_ms = EXCLUDED.native_ms,
            native_vortex_ms = EXCLUDED.native_vortex_ms,
            duck_ms = EXCLUDED.duck_ms,
            duck_hive_ms = EXCLUDED.duck_hive_ms,
            duck_vortex_ms = EXCLUDED.duck_vortex_ms,
            datafusion_ms = EXCLUDED.datafusion_ms,
            datafusion_hive_ms = EXCLUDED.datafusion_hive_ms,
            datafusion_vortex_ms = EXCLUDED.datafusion_vortex_ms,
            gpu_gqe_ms = EXCLUDED.gpu_gqe_ms,
            pg_ms = EXCLUDED.pg_ms,
            entry = EXCLUDED.entry
        "#
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
}

fn copy_profile_points(target_profile: &str, source_profile: &str, caller: &str) {
    let target_lit = sql_lit(target_profile);
    let source_lit = sql_lit(source_profile);
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.route_profile_points
            (profile_name, shape_family, table_rows, native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, point)
        SELECT {target_lit}, shape_family, table_rows, native_ms, native_vortex_ms, duck_ms, duck_hive_ms, duck_vortex_ms, datafusion_ms, datafusion_hive_ms, datafusion_vortex_ms, gpu_gqe_ms, pg_ms, point
        FROM rvbbit.route_profile_points
        WHERE profile_name = {source_lit}
        "#
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
}

fn replace_profile_entries_from_source(target_profile: &str, source_profile: &str, caller: &str) {
    clear_profile_entries(target_profile, caller);
    copy_profile_entries(target_profile, source_profile, caller);
}

fn replace_profile_points_from_source(target_profile: &str, source_profile: &str, caller: &str) {
    clear_profile_points(target_profile, caller);
    copy_profile_points(target_profile, source_profile, caller);
}

fn refresh_profile_json_from_tables(
    profile_name: &str,
    metadata: Value,
    caller: &str,
) -> (i64, i64) {
    let name_lit = sql_lit(profile_name);
    let metadata_lit = sql_json_lit(&compact_profile_for_storage(&metadata));
    Spi::run(&format!(
        r#"
        UPDATE rvbbit.route_profiles rp
        SET profile = {metadata_lit}::jsonb || jsonb_build_object(
                'entries',
                coalesce((
                    SELECT jsonb_object_agg(shape_key, entry)
                    FROM rvbbit.route_profile_entries
                    WHERE profile_name = {name_lit}
                ), '{{}}'::jsonb),
                'entry_count',
                (SELECT count(*)::bigint
                 FROM rvbbit.route_profile_entries
                 WHERE profile_name = {name_lit}),
                'profile_point_count',
                (SELECT count(*)::bigint
                 FROM rvbbit.route_profile_points
                 WHERE profile_name = {name_lit})
            )
        WHERE rp.name = {name_lit}
        "#
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.{caller}: {e}"));
    let entries: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_entries WHERE profile_name = {name_lit}"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    let points: i64 = Spi::get_one(&format!(
        "SELECT count(*)::bigint FROM rvbbit.route_profile_points WHERE profile_name = {name_lit}"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    (entries, points)
}

fn sql_stringless(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len());
    let chars: Vec<char> = sql.chars().collect();
    let mut i = 0;
    let mut in_string = false;
    let mut in_line_comment = false;
    let mut in_block_comment = false;
    while i < chars.len() {
        let ch = chars[i];
        let next = chars.get(i + 1).copied().unwrap_or('\0');
        if in_line_comment {
            if ch == '\n' {
                in_line_comment = false;
                out.push(ch);
            } else {
                out.push(' ');
            }
            i += 1;
            continue;
        }
        if in_block_comment {
            if ch == '*' && next == '/' {
                in_block_comment = false;
                out.push_str("  ");
                i += 2;
            } else {
                out.push(' ');
                i += 1;
            }
            continue;
        }
        if in_string {
            if ch == '\'' {
                if next == '\'' {
                    out.push_str("  ");
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            out.push(' ');
            i += 1;
            continue;
        }
        if ch == '-' && next == '-' {
            in_line_comment = true;
            out.push_str("  ");
            i += 2;
        } else if ch == '/' && next == '*' {
            in_block_comment = true;
            out.push_str("  ");
            i += 2;
        } else if ch == '\'' {
            in_string = true;
            out.push(' ');
            i += 1;
        } else {
            out.push(ch);
            i += 1;
        }
    }
    out
}

fn normalize_sql(sql: &str) -> String {
    let lowered = sql.to_lowercase();
    let mut out = String::with_capacity(lowered.len());
    let mut prev_space = false;
    let mut chars = lowered.chars().peekable();
    while let Some(ch) = chars.next() {
        let mapped = if ch.is_ascii_digit() {
            while chars
                .peek()
                .is_some_and(|c| c.is_ascii_digit() || *c == '.')
            {
                chars.next();
            }
            '?'
        } else if ch.is_whitespace() {
            ' '
        } else {
            ch
        };
        if mapped == ' ' {
            if !prev_space {
                out.push(mapped);
                prev_space = true;
            }
        } else {
            out.push(mapped);
            prev_space = false;
        }
    }
    out.trim().trim_end_matches(';').to_string()
}

fn top_level_clause(sql: &str, keyword: &str, end_keywords: &[&str]) -> String {
    let Some(start) = find_top_level_keyword(sql, keyword) else {
        return String::new();
    };
    let mut depth = 0i32;
    let mut end = sql.len();
    let mut i = start + keyword.len();
    let bytes = sql.as_bytes();
    while i < sql.len() {
        match bytes[i] as char {
            '(' => depth += 1,
            ')' => depth = (depth - 1).max(0),
            _ => {
                if depth == 0 && end_keywords.iter().any(|k| keyword_at(sql, i, k)) {
                    end = i;
                    break;
                }
            }
        }
        i += 1;
    }
    sql[start + keyword.len()..end].trim().to_string()
}

fn find_top_level_keyword(sql: &str, keyword: &str) -> Option<usize> {
    let mut depth = 0i32;
    let bytes = sql.as_bytes();
    let mut i = 0;
    while i < sql.len() {
        match bytes[i] as char {
            '(' => depth += 1,
            ')' => depth = (depth - 1).max(0),
            _ => {
                if depth == 0 && keyword_at(sql, i, keyword) {
                    return Some(i);
                }
            }
        }
        i += 1;
    }
    None
}

fn keyword_at(sql: &str, idx: usize, keyword: &str) -> bool {
    if !sql[idx..].starts_with(keyword) {
        return false;
    }
    let before = if idx == 0 {
        ' '
    } else {
        sql.as_bytes()[idx - 1] as char
    };
    let after_idx = idx + keyword.len();
    let after = if after_idx >= sql.len() {
        ' '
    } else {
        sql.as_bytes()[after_idx] as char
    };
    !is_ident_char(before) && !is_ident_char(after)
}

fn split_top_level_commas(value: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut depth = 0i32;
    let mut start = 0usize;
    for (i, ch) in value.char_indices() {
        match ch {
            '(' => depth += 1,
            ')' => depth = (depth - 1).max(0),
            ',' if depth == 0 => {
                parts.push(value[start..i].to_string());
                start = i + 1;
            }
            _ => {}
        }
    }
    parts.push(value[start..].to_string());
    parts
}

fn count_distinct_signature(sql: &str) -> String {
    let Some(expr) = count_distinct_expr(sql) else {
        return "none".into();
    };
    if expr.is_empty() {
        "none".into()
    } else {
        hash_short(&expr)
    }
}

fn count_distinct_expr(sql: &str) -> Option<String> {
    let Some(pos) = sql.find("count") else {
        return None;
    };
    let tail = &sql[pos..];
    let Some(distinct) = tail.find("distinct") else {
        return None;
    };
    let expr = &tail[distinct + "distinct".len()..];
    let expr = expr.trim().trim_start_matches('(').trim();
    let end = expr.find(')').unwrap_or(expr.len());
    Some(expr[..end].trim().to_string())
}

fn expr_signature(expr: &str) -> String {
    let trimmed = expr.trim();
    if trimmed.is_empty() {
        "none".into()
    } else {
        let mut normalized = trimmed
            .to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        for marker in [" asc|", " desc|", " nulls first|", " nulls last|"] {
            normalized = normalized.replace(marker, " |");
        }
        for marker in [" asc", " desc", " nulls first", " nulls last"] {
            normalized = normalized.replace(marker, "");
        }
        normalized = normalized.split_whitespace().collect::<Vec<_>>().join(" ");
        if normalized.is_empty() {
            "none".into()
        } else {
            sha256_short(&normalized, 8)
        }
    }
}

fn clause_expr_signature(clause: &str) -> String {
    let clause = clause.trim().trim_end_matches(';');
    let exprs = split_top_level_commas(clause)
        .into_iter()
        .map(|expr| expr.trim().to_string())
        .filter(|expr| !expr.is_empty())
        .collect::<Vec<_>>();
    if exprs.is_empty() {
        "none".into()
    } else {
        expr_signature(&exprs.join("|"))
    }
}

fn fixed_contains_like_count(sql: &str) -> i64 {
    let lowered = sql.to_lowercase();
    let mut count = 0;
    let mut start = 0usize;
    while let Some(pos) = lowered[start..].find("like") {
        let abs = start + pos;
        if keyword_at(&lowered, abs, "like") && !previous_keyword_is(&lowered, abs, "not") {
            let after = lowered[abs + 4..].trim_start();
            if fixed_contains_like_literal(after) {
                count += 1;
            }
        }
        start = abs + 4;
    }
    count
}

fn fixed_contains_like_literal(after_like: &str) -> bool {
    let bytes = after_like.as_bytes();
    if bytes.len() < 4 || bytes[0] != b'\'' || bytes[1] != b'%' {
        return false;
    }
    let mut i = 2usize;
    let mut payload_chars = 0usize;
    while i < bytes.len() {
        match bytes[i] {
            b'\'' => {
                if i + 1 < bytes.len() && bytes[i + 1] == b'\'' {
                    payload_chars += 1;
                    i += 2;
                    continue;
                }
                return i > 2 && bytes[i - 1] == b'%' && payload_chars > 0;
            }
            b'%' => {
                if i + 1 < bytes.len() && bytes[i + 1] == b'\'' {
                    i += 1;
                    continue;
                }
                return false;
            }
            b'_' => return false,
            _ => {
                payload_chars += 1;
                i += 1;
            }
        }
    }
    false
}

fn previous_keyword_is(sql: &str, idx: usize, keyword: &str) -> bool {
    if idx == 0 {
        return false;
    }
    let before = &sql[..idx];
    let trimmed = before.trim_end();
    if trimmed.len() < keyword.len() {
        return false;
    }
    let start = trimmed.len() - keyword.len();
    trimmed[start..].eq_ignore_ascii_case(keyword)
        && (start == 0
            || trimmed
                .as_bytes()
                .get(start - 1)
                .is_none_or(|b| !is_ident_char(*b as char)))
}

fn limit_bucket(sql: &str) -> String {
    let Some(pos) = sql.find("limit") else {
        return "unknown".into();
    };
    let after = sql[pos + 5..].trim_start();
    let digits: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
    if let Ok(v) = digits.parse::<i64>() {
        bucket(v, &[1, 10, 100, 1000, 10000])
    } else {
        "unknown".into()
    }
}

fn max_plan_number(plan: &str, marker: &str) -> Option<i64> {
    let mut max_v = None;
    for part in plan.split(marker).skip(1) {
        let digits: String = part.chars().take_while(|c| c.is_ascii_digit()).collect();
        if let Ok(v) = digits.parse::<i64>() {
            max_v = Some(max_v.map_or(v, |m: i64| m.max(v)));
        }
    }
    max_v
}

fn function_scan_name(plan: &str) -> Option<String> {
    let marker = "Function Scan on ";
    let start = plan.find(marker)? + marker.len();
    let name: String = plan[start..]
        .chars()
        .take_while(|ch| ch.is_ascii_alphanumeric() || *ch == '_')
        .collect();
    if name.is_empty() {
        None
    } else {
        Some(name)
    }
}

fn contains_identifier(haystack: &str, ident: &str) -> bool {
    let mut start = 0usize;
    while let Some(pos) = haystack[start..].find(ident) {
        let abs = start + pos;
        let before = if abs == 0 {
            ' '
        } else {
            haystack.as_bytes()[abs - 1] as char
        };
        let after_idx = abs + ident.len();
        let after = if after_idx >= haystack.len() {
            ' '
        } else {
            haystack.as_bytes()[after_idx] as char
        };
        if !is_ident_char(before) && !is_ident_char(after) {
            return true;
        }
        start = after_idx;
    }
    false
}

fn plan_mentions_relation(plan: &str, schema: &str, relname: &str) -> bool {
    let qualified = format!("{schema}.{relname}");
    plan.lines().any(|line| {
        let line = line.trim();
        if line.contains("cte scan on ")
            || line.contains("subquery scan on ")
            || line.contains("function scan on ")
            || line.contains("values scan on ")
        {
            return false;
        }
        line.contains(&format!(" on {relname} "))
            || line.ends_with(&format!(" on {relname}"))
            || line.contains(&format!(" on {qualified} "))
            || line.ends_with(&format!(" on {qualified}"))
            || line.contains(&format!(" on \"{relname}\" "))
            || line.ends_with(&format!(" on \"{relname}\""))
            || line.contains(&format!(" on \"{schema}\".\"{relname}\" "))
            || line.ends_with(&format!(" on \"{schema}\".\"{relname}\""))
    })
}

fn sql_mentions_relation(sql: &str, schema: &str, relname: &str) -> bool {
    let tokens = sql_relation_tokens(sql);
    let mut in_from_clause = false;
    let mut expect_table = false;
    let mut i = 0usize;
    while i < tokens.len() {
        let token = tokens[i].as_str();
        if matches!(
            token,
            "where"
                | "group"
                | "order"
                | "having"
                | "limit"
                | "offset"
                | "union"
                | "except"
                | "intersect"
                | "on"
        ) {
            in_from_clause = false;
            expect_table = false;
        }
        if token == "from" || token == "join" {
            in_from_clause = true;
            expect_table = true;
            i += 1;
            continue;
        }
        if token == "," && in_from_clause {
            expect_table = true;
            i += 1;
            continue;
        }
        if expect_table {
            if matches!(token, "lateral" | "only") {
                i += 1;
                continue;
            }
            if token == "(" {
                expect_table = false;
                i += 1;
                continue;
            }
            if token == schema
                && tokens.get(i + 1).is_some_and(|t| t == ".")
                && tokens.get(i + 2).is_some_and(|t| t == relname)
            {
                return true;
            }
            if token == relname {
                return true;
            }
            expect_table = false;
        }
        i += 1;
    }
    false
}

fn sql_relation_tokens(sql: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    for ch in sql.chars() {
        if is_ident_char(ch) {
            current.push(ch.to_ascii_lowercase());
            continue;
        }
        if !current.is_empty() {
            tokens.push(std::mem::take(&mut current));
        }
        if matches!(ch, '.' | ',' | '(' | ')') {
            tokens.push(ch.to_string());
        }
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
}

fn has_word(s: &str, word: &str) -> bool {
    contains_identifier(s, word)
}

fn count_word(s: &str, word: &str) -> i64 {
    let mut count = 0;
    let mut start = 0usize;
    while let Some(pos) = s[start..].find(word) {
        let abs = start + pos;
        let before = if abs == 0 {
            ' '
        } else {
            s.as_bytes()[abs - 1] as char
        };
        let after_idx = abs + word.len();
        let after = if after_idx >= s.len() {
            ' '
        } else {
            s.as_bytes()[after_idx] as char
        };
        if !is_ident_char(before) && !is_ident_char(after) {
            count += 1;
        }
        start = after_idx;
    }
    count
}

fn count_word_fn(s: &str, word: &str) -> i64 {
    let needle = format!("{word}(");
    let needle_spaced = format!("{word} (");
    count_substr(s, &needle) + count_substr(s, &needle_spaced)
}

fn count_substr(s: &str, needle: &str) -> i64 {
    s.matches(needle).count() as i64
}

fn is_ident_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_' || ch == '$' || ch == '"'
}

fn hash_short(value: &str) -> String {
    sha256_short(value, 16)
}

fn sha256_short(value: &str, len: usize) -> String {
    let digest = Sha256::digest(value.as_bytes());
    let mut out = String::with_capacity(len);
    for byte in digest {
        if out.len() >= len {
            break;
        }
        out.push_str(&format!("{byte:02x}"));
    }
    out.truncate(len);
    out
}

fn bucket(value: i64, cuts: &[i64]) -> String {
    for cut in cuts {
        if value <= *cut {
            return format!("<={cut}");
        }
    }
    format!(">{}", cuts.last().copied().unwrap_or(0))
}

fn metric_bucket(value: i64) -> String {
    if value <= 0 {
        return "unknown".into();
    }
    bucket(
        value,
        &[10_000, 100_000, 1_000_000, 10_000_000, 100_000_000],
    )
}

fn shape_family_key(key: &str) -> String {
    key.split('|')
        .filter(|part| !part.starts_with("table_rows="))
        .collect::<Vec<_>>()
        .join("|")
}

fn canonical_shape_key(key: &str, features: Option<&Value>) -> String {
    if key.starts_with("native_cap=") {
        return key.to_string();
    }
    let Some(rest) = key.strip_prefix("native=") else {
        return key.to_string();
    };
    let native_value = rest.split('|').next().unwrap_or("none");
    let has_native = features
        .and_then(|f| f.get("has_native_function"))
        .and_then(Value::as_bool)
        .unwrap_or_else(|| native_value != "none" && !native_value.is_empty());
    format!("native_cap={}", has_native as i32)
        + rest.find('|').map(|idx| &rest[idx..]).unwrap_or_default()
}

fn median_f64(mut values: Vec<f64>) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(Ordering::Equal));
    let mid = values.len() / 2;
    if values.len() % 2 == 1 {
        values[mid]
    } else {
        (values[mid - 1] + values[mid]) / 2.0
    }
}

fn median_option(values: Vec<f64>) -> Option<f64> {
    (!values.is_empty()).then(|| median_f64(values))
}

fn interpolate_predictions(
    lower: RouteCurveSample,
    upper: RouteCurveSample,
    position: f64,
) -> Vec<(Candidate, f64)> {
    [
        (Candidate::RvbbitNative, lower.native_ms, upper.native_ms),
        (
            Candidate::RvbbitNativeVortex,
            lower.native_vortex_ms,
            upper.native_vortex_ms,
        ),
        (Candidate::DuckVector, lower.duck_ms, upper.duck_ms),
        (Candidate::DuckHive, lower.duck_hive_ms, upper.duck_hive_ms),
        (
            Candidate::DuckVortex,
            lower.duck_vortex_ms,
            upper.duck_vortex_ms,
        ),
        (
            Candidate::DataFusionVector,
            lower.datafusion_ms,
            upper.datafusion_ms,
        ),
        (
            Candidate::DataFusionHive,
            lower.datafusion_hive_ms,
            upper.datafusion_hive_ms,
        ),
        (
            Candidate::DataFusionVortex,
            lower.datafusion_vortex_ms,
            upper.datafusion_vortex_ms,
        ),
        (Candidate::GpuGqe, lower.gpu_gqe_ms, upper.gpu_gqe_ms),
        (Candidate::PgRowstore, lower.pg_ms, upper.pg_ms),
    ]
    .into_iter()
    .filter_map(|(candidate, lower_ms, upper_ms)| {
        Some((candidate, lower_ms? + position * (upper_ms? - lower_ms?)))
    })
    .collect()
}

fn fastest_routable_prediction(
    predictions: &[(Candidate, f64)],
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<(Candidate, f64, f64)> {
    let mut values = predictions
        .iter()
        .copied()
        .filter(|(candidate, ms)| {
            *ms > 0.0 && candidate_availability(*candidate, features, tables).0
        })
        .collect::<Vec<_>>();
    values.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(Ordering::Equal));
    if values.len() < 2 {
        return None;
    }
    let (candidate, best_ms, second_ms) = (values[0].0, values[0].1, values[1].1);
    let confidence = if second_ms > 0.0 {
        (1.0 - best_ms / second_ms).clamp(0.0, 1.0)
    } else {
        0.0
    };
    if candidate == Candidate::PgRowstore && confidence < min_confidence_for_candidate(candidate) {
        values.retain(|(candidate, _)| *candidate != Candidate::PgRowstore);
        if values.len() < 2 {
            return None;
        }
        return Some((values[0].0, values[0].1, values[1].1));
    }
    Some((candidate, best_ms, second_ms))
}

fn predicted_ms(predictions: &[(Candidate, f64)], candidate: Candidate) -> Option<f64> {
    predictions
        .iter()
        .find_map(|(c, ms)| (*c == candidate).then_some(*ms))
}

fn ratio_text_many(candidate: Candidate, best_ms: f64, second_ms: f64) -> String {
    let ratio = if best_ms > 0.0 {
        second_ms / best_ms
    } else {
        0.0
    };
    format!(
        "{} {:.2}x faster than next candidate",
        candidate.route(),
        ratio
    )
}

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

fn sql_nullable_text(s: &str) -> String {
    if s.is_empty() {
        "NULL".to_string()
    } else {
        sql_lit(s)
    }
}

fn sql_nullable_i64(value: Option<i64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NULL".to_string())
}

fn sql_nullable_f64(value: Option<f64>) -> String {
    value
        .filter(|v| v.is_finite())
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NULL".to_string())
}

fn sql_text_array_lit(items: &[String]) -> String {
    let values = items
        .iter()
        .map(|item| sql_lit(item))
        .collect::<Vec<_>>()
        .join(", ");
    format!("ARRAY[{values}]::text[]")
}

fn sql_json_lit(v: &Value) -> String {
    sql_lit(&v.to_string())
}

#[cfg(test)]
mod route_unit_tests {
    use super::*;

    fn test_table(rows: i64) -> RvbbitTableMetric {
        RvbbitTableMetric {
            schema: "public".to_string(),
            relname: "hits".to_string(),
            oid: 1,
            row_groups: 1,
            rows,
            bytes: rows.saturating_mul(100),
            heap_bytes: 0,
            shadow_heap_retained: true,
            shadow_heap_dirty: false,
            native_overlay_readable: false,
            delete_count: 0,
            text_columns: Vec::new(),
            temporal_columns: Vec::new(),
            date_columns: Vec::new(),
            timestamp_columns: Vec::new(),
            denied_engines: Vec::new(),
            denied_layouts: Vec::new(),
        }
    }

    #[test]
    fn table_policy_denies_engine_and_layout() {
        let mut t = test_table(1_000_000);
        t.denied_engines = vec!["duck".to_string()];
        t.denied_layouts = vec!["vortex".to_string()];
        let tables = [t];
        // duck_* gated by the engine deny
        assert!(candidate_denied_by_table_policy(Candidate::DuckVector, &tables).is_some());
        assert!(candidate_denied_by_table_policy(Candidate::DuckVortex, &tables).is_some());
        // datafusion_vortex gated by the layout deny (engine is fine)
        assert!(candidate_denied_by_table_policy(Candidate::DataFusionVortex, &tables).is_some());
        // datafusion_vector survives (datafusion engine ok, vector layout ok)
        assert!(candidate_denied_by_table_policy(Candidate::DataFusionVector, &tables).is_none());
        // the correctness floor is never gated
        assert!(candidate_denied_by_table_policy(Candidate::RvbbitNative, &tables).is_none());
        assert!(candidate_denied_by_table_policy(Candidate::PgRowstore, &tables).is_none());
    }

    #[test]
    fn table_policy_multi_table_is_most_restrictive() {
        let a = test_table(1_000_000);
        let mut b = test_table(1_000_000);
        b.relname = "b".to_string();
        b.denied_layouts = vec!["vortex".to_string()];
        let tables = [a, b];
        // b vetoes vortex even though a allows it
        assert!(candidate_denied_by_table_policy(Candidate::DataFusionVortex, &tables).is_some());
        // vector is allowed by both
        assert!(candidate_denied_by_table_policy(Candidate::DataFusionVector, &tables).is_none());
    }

    fn test_features(sql: &str, rows: i64) -> RouteFeatures {
        build_features(sql, None, &[test_table(rows)])
    }

    fn test_features_with_text(sql: &str, rows: i64, text_columns: &[&str]) -> RouteFeatures {
        let mut table = test_table(rows);
        table.text_columns = text_columns.iter().map(|col| col.to_string()).collect();
        build_features(sql, None, &[table])
    }

    #[test]
    fn gpu_gqe_shape_gate_rejects_known_risky_shapes() {
        let mut lineitem = test_table(1_000_000);
        lineitem.relname = "lineitem".to_string();
        let tables = [test_table(1_000_000), lineitem];

        let multi_star = build_features(
            "SELECT * FROM hits h JOIN lineitem l ON h.id = l.id",
            None,
            &tables,
        );
        assert!(
            gpu_gqe_unsupported_shape_reason_inner(&multi_star, &tables, false)
                .is_some_and(|reason| reason.contains("multiple tables"))
        );

        let qualified_star = test_features("SELECT h.* FROM hits h", 1_000_000);
        assert!(gpu_gqe_unsupported_shape_reason_inner(
            &qualified_star,
            &[test_table(1_000_000)],
            false
        )
        .is_some_and(|reason| reason.contains("qualified SELECT *")));

        let schema_qualified = test_features("SELECT count(*) FROM public.hits", 1_000_000);
        assert!(gpu_gqe_unsupported_shape_reason_inner(
            &schema_qualified,
            &[test_table(1_000_000)],
            false
        )
        .is_some_and(|reason| reason.contains("schema-qualified")));

        let q23_like = test_features(
            r#"SELECT * FROM hits WHERE "URL" LIKE '%google%' ORDER BY "EventTime" LIMIT 10"#,
            1_000_000,
        );
        assert!(
            gpu_gqe_unsupported_shape_reason_inner(&q23_like, &[test_table(1_000_000)], false)
                .is_some_and(|reason| reason.contains("wide SELECT *"))
        );

        let simple_join = build_features(
            "SELECT h.id, l.id FROM hits h JOIN lineitem l ON h.id = l.id",
            None,
            &tables,
        );
        assert!(gpu_gqe_unsupported_shape_reason_inner(&simple_join, &tables, false).is_none());
        assert!(gpu_gqe_unsupported_shape_reason_inner(&multi_star, &tables, true).is_none());
    }

    #[test]
    fn route_rejects_non_select() {
        assert!(safe_select("delete from t").is_err());
    }

    #[test]
    fn route_normalizes_numbers() {
        assert_eq!(normalize_sql("SELECT 123 FROM t"), "select ? from t");
        assert_eq!(hash_short("select ? from t"), "ed97449ab5339f2e");
    }

    #[test]
    fn route_uses_python_compatible_clause_signatures() {
        assert_eq!(expr_signature("a, b"), "4a479db6");
        assert_eq!(expr_signature("a DESC NULLS LAST"), expr_signature("a"));
        assert_eq!(clause_expr_signature("a, b"), "0eab8a0a");
        assert_eq!(
            clause_expr_signature("custdist desc, c_count desc"),
            "579dbcc1"
        );
    }

    #[test]
    fn route_counts_only_positive_fixed_contains_like() {
        assert_eq!(fixed_contains_like_count("x LIKE '%google%'"), 1);
        assert_eq!(
            fixed_contains_like_count("x NOT LIKE '%special%requests%'"),
            0
        );
        assert_eq!(fixed_contains_like_count("x LIKE '%foo_bar%'"), 0);
    }

    #[test]
    fn route_keeps_simple_metadata_aggregates_native() {
        let features = test_features(
            r#"SELECT SUM("AdvEngineID"), COUNT(*), AVG("ResolutionWidth") FROM hits"#,
            2_000_000,
        );
        assert!(simple_metadata_aggregate_should_stay_native(&features));

        let distinct_features =
            test_features(r#"SELECT COUNT(DISTINCT "UserID") FROM hits"#, 2_000_000);
        assert!(!simple_metadata_aggregate_should_stay_native(
            &distinct_features
        ));
    }

    #[test]
    fn route_no_profile_splits_native_and_datafusion_shapes() {
        let mut native_rewrite = test_features(
            r#"SELECT "URL", COUNT(*) AS c FROM hits GROUP BY "URL" ORDER BY c DESC LIMIT 10"#,
            500_000,
        );
        native_rewrite.has_native_function = true;
        native_rewrite.native_function = Some("top_count_1col".to_string());
        assert!(no_profile_native_reason(&native_rewrite)
            .is_some_and(|reason| reason.contains("native PostgreSQL plan rewrite")));

        let analytical = test_features(
            r#"SELECT "RegionID", COUNT(DISTINCT "UserID") AS u FROM hits GROUP BY "RegionID" ORDER BY u DESC LIMIT 10"#,
            2_000_000,
        );
        assert!(no_profile_native_reason(&analytical).is_none());
        assert!(no_profile_prefers_datafusion(&analytical));

        let row_returning = test_features(
            r#"SELECT "UserID" FROM hits WHERE "UserID" = 435090932899640449"#,
            2_000_000,
        );
        assert!(no_profile_native_reason(&row_returning)
            .is_some_and(|reason| reason.contains("row-returning query")));
    }

    #[test]
    fn route_no_profile_keeps_filtered_counts_native() {
        let features = test_features(
            r#"SELECT COUNT(*) FROM hits WHERE "AdvEngineID" <> 0"#,
            5_000_000,
        );

        assert!(filtered_count_should_stay_native(&features));
        assert!(no_profile_native_reason(&features)
            .is_some_and(|reason| reason.contains("filtered count metadata")));
        assert!(!fallback_prefers_external_analytical_shape(&features));
    }

    #[test]
    fn route_no_profile_keeps_metadata_group_counts_native() {
        let mut features = test_features(
            r#"SELECT "AdvEngineID", COUNT(*) FROM hits WHERE "AdvEngineID" <> 0 GROUP BY "AdvEngineID" ORDER BY COUNT(*) DESC"#,
            5_000_000,
        );
        features.has_native_function = true;
        features.native_function = Some("agg_groupby_count".to_string());

        assert!(native_function_should_stay_native(&features));
        assert!(!fallback_prefers_external_analytical_shape(&features));
        assert!(no_profile_native_reason(&features)
            .is_some_and(|reason| reason.contains("native PostgreSQL plan rewrite")));
    }

    #[test]
    fn route_no_profile_keeps_small_simple_analytics_native() {
        let features = test_features(
            r#"SELECT "AdvEngineID", COUNT(*) FROM hits WHERE "AdvEngineID" <> 0 GROUP BY "AdvEngineID" ORDER BY COUNT(*) DESC"#,
            100_000,
        );
        assert!(no_profile_native_reason(&features)
            .is_some_and(|reason| reason.contains("small/simple analytical table")));
        assert!(!fallback_prefers_external_analytical_shape(&features));
    }

    #[test]
    fn route_no_profile_lets_text_count_distinct_escape_small_native() {
        let features = test_features_with_text(
            r#"SELECT COUNT(DISTINCT "SearchPhrase") FROM hits"#,
            50_000,
            &["searchphrase"],
        );

        assert!(no_profile_native_reason(&features).is_none());
        assert!(fallback_prefers_external_analytical_shape(&features));
        assert!(!fallback_prefers_variant(&features));
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DataFusionVector)
        );
    }

    #[test]
    fn route_no_profile_promotes_large_text_shapes_to_variants() {
        let distinct_text = test_features_with_text(
            r#"SELECT COUNT(DISTINCT "SearchPhrase") FROM hits"#,
            500_000,
            &["searchphrase"],
        );
        assert!(fallback_prefers_variant(&distinct_text));
        assert!(single_table_text_distinct_prefers_vector(&distinct_text));
        assert_eq!(
            fallback_external_candidate_order(&distinct_text).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );

        let text_topk = test_features_with_text(
            r#"SELECT "UserID", "SearchPhrase", COUNT(*) FROM hits GROUP BY "UserID", "SearchPhrase" ORDER BY COUNT(*) DESC LIMIT 10"#,
            500_000,
            &["searchphrase"],
        );
        assert!(fallback_prefers_variant(&text_topk));
        assert_eq!(
            fallback_external_candidate_order(&text_topk).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );

        let single_text_topk = test_features_with_text(
            r#"SELECT "SearchPhrase", COUNT(*) AS c FROM hits WHERE "SearchPhrase" <> '' GROUP BY "SearchPhrase" ORDER BY c DESC LIMIT 10"#,
            5_000_000,
            &["searchphrase"],
        );
        assert!(!fallback_prefers_external_analytical_shape(
            &single_text_topk
        ));
        assert!(!fallback_prefers_variant(&single_text_topk));
    }

    #[test]
    fn route_no_profile_sends_large_native_topk_rewrites_to_vortex() {
        let mut text_topk = test_features_with_text(
            r#"SELECT "URL", COUNT(*) AS c FROM hits GROUP BY "URL" ORDER BY c DESC LIMIT 10"#,
            5_000_000,
            &["url"],
        );
        text_topk.has_native_function = true;
        text_topk.native_function = Some("top_count_1col".to_string());

        assert!(native_function_prefers_vector_external(&text_topk));
        assert!(fallback_prefers_external_analytical_shape(&text_topk));
        assert!(no_profile_native_reason(&text_topk).is_none());
        assert_eq!(
            fallback_external_candidate_order(&text_topk).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );

        let mut mid_sized_distinct = test_features_with_text(
            r#"SELECT "MobilePhoneModel", COUNT(DISTINCT "UserID") AS u FROM hits WHERE "MobilePhoneModel" <> '' GROUP BY "MobilePhoneModel" ORDER BY u DESC LIMIT 10"#,
            1_000_000,
            &["mobilephonemodel"],
        );
        mid_sized_distinct.has_native_function = true;
        mid_sized_distinct.native_function = Some("top_count_distinct_1col".to_string());
        assert!(native_function_should_stay_native(&mid_sized_distinct));

        let mut large_distinct = mid_sized_distinct.clone();
        large_distinct.table_rows = 5_000_000;
        assert!(native_function_prefers_vector_external(&large_distinct));
        assert_eq!(
            fallback_external_candidate_order(&large_distinct).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );

        let mut rollup_distinct = test_features(
            r#"SELECT "RegionID", SUM("AdvEngineID"), COUNT(*) AS c,
                      AVG("ResolutionWidth"), COUNT(DISTINCT "UserID")
               FROM hits GROUP BY "RegionID" ORDER BY c DESC LIMIT 10"#,
            5_000_000,
        );
        rollup_distinct.has_native_function = true;
        rollup_distinct.native_function = Some("top_rollup_1int_distinct".to_string());
        assert!(!native_function_should_stay_native(&rollup_distinct));
        assert!(no_profile_native_reason(&rollup_distinct).is_none());
        assert_eq!(
            fallback_external_candidate_order(&rollup_distinct).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );

        let mut any_int_text = test_features_with_text(
            r#"SELECT "UserID", "SearchPhrase", COUNT(*)
               FROM hits GROUP BY "UserID", "SearchPhrase" LIMIT 10"#,
            5_000_000,
            &["searchphrase"],
        );
        any_int_text.has_native_function = true;
        any_int_text.native_function = Some("any_count_int_text".to_string());
        assert!(!native_function_should_stay_native(&any_int_text));
        assert!(no_profile_native_reason(&any_int_text).is_none());
        assert_eq!(
            fallback_external_candidate_order(&any_int_text).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );

        let mut filtered_topk = test_features_with_text(
            r#"SELECT "URL", COUNT(*) AS pageviews FROM hits
               WHERE "CounterID" = 62
                 AND "EventDate" >= '2013-07-01'
                 AND "EventDate" <= '2013-07-31'
                 AND "DontCountHits" = 0
                 AND "IsRefresh" = 0
                 AND "URL" <> ''
               GROUP BY "URL" ORDER BY pageviews DESC LIMIT 10"#,
            5_000_000,
            &["url"],
        );
        filtered_topk.has_native_function = true;
        filtered_topk.native_function = Some("top_count_filtered".to_string());
        assert!(selective_single_table_topk_should_stay_native(
            &filtered_topk
        ));
        assert!(!native_function_should_stay_native(&filtered_topk));
        assert!(no_profile_native_reason(&filtered_topk).is_none());
        assert_eq!(
            fallback_external_candidate_order(&filtered_topk).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );
    }

    #[test]
    fn route_no_profile_sends_large_time_bucket_to_vortex() {
        let features = test_features(
            r#"SELECT DATE_TRUNC('minute', "EventTime") AS m, COUNT(*) FROM hits GROUP BY DATE_TRUNC('minute', "EventTime") ORDER BY DATE_TRUNC('minute', "EventTime") LIMIT 10 OFFSET 1000"#,
            1_000_000,
        );
        assert!(fallback_prefers_external_analytical_shape(&features));
        assert!(!fallback_prefers_variant(&features));
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );
    }

    #[test]
    fn route_no_profile_sends_large_wide_grouped_aggregate_to_vortex() {
        let mut features = test_features(
            r#"SELECT
                 "EventDate",
                 "AdvEngineID",
                 SUM("ResolutionWidth"),
                 SUM("ResolutionHeight"),
                 AVG("UserID"),
                 COUNT(*)
               FROM hits
               WHERE "EventDate" <= '2013-07-31'
               GROUP BY "EventDate", "AdvEngineID"
               ORDER BY "EventDate", "AdvEngineID""#,
            5_000_000,
        );
        features.has_native_function = true;
        features.native_function = Some("top_count_1col".to_string());

        assert!(large_wide_grouped_aggregate_prefers_vector(&features));
        assert!(fallback_prefers_external_analytical_shape(&features));
        assert!(no_profile_native_reason(&features).is_none());
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );
    }

    #[test]
    fn gpu_gqe_prior_shape_screens_large_analytical_only() {
        let min = 1_000_000;
        // Large grouped aggregate -> applies.
        let grouped = test_features(
            "SELECT \"AdvEngineID\", count(*) FROM hits GROUP BY \"AdvEngineID\"",
            5_000_000,
        );
        assert!(gpu_gqe_prior_shape_applies(&grouped, min));
        // Large bare aggregate (no group) -> applies.
        let agg = test_features("SELECT count(*) FROM hits", 5_000_000);
        assert!(gpu_gqe_prior_shape_applies(&agg, min));
        // Small grouped aggregate -> below the row floor, does NOT apply.
        let small = test_features(
            "SELECT \"AdvEngineID\", count(*) FROM hits GROUP BY \"AdvEngineID\"",
            10_000,
        );
        assert!(!gpu_gqe_prior_shape_applies(&small, min));
        // Large non-analytical scan (no aggregate, no group) -> does NOT apply.
        let scan = test_features("SELECT \"URL\" FROM hits WHERE \"UserID\" = 42", 5_000_000);
        assert!(!gpu_gqe_prior_shape_applies(&scan, min));
    }

    #[test]
    fn ml_feature_value_and_engine_mapping() {
        let f = test_features(
            "SELECT \"AdvEngineID\", count(*) FROM hits GROUP BY \"AdvEngineID\"",
            2_000_000,
        );
        assert_eq!(feature_value(&f, "group_by"), 1.0);
        assert!(feature_value(&f, "aggregate_count") >= 1.0);
        assert!((feature_value(&f, "ln_table_rows") - (2_000_000f64 + 1.0).ln()).abs() < 1e-9);
        assert_eq!(feature_value(&f, "select_star"), 0.0);
        assert_eq!(feature_value(&f, "unknown_feature_xyz"), 0.0);
        assert_eq!(engine_name_to_candidate("gpu_gqe"), Some(Candidate::GpuGqe));
        assert_eq!(
            engine_name_to_candidate("duck_vortex"),
            Some(Candidate::DuckVortex)
        );
        assert_eq!(engine_name_to_candidate("pg"), Some(Candidate::PgRowstore));
        assert_eq!(engine_name_to_candidate("nope"), None);
    }

    #[test]
    fn route_table_parser_does_not_treat_alias_as_table() {
        let sql = "WITH store_v AS (SELECT 1) \
                   SELECT * FROM store_v store \
                   JOIN store_sales ss ON ss.id = store.id";

        assert!(!sql_mentions_relation(sql, "public", "store"));
        assert!(sql_mentions_relation(sql, "public", "store_sales"));
        assert!(sql_mentions_relation(
            "SELECT * FROM public.hits h WHERE h.id = 1",
            "public",
            "hits"
        ));
    }

    #[test]
    fn route_no_profile_prefers_vortex_for_complex_large_join_shapes() {
        let features = test_features(
            "SELECT COUNT(*) FROM hits h1 \
             JOIN hits h2 ON h1.id = h2.id \
             JOIN hits h3 ON h1.id = h3.id \
             JOIN hits h4 ON h1.id = h4.id \
             JOIN hits h5 ON h1.id = h5.id",
            2_000_000,
        );

        assert!(complex_analytical_shape(&features));
        assert!(fallback_prefers_complex_duck_hive(&features));
        assert!(fallback_prefers_duck_vector(&features));
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );
    }

    #[test]
    fn route_no_profile_demotes_native_for_complex_plan_rewrites() {
        let mut features = test_features(
            "WITH x AS (SELECT h1.id, count(*) c FROM hits h1 \
             JOIN hits h2 ON h1.id = h2.id GROUP BY h1.id) \
             SELECT id FROM x ORDER BY c DESC LIMIT 100",
            2_000_000,
        );
        features.has_native_function = true;
        features.native_function = Some("top_count_1col".to_string());
        features.plan_has_join = true;
        features.plan_has_subplan = true;

        assert!(complex_analytical_shape(&features));
        assert!(fallback_prefers_external_analytical_shape(&features));
        assert!(no_profile_native_reason(&features).is_none());
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );
    }

    #[test]
    fn route_no_profile_keeps_selective_single_table_topk_native() {
        let mut features = test_features_with_text(
            r#"SELECT "Title", COUNT(*) AS pageviews FROM hits
               WHERE "CounterID" = 62
                 AND "EventDate" >= '2013-07-01'
                 AND "EventDate" <= '2013-07-31'
                 AND "DontCountHits" = 0
                 AND "IsRefresh" = 0
                 AND "Title" <> ''
               GROUP BY "Title" ORDER BY pageviews DESC LIMIT 10"#,
            5_000_000,
            &["title"],
        );
        features.has_native_function = true;
        features.native_function = Some("top_count_1col".to_string());

        assert!(selective_single_table_topk_should_stay_native(&features));
        assert!(!fallback_prefers_external_analytical_shape(&features));
        assert!(no_profile_native_reason(&features)
            .is_some_and(|reason| reason.contains("selective single-table top-k")));
    }

    #[test]
    fn route_no_profile_avoids_duck_hive_for_known_unsupported_alias_shape() {
        let mut features = test_features(
            "SELECT ratio FROM \
             (SELECT count(*) AS amc FROM hits WHERE id BETWEEN 1 AND 2) at_, \
             (SELECT count(*) AS pmc FROM hits WHERE id BETWEEN 3 AND 4) pt \
             ORDER BY ratio LIMIT 100",
            2_000_000,
        );
        features.from_count = 2;

        assert!(complex_analytical_shape(&features));
        assert!(duck_hive_known_unsupported(&features));
        assert!(!fallback_prefers_complex_duck_hive(&features));
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DuckVortex)
        );
    }
}
