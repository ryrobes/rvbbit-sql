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
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
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
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
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
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
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
    duck_ms       double precision,
    duck_hive_ms  double precision,
    datafusion_ms double precision,
    datafusion_hive_ms double precision,
    pg_ms         double precision,
    entry         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_name, shape_key),
    CHECK (choice IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
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
    duck_ms       double precision NOT NULL,
    duck_hive_ms  double precision,
    datafusion_ms double precision,
    datafusion_hive_ms double precision,
    pg_ms         double precision,
    point         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (table_rows >= 0),
    CHECK (native_ms > 0),
    CHECK (duck_ms > 0),
    CHECK (duck_hive_ms IS NULL OR duck_hive_ms > 0),
    CHECK (datafusion_ms IS NULL OR datafusion_ms > 0),
    CHECK (datafusion_hive_ms IS NULL OR datafusion_hive_ms > 0),
    CHECK (pg_ms IS NULL OR pg_ms > 0)
);

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS pg_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

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
    pe.datafusion_ms,
    pe.datafusion_hive_ms,
    pe.pg_ms
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
    DataFusionMem,
    DataFusionVector,
    DataFusionHive,
    RvbbitNative,
    PgRowstore,
}

impl Candidate {
    fn all() -> [Self; 7] {
        [
            Candidate::DuckVector,
            Candidate::DuckHive,
            Candidate::DataFusionMem,
            Candidate::DataFusionVector,
            Candidate::DataFusionHive,
            Candidate::RvbbitNative,
            Candidate::PgRowstore,
        ]
    }

