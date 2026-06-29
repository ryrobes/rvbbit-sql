-- 0112_drop_table_orphan_files
--
-- Dropping a registry-backed rvbbit table removes its catalog rows by
-- deleting rvbbit.tables. Capture local accelerator paths first so
-- maintenance can unlink them after the DROP transaction commits.

CREATE OR REPLACE FUNCTION rvbbit.on_drop_table()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    obj record;
    orphan_paths text[];
BEGIN
    IF to_regclass('rvbbit.tables') IS NULL THEN
        RETURN;
    END IF;

    FOR obj IN
        SELECT * FROM pg_event_trigger_dropped_objects()
        WHERE object_type = 'table'
    LOOP
        IF to_regclass('rvbbit.orphaned_files') IS NOT NULL THEN
            SELECT array_agg(path ORDER BY path)
              INTO orphan_paths
              FROM (
                  SELECT path FROM rvbbit.row_groups WHERE table_oid = obj.objid
                  UNION ALL
                  SELECT substring(cold_url FROM 8)
                  FROM rvbbit.row_groups
                  WHERE table_oid = obj.objid
                    AND cold_url LIKE 'file:///%'
                  UNION ALL
                  SELECT path FROM rvbbit.row_group_variants WHERE table_oid = obj.objid
                  UNION ALL
                  SELECT path FROM rvbbit.text_dictionaries WHERE table_oid = obj.objid
              ) old_files
             WHERE path IS NOT NULL
               AND btrim(path) <> '';

            IF orphan_paths IS NOT NULL THEN
                INSERT INTO rvbbit.orphaned_files (path, table_oid, reason)
                SELECT DISTINCT p, obj.objid, 'drop_table'
                FROM unnest(orphan_paths) AS p
                WHERE p IS NOT NULL AND btrim(p) <> ''
                ON CONFLICT (path) DO UPDATE
                   SET table_oid = EXCLUDED.table_oid,
                       reason = EXCLUDED.reason,
                       operation_id = NULL,
                       queued_at = clock_timestamp(),
                       last_error = NULL;
            END IF;
        END IF;

        DELETE FROM rvbbit.tables WHERE table_oid = obj.objid;
        DELETE FROM rvbbit.delete_log WHERE table_oid = obj.objid;
        DELETE FROM rvbbit.table_dirty_markers WHERE table_oid = obj.objid;
    END LOOP;
END;
$$;
