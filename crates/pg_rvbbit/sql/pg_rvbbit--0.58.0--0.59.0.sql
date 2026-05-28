-- pg_rvbbit 0.58.0 -> 0.59.0
-- Metadata-only time-travel timeline helper.

CREATE OR REPLACE FUNCTION rvbbit.time_travel_timeline(reloid regclass)
RETURNS TABLE (
    generation            bigint,
    committed_at          timestamptz,
    rows_written          bigint,
    row_groups_written    int,
    visible_rows_estimate bigint,
    visible_row_groups    bigint,
    tombstones_visible    bigint
) LANGUAGE sql STABLE AS $$
    SELECT
        g.generation,
        g.committed_at,
        g.n_rows AS rows_written,
        g.n_row_groups AS row_groups_written,
        greatest(
            coalesce(rg.visible_rows, 0) - coalesce(dl.tombstones_visible, 0),
            0
        )::bigint AS visible_rows_estimate,
        coalesce(rg.visible_row_groups, 0)::bigint AS visible_row_groups,
        coalesce(dl.tombstones_visible, 0)::bigint AS tombstones_visible
    FROM rvbbit.generations g
    LEFT JOIN LATERAL (
        SELECT
            coalesce(sum(rg.n_rows), 0)::bigint AS visible_rows,
            count(*)::bigint AS visible_row_groups
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = reloid
          AND rg.generation <= g.generation
    ) rg ON true
    LEFT JOIN LATERAL (
        SELECT count(*)::bigint AS tombstones_visible
        FROM rvbbit.delete_log dl
        WHERE dl.table_oid = reloid
          AND dl.deleted_generation <= g.generation
    ) dl ON true
    WHERE g.table_oid = reloid
    ORDER BY g.generation DESC
$$;