    fn as_str(self) -> &'static str {
        match self {
            Candidate::DuckVector => "duck_vector",
            Candidate::DuckHive => "duck_hive",
            Candidate::DataFusionMem => "datafusion_mem",
            Candidate::DataFusionVector => "datafusion_vector",
            Candidate::DataFusionHive => "datafusion_hive",
            Candidate::RvbbitNative => "rvbbit_native",
            Candidate::PgRowstore => "pg_rowstore",
        }
    }

    fn route(self) -> &'static str {
        match self {
            Candidate::DuckVector => "duck",
            Candidate::DuckHive => "duck_hive",
            Candidate::DataFusionMem => "datafusion_mem",
            Candidate::DataFusionVector => "datafusion",
            Candidate::DataFusionHive => "datafusion_hive",
            Candidate::RvbbitNative => "native",
            Candidate::PgRowstore => "postgres_rowstore",
        }
    }

    fn from_str(s: &str) -> Option<Self> {
        match s {
            "duck_vector" | "duck" => Some(Candidate::DuckVector),
            "duck_hive" | "duck-hive" => Some(Candidate::DuckHive),
            "datafusion_mem" | "datafusion-memory" | "df_mem" => Some(Candidate::DataFusionMem),
            "datafusion_vector" | "datafusion" | "df" => Some(Candidate::DataFusionVector),
            "datafusion_hive" | "datafusion-hive" | "df_hive" => Some(Candidate::DataFusionHive),
            "rvbbit_native" | "native" => Some(Candidate::RvbbitNative),
            "pg_rowstore" | "postgres_rowstore" => Some(Candidate::PgRowstore),
            _ => None,
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
    delete_count: i64,
    text_columns: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
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
    duck_ms: Option<f64>,
    duck_hive_ms: Option<f64>,
    datafusion_ms: Option<f64>,
    datafusion_hive_ms: Option<f64>,
    pg_ms: Option<f64>,
}

#[derive(Default)]
struct CandidateBuckets {
    native: Vec<f64>,
    duck: Vec<f64>,
    duck_hive: Vec<f64>,
    datafusion: Vec<f64>,
    datafusion_hive: Vec<f64>,
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
            self.duck_ms,
            self.duck_hive_ms,
            self.datafusion_ms,
            self.datafusion_hive_ms,
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
        "datafusion_mem_entries": datafusion_mem_entries,
        "datafusion_entries": datafusion_entries,
        "datafusion_hive_entries": datafusion_hive_entries,
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

fn choose_route_fast(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
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
    if simple_metadata_aggregate_should_stay_native(features) {
        return Some(decision(
            Candidate::RvbbitNative,
            "hard-rule-fast",
            "native simple aggregate metadata",
            None,
            None,
        ));
    }
    if features.min_count > 0 && features.max_count > 0 && !features.where_present {
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
    choose_from_fast_profile_entry(features, tables, profile)
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
    if simple_metadata_aggregate_should_stay_native(features) {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native simple aggregate metadata",
            None,
            None,
        );
    }
    if features.min_count > 0 && features.max_count > 0 && !features.where_present {
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
    {
        return decision(
            Candidate::RvbbitNative,
            "hard-rule",
            "native expression-key top count",
            None,
            None,
        );
    }

    if let Some(decision) = choose_from_active_profile(features, tables, profile) {
        return decision;
    }

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

    if let Some(reason) = fallback_native_reason(features) {
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
            let reason = if matches!(candidate, Candidate::DataFusionHive | Candidate::DuckHive) {
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

fn choose_from_active_profile(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
) -> Option<RouteDecision> {
    if !relations_present(&["rvbbit.route_profiles"]) {
        return None;
    }
    profile.effective.as_ref()?;
    if let Some(decision) = choose_from_active_profile_entry(features, tables, profile) {
        return Some(decision);
    }
    if let Some(curve) = choose_from_profile_points(features, tables, profile) {
        return Some(curve);
    }
    if let Some(profile_json) = selected_profile(profile) {
        if let Some(curve) = choose_from_profile_curve(&profile_json, features, tables) {
            return Some(curve);
        }
        if let Some(decision) = choose_from_profile_json_entry(&profile_json, features, tables) {
            return Some(decision);
        }
    }
    if profile.requested.is_some() {
        return None;
    }
    choose_from_observation_curve(features, tables)
}

fn choose_from_profile_json_entry(
    profile: &Value,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    let entry = profile
        .get("entries")
        .and_then(Value::as_object)
        .and_then(|entries| {
            entries
                .get(&features.shape_key)
                .or_else(|| entries.get(&features.legacy_shape_key))
        })?;
    let choice = entry.get("choice").and_then(Value::as_str)?;
    let candidate = Candidate::from_str(choice)?;
    let confidence = entry.get("confidence").and_then(Value::as_f64);
    if !candidate_can_route(candidate, features, tables, confidence.unwrap_or(0.0)) {
        return None;
    }
    Some(decision(
        candidate,
        "profile",
        entry
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("profile match"),
        confidence,
        Some(entry.clone()),
    ))
}

fn choose_from_active_profile_entry(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
) -> Option<RouteDecision> {
    if !relations_present(&["rvbbit.route_profile_entries"]) {
        return None;
    }
    let profile_name = profile.effective.as_deref()?;
    let profile_lit = sql_lit(profile_name);
    let shape_lit = sql_lit(&features.shape_key);
    let legacy_shape_lit = sql_lit(&features.legacy_shape_key);
    let mut out = None;
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT pe.choice, pe.confidence, pe.reason, pe.entry \
                 FROM rvbbit.route_profiles rp \
                 JOIN rvbbit.route_profile_entries pe ON pe.profile_name = rp.name \
                 WHERE rp.name = {profile_lit} \
                   AND pe.shape_key IN ({shape_lit}, {legacy_shape_lit}) \
                 ORDER BY (pe.shape_key = {shape_lit}) DESC, rp.updated_at DESC \
                 LIMIT 1"
            ),
            None,
            &[],
        )?;
        for row in table {
            let choice: String = row.get(1)?.unwrap_or_default();
            let Some(candidate) = Candidate::from_str(&choice) else {
                continue;
            };
            let confidence: f64 = row.get(2)?.unwrap_or_default();
            if !candidate_can_route(candidate, features, tables, confidence) {
                continue;
            }
            let reason: String = row
                .get(3)?
                .unwrap_or_else(|| "profile entry match".to_string());
            let entry: JsonB = row.get(4)?.unwrap_or_else(|| JsonB(json!({})));
            out = Some(decision(
                candidate,
                "profile-entry",
                &reason,
                Some(confidence),
                Some(entry.0),
            ));
        }
        Ok(())
    });
    out
}

fn choose_from_fast_profile_entry(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
) -> Option<RouteDecision> {
    if !relations_present(&["rvbbit.route_profile_entries"]) {
        return None;
    }
    let profile_name = profile.effective.as_deref()?;
    let profile_lit = sql_lit(profile_name);
    let shape_lit = sql_lit(&planless_shape_key(&features.shape_key));
    let legacy_shape_lit = sql_lit(&planless_shape_key(&features.legacy_shape_key));
    let mut best: Option<(Candidate, f64, String, i64, Value)> = None;
    let mut ambiguous = false;
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT pe.choice, pe.confidence, pe.reason, pe.entry, pe.observations \
                 FROM rvbbit.route_profiles rp \
                 JOIN rvbbit.route_profile_entries pe ON pe.profile_name = rp.name \
                 WHERE rp.name = {profile_lit} \
                   AND regexp_replace(pe.shape_key, '(\\|width=[^|]*|\\|plan_join=[^|]*|\\|subplan=[^|]*)', '', 'g') \
                       IN ({shape_lit}, {legacy_shape_lit}) \
                 ORDER BY \
                   (regexp_replace(pe.shape_key, '(\\|width=[^|]*|\\|plan_join=[^|]*|\\|subplan=[^|]*)', '', 'g') = {shape_lit}) DESC, \
                   pe.confidence DESC, pe.observations DESC, rp.updated_at DESC \
                 LIMIT 32"
            ),
            None,
            &[],
        )?;
        for row in table {
            let choice: String = row.get(1)?.unwrap_or_default();
            let Some(candidate) = Candidate::from_str(&choice) else {
                continue;
            };
            let confidence: f64 = row.get(2)?.unwrap_or_default();
            if !candidate_can_route(candidate, features, tables, confidence) {
                continue;
            }
            if best
                .as_ref()
                .is_some_and(|(best_candidate, _, _, _, _)| *best_candidate != candidate)
            {
                ambiguous = true;
                break;
            }
            let base_reason: String = row
                .get(3)?
                .unwrap_or_else(|| "planless profile entry match".to_string());
            let observations: i64 = row.get(5)?.unwrap_or_default();
            let mut entry: Value = row.get::<JsonB>(4)?.unwrap_or_else(|| JsonB(json!({}))).0;
            if let Value::Object(map) = &mut entry {
                map.insert("fast_planless_match".into(), json!(true));
                map.insert("matched_observations".into(), json!(observations));
            }
            best = Some((candidate, confidence, base_reason, observations, entry));
        }
        Ok(())
    });
    if ambiguous {
        return None;
    }
    let (candidate, confidence, base_reason, _, entry) = best?;
    Some(decision(
        candidate,
        "profile-entry-fast",
        &format!("fast profile match: {base_reason}"),
        Some(confidence),
        Some(entry),
    ))
}

