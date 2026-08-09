-- 0260_acceleration_storage_policies.sql
-- Separate how generations compose (cumulative deltas vs complete snapshots)
-- from how much user-visible history is retained. Also close two tombstone
-- leaks found on a production mirror workload:
--   * snapshot_load's internal TRUNCATE must not tombstone the old snapshot;
--   * current-only snapshots retire superseded catalog/file generations as an
--     atomic metadata swap, not millions of per-row delete_log entries;
--   * ordinary current-only TRUNCATE is an O(1) replacement invalidation,
--     followed by heap reads and a policy-driven full rebuild.

ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS generation_semantics text NOT NULL DEFAULT 'cumulative';
ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS history_policy text NOT NULL DEFAULT 'retained';

-- Upgrades preserve every pre-policy table as retained above. Only tables
-- registered after this migration adopt the safer, lower-maintenance default.
ALTER TABLE rvbbit.tables
    ALTER COLUMN history_policy SET DEFAULT 'current';

-- min_visible_generation was the pre-policy snapshot marker. Preserve that
-- contract when upgrading instead of misclassifying existing snapshot cubes.
UPDATE rvbbit.tables
   SET generation_semantics = 'snapshot'
 WHERE min_visible_generation > 0
   AND generation_semantics <> 'snapshot';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'rvbbit.tables'::regclass
           AND conname = 'tables_generation_semantics_check'
    ) THEN
        ALTER TABLE rvbbit.tables
            ADD CONSTRAINT tables_generation_semantics_check
            CHECK (generation_semantics IN ('cumulative', 'snapshot'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'rvbbit.tables'::regclass
           AND conname = 'tables_history_policy_check'
    ) THEN
        ALTER TABLE rvbbit.tables
            ADD CONSTRAINT tables_history_policy_check
            CHECK (history_policy IN ('retained', 'current'));
    END IF;
END $$;

COMMENT ON COLUMN rvbbit.tables.generation_semantics IS
    'cumulative = generations are current-state deltas; snapshot = each generation is a complete replacement';
COMMENT ON COLUMN rvbbit.tables.history_policy IS
    'retained = AS OF history is supported; current = superseded snapshots are retired immediately and AS OF is disabled';

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

-- Add the policy invalidation boundary to the existing identity-completeness
-- test. Existing refresh/planner callers then reject current-only TRUNCATE
-- overlays without each needing an independent policy check.
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

-- Re-issue the canonical dirty trigger. Retained tables keep the existing
-- row-tombstone overlay; current-only TRUNCATE is an O(1) invalidation marker.
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
                   dirty_since = CASE WHEN shadow_heap_dirty THEN dirty_since ELSE clock_timestamp() END
             WHERE table_oid = TG_RELID;
        END IF;
    EXCEPTION WHEN lock_not_available THEN
        NULL;
    END;

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
        SELECT TG_RELID, rg.rg_id, ord::int, pg_current_xact_id(), tombstone_gen
          FROM rvbbit.row_groups rg
          CROSS JOIN LATERAL generate_series(0, rg.n_rows - 1) AS ord
         WHERE rg.table_oid = TG_RELID
        ON CONFLICT (table_oid, rg_id, ordinal) DO NOTHING;
    END IF;

    IF TG_OP = 'TRUNCATE' THEN
        -- TRUNCATE itself is a known-safe relfilenode swap: this trigger has
        -- recorded the dirty boundary. Retained tables have tombstoned the old
        -- rows; current-only tables now require a full replacement rebuild.
        -- Keep the rewrite guard aligned without weakening unrelated rewrites.
        UPDATE rvbbit.acceleration_state
           SET refresh_relfilenode = pg_relation_filenode(TG_RELID),
               updated_at = clock_timestamp()
         WHERE table_oid = TG_RELID;
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.prune_snapshot_history(
    rel regclass,
    force_prune boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    floor_generation bigint;
    semantics text;
    history text;
    old_rg_ids bigint[];
    old_paths text[];
    generations_pruned bigint := 0;
    row_groups_pruned bigint := 0;
    tombstones_pruned bigint := 0;
    variants_pruned bigint := 0;
    files_queued bigint := 0;
    n bigint := 0;
BEGIN
    SELECT t.min_visible_generation, t.generation_semantics, t.history_policy
      INTO floor_generation, semantics, history
      FROM rvbbit.tables t
     WHERE t.table_oid = rel;
    IF NOT FOUND THEN
        RAISE EXCEPTION '% is not a registered rvbbit table', rel;
    END IF;
    IF semantics <> 'snapshot' OR floor_generation <= 0 THEN
        RAISE EXCEPTION '% is not an active snapshot table (semantics %, floor %)',
            rel, semantics, floor_generation;
    END IF;
    IF history <> 'current' AND NOT force_prune THEN
        RETURN jsonb_build_object(
            'status', 'skipped', 'table', rel::text,
            'reason', 'history_policy is retained'
        );
    END IF;

    PERFORM pg_advisory_xact_lock((1380336724::bigint << 32) | rel::oid::bigint);

    SELECT array_agg(rg.rg_id ORDER BY rg.rg_id)
      INTO old_rg_ids
      FROM rvbbit.row_groups rg
     WHERE rg.table_oid = rel
       AND rg.generation <> floor_generation;

    SELECT array_agg(f.path ORDER BY f.path)
      INTO old_paths
      FROM (
          SELECT rg.path
            FROM rvbbit.row_groups rg
           WHERE rg.table_oid = rel AND rg.generation <> floor_generation
          UNION ALL
          SELECT v.path
            FROM rvbbit.row_group_variants v
            JOIN rvbbit.row_groups rg
              ON rg.table_oid = v.table_oid AND rg.rg_id = v.rg_id
           WHERE rg.table_oid = rel AND rg.generation <> floor_generation
          UNION ALL
          SELECT d.path
            FROM rvbbit.text_dictionaries d
            JOIN rvbbit.row_groups rg
              ON rg.table_oid = d.table_oid AND rg.rg_id = d.rg_id
           WHERE rg.table_oid = rel AND rg.generation <> floor_generation
      ) AS f;

    IF old_rg_ids IS NOT NULL THEN
        DELETE FROM rvbbit.delete_log dl
         WHERE dl.table_oid = rel AND dl.rg_id = ANY(old_rg_ids);
        GET DIAGNOSTICS tombstones_pruned = ROW_COUNT;

        DELETE FROM rvbbit.row_group_variants v
         WHERE v.table_oid = rel AND v.rg_id = ANY(old_rg_ids);
        GET DIAGNOSTICS variants_pruned = ROW_COUNT;

        DELETE FROM rvbbit.row_groups rg
         WHERE rg.table_oid = rel AND rg.rg_id = ANY(old_rg_ids);
        GET DIAGNOSTICS row_groups_pruned = ROW_COUNT;
    END IF;

    DELETE FROM rvbbit.generations g
     WHERE g.table_oid = rel AND g.generation <> floor_generation;
    GET DIAGNOSTICS generations_pruned = ROW_COUNT;

    DELETE FROM rvbbit.delete_log dl
     WHERE dl.table_oid = rel
       AND NOT EXISTS (
           SELECT 1 FROM rvbbit.row_groups rg
            WHERE rg.table_oid = dl.table_oid AND rg.rg_id = dl.rg_id
       );
    GET DIAGNOSTICS n = ROW_COUNT;
    tombstones_pruned := tombstones_pruned + n;

    IF old_paths IS NOT NULL THEN
        INSERT INTO rvbbit.orphaned_files (path, table_oid, reason, operation_id)
        SELECT DISTINCT p, rel, 'current_snapshot_history_prune', NULL::bigint
          FROM unnest(old_paths) AS p
         WHERE p IS NOT NULL AND btrim(p) <> ''
        ON CONFLICT (path) DO UPDATE
           SET table_oid = EXCLUDED.table_oid,
               reason = EXCLUDED.reason,
               operation_id = NULL,
               queued_at = clock_timestamp(),
               last_error = NULL;
        GET DIAGNOSTICS files_queued = ROW_COUNT;
    END IF;

    RETURN jsonb_build_object(
        'status', 'ok',
        'table', rel::text,
        'floor_generation', floor_generation,
        'generations_pruned', generations_pruned,
        'row_groups_pruned', row_groups_pruned,
        'variants_pruned', variants_pruned,
        'tombstones_pruned', tombstones_pruned,
        'files_queued', files_queued
    );
END;
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
           EXISTS (SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid)
      INTO old_semantics, old_history, floor_generation, has_row_groups
      FROM rvbbit.tables t
     WHERE t.table_oid = rel;
    IF NOT FOUND THEN
        RAISE EXCEPTION '% is not a registered rvbbit table', rel;
    END IF;

    next_semantics := coalesce(nullif(lower(btrim(generation_semantics)), ''), old_semantics);
    next_history := coalesce(nullif(lower(btrim(history_policy)), ''), old_history);
    IF next_semantics NOT IN ('cumulative', 'snapshot') THEN
        RAISE EXCEPTION 'generation_semantics must be cumulative or snapshot, got %', next_semantics;
    END IF;
    IF next_history NOT IN ('retained', 'current') THEN
        RAISE EXCEPTION 'history_policy must be retained or current, got %', next_history;
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
                    EXISTS (SELECT 1 FROM rvbbit.delete_log dl WHERE dl.table_oid = rel)
                    OR (SELECT count(*) FROM rvbbit.row_groups rg WHERE rg.table_oid = rel) > 1
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

CREATE OR REPLACE VIEW rvbbit.acceleration_storage_policy AS
SELECT t.table_oid,
       t.table_oid::regclass::text AS table_name,
       t.generation_semantics,
       t.history_policy,
       t.min_visible_generation,
       coalesce(g.generations, 0) AS retained_generations,
       coalesce(rg.row_groups, 0) AS retained_row_groups,
       coalesce(rg.rows, 0) AS retained_rows
  FROM rvbbit.tables t
  LEFT JOIN LATERAL (
      SELECT count(*)::bigint AS generations
        FROM rvbbit.generations g
       WHERE g.table_oid = t.table_oid
  ) g ON true
  LEFT JOIN LATERAL (
      SELECT count(*)::bigint AS row_groups,
             coalesce(sum(r.n_rows), 0)::bigint AS rows
        FROM rvbbit.row_groups r
       WHERE r.table_oid = t.table_oid
  ) rg ON true;

CREATE OR REPLACE FUNCTION rvbbit.snapshot_load(dest regclass, source_query text)
RETURNS TABLE (generation bigint, rows_loaded bigint, action text)
LANGUAGE plpgsql
AS $$
DECLARE
    g bigint;
    n bigint;
    previous_snapshot_target text;
    prune_result jsonb;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(dest) THEN
        RAISE EXCEPTION '% is not an rvbbit table', dest;
    END IF;

    previous_snapshot_target := current_setting('rvbbit.snapshot_load_target', true);
    PERFORM set_config('rvbbit.snapshot_load_target', dest::oid::text, true);
    EXECUTE format('TRUNCATE TABLE %s', dest);
    PERFORM set_config(
        'rvbbit.snapshot_load_target', coalesce(previous_snapshot_target, ''), true
    );
    EXECUTE format('INSERT INTO %s %s', dest, source_query);

    PERFORM rvbbit.compact(dest, keep_heap => true);
    SELECT t.next_generation - 1 INTO g
      FROM rvbbit.tables t WHERE t.table_oid = dest;

    SELECT count(*) INTO n
      FROM rvbbit.generations gg
     WHERE gg.table_oid = dest AND gg.generation = g;
    IF n = 0 THEN
        INSERT INTO rvbbit.generations (table_oid, generation, n_rows, n_row_groups)
        VALUES (dest, g, 0, 0);
    END IF;

    PERFORM rvbbit.set_visible_floor(dest, g);
    UPDATE rvbbit.tables
       SET generation_semantics = 'snapshot'
     WHERE table_oid = dest;

    IF (SELECT t.history_policy FROM rvbbit.tables t WHERE t.table_oid = dest) = 'current' THEN
        SELECT rvbbit.prune_snapshot_history(dest) INTO prune_result;
    END IF;

    SELECT gg.n_rows INTO n
      FROM rvbbit.generations gg
     WHERE gg.table_oid = dest AND gg.generation = g;

    RETURN QUERY
        SELECT g, coalesce(n, 0),
               CASE WHEN coalesce(n, 0) = 0 THEN 'empty' ELSE 'snapshot' END;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.snapshot_load_current(dest regclass, source_query text)
RETURNS TABLE (generation bigint, rows_loaded bigint, action text)
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT rvbbit.is_rvbbit_table(dest) THEN
        RAISE EXCEPTION '% is not an rvbbit table', dest;
    END IF;
    UPDATE rvbbit.tables
       SET history_policy = 'current'
     WHERE table_oid = dest;
    RETURN QUERY SELECT * FROM rvbbit.snapshot_load(dest, source_query);
END;
$$;
