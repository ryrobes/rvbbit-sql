-- CTID-backed hidden identity fallback for no-primary-key rvbbit tables.
--
-- Transition tables cannot expose system columns, so primary-key tables keep the
-- statement-level OLD TABLE path from 0072. No-PK tables use row-level UPDATE/DELETE
-- triggers, where OLD.ctid is available, and canonical exports write matching CTID
-- side keys into rvbbit.row_identity_map. This avoids adding a visible internal
-- column that would break positional INSERT statements.

CREATE OR REPLACE FUNCTION rvbbit.accel_identity_mode(reloid regclass)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN cardinality(rvbbit.accel_identity_columns(reloid)) > 0 THEN 'primary_key'
        WHEN rvbbit.is_rvbbit_table(reloid) THEN 'ctid'
        ELSE NULL
    END
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_overlay_ready(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT rvbbit.accel_identity_mode(reloid) IS NOT NULL
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
    identity_mode text;
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

    identity_mode := rvbbit.accel_identity_mode(TG_RELID);
    overlay_ready := rvbbit.accel_overlay_ready(TG_RELID);
    IF TG_OP IN ('UPDATE', 'DELETE') AND overlay_ready AND identity_mode = 'primary_key' THEN
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

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_ctid_tombstone()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_key_json text;
    gen_setting text;
    tombstone_gen bigint;
BEGIN
    IF TG_OP NOT IN ('UPDATE', 'DELETE')
       OR rvbbit.accel_identity_mode(TG_RELID) <> 'ctid'
       OR NOT rvbbit.accel_overlay_ready(TG_RELID) THEN
        RETURN NULL;
    END IF;

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

    old_key_json := jsonb_build_array(OLD.ctid::text)::text;
    INSERT INTO rvbbit.delete_log
        (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
    SELECT TG_RELID, m.rg_id, m.ordinal, pg_current_xact_id(), tombstone_gen
    FROM rvbbit.row_identity_map m
    WHERE m.table_oid = TG_RELID
      AND m.key_json = old_key_json
    ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.install_shadow_heap_dirty_triggers(reloid regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    identity_mode text := rvbbit.accel_identity_mode(reloid);
BEGIN
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_insert ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_update ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_delete ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty_truncate ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_ctid_update ON %s', reloid);
    EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_ctid_delete ON %s', reloid);

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