fn choose_from_profile_curve(
    profile: &Value,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<RouteDecision> {
    let observations = profile.get("observations").and_then(Value::as_array)?;
    let mut anchors: BTreeMap<i64, Vec<RouteCurveSample>> = BTreeMap::new();
    for obs in observations {
        let obs_features = obs.get("features")?;
        let obs_shape = obs_features.get("shape_key").and_then(Value::as_str)?;
        let obs_family = shape_family_key(obs_shape);
        if obs_family != features.shape_family && obs_family != features.legacy_shape_family {
            continue;
        }
        let rows = obs_features.get("table_rows").and_then(Value::as_i64)?;
        let sample = RouteCurveSample {
            native_ms: positive_f64(obs.get("native_ms")),
            duck_ms: positive_f64(obs.get("duck_ms")),
            duck_hive_ms: positive_f64(obs.get("duck_hive_ms")),
            datafusion_ms: positive_f64(obs.get("datafusion_ms")),
            datafusion_hive_ms: positive_f64(obs.get("datafusion_hive_ms")),
            pg_ms: positive_f64(obs.get("pg_ms")),
        };
        if rows > 0 && sample.has_at_least_two() {
            anchors.entry(rows).or_default().push(sample);
        }
    }
    route_curve_from_anchors(anchors, features, tables, "profile-curve")
}

fn choose_from_profile_points(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    profile: &RouteProfileSelection,
) -> Option<RouteDecision> {
    if !relations_present(&["rvbbit.route_profile_points"]) {
        return None;
    }
    let profile_name = profile.effective.as_deref()?;
    let profile_lit = sql_lit(profile_name);
    let family_lit = sql_lit(&features.shape_family);
    let legacy_family_lit = sql_lit(&features.legacy_shape_family);
    let mut anchors: BTreeMap<i64, Vec<RouteCurveSample>> = BTreeMap::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT p.table_rows, p.native_ms, p.duck_ms, p.duck_hive_ms, \
                        p.datafusion_ms, p.datafusion_hive_ms, p.pg_ms \
                 FROM rvbbit.route_profiles rp \
                 JOIN rvbbit.route_profile_points p ON p.profile_name = rp.name \
                 WHERE rp.name = {profile_lit} \
                   AND p.shape_family IN ({family_lit}, {legacy_family_lit}) \
                 ORDER BY p.table_rows \
                 LIMIT 2000"
            ),
            None,
            &[],
        )?;
        for row in table {
            let rows: i64 = row.get(1)?.unwrap_or_default();
            let native: f64 = row.get(2)?.unwrap_or_default();
            let duck: f64 = row.get(3)?.unwrap_or_default();
            let duck_hive: f64 = row.get(4)?.unwrap_or_default();
            let datafusion: f64 = row.get(5)?.unwrap_or_default();
            let datafusion_hive: f64 = row.get(6)?.unwrap_or_default();
            let pg: f64 = row.get(7)?.unwrap_or_default();
            let sample = RouteCurveSample {
                native_ms: (native > 0.0).then_some(native),
                duck_ms: (duck > 0.0).then_some(duck),
                duck_hive_ms: (duck_hive > 0.0).then_some(duck_hive),
                datafusion_ms: (datafusion > 0.0).then_some(datafusion),
                datafusion_hive_ms: (datafusion_hive > 0.0).then_some(datafusion_hive),
                pg_ms: (pg > 0.0).then_some(pg),
            };
            if rows > 0 && sample.has_at_least_two() {
                anchors.entry(rows).or_default().push(sample);
            }
        }
        Ok(())
    });
    route_curve_from_anchors(anchors, features, tables, "profile-point-curve")
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
                   AND candidate IN ('rvbbit_native', 'duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'pg_rowstore') \
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
                "duck_vector" => entry.duck.push(elapsed_ms),
                "duck_hive" => entry.duck_hive.push(elapsed_ms),
                "datafusion_vector" => entry.datafusion.push(elapsed_ms),
                "datafusion_hive" => entry.datafusion_hive.push(elapsed_ms),
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
            duck_ms: (!values.duck.is_empty()).then(|| median_f64(values.duck)),
            duck_hive_ms: (!values.duck_hive.is_empty()).then(|| median_f64(values.duck_hive)),
            datafusion_ms: (!values.datafusion.is_empty()).then(|| median_f64(values.datafusion)),
            datafusion_hive_ms: (!values.datafusion_hive.is_empty())
                .then(|| median_f64(values.datafusion_hive)),
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
                duck_ms: median_option(vals.iter().filter_map(|v| v.duck_ms).collect()),
                duck_hive_ms: median_option(vals.iter().filter_map(|v| v.duck_hive_ms).collect()),
                datafusion_ms: median_option(vals.iter().filter_map(|v| v.datafusion_ms).collect()),
                datafusion_hive_ms: median_option(
                    vals.iter().filter_map(|v| v.datafusion_hive_ms).collect(),
                ),
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
            "datafusion_ms_predicted": predicted_ms(&predictions, Candidate::DataFusionVector),
            "datafusion_hive_ms_predicted": predicted_ms(&predictions, Candidate::DataFusionHive),
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
        "duck_ms": duck_ms,
        "duck_hive_ms": get(Candidate::DuckHive),
        "datafusion_ms": get(Candidate::DataFusionVector),
        "datafusion_hive_ms": get(Candidate::DataFusionHive),
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
        Candidate::DuckVector => Some("duck_ms_median"),
        Candidate::DuckHive => Some("duck_hive_ms_median"),
        Candidate::DataFusionMem => None,
        Candidate::DataFusionVector => Some("datafusion_ms_median"),
        Candidate::DataFusionHive => Some("datafusion_hive_ms_median"),
        Candidate::PgRowstore => Some("pg_ms_median"),
    }
}

