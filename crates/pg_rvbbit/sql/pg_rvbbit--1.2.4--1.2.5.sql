-- pg_rvbbit 1.2.4 -> 1.2.5
--
-- Retention reaper for the temporal-mirror workflow. Bounds disk + AS OF
-- history for snapshot tables. The file-unlink helper is a Rust pg_extern
-- (declared here for already-installed databases; fresh installs get it from
-- the generated full SQL).

CREATE OR REPLACE FUNCTION rvbbit.reap_unlink_files(paths text[])
RETURNS integer
LANGUAGE c
AS 'MODULE_PATHNAME', 'reap_unlink_files_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.reap_generations(
    reloid regclass DEFAULT NULL,
    keep_days integer DEFAULT 30
) RETURNS TABLE (relname text, generations_reaped bigint, row_groups_reaped bigint, files_unlinked integer)
LANGUAGE plpgsql AS $$
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

        nfiles := coalesce(rvbbit.reap_unlink_files(paths), 0);

        IF coalesce(gens, 0) > 0 OR coalesce(rgs, 0) > 0 THEN
            relname := rec.table_oid::regclass::text;
            generations_reaped := coalesce(gens, 0);
            row_groups_reaped := coalesce(rgs, 0);
            files_unlinked := nfiles;
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$$;
