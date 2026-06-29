-- 0110_workload_layout_build_status
--
-- Make "Accept + Build" mean exactly that for workload-derived layouts. A
-- plain refresh_acceleration(..., true) can legitimately no-op when the base
-- accelerator is already current, which left newly accepted layouts without
-- row_group_variants/layout_variant_status rows.

CREATE OR REPLACE FUNCTION rvbbit.workload_layout_variants_pending(rel oid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(bool_or(s.status IS DISTINCT FROM 'ready'), false)
    FROM rvbbit.workload_layout_recommendations r
    LEFT JOIN rvbbit.layout_variant_status s
      ON s.table_oid = r.table_oid
     AND lower(s.layout) = lower(r.layout)
    WHERE r.table_oid = rel
      AND r.status = 'accepted'
$$;

CREATE OR REPLACE FUNCTION rvbbit.build_accepted_workload_layouts(
    reloid regclass
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    table_name_text text := reloid::text;
    accepted_layouts bigint := 0;
    ready_layouts bigint := 0;
    row_groups_before bigint := 0;
    row_groups_after bigint := 0;
    shadow_dirty boolean := false;
    dirty_update boolean := false;
    dirty_delete boolean := false;
    dirty_truncate boolean := false;
    base_action text := 'none';
    base_result jsonb := NULL;
    layout_rows bigint := 0;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    SELECT count(*)::bigint
      INTO accepted_layouts
      FROM rvbbit.workload_layout_recommendations r
     WHERE r.table_oid = reloid
       AND r.status = 'accepted';

    IF accepted_layouts = 0 THEN
        RETURN jsonb_build_object(
            'status', 'noop',
            'operation', 'build_accepted_workload_layouts',
            'table', table_name_text,
            'reason', 'no accepted workload layouts',
            'accepted_layouts', 0,
            'ready_layouts', 0,
            'layout_rows', 0
        );
    END IF;

    SELECT count(*)::bigint
      INTO row_groups_before
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(ds.shadow_heap_dirty, false),
           coalesce(ds.dirty_has_update, false),
           coalesce(ds.dirty_has_delete, false),
           coalesce(ds.dirty_has_truncate, false)
      INTO shadow_dirty, dirty_update, dirty_delete, dirty_truncate
      FROM rvbbit.table_dirty_state ds
     WHERE ds.table_oid = reloid;

    IF row_groups_before = 0 THEN
        base_action := 'rebuild_acceleration';
        SELECT rvbbit.rebuild_acceleration(reloid, false) INTO base_result;
    ELSIF shadow_dirty THEN
        IF dirty_update OR dirty_delete OR dirty_truncate THEN
            base_action := 'rebuild_acceleration';
            SELECT rvbbit.rebuild_acceleration(reloid, false) INTO base_result;
        ELSE
            base_action := 'refresh_acceleration';
            SELECT rvbbit.refresh_acceleration(reloid, false) INTO base_result;
        END IF;
    END IF;

    SELECT count(*)::bigint
      INTO row_groups_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    IF row_groups_after > 0 THEN
        SELECT rvbbit.refresh_layout_variants(reloid::oid) INTO layout_rows;
    END IF;

    SELECT count(*)::bigint
      INTO ready_layouts
      FROM rvbbit.workload_layout_recommendations r
      JOIN rvbbit.layout_variant_status s
        ON s.table_oid = r.table_oid
       AND lower(s.layout) = lower(r.layout)
     WHERE r.table_oid = reloid
       AND r.status = 'accepted'
       AND s.status = 'ready';

    RETURN jsonb_build_object(
        'status',
            CASE
                WHEN ready_layouts >= accepted_layouts THEN 'ok'
                WHEN ready_layouts > 0 THEN 'partial'
                ELSE 'noop'
            END,
        'operation', 'build_accepted_workload_layouts',
        'table', table_name_text,
        'base_action', base_action,
        'base_result', base_result,
        'accepted_layouts', accepted_layouts,
        'ready_layouts', ready_layouts,
        'layout_rows', coalesce(layout_rows, 0),
        'row_groups_before', row_groups_before,
        'row_groups_after', row_groups_after
    );
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.maintain_storage(
    max_tables bigint DEFAULT 4,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
    n bigint;
    compacted jsonb := '[]'::jsonb;
    refreshed jsonb := '[]'::jsonb;
    errors jsonb := '[]'::jsonb;
    logs_reaped jsonb := '[]'::jsonb;
    orphaned_files_reaped jsonb := '{}'::jsonb;
    cap bigint := greatest(coalesce(max_tables, 0), 0);
BEGIN
    IF cap = 0 THEN
        RETURN jsonb_build_object(
            'compacted', compacted,
            'refreshed_variants', refreshed,
            'errors', errors,
            'skipped', 'max_tables is zero'
        );
    END IF;

    FOR rec IN
        SELECT t.table_oid::regclass AS rel
        FROM rvbbit.tables t
        JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE ds.shadow_heap_dirty
        ORDER BY t.created_at
        LIMIT cap
    LOOP
        BEGIN
            SELECT count(*) INTO n FROM rvbbit.compact(rec.rel);
            compacted := compacted || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'row_groups', n)
            );
        EXCEPTION WHEN OTHERS THEN
            errors := errors || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'phase', 'compact', 'error', SQLERRM)
            );
        END;
    END LOOP;

    IF refresh_variants THEN
        FOR rec IN
            WITH candidates AS (
                SELECT
                    t.table_oid,
                    t.table_oid::regclass AS rel,
                    coalesce(max(rg.created_at), '-infinity'::timestamptz) AS newest_rg,
                    coalesce(max(rgv.created_at), '-infinity'::timestamptz) AS newest_variant,
                    count(rg.*) AS row_groups,
                    count(rgv.*) AS variants,
                    rvbbit.workload_layout_variants_pending(t.table_oid) AS workload_pending
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              AND (variants = 0 OR newest_variant < newest_rg OR workload_pending)
            ORDER BY newest_rg DESC
            LIMIT cap
        LOOP
            BEGIN
                SELECT rvbbit.refresh_layout_variants(rec.rel) INTO n;
                refreshed := refreshed || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'variants', n)
                );
            EXCEPTION WHEN OTHERS THEN
                errors := errors || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'phase', 'refresh_variants', 'error', SQLERRM)
                );
            END;
        END LOOP;
    END IF;

    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object('table', table_name, 'rows', rows_reaped)),
                   '[]'::jsonb)
          INTO logs_reaped
          FROM rvbbit.reap_logs();
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_logs', 'error', SQLERRM)
        );
    END;

    BEGIN
        SELECT to_jsonb(r)
          INTO orphaned_files_reaped
          FROM rvbbit.reap_orphaned_files() AS r;
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_orphaned_files', 'error', SQLERRM)
        );
    END;

    RETURN jsonb_build_object(
        'compacted', compacted,
        'refreshed_variants', refreshed,
        'logs_reaped', logs_reaped,
        'orphaned_files_reaped', coalesce(orphaned_files_reaped, '{}'::jsonb),
        'errors', errors
    );
END;
$$;

CREATE OR REPLACE VIEW rvbbit.workload_layout_recommendation_status AS
SELECT
    r.table_oid::regclass::text AS table_name,
    r.table_oid,
    r.layout_kind,
    r.column_name,
    r.layout,
    r.status,
    r.score,
    r.observations,
    r.weighted_ms,
    r.role_counts,
    r.sample_shapes,
    s.status AS layout_status,
    s.actual_rows AS layout_rows,
    s.file_count AS layout_files,
    r.reason,
    r.details,
    r.recommended_at,
    r.updated_at
FROM rvbbit.workload_layout_recommendations r
LEFT JOIN rvbbit.layout_variant_status s
  ON s.table_oid = r.table_oid
 AND lower(s.layout) = lower(r.layout);