fn parse_training_candidates(value: &str, caller: &str) -> Vec<Candidate> {
    let trimmed = value.trim();
    let requested = if trimmed.is_empty() { "all" } else { trimmed };
    let raw = if requested.eq_ignore_ascii_case("all") {
        vec![
            Candidate::RvbbitNative,
            Candidate::DataFusionMem,
            Candidate::DataFusionVector,
            Candidate::DuckVector,
            Candidate::PgRowstore,
            Candidate::DataFusionHive,
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

fn caught_error_message(cause: pg_sys::panic::CaughtError) -> String {
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
                'datafusion_mem_entries', datafusion_mem_entries,
                'datafusion_entries', datafusion_entries,
                'datafusion_hive_entries', datafusion_hive_entries,
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
                coalesce(e.datafusion_mem_entries, 0) AS datafusion_mem_entries,
                coalesce(e.datafusion_entries, 0) AS datafusion_entries,
                coalesce(e.datafusion_hive_entries, 0) AS datafusion_hive_entries,
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
                    count(*) FILTER (WHERE choice = 'datafusion_mem')::bigint AS datafusion_mem_entries,
                    count(*) FILTER (WHERE choice = 'datafusion_vector')::bigint AS datafusion_entries,
                    count(*) FILTER (WHERE choice = 'datafusion_hive')::bigint AS datafusion_hive_entries,
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

fn selected_profile(profile: &RouteProfileSelection) -> Option<Value> {
    let name = profile.effective.as_deref()?;
    if !relations_present(&["rvbbit.route_profiles"]) {
        return None;
    }
    Spi::get_one::<JsonB>(&format!(
        "SELECT profile FROM rvbbit.route_profiles WHERE name = {} LIMIT 1",
        sql_lit(name)
    ))
    .ok()
    .flatten()
    .map(|j| j.0)
}

pub(crate) fn route_runtime_stamp() -> String {
    if !relations_present(&[
        "rvbbit.route_profiles",
        "rvbbit.row_groups",
        "rvbbit.delete_log",
    ]) {
        return "route-runtime-stamp-unavailable".to_string();
    }
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
    let profile_stamp_lit = sql_lit(&profile_stamp);
    Spi::get_one::<String>(&format!(
        "WITH table_state AS ( \
             SELECT string_agg( \
                        c.oid::text || ':' || pg_relation_size(c.oid)::text || ':' || \
                        coalesce(rg.rows, 0)::text || ':' || coalesce(rg.bytes, 0)::text || ':' || \
                        coalesce(dl.deletes, 0)::text || ':' || \
                        coalesce(t.shadow_heap_retained, false)::text || ':' || \
                        coalesce(t.shadow_heap_dirty, false)::text, \
                        ',' ORDER BY c.oid \
                    ) AS stamp \
             FROM pg_class c \
             JOIN pg_am am ON am.oid = c.relam \
             LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid \
             LEFT JOIN ( \
                 SELECT table_oid, sum(n_rows)::bigint AS rows, sum(n_bytes)::bigint AS bytes \
                 FROM rvbbit.row_groups \
                 GROUP BY table_oid \
             ) rg ON rg.table_oid = c.oid \
             LEFT JOIN ( \
                 SELECT table_oid, count(*)::bigint AS deletes \
                 FROM rvbbit.delete_log \
                 GROUP BY table_oid \
             ) dl ON dl.table_oid = c.oid \
             WHERE am.amname = 'rvbbit' \
         ) \
         SELECT {profile_stamp_lit} || \
                '|tables=' || coalesce((SELECT stamp FROM table_state), 'none')"
    ))
    .ok()
    .flatten()
    .unwrap_or_else(|| "route-runtime-stamp-unavailable".to_string())
}

fn referenced_rvbbit_tables(sql: &str, plan_text: Option<&str>) -> Vec<RvbbitTableMetric> {
    if !relations_present(&["rvbbit.row_groups", "rvbbit.delete_log"]) {
        return Vec::new();
    }
    let stringless = sql_stringless(sql).to_lowercase();
    let plan_lower = plan_text.map(str::to_lowercase);
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            "SELECT lower(n.nspname), lower(c.relname), c.oid::bigint, \
                    count(rg.*)::bigint, coalesce(sum(rg.n_rows), 0)::bigint, \
                    coalesce(sum(rg.n_bytes), 0)::bigint, \
                    pg_relation_size(c.oid)::bigint, \
                    coalesce(t.shadow_heap_retained, false), \
                    coalesce(t.shadow_heap_dirty, false), \
                    (SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid), \
                    coalesce(( \
                        SELECT string_agg(lower(a.attname::text), ',' ORDER BY a.attnum) \
                        FROM pg_attribute a \
                        WHERE a.attrelid = c.oid \
                          AND a.attnum > 0 \
                          AND NOT a.attisdropped \
                          AND a.atttypid IN ('text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, 'name'::regtype) \
                    ), '') \
             FROM pg_class c \
             JOIN pg_namespace n ON n.oid = c.relnamespace \
             JOIN pg_am am ON am.oid = c.relam \
             LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid \
             LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.oid \
             WHERE am.amname = 'rvbbit' \
             GROUP BY n.nspname, c.relname, c.oid, t.shadow_heap_retained, t.shadow_heap_dirty",
            None,
            &[],
        )?;
        for row in table {
            let schema: String = row.get(1)?.unwrap_or_default();
            let relname: String = row.get(2)?.unwrap_or_default();
            let sql_referenced = sql_mentions_relation(&stringless, &schema, &relname);
            let plan_referenced = plan_lower
                .as_deref()
                .map(|plan| plan_mentions_relation(plan, &schema, &relname))
                .unwrap_or(false);
            let referenced = sql_referenced || plan_referenced;
            if !referenced {
                continue;
            }
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
                delete_count: row.get(10)?.unwrap_or_default(),
                text_columns: row
                    .get::<String>(11)?
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|col| !col.is_empty())
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
        regex_count: count_substr(&lowered, "regex_replace(")
            + count_substr(&lowered, "regexp_replace(")
            + count_word_fn(&lowered, "regex_replace")
            + count_word_fn(&lowered, "regexp_replace"),
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
        "text_columns": t.text_columns,
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
    })
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
        return (
            false,
            format!("parquet is not authoritative; heap tail has {dirty_heap_bytes} byte(s)"),
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

fn table_has_hive_variant(table_oid: u32) -> bool {
    Spi::get_one::<bool>(&format!(
        "SELECT EXISTS (\
             SELECT 1 FROM rvbbit.row_group_variants rg \
             JOIN rvbbit.layout_variant_status s \
               ON s.table_oid = rg.table_oid AND s.layout = rg.layout \
             WHERE rg.table_oid = {table_oid}::oid \
               AND rg.layout LIKE 'hive:%' \
               AND s.status = 'ready' \
             LIMIT 1\
         )"
    ))
    .ok()
    .flatten()
    .unwrap_or(false)
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
        Candidate::DataFusionHive => {
            route_enabled("RVBBIT_ROUTE_HIVE", "rvbbit.route_hive", true)
                && route_enabled(
                    "RVBBIT_ROUTE_DATAFUSION_HIVE",
                    "rvbbit.route_datafusion_hive",
                    true,
                )
        }
        Candidate::RvbbitNative => route_enabled(
            "RVBBIT_ROUTE_RVBBIT_NATIVE",
            "rvbbit.route_rvbbit_native",
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
    match candidate {
        Candidate::DuckVector => vector_availability("DuckDB", features, tables),
        Candidate::DataFusionMem => hot_mem_availability(features, tables),
        Candidate::DataFusionVector => vector_availability("DataFusion", features, tables),
        Candidate::DuckHive | Candidate::DataFusionHive => hive_availability(features, tables),
        Candidate::RvbbitNative => (true, "Rvbbit native PostgreSQL path available".to_string()),
        Candidate::PgRowstore => pg_rowstore_availability(tables),
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
        _ => 0.05,
    }
}

const FALLBACK_VECTOR_FIRST: [Candidate; 3] = [
    Candidate::DataFusionVector,
    Candidate::DuckVector,
    Candidate::DuckHive,
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

fn default_external_candidate(
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
) -> Option<Candidate> {
    if hot_store_no_profile_enabled() && hot_store_prefers_mem(features) {
        first_available_candidate(&FALLBACK_MEM_FIRST, features, tables)
    } else {
        first_available_candidate(&FALLBACK_VECTOR_FIRST, features, tables)
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

fn candidate_can_route(
    candidate: Candidate,
    features: &RouteFeatures,
    tables: &[RvbbitTableMetric],
    confidence: f64,
) -> bool {
    if confidence < min_confidence_for_candidate(candidate) {
        return false;
    }
    candidate_availability(candidate, features, tables).0
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

fn fallback_native_reason(features: &RouteFeatures) -> Option<&'static str> {
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
    {
        return Some("row-returning query stays on native path");
    }
    None
}

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
    if native_function_should_stay_native(features)
        || simple_metadata_aggregate_should_stay_native(features)
    {
        return false;
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
        && (has_multiple_group_keys(features)
            || features.count_distinct_count > 0
            || features.table_rows >= 1_000_000)
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
    if fallback_prefers_duck_vector(features) {
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
    if let Some(reason) = no_profile_native_reason(features) {
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
                Candidate::DataFusionHive | Candidate::DuckHive
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
    if !matches!(
        name,
        "vector_float_agg"
            | "top_searchphrase_ordered"
            | "count_text_contains"
            | "top_phrase_min_url_for_url_contains"
            | "top_phrase_url_title_rollup"
            | "top_rows_text_contains_ordered_json"
            | "top_text_transform_avg_len"
            | "any_count_int_text"
    ) {
        return false;
    }
    !(features.plan_has_sort
        || features.plan_has_group
        || features.plan_has_join
        || features.plan_has_subplan)
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
                           'duck_ms', duck_ms,
                           'duck_hive_ms', duck_hive_ms,
                           'datafusion_ms', datafusion_ms,
                           'datafusion_hive_ms', datafusion_hive_ms,
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
             native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, entry)
        SELECT {name_lit},
               e.key,
               CASE e.value->>'choice'
                   WHEN 'native' THEN 'rvbbit_native'
                   WHEN 'duck' THEN 'duck_vector'
                   WHEN 'df_mem' THEN 'datafusion_mem'
                   WHEN 'datafusion' THEN 'datafusion_vector'
                   WHEN 'df_hive' THEN 'datafusion_hive'
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
               (
                   SELECT nullif(m->>'median_ms', '')::double precision
                   FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
                   WHERE m->>'candidate' = 'pg_rowstore'
                   LIMIT 1
               ),
               e.value
        FROM jsonb_each(coalesce({profile_lit}::jsonb->'entries', '{{}}'::jsonb)) AS e(key, value)
        WHERE e.value ? 'choice'
          AND e.value->>'choice' IN ('duck', 'duck_hive', 'native', 'datafusion_mem', 'df_mem', 'datafusion', 'datafusion_hive', 'df_hive', 'pg_heap', 'duck_vector', 'datafusion_vector', 'rvbbit_native', 'pg_rowstore')
        "#
    ))?;
    Spi::run(&format!(
        r#"
        INSERT INTO rvbbit.route_profile_points
            (profile_name, shape_family, table_rows, native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, point)
        SELECT {name_lit},
               regexp_replace(
                   regexp_replace(coalesce(obs->'features'->>'shape_key', ''),
                                  '(^|\|)table_rows=[^|]*', '', 'g'),
                   '^\|', ''
               ),
               coalesce(nullif(obs->'features'->>'table_rows', '')::bigint, 0),
               nullif(obs->>'native_ms', '')::double precision,
               nullif(obs->>'duck_ms', '')::double precision,
               nullif(obs->>'duck_hive_ms', '')::double precision,
               nullif(obs->>'datafusion_ms', '')::double precision,
               nullif(obs->>'datafusion_hive_ms', '')::double precision,
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
            (profile_name, shape_family, table_rows, native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, point)
        SELECT {name_lit},
               shape_family,
               table_rows,
               native_ms,
               duck_ms,
               duck_hive_ms,
               datafusion_ms,
               datafusion_hive_ms,
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
                       nullif(pp->>'duck_ms', '')::double precision,
                       nullif(pp->'point'->>'duck_ms', '')::double precision
                   ) AS duck_ms,
                   coalesce(
                       nullif(pp->>'duck_hive_ms', '')::double precision,
                       nullif(pp->'point'->>'duck_hive_ms', '')::double precision
                   ) AS duck_hive_ms,
                   coalesce(
                       nullif(pp->>'datafusion_ms', '')::double precision,
                       nullif(pp->'point'->>'datafusion_ms', '')::double precision
                   ) AS datafusion_ms,
                   coalesce(
                       nullif(pp->>'datafusion_hive_ms', '')::double precision,
                       nullif(pp->'point'->>'datafusion_hive_ms', '')::double precision
                   ) AS datafusion_hive_ms,
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
             native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, entry)
        SELECT {target_lit}, shape_key, choice, confidence, reason, observations,
               native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, entry
        FROM rvbbit.route_profile_entries
        WHERE profile_name = {source_lit}
        ON CONFLICT (profile_name, shape_key) DO UPDATE SET
            choice = EXCLUDED.choice,
            confidence = EXCLUDED.confidence,
            reason = EXCLUDED.reason,
            observations = EXCLUDED.observations,
            native_ms = EXCLUDED.native_ms,
            duck_ms = EXCLUDED.duck_ms,
            duck_hive_ms = EXCLUDED.duck_hive_ms,
            datafusion_ms = EXCLUDED.datafusion_ms,
            datafusion_hive_ms = EXCLUDED.datafusion_hive_ms,
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
            (profile_name, shape_family, table_rows, native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, point)
        SELECT {target_lit}, shape_family, table_rows, native_ms, duck_ms, duck_hive_ms, datafusion_ms, datafusion_hive_ms, pg_ms, point
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

fn planless_shape_key(key: &str) -> String {
    key.split('|')
        .filter(|part| {
            !part.starts_with("width=")
                && !part.starts_with("plan_join=")
                && !part.starts_with("subplan=")
        })
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

fn positive_f64(value: Option<&Value>) -> Option<f64> {
    value.and_then(Value::as_f64).filter(|v| *v > 0.0)
}

fn interpolate_predictions(
    lower: RouteCurveSample,
    upper: RouteCurveSample,
    position: f64,
) -> Vec<(Candidate, f64)> {
    [
        (Candidate::RvbbitNative, lower.native_ms, upper.native_ms),
        (Candidate::DuckVector, lower.duck_ms, upper.duck_ms),
        (Candidate::DuckHive, lower.duck_hive_ms, upper.duck_hive_ms),
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
            delete_count: 0,
            text_columns: Vec::new(),
        }
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
            2_000_000,
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
        assert_eq!(
            fallback_external_candidate_order(&distinct_text).map(|order| order[0]),
            Some(Candidate::DuckHive)
        );

        let text_topk = test_features_with_text(
            r#"SELECT "UserID", "SearchPhrase", COUNT(*) FROM hits GROUP BY "UserID", "SearchPhrase" ORDER BY COUNT(*) DESC LIMIT 10"#,
            500_000,
            &["searchphrase"],
        );
        assert!(fallback_prefers_variant(&text_topk));
        assert_eq!(
            fallback_external_candidate_order(&text_topk).map(|order| order[0]),
            Some(Candidate::DuckHive)
        );
    }

    #[test]
    fn route_no_profile_keeps_time_bucket_without_text_vector_first() {
        let features = test_features(
            r#"SELECT DATE_TRUNC('minute', "EventTime") AS m, COUNT(*) FROM hits GROUP BY DATE_TRUNC('minute', "EventTime") ORDER BY DATE_TRUNC('minute', "EventTime") LIMIT 10 OFFSET 1000"#,
            1_000_000,
        );
        assert!(fallback_prefers_external_analytical_shape(&features));
        assert!(!fallback_prefers_variant(&features));
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DataFusionVector)
        );
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
    fn route_no_profile_prefers_duck_for_complex_large_join_shapes() {
        let features = test_features(
            "SELECT COUNT(*) FROM hits h1 \
             JOIN hits h2 ON h1.id = h2.id \
             JOIN hits h3 ON h1.id = h3.id \
             JOIN hits h4 ON h1.id = h4.id \
             JOIN hits h5 ON h1.id = h5.id",
            2_000_000,
        );

        assert!(fallback_prefers_duck_vector(&features));
        assert_eq!(
            fallback_external_candidate_order(&features).map(|order| order[0]),
            Some(Candidate::DuckVector)
        );
    }
}
