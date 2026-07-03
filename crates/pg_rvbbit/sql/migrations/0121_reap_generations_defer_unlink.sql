-- 0121_reap_generations_defer_unlink
--
-- Pre-release audit fix (2026-07-03): rvbbit.reap_generations deleted row_groups
-- and generations rows and then unlinked the files IN THE SAME TXN. File
-- deletion is not transactional, so a rollback restored the catalog rows while
-- the files were already gone -> dangling row_groups / data loss. Defer the
-- physical unlink to rvbbit.reap_orphaned_files by queueing paths into
-- rvbbit.orphaned_files, exactly like rvbbit.rebuild_acceleration. Body is
-- otherwise the committed definition verbatim.

CREATE OR REPLACE FUNCTION rvbbit.reap_generations(reloid regclass DEFAULT NULL::regclass, keep_days integer DEFAULT 30)
 RETURNS TABLE(relname text, generations_reaped bigint, row_groups_reaped bigint, files_unlinked integer)
 LANGUAGE plpgsql
AS $function$
DECLARE
    rec    record;
    cutoff timestamptz := now() - make_interval(days => greatest(keep_days, 0));
    paths  text[];
    gens   bigint;
    rgs    bigint;
    nfiles integer;
BEGIN
    FOR rec IN
        SELECT t.table_oid, t.min_visible_generation AS floor
        FROM rvbbit.tables t
        WHERE t.min_visible_generation > 0
          AND (reloid IS NULL OR t.table_oid = reloid)
    LOOP
        -- local file paths for the generations we're about to reap
        SELECT array_agg(rg.path)
        INTO paths
        FROM rvbbit.row_groups rg
        JOIN rvbbit.generations g
          ON g.table_oid = rg.table_oid AND g.generation = rg.generation
        WHERE rg.table_oid = rec.table_oid
          AND rg.generation < rec.floor
          AND g.committed_at < cutoff
          AND rg.cold_url IS NULL;

        WITH reap_gens AS (
            SELECT g.generation
            FROM rvbbit.generations g
            WHERE g.table_oid = rec.table_oid
              AND g.generation < rec.floor
              AND g.committed_at < cutoff
        ),
        del_rg AS (
            DELETE FROM rvbbit.row_groups rg
            WHERE rg.table_oid = rec.table_oid
              AND rg.generation IN (SELECT generation FROM reap_gens)
            RETURNING 1
        ),
        del_gen AS (
            DELETE FROM rvbbit.generations g
            WHERE g.table_oid = rec.table_oid
              AND g.generation IN (SELECT generation FROM reap_gens)
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM del_gen), (SELECT count(*) FROM del_rg)
        INTO gens, rgs;

        -- Do NOT unlink files in-txn. File deletion is not transactional: if
        -- this txn rolls back (an error in a later loop iteration, or the
        -- caller aborting) the row_groups/generations DELETEs are undone but the
        -- files are already gone -> catalog rows dangle. Queue them for the
        -- background reaper (rvbbit.reap_orphaned_files), which re-checks that no
        -- catalog row still references the path, honors a grace period, and
        -- unlinks in its own txn. Mirrors rebuild_acceleration.
        nfiles := 0;
        IF paths IS NOT NULL THEN
            INSERT INTO rvbbit.orphaned_files (path, table_oid, reason, operation_id)
            SELECT DISTINCT p, rec.table_oid, 'reap_generations', NULL::bigint
            FROM unnest(paths) AS p
            WHERE p IS NOT NULL AND btrim(p) <> ''
            ON CONFLICT (path) DO UPDATE
               SET table_oid = EXCLUDED.table_oid,
                   reason = EXCLUDED.reason,
                   operation_id = EXCLUDED.operation_id,
                   queued_at = clock_timestamp(),
                   last_error = NULL;
            GET DIAGNOSTICS nfiles = ROW_COUNT;
        END IF;

        IF coalesce(gens, 0) > 0 OR coalesce(rgs, 0) > 0 THEN
            relname := rec.table_oid::regclass::text;
            generations_reaped := coalesce(gens, 0);
            row_groups_reaped := coalesce(rgs, 0);
            files_unlinked := nfiles;
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$function$

