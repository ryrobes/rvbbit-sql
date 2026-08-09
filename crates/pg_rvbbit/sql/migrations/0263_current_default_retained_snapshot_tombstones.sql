-- 0263: Make the current-only default explicit on databases that may have
-- applied an early 0260 draft, and retain historical correctness for cubes and
-- other retained snapshot tables. Current-only TRUNCATE remains an O(1)
-- replacement marker; retained TRUNCATE continues to record row tombstones.

ALTER TABLE rvbbit.tables
    ALTER COLUMN history_policy SET DEFAULT 'current';

CREATE OR REPLACE FUNCTION rvbbit.current_replacement_pending(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce((
        SELECT t.history_policy = 'current' AND ds.dirty_has_truncate
          FROM rvbbit.tables t
          JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
         WHERE t.table_oid = reloid
    ), false)
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_overlay_ready(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT (CASE rvbbit.accel_identity_mode(reloid)
        WHEN 'primary_key' THEN rvbbit.accel_identity_map_complete(reloid)
        WHEN 'ctid' THEN rvbbit.accel_identity_map_complete(reloid)
                         AND rvbbit.accel_ctid_identity_valid(reloid)
        ELSE false
    END)
       AND NOT rvbbit.current_replacement_pending(reloid)
$$;

CREATE OR REPLACE FUNCTION rvbbit.set_acceleration_storage_policy(
    rel regclass,
    generation_semantics text DEFAULT NULL,
    history_policy text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    old_semantics text;
    old_history text;
    next_semantics text;
    next_history text;
    floor_generation bigint;
    has_row_groups boolean;
    fold_required boolean;
    prune_result jsonb;
BEGIN
    SELECT t.generation_semantics,
           t.history_policy,
           t.min_visible_generation,
           EXISTS (
               SELECT 1
                 FROM rvbbit.row_groups rg
                WHERE rg.table_oid = t.table_oid
           )
      INTO old_semantics, old_history, floor_generation, has_row_groups
      FROM rvbbit.tables t
     WHERE t.table_oid = rel;
    IF NOT FOUND THEN
        RAISE EXCEPTION '% is not a registered rvbbit table', rel;
    END IF;

    next_semantics := coalesce(
        nullif(lower(btrim(generation_semantics)), ''), old_semantics
    );
    next_history := coalesce(
        nullif(lower(btrim(history_policy)), ''), old_history
    );
    IF next_semantics NOT IN ('cumulative', 'snapshot') THEN
        RAISE EXCEPTION
            'generation_semantics must be cumulative or snapshot, got %',
            next_semantics;
    END IF;
    IF next_history NOT IN ('retained', 'current') THEN
        RAISE EXCEPTION
            'history_policy must be retained or current, got %', next_history;
    END IF;
    IF has_row_groups AND next_semantics <> old_semantics THEN
        RAISE EXCEPTION
            'cannot change materialized table % from % to % with metadata alone; publish a complete generation with rvbbit.snapshot_load[_current]() or perform a full conversion fold',
            rel, old_semantics, next_semantics;
    END IF;

    UPDATE rvbbit.tables t
       SET generation_semantics = next_semantics,
           history_policy = next_history
     WHERE t.table_oid = rel;

    IF next_semantics = 'snapshot'
       AND next_history = 'current'
       AND floor_generation > 0 THEN
        SELECT rvbbit.prune_snapshot_history(rel) INTO prune_result;
    END IF;

    fold_required := next_history = 'current'
        AND (
            rvbbit.current_replacement_pending(rel)
            OR (
                next_semantics = 'cumulative'
                AND (
                    EXISTS (
                        SELECT 1
                          FROM rvbbit.delete_log dl
                         WHERE dl.table_oid = rel
                    )
                    OR (
                        SELECT count(*)
                          FROM rvbbit.row_groups rg
                         WHERE rg.table_oid = rel
                    ) > 1
                )
            )
        );

    RETURN jsonb_build_object(
        'table', rel::text,
        'generation_semantics', next_semantics,
        'history_policy', next_history,
        'active_snapshot_floor', floor_generation,
        'fold_required', fold_required,
        'prune', prune_result,
        'note', CASE
            WHEN rvbbit.current_replacement_pending(rel) THEN
                'current replacement is heap-authoritative until rebuild_acceleration publishes a new baseline'
            WHEN fold_required THEN
                'current-only cumulative tables retain correctness deltas/tombstones until rebuild_acceleration folds them'
            ELSE NULL
        END
    );
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    key_expr text;
    tombstone_gen bigint;
    marker_op char(1);
    marker_shard smallint;
    identity_mode text;
    overlay_ready boolean;
BEGIN
    marker_op := CASE TG_OP
        WHEN 'INSERT' THEN 'I'
        WHEN 'UPDATE' THEN 'U'
        WHEN 'DELETE' THEN 'D'
        WHEN 'TRUNCATE' THEN 'T'
    END;
    marker_shard := mod(pg_backend_pid(), 1024)::smallint;

    INSERT INTO rvbbit.table_dirty_markers (table_oid, shard, dirty_op, marked_at)
    SELECT TG_RELID, marker_shard, marker_op, clock_timestamp()
    WHERE marker_op IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM rvbbit.tables
           WHERE table_oid = TG_RELID AND shadow_heap_retained
      )
    ON CONFLICT (table_oid, shard, dirty_op) DO NOTHING;

    BEGIN
        PERFORM 1
          FROM rvbbit.tables
         WHERE table_oid = TG_RELID AND shadow_heap_retained
         FOR NO KEY UPDATE NOWAIT;
        IF FOUND THEN
            UPDATE rvbbit.tables
               SET shadow_heap_dirty = true,
                   dirty_has_insert = dirty_has_insert OR TG_OP = 'INSERT',
                   dirty_has_update = dirty_has_update OR TG_OP = 'UPDATE',
                   dirty_has_delete = dirty_has_delete OR TG_OP = 'DELETE',
                   dirty_has_truncate = dirty_has_truncate OR TG_OP = 'TRUNCATE',
                   last_write_at = clock_timestamp(),
                   dirty_since = CASE
                       WHEN shadow_heap_dirty THEN dirty_since
                       ELSE clock_timestamp()
                   END
             WHERE table_oid = TG_RELID;
        END IF;
    EXCEPTION WHEN lock_not_available THEN
        NULL;
    END;

    identity_mode := rvbbit.accel_identity_mode(TG_RELID);
    overlay_ready := rvbbit.accel_overlay_ready(TG_RELID);
    IF TG_OP IN ('UPDATE', 'DELETE')
       AND overlay_ready
       AND identity_mode = 'primary_key' THEN
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
    ELSIF TG_OP = 'TRUNCATE'
          AND NOT coalesce((
              SELECT t.history_policy = 'current'
                FROM rvbbit.tables t
               WHERE t.table_oid = TG_RELID
          ), false)
          AND nullif(current_setting('rvbbit.snapshot_load_target', true), '')
              IS DISTINCT FROM TG_RELID::oid::text THEN
        tombstone_gen := rvbbit.allocate_generation(TG_RELID);
        INSERT INTO rvbbit.delete_log
            (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
        SELECT TG_RELID, rg.rg_id, ord::int,
               pg_current_xact_id(), tombstone_gen
          FROM rvbbit.row_groups rg
          CROSS JOIN LATERAL generate_series(0, rg.n_rows - 1) AS ord
         WHERE rg.table_oid = TG_RELID
        ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING;
    END IF;

    IF TG_OP = 'TRUNCATE' THEN
        UPDATE rvbbit.acceleration_state
           SET refresh_relfilenode = pg_relation_filenode(TG_RELID),
               updated_at = clock_timestamp()
         WHERE table_oid = TG_RELID;
    END IF;

    RETURN NULL;
END;
$$;
