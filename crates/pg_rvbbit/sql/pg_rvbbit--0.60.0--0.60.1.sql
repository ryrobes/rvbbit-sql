-- pg_rvbbit 0.60.0 -> 0.60.1
-- SQL-visible acceleration phase observability and Hive delta-append refresh.

CREATE TABLE IF NOT EXISTS rvbbit.acceleration_operation_phases (
    id                  bigserial PRIMARY KEY,
    operation_id        bigint REFERENCES rvbbit.acceleration_operations(id) ON DELETE CASCADE,
    table_oid           oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name          text NOT NULL,
    phase               text NOT NULL,
    layout              text,
    partition_key       text,
    status              text NOT NULL DEFAULT 'running',
    started_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at         timestamptz,
    rows_written        bigint,
    row_groups_written  bigint,
    bytes_written       bigint,
    files_written       integer,
    expected_rows       bigint,
    actual_rows         bigint,
    details             jsonb NOT NULL DEFAULT '{}'::jsonb,
    error               text,
    CHECK (status IN ('running', 'ok', 'failed', 'invalid', 'skipped')),
    CHECK (rows_written IS NULL OR rows_written >= 0),
    CHECK (row_groups_written IS NULL OR row_groups_written >= 0),
    CHECK (bytes_written IS NULL OR bytes_written >= 0),
    CHECK (files_written IS NULL OR files_written >= 0),
    CHECK (expected_rows IS NULL OR expected_rows >= 0),
    CHECK (actual_rows IS NULL OR actual_rows >= 0)
);

CREATE INDEX IF NOT EXISTS acceleration_operation_phases_operation_idx
    ON rvbbit.acceleration_operation_phases (operation_id, started_at);

CREATE INDEX IF NOT EXISTS acceleration_operation_phases_table_started_idx
    ON rvbbit.acceleration_operation_phases (table_oid, started_at DESC);

CREATE OR REPLACE FUNCTION rvbbit.refresh_layout_variants_xid_range(
    rel oid,
    min_xid text,
    max_xid text
) RETURNS bigint
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'refresh_layout_variants_xid_range_wrapper';

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
    safe_upper_xid numeric;
    phase_id bigint;
    phase_bytes_written bigint := 0;
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
            'mode', 'full_heap_rebuild',
            'heap_guard', 'LOCK TABLE IN SHARE MODE'
        )
    )
    RETURNING id INTO op_id;

    SELECT count(*)::int INTO dropped_rgs
      FROM rvbbit.row_groups WHERE table_oid = reloid;

    DELETE FROM rvbbit.delete_log         WHERE table_oid = reloid;
    DELETE FROM rvbbit.layout_variant_status WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_group_variants WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_groups         WHERE table_oid = reloid;
    DELETE FROM rvbbit.generations        WHERE table_oid = reloid;
    UPDATE rvbbit.tables
       SET next_generation = 1,
           shadow_heap_retained = true,
           shadow_heap_dirty = false
     WHERE table_oid = reloid;
    DELETE FROM rvbbit.acceleration_state WHERE table_oid = reloid;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_full_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'full_heap_rebuild',
            'dropped_row_groups', dropped_rgs
        )
    )
    RETURNING id INTO phase_id;

    SELECT rvbbit.export_to_parquet_full_scan(reloid::oid) INTO rebuilt_rows;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_written
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

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

    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows,
           row_groups_written = accel_rebuild.row_groups_written,
           variants_rows = accel_rebuild.variants_rows,
           generation_after = accel_rebuild.generation_after,
           settings = settings || jsonb_build_object('dropped_row_groups', dropped_rgs)
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'operation', 'rebuild_acceleration',
        'dropped_row_groups', dropped_rgs,
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

CREATE OR REPLACE FUNCTION rvbbit.refresh_acceleration(
    reloid regclass,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql AS $$
<<accel_refresh>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    last_xid numeric;
    safe_upper_xid numeric;
    rows_written bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    max_rg_id_pre bigint;
    existing_rgs bigint;
    generation_after bigint := 0;
    shadow_retained boolean := false;
    shadow_dirty boolean := false;
    heap_bytes bigint := 0;
    phase_id bigint;
    phase_bytes_before bigint := 0;
    phase_bytes_after bigint := 0;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);

    INSERT INTO rvbbit.acceleration_state (table_oid)
    VALUES (reloid)
    ON CONFLICT (table_oid) DO NOTHING;

    SELECT s.last_refresh_xid
      INTO last_xid
      FROM rvbbit.acceleration_state s
     WHERE s.table_oid = reloid
     FOR UPDATE;

    safe_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1
    );

    SELECT count(*)::bigint, coalesce(max(rg_id), -1)::bigint,
           coalesce(max(generation), 0)::bigint
      INTO existing_rgs, max_rg_id_pre, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(t.shadow_heap_retained, false),
           coalesce(t.shadow_heap_dirty, false)
      INTO shadow_retained, shadow_dirty
      FROM rvbbit.tables t
     WHERE t.table_oid = reloid;

    heap_bytes := pg_relation_size(reloid);

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

    IF last_xid = 0 AND existing_rgs > 0 AND heap_bytes > 0 THEN
        IF shadow_retained AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false
             WHERE table_oid = reloid;
            EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
            EXECUTE format(
                'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
                reloid
            );
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
                   shadow_heap_dirty = false
             WHERE table_oid = reloid;
            EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
            EXECUTE format(
                'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
                reloid
            );
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

    SELECT rvbbit.export_to_parquet_xid_range(
        reloid::oid,
        last_xid::text,
        safe_upper_xid::text
    ) INTO rows_written;

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

    IF existing_rgs > 0 OR row_groups_written > 0 THEN
        UPDATE rvbbit.tables
           SET shadow_heap_retained = true,
               shadow_heap_dirty = false
         WHERE table_oid = reloid;
        EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
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
$$;

DROP FUNCTION IF EXISTS rvbbit.layout_variant_status_for(regclass);

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
