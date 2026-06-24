-- CTID identity maps are only valid for the heap relfilenode that produced
-- them. Heap rewrites such as VACUUM FULL and CLUSTER can preserve logical rows
-- while assigning new CTIDs, so no-PK mutable delta refresh must verify the
-- current relfilenode before trusting CTID tombstones.

ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS ctid_identity_relfilenode oid;

CREATE OR REPLACE FUNCTION rvbbit.accel_identity_map_complete(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce((
               SELECT count(*)::bigint
               FROM rvbbit.row_identity_map m
               WHERE m.table_oid = reloid
           ), 0) >= coalesce((
               SELECT sum(rg.n_rows)::bigint
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = reloid
           ), 0)
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_all_rows_tombstoned(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce((
               SELECT count(*)::bigint
               FROM rvbbit.delete_log dl
               WHERE dl.table_oid = reloid
           ), 0) >= coalesce((
               SELECT sum(rg.n_rows)::bigint
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = reloid
           ), 0)
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_ctid_identity_valid(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN rvbbit.accel_identity_mode(reloid) <> 'ctid' THEN true
        ELSE EXISTS (
            SELECT 1
            FROM rvbbit.tables t
            WHERE t.table_oid = reloid
              AND t.ctid_identity_relfilenode IS NOT NULL
              AND t.ctid_identity_relfilenode = pg_relation_filenode(reloid)
        ) OR rvbbit.accel_all_rows_tombstoned(reloid)
    END
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_overlay_ready(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT CASE rvbbit.accel_identity_mode(reloid)
        WHEN 'primary_key' THEN rvbbit.accel_identity_map_complete(reloid)
        WHEN 'ctid' THEN rvbbit.accel_identity_map_complete(reloid)
                         AND rvbbit.accel_ctid_identity_valid(reloid)
        ELSE false
    END
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
    ELSIF TG_OP = 'TRUNCATE' THEN
        tombstone_gen := rvbbit.allocate_generation(TG_RELID);
        INSERT INTO rvbbit.delete_log
            (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
        SELECT TG_RELID, rg.rg_id, ord::int, pg_current_xact_id(), tombstone_gen
        FROM rvbbit.row_groups rg
        CROSS JOIN LATERAL generate_series(0, rg.n_rows - 1) AS ord
        WHERE rg.table_oid = TG_RELID
        ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING;
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.record_ctid_identity_relfilenode()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
    current_node oid;
    recorded_node oid;
    has_existing_row_groups boolean;
BEGIN
    FOR rec IN
        SELECT DISTINCT table_oid
        FROM rvbbit_inserted_identity_rows
    LOOP
        current_node := pg_relation_filenode(rec.table_oid::regclass);
        IF rvbbit.accel_identity_mode(rec.table_oid::regclass) <> 'ctid'
           OR current_node IS NULL THEN
            CONTINUE;
        END IF;

        SELECT t.ctid_identity_relfilenode
          INTO recorded_node
          FROM rvbbit.tables t
         WHERE t.table_oid = rec.table_oid;

        SELECT EXISTS (
            SELECT 1
            FROM rvbbit.row_groups rg
            WHERE rg.table_oid = rec.table_oid
        ) INTO has_existing_row_groups;

        IF recorded_node IS NULL
           OR recorded_node = current_node
           OR NOT has_existing_row_groups
           OR rvbbit.accel_all_rows_tombstoned(rec.table_oid::regclass) THEN
            UPDATE rvbbit.tables
               SET ctid_identity_relfilenode = current_node
             WHERE table_oid = rec.table_oid;
        END IF;
    END LOOP;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS rvbbit_record_ctid_identity_relfilenode ON rvbbit.row_identity_map;
CREATE TRIGGER rvbbit_record_ctid_identity_relfilenode
    AFTER INSERT ON rvbbit.row_identity_map
    REFERENCING NEW TABLE AS rvbbit_inserted_identity_rows
    FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.record_ctid_identity_relfilenode();

-- Existing CTID side maps were created before relfilenode tracking existed.
-- Leave them invalid until their next full rebuild; new exports stamp the
-- relfilenode through the trigger above.
UPDATE rvbbit.tables t
   SET ctid_identity_relfilenode = NULL
 WHERE rvbbit.accel_identity_mode(t.table_oid::regclass) = 'ctid';
