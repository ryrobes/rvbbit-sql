-- 0109_workload_layout_advisor
--
-- Workload-derived physical layout advice. This is deliberately advisory:
-- recommendation generation observes routed query shapes and representative SQL, but
-- no files are built until a recommendation is explicitly accepted or the user calls
-- the normal layout refresh path with matching GUCs.

CREATE OR REPLACE FUNCTION rvbbit.recommend_workload_layouts(
    rel oid,
    lookback_hours integer DEFAULT 24,
    min_observations integer DEFAULT 2,
    max_recommendations integer DEFAULT 8,
    persist boolean DEFAULT true
) RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'recommend_workload_layouts_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.accept_workload_layout(
    rel oid,
    layout_kind text,
    column_name text
) RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'accept_workload_layout_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.reject_workload_layout(
    rel oid,
    layout_kind text,
    column_name text
) RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'reject_workload_layout_wrapper';

CREATE TABLE IF NOT EXISTS rvbbit.workload_layout_recommendations (
    table_oid      oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    layout_kind    text NOT NULL,
    column_name    text NOT NULL,
    layout         text NOT NULL,
    score          double precision NOT NULL DEFAULT 0,
    observations   bigint NOT NULL DEFAULT 0,
    weighted_ms    double precision NOT NULL DEFAULT 0,
    role_counts    jsonb NOT NULL DEFAULT '{}'::jsonb,
    sample_shapes  text[] NOT NULL DEFAULT ARRAY[]::text[],
    reason         text NOT NULL DEFAULT '',
    details        jsonb NOT NULL DEFAULT '{}'::jsonb,
    status         text NOT NULL DEFAULT 'candidate',
    recommended_at timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, layout_kind, column_name),
    CHECK (layout_kind IN ('cluster', 'hive')),
    CHECK (status IN ('candidate', 'accepted', 'rejected', 'retired')),
    CHECK (score >= 0),
    CHECK (observations >= 0),
    CHECK (weighted_ms >= 0)
);

CREATE INDEX IF NOT EXISTS workload_layout_recommendations_status_idx
    ON rvbbit.workload_layout_recommendations (status, score DESC, updated_at DESC);

CREATE OR REPLACE VIEW rvbbit.workload_layout_recommendation_status AS
SELECT
    r.table_oid::regclass::text AS table_name,
    r.table_oid,
    r.layout_kind,
    r.column_name,
    r.layout,
    r.status,
    r.score,
    r.observations,
    r.weighted_ms,
    r.role_counts,
    r.sample_shapes,
    s.status AS layout_status,
    s.actual_rows AS layout_rows,
    s.file_count AS layout_files,
    r.reason,
    r.details,
    r.recommended_at,
    r.updated_at
FROM rvbbit.workload_layout_recommendations r
LEFT JOIN rvbbit.layout_variant_status s
  ON s.table_oid = r.table_oid
 AND s.layout = r.layout;
