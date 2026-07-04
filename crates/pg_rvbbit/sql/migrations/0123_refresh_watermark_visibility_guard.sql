-- 0123_refresh_watermark_visibility_guard
--
-- Pre-release audit fix (2026-07-03): refresh_acceleration set its export ceiling
-- to pg_snapshot_xmin-1 (the DB-wide oldest active xid) and then cleared the
-- dirty flag UNCONDITIONALLY. When an unrelated long-lived snapshot pinned
-- pg_snapshot_xmin, rows already committed to the table sat above the ceiling,
-- were never exported, and the dirty-clear both dropped them from accelerated
-- reads and disabled the heap-tail overlay -> silent wrong results (reproduced:
-- a refresh under a pin returned 100 of 200 committed rows). This adds a
-- pending-above-ceiling probe that keeps the table dirty (overlay serves the
-- rows; freshness plane retries) instead of clearing. Body is otherwise the
-- committed definition verbatim.

CREATE OR REPLACE FUNCTION rvbbit.refresh_acceleration(reloid regclass, refresh_variants boolean DEFAULT true)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $function$
<<accel_refresh>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    last_xid numeric;
    safe_upper_xid numeric;
    frontier_fxid numeric := 0;
    has_pending_above boolean := false;
    rows_written bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    max_rg_id_pre bigint;
    existing_rgs bigint;
    generation_after bigint := 0;
    shadow_retained boolean := false;
    shadow_dirty boolean := false;
    dirty_update boolean := false;
    dirty_delete boolean := false;
    dirty_truncate boolean := false;
    overlay_ready boolean := false;
    heap_bytes bigint := 0;
    phase_id bigint;
    phase_bytes_before bigint := 0;
    phase_bytes_after bigint := 0;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    -- The refresh watermark is a correctness boundary. Block writers while
    -- we snapshot/export the safe heap range, then install the dirty trigger
    -- before releasing the lock at transaction end.
    EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);

    INSERT INTO rvbbit.acceleration_state (table_oid)
    VALUES (reloid)
    ON CONFLICT (table_oid) DO NOTHING;

    SELECT s.last_refresh_xid
      INTO last_xid
      FROM rvbbit.acceleration_state s
     WHERE s.table_oid = reloid
     FOR UPDATE;

    -- pg_snapshot_xmin is the oldest still-active xid in this snapshot.
    -- XIDs below it are complete, so rows in that range are safe to mark
    -- accelerated without skipping concurrent transactions that commit later.
    safe_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1
    );

    -- The live XID frontier (highest possibly-assigned xid). When an unrelated
    -- long-lived snapshot pins pg_snapshot_xmin far below this, safe_upper_xid is
    -- held back below rows that are already committed to THIS table.
    frontier_fxid := greatest(
        0::numeric,
        (pg_snapshot_xmax(pg_current_snapshot())::text)::numeric - 1
    );

    SELECT count(*)::bigint, coalesce(max(rg_id), -1)::bigint,
           coalesce(max(generation), 0)::bigint
      INTO existing_rgs, max_rg_id_pre, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(ds.shadow_heap_retained, false),
           coalesce(ds.shadow_heap_dirty, false),
           coalesce(ds.dirty_has_update, false),
           coalesce(ds.dirty_has_delete, false),
           coalesce(ds.dirty_has_truncate, false)
      INTO shadow_retained, shadow_dirty, dirty_update, dirty_delete, dirty_truncate
      FROM rvbbit.table_dirty_state ds
     WHERE ds.table_oid = reloid;

    heap_bytes := pg_relation_size(reloid);
    overlay_ready := rvbbit.accel_overlay_ready(reloid);

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'refresh_acceleration', 'running',
        last_xid, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'watermark', 'heap xmin <= pg_snapshot_xmin(pg_current_snapshot()) - 1',
            'heap_guard', 'LOCK TABLE IN SHARE MODE'
        )
    )
    RETURNING id INTO op_id;

    IF existing_rgs > 0
       AND shadow_dirty
       AND (dirty_update OR dirty_delete OR dirty_truncate)
       AND NOT overlay_ready THEN
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = 'non-append dirty episode requires rebuild or complete row identity overlay',
               settings = settings || jsonb_build_object(
                   'dirty_has_update', dirty_update,
                   'dirty_has_delete', dirty_delete,
                   'dirty_has_truncate', dirty_truncate,
                   'overlay_ready', overlay_ready,
                   'recommended_action', 'rebuild_acceleration'
               )
         WHERE id = op_id;
        RAISE EXCEPTION
            'rvbbit.refresh_acceleration: % has UPDATE/DELETE/TRUNCATE changes since the last refresh; run rvbbit.rebuild_acceleration(%) or use an overlay-capable path',
            reloid, quote_literal(reloid::text);
    END IF;

    IF last_xid = 0 AND existing_rgs > 0 AND heap_bytes > 0 THEN
        IF shadow_retained AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false,
                   dirty_has_insert = false,
                   dirty_has_update = false,
                   dirty_has_delete = false,
                   dirty_has_truncate = false
             WHERE table_oid = reloid;
            PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
            PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
            UPDATE rvbbit.acceleration_state
               SET last_refresh_xid = safe_upper_xid,
                   last_refresh_generation = generation_after,
                   last_refresh_at = clock_timestamp(),
                   updated_at = clock_timestamp()
             WHERE table_oid = reloid;
            UPDATE rvbbit.acceleration_operations
               SET status = 'noop',
                   finished_at = clock_timestamp(),
                   rows_written = 0,
                   row_groups_written = 0,
                   generation_after = accel_refresh.generation_after,
                   settings = settings || jsonb_build_object('bootstrap', 'clean shadow heap already covered by existing row groups')
             WHERE id = op_id;
            RETURN jsonb_build_object(
                'status', 'noop',
                'operation_id', op_id,
                'table', table_name_text,
                'watermark_before', last_xid,
                'watermark_after', safe_upper_xid,
                'rows_written', 0,
                'row_groups_written', 0,
                'bootstrap', true
            );
        ELSIF shadow_dirty THEN
            RAISE EXCEPTION
                'rvbbit.refresh_acceleration: % has existing row groups and a dirty retained heap; run rvbbit.rebuild_acceleration(%) before incremental refresh',
                reloid, quote_literal(reloid::text);
        END IF;
    END IF;

    IF safe_upper_xid <= last_xid THEN
        IF existing_rgs > 0 AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false,
                   dirty_has_insert = false,
                   dirty_has_update = false,
                   dirty_has_delete = false,
                   dirty_has_truncate = false
             WHERE table_oid = reloid;
            PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
            PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
        END IF;
        UPDATE rvbbit.acceleration_operations
           SET status = 'noop',
               finished_at = clock_timestamp(),
               rows_written = 0,
               row_groups_written = 0,
               generation_after = accel_refresh.generation_after
         WHERE id = op_id;
        RETURN jsonb_build_object(
            'status', 'noop',
            'operation_id', op_id,
            'table', table_name_text,
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid,
            'rows_written', 0,
            'row_groups_written', 0
        );
    END IF;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_before
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_delta_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'watermark_delta',
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid
        )
    )
    RETURNING id INTO phase_id;

    PERFORM set_config('rvbbit.acceleration_phase_id', phase_id::text, true);
    SELECT rvbbit.export_to_parquet_xid_range(
        reloid::oid,
        last_xid::text,
        safe_upper_xid::text
    ) INTO rows_written;
    PERFORM set_config('rvbbit.acceleration_phase_id', '', true);

    SELECT count(*)::bigint, coalesce(max(generation), generation_after)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id > max_rg_id_pre;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = accel_refresh.rows_written,
           row_groups_written = accel_refresh.row_groups_written,
           files_written = accel_refresh.row_groups_written::integer,
           bytes_written = greatest(0, phase_bytes_after - phase_bytes_before),
           expected_rows = accel_refresh.rows_written,
           actual_rows = accel_refresh.rows_written
     WHERE id = phase_id;

    IF refresh_variants AND rows_written > 0 THEN
        PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
        SELECT rvbbit.refresh_layout_variants_xid_range(
            reloid::oid,
            last_xid::text,
            safe_upper_xid::text
        ) INTO variants_rows;
        PERFORM set_config('rvbbit.acceleration_operation_id', '', true);
    END IF;

    -- Watermark-visibility guard (audit 2026-07-03): if an unrelated long-lived
    -- snapshot pinned pg_snapshot_xmin (thus safe_upper_xid) below rows that are
    -- already committed to this table, those rows sit ABOVE the export ceiling
    -- and were not captured. Clearing the dirty flag would drop them from every
    -- accelerated route AND disable the heap-tail overlay -> silent row loss
    -- until the next unrelated write. Detect that and KEEP the table dirty: the
    -- overlay serves the pending rows and the freshness plane retries once the
    -- pin clears and safe_upper_xid rises. Probe only when the ceiling was held
    -- back (frontier check cheap-outs the common no-pin path).
    has_pending_above := false;
    IF shadow_dirty AND safe_upper_xid < frontier_fxid THEN
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s WHERE rvbbit.xid_to_fxid(xmin) > %s::numeric)',
            reloid::text, safe_upper_xid::text
        ) INTO has_pending_above;
    END IF;

    IF existing_rgs > 0 OR row_groups_written > 0 THEN
        IF has_pending_above THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true
             WHERE table_oid = reloid;
            PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
        ELSE
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false,
                   dirty_has_insert = false,
                   dirty_has_update = false,
                   dirty_has_delete = false,
                   dirty_has_truncate = false
             WHERE table_oid = reloid;
            PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
            PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
        END IF;
    END IF;

    UPDATE rvbbit.acceleration_state
       SET last_refresh_xid = safe_upper_xid,
           last_refresh_generation = generation_after,
           last_refresh_rows = coalesce(last_refresh_rows, 0) + coalesce(rows_written, 0),
           last_refresh_row_groups = coalesce(last_refresh_row_groups, 0) + coalesce(row_groups_written, 0),
           last_refresh_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = accel_refresh.rows_written,
           row_groups_written = accel_refresh.row_groups_written,
           variants_rows = accel_refresh.variants_rows,
           generation_after = accel_refresh.generation_after
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'watermark_before', last_xid,
        'watermark_after', safe_upper_xid,
        'rows_written', rows_written,
        'row_groups_written', row_groups_written,
        'variants_rows', variants_rows,
        'generation_after', generation_after
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
END;
$function$;
