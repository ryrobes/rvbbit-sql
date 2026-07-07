-- 0131_refresh_same_txn_pending_visibility
--
-- Launch-eve fix (2026-07-07, reproduced on two virgin 3.0.2 boxes): calling
-- refresh_acceleration in the SAME transaction as the table's writes (psql -c
-- multi-statement batches, GUI clients that wrap scripts in a transaction,
-- ORMs) makes pg_snapshot_xmin = our own xid, so the export ceiling
-- (safe_upper_xid = xmin - 1) sits below rows this very transaction wrote.
-- The 0123 pending-above-ceiling probe never fired for this shape because it
-- was gated on shadow_dirty, which is false on a FIRST refresh (dirty
-- triggers are only installed further down paths that a zero-export first
-- refresh never reaches). Result: refresh returns status=ok/rows_written=0,
-- a second same-transaction call returns noop, and the table looks refreshed
-- while row_groups = 0 — silent non-acceleration with no retry breadcrumb
-- (heap serves queries, so results stay correct but nothing accelerates).
--
-- This migration replaces refresh_acceleration with the 0123 body plus:
--   1. the pending probe also fires when a first refresh exports nothing
--      while the heap has bytes (no shadow_dirty requirement),
--   2. a sub-probe distinguishes "pending rows were written by THIS
--      transaction" and says so in the hint,
--   3. when rows are pending and nothing is exported yet, the table is
--      marked dirty (dirty_has_insert) and the dirty triggers are installed,
--      so the freshness plane retries the refresh once the writes commit,
--   4. a RAISE WARNING plus pending_above_ceiling/hint fields in the return
--      payload on both the export and noop paths — no more silent "ok".

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
    pending_own_txn boolean := false;
    pending_hint text := NULL;
    prev_force_heap text;
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
    -- 0131: include our OWN assigned xid — when this transaction is the only
    -- active one, pg_snapshot_xmax does NOT cover it (xmin = xmax), which made
    -- the safe_upper < frontier cheap-out silently skip the pending probe for
    -- the same-transaction-write shape.
    frontier_fxid := greatest(
        0::numeric,
        (pg_snapshot_xmax(pg_current_snapshot())::text)::numeric - 1,
        coalesce((pg_current_xact_id_if_assigned()::text)::numeric, 0)
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
        -- Same-transaction / pinned-ceiling probe (0131): a noop can hide rows
        -- that sit above a ceiling which cannot rise inside this transaction.
        IF safe_upper_xid < frontier_fxid
           AND (shadow_dirty OR (existing_rgs = 0 AND heap_bytes > 0)) THEN
            -- xmin is a heap system column; force the heap path so the probe
            -- cannot be routed through a custom scan (restored right after).
            prev_force_heap := coalesce(current_setting('rvbbit.force_heap_scan', true), '');
            PERFORM set_config('rvbbit.force_heap_scan', 'on', true);
            EXECUTE format(
                'SELECT EXISTS (SELECT 1 FROM %s WHERE rvbbit.xid_to_fxid(xmin) > %s::numeric)',
                reloid::text, safe_upper_xid::text
            ) INTO has_pending_above;
            PERFORM set_config('rvbbit.force_heap_scan', prev_force_heap, true);
        END IF;
        IF has_pending_above THEN
            prev_force_heap := coalesce(current_setting('rvbbit.force_heap_scan', true), '');
            PERFORM set_config('rvbbit.force_heap_scan', 'on', true);
            EXECUTE format(
                'SELECT EXISTS (SELECT 1 FROM %s WHERE xmin = pg_current_xact_id_if_assigned()::xid)',
                reloid::text
            ) INTO pending_own_txn;
            PERFORM set_config('rvbbit.force_heap_scan', prev_force_heap, true);
            IF pending_own_txn THEN
                pending_hint :=
                    'rows written by this same transaction cannot be exported; '
                    'commit the writes first (do not batch INSERTs and '
                    'refresh_acceleration in one transaction or multi-statement '
                    'batch), then re-run refresh_acceleration';
            ELSE
                pending_hint :=
                    'rows are committed above the current visibility ceiling '
                    '(a long-lived snapshot is holding it down); they remain '
                    'served from the heap and the freshness plane will retry';
            END IF;
            IF existing_rgs = 0 THEN
                UPDATE rvbbit.tables
                   SET shadow_heap_retained = true,
                       shadow_heap_dirty = true,
                       dirty_has_insert = true
                 WHERE table_oid = reloid;
                PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
            END IF;
            RAISE WARNING 'rvbbit.refresh_acceleration(%): no rows exported — %',
                table_name_text, pending_hint;
        END IF;
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
               generation_after = accel_refresh.generation_after,
               settings = settings || jsonb_build_object(
                   'pending_above_ceiling', has_pending_above,
                   'pending_own_txn', pending_own_txn
               )
         WHERE id = op_id;
        RETURN jsonb_build_object(
            'status', 'noop',
            'operation_id', op_id,
            'table', table_name_text,
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid,
            'rows_written', 0,
            'row_groups_written', 0,
            'pending_above_ceiling', has_pending_above,
            'hint', pending_hint
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

    -- Watermark-visibility guard (audit 2026-07-03, widened 2026-07-07): if the
    -- export ceiling was held below the live XID frontier — by an unrelated
    -- long-lived snapshot OR by this very transaction having written the rows —
    -- committed/visible-soon rows sit ABOVE the ceiling and were not captured.
    -- Clearing the dirty flag would drop them from every accelerated route AND
    -- disable the heap-tail overlay -> silent row loss until the next unrelated
    -- write. Detect that and KEEP the table dirty: the overlay serves the
    -- pending rows and the freshness plane retries once the ceiling rises.
    -- The probe also fires (0131) when a FIRST refresh exports nothing while
    -- the heap has bytes — the same-transaction shape — which the shadow_dirty
    -- gate alone used to miss.
    has_pending_above := false;
    IF safe_upper_xid < frontier_fxid
       AND (shadow_dirty
            OR (rows_written = 0 AND existing_rgs = 0 AND heap_bytes > 0)) THEN
        -- xmin is a heap system column; force the heap path so the probe
        -- cannot be routed through a custom scan (restored right after).
        prev_force_heap := coalesce(current_setting('rvbbit.force_heap_scan', true), '');
        PERFORM set_config('rvbbit.force_heap_scan', 'on', true);
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s WHERE rvbbit.xid_to_fxid(xmin) > %s::numeric)',
            reloid::text, safe_upper_xid::text
        ) INTO has_pending_above;
        PERFORM set_config('rvbbit.force_heap_scan', prev_force_heap, true);
    END IF;

    IF has_pending_above THEN
        prev_force_heap := coalesce(current_setting('rvbbit.force_heap_scan', true), '');
        PERFORM set_config('rvbbit.force_heap_scan', 'on', true);
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s WHERE xmin = pg_current_xact_id_if_assigned()::xid)',
            reloid::text
        ) INTO pending_own_txn;
        PERFORM set_config('rvbbit.force_heap_scan', prev_force_heap, true);
        IF pending_own_txn THEN
            pending_hint :=
                'rows written by this same transaction cannot be exported; '
                'commit the writes first (do not batch INSERTs and '
                'refresh_acceleration in one transaction or multi-statement '
                'batch), then re-run refresh_acceleration';
        ELSE
            pending_hint :=
                'rows are committed above the current visibility ceiling '
                '(a long-lived snapshot is holding it down); they remain '
                'served from the heap and the freshness plane will retry';
        END IF;
        RAISE WARNING 'rvbbit.refresh_acceleration(%): % row(s) group(s) exported with rows still pending — %',
            table_name_text, row_groups_written, pending_hint;
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
    ELSIF has_pending_above THEN
        -- Nothing exported yet and rows are pending above the ceiling
        -- (typically: refresh ran inside the transaction that wrote them).
        -- Leave a retry breadcrumb so the freshness plane re-runs this
        -- refresh after commit, and start tracking dirtiness now.
        UPDATE rvbbit.tables
           SET shadow_heap_retained = true,
               shadow_heap_dirty = true,
               dirty_has_insert = true
         WHERE table_oid = reloid;
        PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
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
           generation_after = accel_refresh.generation_after,
           settings = settings || jsonb_build_object(
               'pending_above_ceiling', has_pending_above,
               'pending_own_txn', pending_own_txn
           )
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
        'generation_after', generation_after,
        'pending_above_ceiling', has_pending_above,
        'hint', pending_hint
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
