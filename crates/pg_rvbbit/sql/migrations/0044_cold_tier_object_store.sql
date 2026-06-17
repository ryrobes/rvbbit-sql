-- 0044_cold_tier_object_store
--
-- Extend the cold tier (rvbbit.row_groups.cold_url) from file://-only to real
-- object storage (s3:// / gs://). The READ side already works once the .so is
-- rebuilt — df.rs now registers the S3/GCS object store before DataFusion reads a
-- cold_url (credentials from the environment / instance metadata). This migration
-- adds the WRITE side:
--
--   1. The C binding for rvbbit.cold_put (a new #[pg_extern]) — created here so the
--      ownership-drift manual-binding step isn't needed; the wrapper symbol exists
--      in the freshly deployed library when migrate() runs.
--   2. rvbbit.migrate_to_cold gains s3:// / gs:// support: for remote prefixes it
--      uploads each row-group file via rvbbit.cold_put instead of a local `cp`.
--
-- Because cold reads route through DataFusion and the catalog (incl. cold_url) is
-- WAL-replicated, a physical standby reads the SAME s3:// object the primary wrote
-- — a shared, accelerated standby with no per-node file copy.

-- 1. C binding for the ObjectStore uploader.
CREATE OR REPLACE FUNCTION rvbbit.cold_put(local_path text, dest_uri text)
RETURNS bigint LANGUAGE c STRICT AS '$libdir/pg_rvbbit', 'cold_put_wrapper';

-- 2. migrate_to_cold with file:// (local copy) + s3:// / gs:// (ObjectStore upload).
CREATE OR REPLACE FUNCTION rvbbit.migrate_to_cold(
    reloid          regclass,
    cold_url_prefix text
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    table_oid_str text := reloid::oid::text;
    prefix_norm   text := rtrim(cold_url_prefix, '/');
    scheme        text := lower(split_part(prefix_norm, '://', 1));
    is_remote     boolean := scheme IN ('s3', 'gs');
    n_migrated    int := 0;
    n_bytes_total bigint := 0;
    rg_record     record;
    src_path      text;
    dest_url      text;
    dest_local    text;
BEGIN
    IF position('://' IN prefix_norm) = 0 OR scheme NOT IN ('file', 's3', 'gs') THEN
        RAISE EXCEPTION 'rvbbit.migrate_to_cold: cold_url_prefix must be file://, s3://, or gs:// (got %)', cold_url_prefix;
    END IF;

    IF NOT is_remote THEN
        -- file:// — local copy (unchanged from the original MVP path). The strict
        -- safe-path allowlist guards the COPY ... TO PROGRAM shell interpolation.
        dest_local := substring(prefix_norm FROM 8);
        IF dest_local !~ '^/[A-Za-z0-9_./-]+$' THEN
            RAISE EXCEPTION 'rvbbit.migrate_to_cold: file:// path may only contain letters, digits, and / _ . - (got %)', dest_local;
        END IF;
        EXECUTE format(
            'COPY (SELECT 1) TO PROGRAM ''mkdir -p %s/%s/scan''',
            replace(dest_local, '''', ''''''), replace(table_oid_str, '''', '''''')
        );
    END IF;

    FOR rg_record IN
        SELECT rg_id, path, n_bytes, cold_url
        FROM rvbbit.row_groups
        WHERE table_oid = reloid
        ORDER BY rg_id
    LOOP
        IF rg_record.cold_url IS NOT NULL THEN
            CONTINUE;   -- already migrated
        END IF;
        src_path := rg_record.path;
        dest_url := format('%s/%s/scan/%s.parquet', prefix_norm, table_oid_str, rg_record.rg_id);

        IF is_remote THEN
            -- s3:// / gs:// — upload through the ObjectStore writer (creds from
            -- env / instance metadata). Files are COPIED; locals remain as-is.
            PERFORM rvbbit.cold_put(src_path, dest_url);
        ELSE
            EXECUTE format(
                'COPY (SELECT 1) TO PROGRAM ''cp %s %s/%s/scan/%s.parquet''',
                replace(src_path, '''', ''''''),
                replace(dest_local, '''', ''''''),
                replace(table_oid_str, '''', ''''''),
                rg_record.rg_id::text
            );
        END IF;

        UPDATE rvbbit.row_groups
           SET cold_url = dest_url
         WHERE table_oid = reloid AND rg_id = rg_record.rg_id;

        n_migrated := n_migrated + 1;
        n_bytes_total := n_bytes_total + rg_record.n_bytes;
    END LOOP;

    RETURN jsonb_build_object(
        'migrated_row_groups', n_migrated,
        'total_bytes',          n_bytes_total,
        'cold_url_prefix',      prefix_norm
    );
END $$;
