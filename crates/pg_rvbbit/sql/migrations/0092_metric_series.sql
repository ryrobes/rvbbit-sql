-- 0092_metric_series
--
-- Canonical chart/drilldown surface for materialized metrics. A metric remains
-- scalar at materialization time, but the observation log is a first-class
-- series that can be opened as an ordinary SQL block.

CREATE OR REPLACE FUNCTION rvbbit.metric_numeric_value(
    p_value   jsonb,
    p_verdict jsonb DEFAULT NULL
) RETURNS double precision
LANGUAGE sql IMMUTABLE AS $fn$
    WITH candidates(txt) AS (
        VALUES
            (p_verdict->>'value'),
            (CASE
                WHEN jsonb_typeof(p_value) = 'object' THEN p_value->>'value'
                WHEN jsonb_typeof(p_value) IN ('number', 'string') THEN trim(both '"' FROM p_value::text)
                ELSE NULL
             END),
            (CASE
                WHEN jsonb_typeof(p_value) = 'array'
                 AND jsonb_array_length(p_value) = 1
                 AND jsonb_typeof(p_value->0) = 'object'
                THEN p_value->0->>'value'
                ELSE NULL
             END)
    )
    SELECT txt::double precision
    FROM candidates
    WHERE txt IS NOT NULL
      AND txt ~ '^[[:space:]]*[+-]?(([0-9]+(\.[0-9]*)?)|(\.[0-9]+))([eE][+-]?[0-9]+)?[[:space:]]*$'
    LIMIT 1
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric_series(
    p_name        text,
    p_from        timestamptz DEFAULT (now() - interval '90 days'),
    p_to          timestamptz DEFAULT now(),
    p_bucket      text        DEFAULT 'day',
    p_params      jsonb       DEFAULT NULL,
    p_stale_after interval    DEFAULT interval '2 days'
) RETURNS TABLE(
    bucket             timestamptz,
    metric_name        text,
    value              double precision,
    status             text,
    ok                 boolean,
    target             jsonb,
    observation_id     bigint,
    metric_version     integer,
    data_as_of         timestamptz,
    observed_at        timestamptz,
    trigger            text,
    params             jsonb,
    stale_source_count integer,
    source_freshness   jsonb
) LANGUAGE sql STABLE AS $fn$
    WITH deps AS (
        SELECT
            count(*) FILTER (WHERE f.stale IS TRUE)::integer AS stale_source_count,
            coalesce(
                jsonb_agg(
                    jsonb_build_object(
                        'table', f.table_schema || '.' || f.table_name,
                        'freshness_column', f.freshness_column,
                        'max_freshness', f.max_freshness,
                        'stale', f.stale
                    )
                    ORDER BY f.table_schema, f.table_name
                ),
                '[]'::jsonb
            ) AS source_freshness
        FROM rvbbit.metric_dependency_freshness(ARRAY[p_name]::text[], p_stale_after) f
    ),
    raw AS (
        SELECT
            CASE
                WHEN lower(coalesce(p_bucket, 'day')) IN ('raw', 'none', 'observation')
                    THEN coalesce(o.data_as_of, o.observed_at)
                ELSE date_trunc(p_bucket, coalesce(o.data_as_of, o.observed_at))
            END AS bucket,
            o.*
        FROM rvbbit.metric_observations o
        WHERE o.metric_name = p_name
          AND coalesce(o.data_as_of, o.observed_at) >= p_from
          AND coalesce(o.data_as_of, o.observed_at) <= p_to
          AND (p_params IS NULL OR o.params = p_params)
    ),
    picked AS (
        SELECT DISTINCT ON (r.bucket)
            r.*
        FROM raw r
        ORDER BY r.bucket, coalesce(r.data_as_of, r.observed_at) DESC, r.observed_at DESC, r.observation_id DESC
    )
    SELECT
        p.bucket,
        p.metric_name,
        rvbbit.metric_numeric_value(p.value, p.verdict) AS value,
        p.status,
        CASE
            WHEN p.verdict ? 'ok' THEN (p.verdict->>'ok') IN ('true', 't', '1')
            ELSE NULL
        END AS ok,
        p.verdict->'target' AS target,
        p.observation_id,
        p.metric_version,
        p.data_as_of,
        p.observed_at,
        p.trigger,
        p.params,
        deps.stale_source_count,
        deps.source_freshness
    FROM picked p
    CROSS JOIN deps
    ORDER BY p.bucket
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric_provenance(
    p_name        text,
    p_stale_after interval DEFAULT interval '2 days'
) RETURNS jsonb
LANGUAGE sql STABLE AS $fn$
    WITH def AS (
        SELECT *
        FROM rvbbit.metric_catalog
        WHERE name = p_name
        ORDER BY created_at DESC, version DESC
        LIMIT 1
    ),
    latest AS (
        SELECT *
        FROM rvbbit.metric_observations
        WHERE metric_name = p_name
        ORDER BY coalesce(data_as_of, observed_at) DESC, observed_at DESC, observation_id DESC
        LIMIT 1
    ),
    deps AS (
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'table', f.table_schema || '.' || f.table_name,
                    'freshness_column', f.freshness_column,
                    'max_freshness', f.max_freshness,
                    'age', f.age,
                    'stale', f.stale
                )
                ORDER BY f.table_schema, f.table_name
            ),
            '[]'::jsonb
        ) AS dependencies
        FROM rvbbit.metric_dependency_freshness(ARRAY[p_name]::text[], p_stale_after) f
    )
    SELECT jsonb_build_object(
        'metric', p_name,
        'definition', to_jsonb(def),
        'latest_observation', to_jsonb(latest),
        'dependencies', deps.dependencies
    )
    FROM def
    CROSS JOIN deps
    LEFT JOIN latest ON true
$fn$;
