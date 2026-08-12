-- 0292: mirror destinations are acceleration targets from their first DDL.
--
-- The dlt destination client now emits CREATE TABLE ... USING rvbbit for user
-- relations only. The RVBBIT DDL trigger registers that relation and converts
-- its physical AM back to heap, so ordinary Postgres semantics remain intact.
-- Backfill mirrors created by older workers and expose both halves of that
-- contract in lineage: physical heap storage plus registry membership.

DO $register_existing_mirror_destinations$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT DISTINCT destination.relation_oid
        FROM rvbbit.mirror_jobs j
        JOIN rvbbit.mirror_tables t USING (job_name)
        CROSS JOIN LATERAL (
            SELECT to_regclass(
                format('%I.%I', j.destination_schema, t.destination_table)
            ) AS relation_oid
        ) destination
        WHERE destination.relation_oid IS NOT NULL
    LOOP
        PERFORM rvbbit.enable_table(target.relation_oid::regclass);
    END LOOP;
END
$register_existing_mirror_destinations$;

CREATE OR REPLACE VIEW rvbbit.mirror_lineage AS
SELECT
    c.connection_name,
    c.label AS connection_label,
    c.dialect,
    nullif(c.metadata ->> 'host', '') AS source_host,
    nullif(c.metadata ->> 'database', '') AS source_database,
    nullif(c.metadata ->> 'environment', '') AS source_environment,
    j.job_name,
    j.source_schema,
    t.source_table,
    j.destination_schema,
    t.destination_table,
    format('%I.%I', j.destination_schema, t.destination_table)
        AS destination_relation,
    destination.relation_oid IS NOT NULL AS destination_exists,
    am.amname AS destination_access_method,
    greatest(coalesce(pc.reltuples, 0), 0)::bigint AS destination_row_estimate,
    CASE
        WHEN destination.relation_oid IS NULL THEN NULL
        ELSE pg_total_relation_size(destination.relation_oid)
    END::bigint AS destination_bytes,
    stats.last_analyze AS destination_last_analyze,
    t.load_mode,
    t.primary_key,
    t.cursor_column,
    c.enabled AS connection_enabled,
    j.enabled AS job_enabled,
    t.enabled AS table_enabled,
    j.schedule_seconds,
    j.next_run_at,
    latest.run_id AS latest_run_id,
    latest.status AS latest_run_status,
    latest.requested_at AS latest_requested_at,
    latest.started_at AS latest_started_at,
    latest.finished_at AS latest_finished_at,
    latest.rows_loaded AS latest_run_rows_loaded,
    latest.load_ids AS latest_run_load_ids,
    table_receipt.status AS latest_table_status,
    table_receipt.rows_loaded AS latest_table_rows_loaded,
    table_receipt.load_ids AS latest_table_load_ids,
    coalesce(table_receipt.error_code, latest.error_code) AS latest_error_code,
    coalesce(table_receipt.error_message, latest.error_message) AS latest_error_message,
    registry.table_oid IS NOT NULL AS destination_registered,
    coalesce(registry.acceleration_enabled, false) AS acceleration_enabled
FROM rvbbit.mirror_connections c
JOIN rvbbit.mirror_jobs j USING (connection_name)
JOIN rvbbit.mirror_tables t USING (job_name)
LEFT JOIN LATERAL (
    SELECT to_regclass(format('%I.%I', j.destination_schema, t.destination_table))
        AS relation_oid
) destination ON true
LEFT JOIN pg_class pc ON pc.oid = destination.relation_oid
LEFT JOIN pg_am am ON am.oid = pc.relam
LEFT JOIN rvbbit.tables registry ON registry.table_oid = destination.relation_oid
LEFT JOIN pg_stat_user_tables stats ON stats.relid = destination.relation_oid
LEFT JOIN LATERAL (
    SELECT r.run_id, r.status, r.requested_at, r.started_at, r.finished_at,
           r.rows_loaded, r.load_ids, r.error_code, r.error_message
    FROM rvbbit.mirror_runs r
    WHERE r.job_name = j.job_name
    ORDER BY r.requested_at DESC
    LIMIT 1
) latest ON true
LEFT JOIN rvbbit.mirror_table_runs table_receipt
    ON table_receipt.run_id = latest.run_id
   AND table_receipt.source_table = t.source_table;

REVOKE ALL ON rvbbit.mirror_lineage FROM PUBLIC;

COMMENT ON VIEW rvbbit.mirror_lineage IS
    'Credential-free source-to-RVBBIT relation lineage with registry, acceleration, and latest table/run health.';
