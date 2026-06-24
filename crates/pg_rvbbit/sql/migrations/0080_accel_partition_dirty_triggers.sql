-- Parent-routed DML on declarative partitions fires row triggers on the child
-- partition, but not statement triggers installed directly on that child. Use
-- partition-only row triggers so accelerated partitions are dirtied and
-- tombstoned when users mutate the partitioned parent.

CREATE OR REPLACE FUNCTION rvbbit.accel_identity_json_from_row(
    reloid regclass,
    row_data jsonb
) RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    cols text[];
    key_json jsonb;
BEGIN
    cols := rvbbit.accel_identity_columns(reloid);
    IF cols IS NULL OR cardinality(cols) = 0 THEN
        RETURN NULL;
    END IF;

    SELECT jsonb_agg(row_data -> col ORDER BY ord)
      INTO key_json
      FROM unnest(cols) WITH ORDINALITY AS c(col, ord);

    RETURN key_json::text;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_dirty_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    marker_op char(1);
    marker_shard smallint;
    identity_mode text;
    overlay_ready boolean := false;
    old_key_json text;
    gen_setting text;
    tombstone_gen bigint;
BEGIN
    marker_op := CASE TG_OP
        WHEN 'INSERT' THEN 'I'
        WHEN 'UPDATE' THEN 'U'
        WHEN 'DELETE' THEN 'D'
    END;
    marker_shard := mod(pg_backend_pid(), 1024)::smallint;

    INSERT INTO rvbbit.table_dirty_markers (table_oid, shard, dirty_op, marked_at)
    SELECT TG_RELID, marker_shard, marker_op, clock_timestamp()
    WHERE marker_op IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM rvbbit.tables
          WHERE table_oid = TG_RELID
            AND shadow_heap_retained
      )
    ON CONFLICT (table_oid, shard, dirty_op) DO NOTHING;

    BEGIN
        PERFORM 1
        FROM rvbbit.tables
        WHERE table_oid = TG_RELID
          AND shadow_heap_retained
        FOR NO KEY UPDATE NOWAIT;

        IF FOUND THEN
            UPDATE rvbbit.tables
            SET shadow_heap_dirty = true,
                dirty_has_insert = dirty_has_insert OR TG_OP = 'INSERT',
                dirty_has_update = dirty_has_update OR TG_OP = 'UPDATE',
                dirty_has_delete = dirty_has_delete OR TG_OP = 'DELETE',
                last_write_at = clock_timestamp(),
                dirty_since = CASE WHEN shadow_heap_dirty THEN dirty_since ELSE clock_timestamp() END
            WHERE table_oid = TG_RELID;
        END IF;
    EXCEPTION WHEN lock_not_available THEN
        NULL;
    END;

    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        identity_mode := rvbbit.accel_identity_mode(TG_RELID);
        overlay_ready := rvbbit.accel_overlay_ready(TG_RELID);

        IF overlay_ready AND identity_mode = 'primary_key' THEN
            old_key_json := rvbbit.accel_identity_json_from_row(TG_RELID, to_jsonb(OLD));
        ELSIF overlay_ready AND identity_mode = 'ctid' THEN
            old_key_json := jsonb_build_array(OLD.ctid::text)::text;
        END IF;

        IF old_key_json IS NOT NULL THEN
            gen_setting := nullif(current_setting('rvbbit.row_tombstone_generation', true), '');
            IF split_part(coalesce(gen_setting, ''), ':', 1) = TG_RELID::oid::text THEN
                tombstone_gen := split_part(gen_setting, ':', 2)::bigint;
            ELSE
                tombstone_gen := rvbbit.allocate_generation(TG_RELID);
                PERFORM set_config(
                    'rvbbit.row_tombstone_generation',
                    TG_RELID::oid::text || ':' || tombstone_gen::text,
                    true
                );
            END IF;

            INSERT INTO rvbbit.delete_log
                (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
            SELECT TG_RELID, m.rg_id, m.ordinal, pg_current_xact_id(), tombstone_gen
            FROM rvbbit.row_identity_map m
            WHERE m.table_oid = TG_RELID
              AND m.key_json = old_key_json
            ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING;
        END IF;
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.install_shadow_heap_dirty_triggers(reloid regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    identity_mode text := rvbbit.accel_identity_mode(reloid);
    is_partition boolean := false;
BEGIN
    SELECT coalesce(c.relispartition, false)
      INTO is_partition
      FROM pg_class c
     WHERE c.oid = reloid;

    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_insert ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_update ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_delete ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_truncate ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_ctid_update ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_ctid_delete ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_row_insert ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_row_update ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_row_delete ON %s', reloid);

    IF is_partition THEN
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_row_insert
                 AFTER INSERT ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty_row()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_row_update
                 AFTER UPDATE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty_row()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_row_delete
                 AFTER DELETE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty_row()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_truncate
                 AFTER TRUNCATE ON %s
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
        RETURN;
    END IF;

    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_insert
             AFTER INSERT ON %s
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
    IF identity_mode = 'primary_key' THEN
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
    ELSE
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_update
                 AFTER UPDATE ON %s
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty_delete
                 AFTER DELETE ON %s
                 FOR EACH STATEMENT
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_ctid_update
                 AFTER UPDATE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_ctid_tombstone()',
            reloid
        );
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_ctid_delete
                 AFTER DELETE ON %s
                 FOR EACH ROW
                 EXECUTE FUNCTION rvbbit.mark_shadow_heap_ctid_tombstone()',
            reloid
        );
    END IF;
    EXECUTE format(
        'CREATE TRIGGER rvbbit_shadow_heap_dirty_truncate
             AFTER TRUNCATE ON %s
             FOR EACH STATEMENT
             EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
        reloid
    );
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
