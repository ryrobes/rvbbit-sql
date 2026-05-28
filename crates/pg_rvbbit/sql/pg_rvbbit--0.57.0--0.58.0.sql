-- pg_rvbbit 0.57.0 -> 0.58.0
-- SQL-visible acceleration refresh state and operation observability.

CREATE TABLE IF NOT EXISTS rvbbit.acceleration_state (
    table_oid                 oid PRIMARY KEY REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    last_refresh_xid          numeric NOT NULL DEFAULT 0,
    last_refresh_generation   bigint NOT NULL DEFAULT 0,
    last_refresh_rows         bigint NOT NULL DEFAULT 0,
    last_refresh_row_groups   bigint NOT NULL DEFAULT 0,
    last_refresh_at           timestamptz,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CHECK (last_refresh_xid >= 0),
    CHECK (last_refresh_generation >= 0),
    CHECK (last_refresh_rows >= 0),
    CHECK (last_refresh_row_groups >= 0)
);

CREATE TABLE IF NOT EXISTS rvbbit.acceleration_operations (
    id                  bigserial PRIMARY KEY,
    table_oid           oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name          text NOT NULL,
    operation           text NOT NULL,
    status              text NOT NULL DEFAULT 'running',
    started_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at         timestamptz,
    watermark_before    numeric,
    watermark_after     numeric,
    rows_written        bigint,
    row_groups_written  bigint,
    variants_rows       bigint,
    generation_after    bigint,
    settings            jsonb NOT NULL DEFAULT '{}'::jsonb,
    error               text,
    CHECK (operation IN ('refresh_acceleration', 'rebuild_acceleration', 'compact_acceleration', 'legacy_compact')),
    CHECK (status IN ('running', 'ok', 'failed', 'noop')),
    CHECK (watermark_before IS NULL OR watermark_before >= 0),
    CHECK (watermark_after IS NULL OR watermark_after >= 0),
    CHECK (rows_written IS NULL OR rows_written >= 0),
    CHECK (row_groups_written IS NULL OR row_groups_written >= 0),
    CHECK (variants_rows IS NULL OR variants_rows >= 0),
    CHECK (generation_after IS NULL OR generation_after >= 0)
);

CREATE INDEX IF NOT EXISTS acceleration_operations_table_started_idx
    ON rvbbit.acceleration_operations (table_oid, started_at DESC);

CREATE INDEX IF NOT EXISTS acceleration_operations_status_idx
    ON rvbbit.acceleration_operations (status, started_at DESC);

CREATE OR REPLACE FUNCTION rvbbit.export_to_parquet_xid_range(
    rel oid,
    min_xid text,
    max_xid text
) RETURNS bigint
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'export_to_parquet_xid_range_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.export_to_parquet_full_scan(
    rel oid
) RETURNS bigint
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'export_to_parquet_full_scan_wrapper';

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
    DELETE FROM rvbbit.row_group_variants WHERE table_oid = reloid;
    DELETE FROM rvbbit.row_groups         WHERE table_oid = reloid;
    DELETE FROM rvbbit.generations        WHERE table_oid = reloid;
    UPDATE rvbbit.tables
       SET next_generation = 1,
           shadow_heap_retained = true,
           shadow_heap_dirty = false
     WHERE table_oid = reloid;
    DELETE FROM rvbbit.acceleration_state WHERE table_oid = reloid;

    SELECT rvbbit.export_to_parquet_full_scan(reloid::oid) INTO rebuilt_rows;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    IF refresh_variants AND rebuilt_rows > 0 THEN
        SELECT rvbbit.refresh_layout_variants(reloid) INTO variants_rows;
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
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END;
$$;

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

    IF refresh_variants AND rows_written > 0 THEN
        SELECT rvbbit.refresh_layout_variants(reloid) INTO variants_rows;
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
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END;
$$;

CREATE OR REPLACE VIEW rvbbit.acceleration_status AS
SELECT
    t.table_oid,
    c.oid::regclass::text AS table_name,
    coalesce(s.last_refresh_xid, 0) AS last_refresh_xid,
    s.last_refresh_at,
    coalesce(s.last_refresh_generation, 0) AS last_refresh_generation,
    coalesce(s.last_refresh_rows, 0) AS last_refresh_rows,
    coalesce(s.last_refresh_row_groups, 0) AS last_refresh_row_groups,
    coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid), 0) AS parquet_rows,
    coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid), 0) AS row_groups,
    pg_relation_size(t.table_oid)::bigint AS heap_bytes,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
    (
        pg_relation_size(t.table_oid) = 0
        OR coalesce(t.shadow_heap_retained AND NOT t.shadow_heap_dirty, false)
    ) AS parquet_authoritative,
    (SELECT max(o.started_at) FROM rvbbit.acceleration_operations o WHERE o.table_oid = t.table_oid) AS last_operation_at
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
LEFT JOIN rvbbit.acceleration_state s ON s.table_oid = t.table_oid;

CREATE OR REPLACE FUNCTION rvbbit.compact(rel regclass)
RETURNS TABLE (rg_id bigint, n_rows bigint, n_bytes bigint, heap_freed_bytes bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    max_rg_id_pre bigint;
    _result jsonb;
BEGIN
    SELECT COALESCE(max(rg.rg_id), -1)
    INTO max_rg_id_pre
    FROM rvbbit.row_groups rg
    WHERE rg.table_oid = rel;

    SELECT rvbbit.refresh_acceleration(
        rel,
        lower(coalesce(current_setting('rvbbit.compact_refresh_variants', true), 'off'))
            IN ('1', 'true', 'on', 'yes')
    ) INTO _result;

    RETURN QUERY
        SELECT rg.rg_id, rg.n_rows, rg.n_bytes, 0::bigint
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = rel
          AND rg.rg_id > max_rg_id_pre
        ORDER BY rg.rg_id DESC;
END;
$$;
