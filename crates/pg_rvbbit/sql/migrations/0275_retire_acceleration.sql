-- 0275: Retire one accelerator without unregistering its PostgreSQL table.
--
-- This is the inverse of building a baseline for operators who are curating
-- accelerator supply from observed workload evidence.  The heap remains the
-- source of truth, the rvbbit.tables registration remains intact, and the
-- refresh policy becomes manual.  Catalog paths are retired through the same
-- MVCC grace-period orphan queue used by staged rebuilds, so readers that began
-- before the metadata swap can still finish safely.

ALTER TABLE rvbbit.acceleration_operations
    DROP CONSTRAINT IF EXISTS acceleration_operations_operation_check;
ALTER TABLE rvbbit.acceleration_operations
    ADD CONSTRAINT acceleration_operations_operation_check
    CHECK (operation IN (
        'refresh_acceleration',
        'rebuild_acceleration',
        'compact_acceleration',
        'legacy_compact',
        'variant_build',
        'retire_acceleration'
    ));

CREATE OR REPLACE FUNCTION rvbbit.retire_acceleration(
    rel regclass
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    table_name_text text := rel::text;
    heap_retained boolean;
    lance_url text;
    row_groups_retired bigint := 0;
    variants_retired bigint := 0;
    dictionaries_retired bigint := 0;
    bytes_retired bigint := 0;
    files_queued bigint := 0;
    op_id bigint;
    result_status text;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(rel) THEN
        RAISE EXCEPTION '% is not an rvbbit table', rel;
    END IF;

    -- Do not wait behind a long rebuild.  Generated batch scripts can continue
    -- to the next relation and retry this one after the active maintainer exits.
    IF NOT pg_try_advisory_xact_lock(
        (1380336724::bigint << 32) | rel::oid::bigint
    ) THEN
        RETURN jsonb_build_object(
            'status', 'busy',
            'table', table_name_text,
            'message', 'table maintenance is already running; retry later'
        );
    END IF;

    SELECT t.shadow_heap_retained, t.lance_url
      INTO heap_retained, lance_url
      FROM rvbbit.tables t
     WHERE t.table_oid = rel;

    -- Never discard the only authoritative copy of a compacted table.
    IF EXISTS (
        SELECT 1 FROM rvbbit.row_groups rg WHERE rg.table_oid = rel
    ) AND NOT coalesce(heap_retained, false) THEN
        RAISE EXCEPTION
            'rvbbit.retire_acceleration: % has a non-authoritative heap; restore the heap before retiring its accelerator',
            rel;
    END IF;

    -- Lance lifecycle is not file-by-file in the local orphan reaper.  Refuse
    -- instead of leaving a separately managed dataset behind by surprise.
    IF nullif(lance_url, '') IS NOT NULL THEN
        RAISE EXCEPTION
            'rvbbit.retire_acceleration: % has a Lance dataset; retire that dataset explicitly first',
            rel;
    END IF;

    -- The local reaper only unlinks local paths.  Cold/published provider
    -- objects need their provider-specific retirement flow first.
    IF EXISTS (
        SELECT 1
          FROM rvbbit.row_groups rg
         WHERE rg.table_oid = rel
           AND (
               nullif(to_jsonb(rg)->>'cold_url', '') IS NOT NULL
               OR nullif(to_jsonb(rg)->>'published_url', '') IS NOT NULL
           )
    ) THEN
        RAISE EXCEPTION
            'rvbbit.retire_acceleration: % has cold or published files; retire those provider objects explicitly first',
            rel;
    END IF;

    SELECT count(*), coalesce(sum(rg.n_bytes), 0)
      INTO row_groups_retired, bytes_retired
      FROM rvbbit.row_groups rg
     WHERE rg.table_oid = rel;

    SELECT count(*), bytes_retired + coalesce(sum(v.n_bytes), 0)
      INTO variants_retired, bytes_retired
      FROM rvbbit.row_group_variants v
     WHERE v.table_oid = rel;

    SELECT count(*), bytes_retired + coalesce(sum(d.n_bytes), 0)
      INTO dictionaries_retired, bytes_retired
      FROM rvbbit.text_dictionaries d
     WHERE d.table_oid = rel;

    result_status := CASE
        WHEN row_groups_retired + variants_retired + dictionaries_retired > 0
            THEN 'retired'
        ELSE 'already_unbuilt'
    END;

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status, started_at, finished_at,
        rows_written, row_groups_written, settings
    ) VALUES (
        rel, table_name_text, 'retire_acceleration', 'ok',
        clock_timestamp(), clock_timestamp(), 0, 0,
        jsonb_build_object(
            'mode', 'operator_retire_to_heap',
            'row_groups_retired', row_groups_retired,
            'variants_retired', variants_retired,
            'dictionaries_retired', dictionaries_retired,
            'bytes_retired', bytes_retired,
            'file_reap', 'grace_period_queue',
            'registry_preserved', true,
            'heap_preserved', true
        )
    ) RETURNING id INTO op_id;

    WITH paths AS MATERIALIZED (
        SELECT DISTINCT path
          FROM (
              SELECT rg.path
                FROM rvbbit.row_groups rg
               WHERE rg.table_oid = rel
              UNION ALL
              SELECT v.path
                FROM rvbbit.row_group_variants v
               WHERE v.table_oid = rel
              UNION ALL
              SELECT d.path
                FROM rvbbit.text_dictionaries d
               WHERE d.table_oid = rel
          ) files
         WHERE path IS NOT NULL AND btrim(path) <> ''
    ), queued AS (
        INSERT INTO rvbbit.orphaned_files (
            path, table_oid, reason, operation_id
        )
        SELECT path, rel, 'operator_retire_acceleration', op_id
          FROM paths
        ON CONFLICT (path) DO UPDATE
           SET table_oid = EXCLUDED.table_oid,
               reason = EXCLUDED.reason,
               operation_id = EXCLUDED.operation_id,
               queued_at = clock_timestamp(),
               attempts = 0,
               last_attempt_at = NULL,
               last_error = NULL
        RETURNING 1
    )
    SELECT count(*) INTO files_queued FROM queued;

    -- Remove only materialized state.  Operation history, observer evidence,
    -- accepted workload recommendations, routing evidence, and table metadata
    -- remain available for deciding whether to build the table again later.
    DELETE FROM rvbbit.row_group_variants WHERE table_oid = rel;
    DELETE FROM rvbbit.semantic_bitmaps WHERE table_oid = rel;
    DELETE FROM rvbbit.delete_log WHERE table_oid = rel;
    DELETE FROM rvbbit.generations WHERE table_oid = rel;
    DELETE FROM rvbbit.acceleration_state WHERE table_oid = rel;
    DELETE FROM rvbbit.layout_variant_status WHERE table_oid = rel;
    DELETE FROM rvbbit.variant_build_queue WHERE table_oid = rel;
    DELETE FROM rvbbit.materialize_queue WHERE table_oid = rel;
    DELETE FROM rvbbit.hot_objects WHERE table_oid = rel;
    DELETE FROM rvbbit.row_groups WHERE table_oid = rel;
    DELETE FROM rvbbit.table_dirty_markers WHERE table_oid = rel;

    -- Existing explicit settings stay inspectable, but cannot be picked up by
    -- accel_tick until the operator deliberately chooses a new strategy.
    UPDATE rvbbit.accel_policy
       SET strategy = 'manual',
           note = CASE
               WHEN position(
                   'Operator retired accelerator' IN coalesce(note, '')
               ) > 0 THEN note
               ELSE concat_ws(
                   ' | ',
                   nullif(note, ''),
                   'Operator retired accelerator; registry and heap preserved'
               )
           END,
           updated_at = clock_timestamp()
     WHERE table_oid = rel;

    UPDATE rvbbit.tables
       SET min_visible_generation = 0,
           shadow_heap_dirty = false,
           dirty_has_insert = false,
           dirty_has_update = false,
           dirty_has_delete = false,
           dirty_has_truncate = false,
           dirty_since = NULL,
           ctid_identity_relfilenode = NULL
     WHERE table_oid = rel;

    UPDATE rvbbit.acceleration_operations
       SET settings = settings || jsonb_build_object(
               'files_queued', files_queued
           )
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', result_status,
        'table', table_name_text,
        'policy', 'manual',
        'row_groups_retired', row_groups_retired,
        'variants_retired', variants_retired,
        'dictionaries_retired', dictionaries_retired,
        'files_queued', files_queued,
        'bytes_retired', bytes_retired,
        'registry_preserved', true,
        'heap_preserved', true,
        'operation_id', op_id
    );
END
$$;

COMMENT ON FUNCTION rvbbit.retire_acceleration(regclass) IS
    'Safely retires one table accelerator to the authoritative heap, switches any explicit policy to manual, queues files for grace-period reaping, and preserves registry and workload evidence.';
