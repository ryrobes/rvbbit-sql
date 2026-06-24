-- 0076: Staged major fold metadata swap.
--
-- A full accelerator rebuild used to delete row_group catalog rows and unlink
-- the old files before exporting the replacement files. Other transactions can
-- still see old catalog rows by MVCC until commit, so physical unlink/overwrite
-- in the middle of the transaction is unsafe and can force readers onto broken
-- or missing files. This changes rebuild_acceleration into a staged fold:
-- write replacement row groups at fresh rg_ids, swap metadata at the end, and
-- queue old files for a later reap after the swap has committed.

CREATE TABLE IF NOT EXISTS rvbbit.orphaned_files (
    path            text PRIMARY KEY,
    table_oid       oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    reason          text NOT NULL,
    operation_id    bigint REFERENCES rvbbit.acceleration_operations(id) ON DELETE SET NULL,
    queued_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts        integer NOT NULL DEFAULT 0,
    last_attempt_at timestamptz,
    last_error      text,
    CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS orphaned_files_queued_idx
    ON rvbbit.orphaned_files (queued_at);

CREATE OR REPLACE FUNCTION rvbbit.reap_orphaned_files(
    max_age interval DEFAULT interval '30 minutes',
    max_files integer DEFAULT 1000
) RETURNS TABLE (files_dequeued integer, files_unlinked integer)
LANGUAGE plpgsql AS $$
DECLARE
    paths text[];
BEGIN
    WITH candidates AS (
        SELECT o.path
        FROM rvbbit.orphaned_files o
        WHERE o.queued_at <= clock_timestamp() - coalesce(max_age, interval '30 minutes')
          AND NOT EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.path = o.path)
          AND NOT EXISTS (SELECT 1 FROM rvbbit.row_group_variants v WHERE v.path = o.path)
          AND NOT EXISTS (SELECT 1 FROM rvbbit.text_dictionaries d WHERE d.path = o.path)
        ORDER BY o.queued_at
        LIMIT greatest(coalesce(max_files, 1000), 0)
    ),
    removed AS (
        DELETE FROM rvbbit.orphaned_files o
        USING candidates c
        WHERE o.path = c.path
        RETURNING o.path
    )
    SELECT coalesce(count(*), 0)::integer, array_agg(path)
      INTO files_dequeued, paths
      FROM removed;

    files_unlinked := coalesce(rvbbit.reap_unlink_files(paths), 0);
    RETURN NEXT;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.rebuild_acceleration(
    reloid regclass,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql AS $$
<<accel_rebuild>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    dropped_rgs int := 0;
    rebuilt_rows bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    generation_after bigint := 0;
    pre_max_rg_id bigint := -1;
    safe_upper_xid numeric;
    phase_id bigint;
    phase_bytes_written bigint := 0;
    queued_orphan_files int := 0;
    orphan_paths text[];
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);

    safe_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1
    );

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'rebuild_acceleration', 'running',
        NULL, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'mode', 'staged_full_heap_fold',
            'heap_guard', 'LOCK TABLE IN SHARE MODE',
            'metadata_swap', 'post_export',
            'file_reap', 'queued_after_swap'
        )
    )
    RETURNING id INTO op_id;

    SELECT count(*)::int, coalesce(max(rg_id), -1)::bigint
      INTO dropped_rgs, pre_max_rg_id
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT array_agg(path ORDER BY path)
      INTO orphan_paths
      FROM (
          SELECT path FROM rvbbit.row_groups WHERE table_oid = reloid
          UNION ALL
          SELECT path FROM rvbbit.row_group_variants WHERE table_oid = reloid
          UNION ALL
          SELECT path FROM rvbbit.text_dictionaries WHERE table_oid = reloid
      ) old_files;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_full_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'staged_full_heap_fold',
            'dropped_row_groups', dropped_rgs,
            'old_max_rg_id', pre_max_rg_id
        )
    )
    RETURNING id INTO phase_id;

    SELECT rvbbit.export_to_parquet_full_scan(reloid::oid) INTO rebuilt_rows;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id > pre_max_rg_id;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_written
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id > pre_max_rg_id;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows,
           row_groups_written = accel_rebuild.row_groups_written,
           files_written = accel_rebuild.row_groups_written::integer,
           bytes_written = phase_bytes_written,
           expected_rows = rebuilt_rows,
           actual_rows = rebuilt_rows
     WHERE id = phase_id;

    DELETE FROM rvbbit.delete_log WHERE table_oid = reloid;
    DELETE FROM rvbbit.layout_variant_status WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_group_variants WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id <= pre_max_rg_id;
    IF row_groups_written > 0 THEN
        DELETE FROM rvbbit.generations
         WHERE table_oid = reloid
           AND generation <> generation_after;
    ELSE
        DELETE FROM rvbbit.generations WHERE table_oid = reloid;
        generation_after := 0;
    END IF;

    UPDATE rvbbit.tables
       SET shadow_heap_retained = true,
           shadow_heap_dirty = false,
           dirty_has_insert = false,
           dirty_has_update = false,
           dirty_has_delete = false,
           dirty_has_truncate = false,
           ctid_identity_relfilenode = CASE
               WHEN rvbbit.accel_identity_mode(reloid) = 'ctid'
               THEN pg_relation_filenode(reloid)
               ELSE ctid_identity_relfilenode
           END
     WHERE table_oid = reloid;

    DELETE FROM rvbbit.acceleration_state WHERE table_oid = reloid;

    IF orphan_paths IS NOT NULL THEN
        INSERT INTO rvbbit.orphaned_files (path, table_oid, reason, operation_id)
        SELECT DISTINCT p, reloid, 'rebuild_acceleration_staged_swap', op_id
        FROM unnest(orphan_paths) AS p
        WHERE p IS NOT NULL AND btrim(p) <> ''
        ON CONFLICT (path) DO UPDATE
           SET table_oid = EXCLUDED.table_oid,
               reason = EXCLUDED.reason,
               operation_id = EXCLUDED.operation_id,
               queued_at = clock_timestamp(),
               last_error = NULL;
        GET DIAGNOSTICS queued_orphan_files = ROW_COUNT;
    END IF;

    IF refresh_variants AND rebuilt_rows > 0 THEN
        PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
        SELECT rvbbit.refresh_layout_variants(reloid) INTO variants_rows;
        PERFORM set_config('rvbbit.acceleration_operation_id', '', true);
    END IF;

    INSERT INTO rvbbit.acceleration_state (
        table_oid,
        last_refresh_xid,
        last_refresh_generation,
        last_refresh_rows,
        last_refresh_row_groups,
        last_refresh_at,
        updated_at
    ) VALUES (
        reloid,
        safe_upper_xid,
        generation_after,
        coalesce(rebuilt_rows, 0),
        coalesce(row_groups_written, 0),
        clock_timestamp(),
        clock_timestamp()
    )
    ON CONFLICT (table_oid) DO UPDATE
       SET last_refresh_xid = EXCLUDED.last_refresh_xid,
           last_refresh_generation = EXCLUDED.last_refresh_generation,
           last_refresh_rows = EXCLUDED.last_refresh_rows,
           last_refresh_row_groups = EXCLUDED.last_refresh_row_groups,
           last_refresh_at = EXCLUDED.last_refresh_at,
           updated_at = EXCLUDED.updated_at;

    PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows,
           row_groups_written = accel_rebuild.row_groups_written,
           variants_rows = accel_rebuild.variants_rows,
           generation_after = accel_rebuild.generation_after,
           settings = settings || jsonb_build_object(
               'dropped_row_groups', dropped_rgs,
               'old_max_rg_id', pre_max_rg_id,
               'queued_orphan_files', queued_orphan_files,
               'metadata_swap', 'staged'
           )
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'operation', 'rebuild_acceleration',
        'dropped_row_groups', dropped_rgs,
        'queued_orphan_files', queued_orphan_files,
        'rows_written', rebuilt_rows,
        'row_groups_written', row_groups_written,
        'variants_rows', variants_rows,
        'generation_after', generation_after,
        'watermark_after', safe_upper_xid
    );
EXCEPTION WHEN OTHERS THEN
    IF op_id IS NOT NULL THEN
        UPDATE rvbbit.acceleration_operation_phases
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE operation_id = op_id
           AND status = 'running';
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END $$;
