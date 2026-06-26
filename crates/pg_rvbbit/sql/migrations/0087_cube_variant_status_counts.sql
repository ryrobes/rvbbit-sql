-- Fix cube_refresh_status.variant_files: COUNT(DISTINCT (NULL, NULL)) from a
-- LEFT JOIN is 1, not 0, so cubes without layout variants looked like they had
-- one variant file while still being marked maintain_storage.

DO $$
BEGIN
    IF to_regclass('rvbbit.cube_catalog') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE $view$
CREATE OR REPLACE VIEW rvbbit.cube_refresh_status AS
WITH cubes AS (
    SELECT
        c.*,
        to_regclass('cubes.' || quote_ident(c.name)) AS table_oid
    FROM rvbbit.cube_catalog c
),
physical AS (
    SELECT
        c.name,
        count(DISTINCT rg.rg_id)::bigint AS row_groups,
        count(DISTINCT (rgv.layout, rgv.rg_id)) FILTER (WHERE rgv.rg_id IS NOT NULL)::bigint AS variant_files,
        coalesce(max(rg.created_at), '-infinity'::timestamptz) AS newest_row_group_at,
        coalesce(max(rgv.created_at), '-infinity'::timestamptz) AS newest_variant_at
    FROM cubes c
    LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.table_oid
    LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = c.table_oid
    GROUP BY c.name
),
status AS (
    SELECT
        c.name,
        c.category,
        c.subcategory,
        ctl.enabled,
        ctl.refreshed_at,
        ctl.last_rows,
        ctl.last_error,
        ctl.last_refresh_seconds,
        p.mode AS refresh_mode,
        p.query_threads,
        p.writer_threads,
        p.scan_chunk_rows,
        p.metadata_profile,
        p.refresh_variants,
        p.refresh_interval_seconds,
        coalesce(ph.row_groups, 0) AS row_groups,
        coalesce(ph.variant_files, 0) AS variant_files,
        (
            coalesce(ph.row_groups, 0) > 0
            AND (
                coalesce(ph.variant_files, 0) = 0
                OR ph.newest_variant_at < ph.newest_row_group_at
            )
        ) AS variants_pending,
        coalesce(f.shadow_heap_dirty, false) AS cube_dirty,
        coalesce(src.source_dirty, false) AS source_accel_dirty,
        (
            src.source_last_write_at IS NOT NULL
            AND (
                coalesce(ctl.last_refresh_started_at, ctl.refreshed_at) IS NULL
                OR src.source_last_write_at > coalesce(ctl.last_refresh_started_at, ctl.refreshed_at)
            )
        ) AS source_dirty,
        (
            coalesce(f.shadow_heap_dirty, false)
            OR (
                src.source_last_write_at IS NOT NULL
                AND (
                    coalesce(ctl.last_refresh_started_at, ctl.refreshed_at) IS NULL
                    OR src.source_last_write_at > coalesce(ctl.last_refresh_started_at, ctl.refreshed_at)
                )
            )
        ) AS dirty,
        coalesce(extract(epoch FROM (now() - ctl.refreshed_at)), f.seconds_since_refresh)
            AS seconds_since_refresh,
        src.source_tables,
        coalesce(src.source_count, 0) AS source_count,
        coalesce(src.tracked_source_count, 0) AS tracked_source_count,
        coalesce(src.dirty_source_count, 0) AS dirty_source_count,
        coalesce(src.untracked_sources, '[]'::jsonb) AS untracked_sources,
        coalesce(src.dirty_sources, '[]'::jsonb) AS dirty_sources,
        src.source_dirty_since,
        src.source_seconds_dirty,
        src.source_last_write_at,
        ctl.last_refresh_policy
    FROM cubes c
    LEFT JOIN rvbbit.cube_control ctl ON ctl.cube_name = c.name
    CROSS JOIN LATERAL rvbbit.cube_refresh_policy_effective(c.name) p
    LEFT JOIN physical ph ON ph.name = c.name
    LEFT JOIN rvbbit.accel_freshness f ON f.table_name = c.table_oid::text
    LEFT JOIN rvbbit.cube_source_refresh_status src ON src.name = c.name
)
SELECT
    s.*,
    CASE
        WHEN s.last_error IS NOT NULL THEN 'fix_error'
        WHEN s.refresh_mode = 'manual' THEN 'manual'
        WHEN s.refreshed_at IS NULL THEN 'refresh_cube'
        WHEN s.source_dirty THEN 'refresh_cube'
        WHEN s.cube_dirty THEN 'refresh_cube'
        WHEN s.refresh_interval_seconds IS NOT NULL
             AND coalesce(s.seconds_since_refresh, 999999999) >= s.refresh_interval_seconds
             THEN 'refresh_cube'
        WHEN s.variants_pending THEN 'maintain_storage'
        ELSE 'ok'
    END AS recommended_action
FROM status s;
$view$;
END;
$$;
