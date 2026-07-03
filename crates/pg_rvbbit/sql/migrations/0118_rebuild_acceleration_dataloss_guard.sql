-- 0118_rebuild_acceleration_dataloss_guard
--
-- Pre-release audit fix (2026-07-03): rvbbit.rebuild_acceleration exported from
-- the heap and, when the export yielded zero rows, dropped every generation and
-- queued every parquet file for the reaper. On a parquet-authoritative table
-- (heap truncated via legacy compact(keep_heap := false)) that destroyed the
-- only copy of the data — reachable automatically via accel_tick's delta->full
-- escalation. This re-defines the function with an early data-loss guard; the
-- body is otherwise the committed definition verbatim.

CREATE OR REPLACE FUNCTION rvbbit.rebuild_acceleration(reloid regclass, refresh_variants boolean DEFAULT true)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $function$
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
    baseline_max_rg_id bigint := -1;
    staging_rg_base bigint := 0;
    baseline_generation bigint := 0;
    catchup_generation bigint := 0;
    safe_upper_xid numeric;
    scan_snapshot text;
    scan_upper_xid numeric;
    final_upper_xid numeric;
    phase_id bigint;
    catchup_phase_id bigint;
    phase_bytes_written bigint := 0;
    catchup_rows bigint := 0;
    catchup_row_groups bigint := 0;
    remapped_tombstones int := 0;
    queued_orphan_files int := 0;
    orphan_paths text[];
    staged_orphan_paths text[];
    final_lock_attempts int := 0;
    final_lock_attempt_timeout_ms int := 100;
    final_lock_retry_sleep_ms int := 50;
    final_lock_max_wait_ms int := 5000;
    final_lock_deadline timestamptz;
    final_lock_acquired boolean := false;
    previous_lock_timeout text;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    -- Data-loss guard (audit 2026-07-03). rebuild regenerates the accelerator
    -- FROM the heap. If the heap is not authoritative (shadow_heap_retained =
    -- false, e.g. a legacy compact(keep_heap := false) truncated it) AND
    -- accelerator row groups already exist, exporting from the empty/partial
    -- heap would delete the only surviving copy of the data. Refuse. Fresh
    -- tables have no row groups yet (guard skipped); retained-heap tables are
    -- unaffected.
    IF EXISTS (SELECT 1 FROM rvbbit.row_groups WHERE table_oid = reloid)
       AND NOT coalesce(
             (SELECT t.shadow_heap_retained FROM rvbbit.tables t
               WHERE t.table_oid = reloid),
             true) THEN
        RAISE EXCEPTION 'rvbbit.rebuild_acceleration: % has a non-authoritative (truncated) heap but existing accelerator row groups; rebuilding from the heap would destroy data. Restore the heap contents or use rvbbit.refresh_acceleration instead.', reloid;
    END IF;

    scan_snapshot := pg_current_snapshot()::text;
    scan_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmax(scan_snapshot::pg_snapshot)::text)::numeric - 1
    );
    safe_upper_xid := scan_upper_xid;

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'rebuild_acceleration', 'running',
        NULL, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'mode', 'lagged_staged_full_heap_fold',
            'heap_guard', 'none_during_baseline_scan',
            'final_guard', 'polite LOCK TABLE IN SHARE MODE polling',
            'final_lock_attempt_timeout_ms', final_lock_attempt_timeout_ms,
            'final_lock_retry_sleep_ms', final_lock_retry_sleep_ms,
            'final_lock_max_wait_ms', final_lock_max_wait_ms,
            'scan_snapshot', scan_snapshot,
            'scan_upper_xid', scan_upper_xid,
            'metadata_swap', 'post_catchup_export',
            'file_reap', 'queued_after_swap',
            'variant_refresh', CASE WHEN refresh_variants THEN 'deferred_to_maintain_storage' ELSE 'skipped' END
        )
    )
    RETURNING id INTO op_id;

    SELECT count(*)::int, coalesce(max(rg_id), -1)::bigint
      INTO dropped_rgs, pre_max_rg_id
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT greatest(coalesce(max(generation), 0) + 1, (op_id * 2) + 1)
      INTO baseline_generation
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;
    catchup_generation := baseline_generation + 1;
    staging_rg_base := greatest(pre_max_rg_id + 1, op_id * 1000000000);

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
            'mode', 'lagged_staged_full_heap_fold',
            'dropped_row_groups', dropped_rgs,
            'old_max_rg_id', pre_max_rg_id,
            'staging_rg_base', staging_rg_base,
            'baseline_generation', baseline_generation,
            'scan_snapshot', scan_snapshot
        )
    )
    RETURNING id INTO phase_id;

    SELECT rvbbit.export_to_parquet_snapshot_visible_at(
        reloid::oid,
        scan_snapshot,
        staging_rg_base,
        baseline_generation
    )
      INTO rebuilt_rows;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;
    SELECT coalesce(max(rg_id), pre_max_rg_id)::bigint
      INTO baseline_max_rg_id
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_written
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

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

    previous_lock_timeout := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', final_lock_attempt_timeout_ms::text || 'ms', true);
    final_lock_deadline := clock_timestamp()
        + ((final_lock_max_wait_ms::text || ' milliseconds')::interval);
    WHILE clock_timestamp() < final_lock_deadline LOOP
        final_lock_attempts := final_lock_attempts + 1;
        BEGIN
            EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);
            final_lock_acquired := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            PERFORM pg_sleep(final_lock_retry_sleep_ms::double precision / 1000.0);
        END;
    END LOOP;
    PERFORM set_config('lock_timeout', previous_lock_timeout, true);

    IF NOT final_lock_acquired THEN
        SELECT array_agg(path ORDER BY path)
          INTO staged_orphan_paths
          FROM (
              SELECT path FROM rvbbit.row_groups
               WHERE table_oid = reloid AND rg_id >= staging_rg_base
              UNION ALL
              SELECT path FROM rvbbit.row_group_variants
               WHERE table_oid = reloid AND rg_id >= staging_rg_base
              UNION ALL
              SELECT path FROM rvbbit.text_dictionaries
               WHERE table_oid = reloid AND rg_id >= staging_rg_base
          ) staged_files;

        DELETE FROM rvbbit.layout_variant_status WHERE table_oid = reloid;
        DELETE FROM rvbbit.row_group_variants
         WHERE table_oid = reloid
           AND rg_id >= staging_rg_base;
        DELETE FROM rvbbit.row_groups
         WHERE table_oid = reloid
           AND rg_id >= staging_rg_base;
        DELETE FROM rvbbit.generations
         WHERE table_oid = reloid
           AND NOT EXISTS (
               SELECT 1
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = rvbbit.generations.table_oid
                 AND rg.generation = rvbbit.generations.generation
           );

        IF staged_orphan_paths IS NOT NULL THEN
            INSERT INTO rvbbit.orphaned_files (path, table_oid, reason, operation_id)
            SELECT DISTINCT p, reloid, 'rebuild_acceleration_final_lock_busy', op_id
            FROM unnest(staged_orphan_paths) AS p
            WHERE p IS NOT NULL AND btrim(p) <> ''
            ON CONFLICT (path) DO UPDATE
               SET table_oid = EXCLUDED.table_oid,
                   reason = EXCLUDED.reason,
                   operation_id = EXCLUDED.operation_id,
                   queued_at = clock_timestamp(),
                   last_error = NULL;
            GET DIAGNOSTICS queued_orphan_files = ROW_COUNT;
        END IF;

        UPDATE rvbbit.acceleration_operation_phases
           SET details = details || jsonb_build_object(
                   'cleaned_up_after_final_lock_busy', true,
                   'final_lock_attempts', final_lock_attempts
               )
         WHERE id = phase_id;

        UPDATE rvbbit.acceleration_operations
           SET status = 'noop',
               finished_at = clock_timestamp(),
               rows_written = 0,
               row_groups_written = 0,
               variants_rows = NULL,
               generation_after = NULL,
               error = 'final lock busy',
               settings = settings || jsonb_build_object(
                   'final_lock_attempts', final_lock_attempts,
                   'final_lock_acquired', false,
                   'queued_orphan_files', queued_orphan_files,
                   'metadata_swap', 'skipped_final_lock_busy'
               )
         WHERE id = op_id;

        RETURN jsonb_build_object(
            'status', 'noop',
            'operation_id', op_id,
            'table', table_name_text,
            'operation', 'rebuild_acceleration',
            'reason', 'final_lock_busy',
            'final_lock_attempts', final_lock_attempts,
            'queued_orphan_files', queued_orphan_files,
            'baseline_rows', rebuilt_rows,
            'catchup_rows', 0,
            'remapped_tombstones', 0,
            'rows_written', 0,
            'row_groups_written', 0,
            'variants_rows', NULL,
            'generation_after', NULL,
            'watermark_after', scan_upper_xid
        );
    END IF;

    final_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmax(pg_current_snapshot())::text)::numeric - 1
    );
    safe_upper_xid := final_upper_xid;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_gap_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'snapshot_gap',
            'scan_snapshot', scan_snapshot,
            'baseline_max_rg_id', baseline_max_rg_id,
            'catchup_generation', catchup_generation
        )
    )
    RETURNING id INTO catchup_phase_id;

    SELECT rvbbit.export_to_parquet_snapshot_gap_at(
        reloid::oid,
        scan_snapshot,
        greatest(baseline_max_rg_id + 1, staging_rg_base),
        catchup_generation
    )
      INTO catchup_rows;

    SELECT count(*)::bigint
      INTO catchup_row_groups
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base
       AND rg_id > baseline_max_rg_id;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = catchup_rows,
           row_groups_written = catchup_row_groups,
           files_written = catchup_row_groups::integer,
           expected_rows = catchup_rows,
           actual_rows = catchup_rows
     WHERE id = catchup_phase_id;

    WITH remapped AS (
        INSERT INTO rvbbit.delete_log
            (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
        SELECT reloid,
               staged_m.rg_id,
               staged_m.ordinal,
               dl.deleted_xid,
               dl.deleted_generation
        FROM rvbbit.delete_log dl
        JOIN rvbbit.row_identity_map old_m
          ON old_m.table_oid = dl.table_oid
         AND old_m.rg_id = dl.rg_id
         AND old_m.ordinal = dl.ordinal
        JOIN rvbbit.row_identity_map staged_m
         ON staged_m.table_oid = old_m.table_oid
         AND staged_m.key_json = old_m.key_json
         AND staged_m.rg_id >= staging_rg_base
         AND staged_m.rg_id <= baseline_max_rg_id
        WHERE dl.table_oid = reloid
          AND dl.rg_id <= pre_max_rg_id
          AND NOT pg_visible_in_snapshot(dl.deleted_xid, scan_snapshot::pg_snapshot)
        ON CONFLICT (table_oid, rg_id, ordinal) DO UPDATE SET
            deleted_xid = EXCLUDED.deleted_xid,
            deleted_generation = EXCLUDED.deleted_generation
        RETURNING 1
    )
    SELECT count(*)::int INTO remapped_tombstones FROM remapped;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

    DELETE FROM rvbbit.delete_log
     WHERE table_oid = reloid
       AND rg_id <= pre_max_rg_id;
    DELETE FROM rvbbit.layout_variant_status WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_group_variants WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id <= pre_max_rg_id;
    IF row_groups_written > 0 THEN
        DELETE FROM rvbbit.generations
         WHERE table_oid = reloid
           AND NOT EXISTS (
               SELECT 1
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = rvbbit.generations.table_oid
                 AND rg.generation = rvbbit.generations.generation
           );
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
           next_generation = greatest(next_generation, generation_after + 1),
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

    variants_rows := NULL;

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
        coalesce(rebuilt_rows, 0) + coalesce(catchup_rows, 0),
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
           rows_written = rebuilt_rows + coalesce(catchup_rows, 0),
           row_groups_written = accel_rebuild.row_groups_written,
           variants_rows = accel_rebuild.variants_rows,
           generation_after = accel_rebuild.generation_after,
           watermark_after = safe_upper_xid,
           settings = settings || jsonb_build_object(
               'dropped_row_groups', dropped_rgs,
               'old_max_rg_id', pre_max_rg_id,
               'baseline_max_rg_id', baseline_max_rg_id,
               'staging_rg_base', staging_rg_base,
               'baseline_generation', baseline_generation,
               'catchup_generation', catchup_generation,
               'baseline_rows', rebuilt_rows,
               'catchup_rows', catchup_rows,
               'catchup_row_groups', catchup_row_groups,
               'remapped_tombstones', remapped_tombstones,
               'final_lock_attempts', final_lock_attempts,
               'final_lock_acquired', true,
               'queued_orphan_files', queued_orphan_files,
               'metadata_swap', 'lagged_staged',
               'watermark_after', safe_upper_xid
           )
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'operation', 'rebuild_acceleration',
        'dropped_row_groups', dropped_rgs,
        'queued_orphan_files', queued_orphan_files,
        'baseline_rows', rebuilt_rows,
        'catchup_rows', catchup_rows,
        'remapped_tombstones', remapped_tombstones,
        'rows_written', rebuilt_rows + coalesce(catchup_rows, 0),
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
END $function$

