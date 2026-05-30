-- pg_rvbbit 0.60.1 -> 0.60.2
-- DataFusion/Vortex sibling accelerator format.

CREATE OR REPLACE FUNCTION rvbbit.datafusion_vortex_query_json(
    "query" text,
    "column_names" jsonb,
    "max_rows" integer
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'datafusion_vortex_query_json_wrapper';

ALTER TABLE IF EXISTS rvbbit.route_observations
    DROP CONSTRAINT IF EXISTS route_observations_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_observations
    ADD CONSTRAINT route_observations_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_training_results
    DROP CONSTRAINT IF EXISTS route_training_results_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_training_results
    ADD CONSTRAINT route_training_results_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_decisions
    DROP CONSTRAINT IF EXISTS route_decisions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD CONSTRAINT route_decisions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_executions
    DROP CONSTRAINT IF EXISTS route_executions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD CONSTRAINT route_executions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'pg_rowstore'));

CREATE OR REPLACE FUNCTION rvbbit.layout_variant_status_for(rel regclass)
RETURNS TABLE (
    layout text,
    layout_kind text,
    partition_key text,
    status text,
    expected_rows bigint,
    actual_rows bigint,
    file_count integer,
    n_bytes bigint,
    status_message text,
    refreshed_at timestamptz
)
LANGUAGE sql
STABLE
AS $$
    SELECT s.layout,
           CASE
             WHEN s.layout LIKE 'hive:%' THEN 'hive'
             WHEN s.layout LIKE 'cluster:%' THEN 'cluster'
             WHEN s.layout = 'vortex_scan' THEN 'vortex'
             ELSE s.layout
           END,
           CASE
             WHEN s.layout LIKE 'hive:%' THEN substring(s.layout from 6)
             WHEN s.layout LIKE 'cluster:%' THEN substring(s.layout from 9)
             ELSE NULL
           END,
           s.status,
           s.expected_rows,
           s.actual_rows,
           s.file_count,
           coalesce((
             SELECT sum(v.n_bytes)::bigint
             FROM rvbbit.row_group_variants v
             WHERE v.table_oid = s.table_oid AND v.layout = s.layout
           ), 0),
           s.status_message,
           s.refreshed_at
    FROM rvbbit.layout_variant_status s
    WHERE s.table_oid = rel
    ORDER BY s.layout;
$$;

CREATE OR REPLACE FUNCTION rvbbit.acceleration_phase_log_for(rel regclass)
RETURNS TABLE (
    operation_id bigint,
    operation text,
    phase text,
    layout text,
    layout_kind text,
    partition_key text,
    status text,
    started_at timestamptz,
    finished_at timestamptz,
    elapsed_ms numeric,
    rows_written bigint,
    row_groups_written bigint,
    bytes_written bigint,
    files_written integer,
    expected_rows bigint,
    actual_rows bigint,
    details jsonb,
    error text
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        p.operation_id,
        o.operation,
        p.phase,
        p.layout,
        CASE
          WHEN p.layout LIKE 'hive:%' THEN 'hive'
          WHEN p.layout LIKE 'cluster:%' THEN 'cluster'
          WHEN p.layout = 'vortex_scan' THEN 'vortex'
          ELSE p.layout
        END,
        coalesce(
          p.partition_key,
          CASE
            WHEN p.layout LIKE 'hive:%' THEN substring(p.layout from 6)
            WHEN p.layout LIKE 'cluster:%' THEN substring(p.layout from 9)
            ELSE NULL
          END
        ),
        p.status,
        p.started_at,
        p.finished_at,
        round((extract(epoch FROM coalesce(p.finished_at, clock_timestamp()) - p.started_at) * 1000)::numeric, 3),
        p.rows_written,
        p.row_groups_written,
        p.bytes_written,
        p.files_written,
        p.expected_rows,
        p.actual_rows,
        p.details,
        p.error
    FROM rvbbit.acceleration_operation_phases p
    LEFT JOIN rvbbit.acceleration_operations o ON o.id = p.operation_id
    WHERE p.table_oid = rel
    ORDER BY p.started_at DESC, p.id DESC;
$$;
