-- PK-backed tombstone overlay for mutable dirty episodes.
--
-- This is the first LSM-style layer over immutable row groups:
--   * canonical exports write a side map from primary-key JSON -> (rg_id, ordinal);
--   * UPDATE/DELETE transition-table triggers translate OLD rows into delete_log tombstones;
--   * refresh_acceleration can delta-export mutable episodes when that side map is complete.
-- Tables without a primary key, or with pre-overlay row groups that lack map entries, keep
-- the explicit rebuild requirement.

CREATE TABLE IF NOT EXISTS rvbbit.row_identity_map (
    table_oid       oid NOT NULL,
    key_json        text NOT NULL,
    rg_id           bigint NOT NULL,
    ordinal         int NOT NULL,
    generation      bigint NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, key_json, rg_id, ordinal),
    FOREIGN KEY (table_oid, rg_id)
        REFERENCES rvbbit.row_groups(table_oid, rg_id)
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS row_identity_map_lookup_idx
    ON rvbbit.row_identity_map (table_oid, key_json);

CREATE OR REPLACE FUNCTION rvbbit.accel_identity_columns(reloid regclass)
RETURNS text[]
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(array_agg(a.attname::text ORDER BY k.ord), ARRAY[]::text[])
    FROM pg_index i
    JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a
      ON a.attrelid = i.indrelid
     AND a.attnum = k.attnum
    WHERE i.indrelid = reloid
      AND i.indisprimary
      AND i.indisvalid
      AND i.indpred IS NULL
      AND i.indexprs IS NULL
      AND NOT a.attisdropped
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_identity_expr(
    reloid regclass,
    rel_alias text DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    cols text[];
    expr text;
BEGIN
    cols := rvbbit.accel_identity_columns(reloid);
    IF cols IS NULL OR cardinality(cols) = 0 THEN
        RETURN NULL;
    END IF;

    SELECT string_agg(
               CASE
                   WHEN rel_alias IS NULL OR btrim(rel_alias) = ''
                   THEN format('%I', col)
                   ELSE format('%I.%I', rel_alias, col)
               END,
               ', ' ORDER BY ord
           )
      INTO expr
      FROM unnest(cols) WITH ORDINALITY AS c(col, ord);

    RETURN format('jsonb_build_array(%s)::text', expr);
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_overlay_ready(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT cardinality(rvbbit.accel_identity_columns(reloid)) > 0
       AND coalesce((
               SELECT count(*)::bigint
               FROM rvbbit.row_identity_map m
               WHERE m.table_oid = reloid
           ), 0) >= coalesce((
               SELECT sum(rg.n_rows)::bigint
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = reloid
           ), 0)
$$;

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    key_expr text;
    tombstone_gen bigint := 0;
    overlay_ready boolean := false;
BEGIN
    UPDATE rvbbit.tables
    SET shadow_heap_dirty = true,
        dirty_has_insert = dirty_has_insert OR TG_OP = 'INSERT',
        dirty_has_update = dirty_has_update OR TG_OP = 'UPDATE',
        dirty_has_delete = dirty_has_delete OR TG_OP = 'DELETE',
        dirty_has_truncate = dirty_has_truncate OR TG_OP = 'TRUNCATE',
        last_write_at = clock_timestamp(),
        dirty_since = CASE WHEN shadow_heap_dirty THEN dirty_since ELSE clock_timestamp() END
    WHERE table_oid = TG_RELID
      AND shadow_heap_retained;

    overlay_ready := rvbbit.accel_overlay_ready(TG_RELID);
    IF TG_OP IN ('UPDATE', 'DELETE') AND overlay_ready THEN
        key_expr := rvbbit.accel_identity_expr(TG_RELID, 'rvbbit_old_rows');
        IF key_expr IS NOT NULL THEN
            tombstone_gen := rvbbit.allocate_generation(TG_RELID);
            EXECUTE format(
                'WITH old_keys AS (
                     SELECT DISTINCT %1$s AS key_json
                     FROM rvbbit_old_rows
                 )
                 INSERT INTO rvbbit.delete_log
                     (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
                 SELECT $1::oid, m.rg_id, m.ordinal, pg_current_xact_id(), $2
                 FROM old_keys k
                 JOIN rvbbit.row_identity_map m
                   ON m.table_oid = $1::oid
                  AND m.key_json = k.key_json
                 ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING',
                key_expr
            ) USING TG_RELID, tombstone_gen;
        END IF;
    ELSIF TG_OP = 'TRUNCATE' AND overlay_ready THEN
        tombstone_gen := rvbbit.allocate_generation(TG_RELID);
        INSERT INTO rvbbit.delete_log
            (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
        SELECT TG_RELID, m.rg_id, m.ordinal, pg_current_xact_id(), tombstone_gen
        FROM rvbbit.row_identity_map m
        WHERE m.table_oid = TG_RELID
        ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING;
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.install_shadow_heap_dirty_triggers(reloid regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_insert ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_update ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_delete ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_truncate ON %s', reloid);

    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_insert
             AFTER INSERT ON %s
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_update
             AFTER UPDATE ON %s
             REFERENCING OLD TABLE AS rvbbit_old_rows
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_delete
             AFTER DELETE ON %s
             REFERENCING OLD TABLE AS rvbbit_old_rows
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_truncate
             AFTER TRUNCATE ON %s
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
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
           coalesce(t.shadow_heap_dirty, false),
           coalesce(t.dirty_has_update, false),
           coalesce(t.dirty_has_delete, false),
           coalesce(t.dirty_has_truncate, false)
      INTO shadow_retained, shadow_dirty, dirty_update, dirty_delete, dirty_truncate
      FROM rvbbit.tables t
     WHERE t.table_oid = reloid;

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
               shadow_heap_dirty = false,
               dirty_has_insert = false,
               dirty_has_update = false,
               dirty_has_delete = false,
               dirty_has_truncate = false
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

DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT table_oid::regclass AS reloid
        FROM rvbbit.tables
        WHERE shadow_heap_retained
    LOOP
        PERFORM rvbbit.install_shadow_heap_dirty_triggers(rec.reloid);
    END LOOP;
END;
$$;
