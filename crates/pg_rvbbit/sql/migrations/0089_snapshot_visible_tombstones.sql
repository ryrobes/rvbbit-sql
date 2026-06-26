-- Snapshot generations keep history, but current acceleration eligibility must
-- only consider tombstones attached to the latest visible row groups.
--
-- Before this migration, cube/snapshot refresh could TRUNCATE+reload a table,
-- producing tombstones for the old snapshot generation. Those tombstones stayed
-- useful for temporal history, but route-explain and maintenance treated them
-- as current deletes and vetoed parquet/variant paths.

CREATE OR REPLACE VIEW rvbbit.row_groups_visible AS
SELECT rg.*
FROM rvbbit.row_groups rg
JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
WHERE t.min_visible_generation = 0
   OR rg.generation = t.min_visible_generation;

CREATE OR REPLACE FUNCTION rvbbit.visible_tombstone_count(
    reloid regclass
) RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT count(*)::bigint
    FROM rvbbit.delete_log dl
    JOIN rvbbit.row_groups rg
      ON rg.table_oid = dl.table_oid
     AND rg.rg_id = dl.rg_id
    JOIN rvbbit.tables t
      ON t.table_oid = dl.table_oid
    WHERE dl.table_oid = reloid
      AND (t.min_visible_generation = 0 OR rg.generation = t.min_visible_generation)
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_all_rows_tombstoned(reloid regclass)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT rvbbit.visible_tombstone_count(reloid) >= coalesce((
               SELECT sum(rg.n_rows)::bigint
               FROM rvbbit.row_groups_visible rg
               WHERE rg.table_oid = reloid
           ), 0)
$$;

CREATE OR REPLACE FUNCTION rvbbit.list_tables()
RETURNS TABLE (table_oid oid, table_name text, n_row_groups bigint, n_deletes bigint)
LANGUAGE sql
STABLE
AS $$
    SELECT
        t.table_oid,
        c.oid::regclass::text,
        (SELECT count(*) FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = t.table_oid),
        rvbbit.visible_tombstone_count(t.table_oid::regclass)
    FROM rvbbit.tables t
    JOIN pg_class c ON c.oid = t.table_oid;
$$;

CREATE OR REPLACE VIEW rvbbit.accel_freshness AS
SELECT
    t.table_oid,
    c.oid::regclass::text                   AS table_name,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    coalesce(ds.shadow_heap_dirty, false)   AS shadow_heap_dirty,
    (
        pg_relation_size(t.table_oid) = 0
        OR coalesce(ds.shadow_heap_retained AND NOT ds.shadow_heap_dirty, false)
    )                                       AS parquet_authoritative,
    CASE WHEN coalesce(ds.shadow_heap_dirty, false) THEN ds.dirty_since END
                                            AS dirty_since,
    CASE WHEN coalesce(ds.shadow_heap_dirty, false) AND ds.dirty_since IS NOT NULL
         THEN greatest(0, extract(epoch FROM now() - ds.dirty_since))
    END                                     AS seconds_dirty,
    ds.last_write_at,
    s.last_refresh_at,
    CASE WHEN s.last_refresh_at IS NOT NULL
         THEN greatest(0, extract(epoch FROM now() - s.last_refresh_at))
    END                                     AS seconds_since_refresh,
    coalesce(s.last_refresh_xid, 0)         AS last_refresh_xid,
    coalesce(rg.parquet_rows, 0)            AS parquet_rows,
    coalesce(rg.row_groups, 0)              AS row_groups,
    coalesce(rg.parquet_bytes, 0)           AS parquet_bytes,
    pg_stat_get_live_tuples(t.table_oid)    AS heap_live_tuples,
    greatest(0, pg_stat_get_live_tuples(t.table_oid) - coalesce(rg.parquet_rows, 0))
                                            AS est_unmirrored_rows,
    coalesce(dl.tombstones, 0)              AS tombstones,
    (greatest(0, pg_stat_get_live_tuples(t.table_oid) - coalesce(rg.parquet_rows, 0))
        + coalesce(dl.tombstones, 0))       AS drift_rows,
    CASE WHEN coalesce(rg.parquet_rows, 0) > 0
         THEN (greatest(0, pg_stat_get_live_tuples(t.table_oid) - coalesce(rg.parquet_rows, 0))
                  + coalesce(dl.tombstones, 0))::float8 / rg.parquet_rows
    END                                     AS drift_ratio,
    pg_stat_get_numscans(t.table_oid)       AS heap_seq_scans,
    op.last_rebuild_ms,
    op.last_rebuild_rows,
    EXISTS (
        SELECT 1 FROM rvbbit.acceleration_operations o
         WHERE o.table_oid = t.table_oid AND o.status = 'running'
    )                                       AS op_running,
    (t.lance_url IS NOT NULL)               AS lance_accelerated
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
LEFT JOIN rvbbit.acceleration_state s ON s.table_oid = t.table_oid
LEFT JOIN LATERAL (
    SELECT sum(r.n_rows)::bigint  AS parquet_rows,
           count(*)::bigint       AS row_groups,
           sum(r.n_bytes)::bigint AS parquet_bytes
      FROM rvbbit.row_groups r
     WHERE r.table_oid = t.table_oid
       AND (t.min_visible_generation = 0 OR r.generation = t.min_visible_generation)
) rg ON true
LEFT JOIN LATERAL (
    SELECT count(*)::bigint AS tombstones
      FROM rvbbit.delete_log d
      JOIN rvbbit.row_groups r
        ON r.table_oid = d.table_oid
       AND r.rg_id = d.rg_id
     WHERE d.table_oid = t.table_oid
       AND (t.min_visible_generation = 0 OR r.generation = t.min_visible_generation)
) dl ON true
LEFT JOIN LATERAL (
    SELECT extract(epoch FROM (o.finished_at - o.started_at)) * 1000.0 AS last_rebuild_ms,
           o.rows_written                                              AS last_rebuild_rows
      FROM rvbbit.acceleration_operations o
     WHERE o.table_oid = t.table_oid
       AND o.status = 'ok'
       AND o.finished_at IS NOT NULL
     ORDER BY o.started_at DESC
     LIMIT 1
) op ON true;

CREATE OR REPLACE VIEW rvbbit.acceleration_status AS
SELECT
    t.table_oid,
    c.oid::regclass::text AS table_name,
    coalesce(s.last_refresh_xid, 0) AS last_refresh_xid,
    s.last_refresh_at,
    coalesce(s.last_refresh_generation, 0) AS last_refresh_generation,
    coalesce(s.last_refresh_rows, 0) AS last_refresh_rows,
    coalesce(s.last_refresh_row_groups, 0) AS last_refresh_row_groups,
    coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = t.table_oid), 0) AS parquet_rows,
    coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = t.table_oid), 0) AS row_groups,
    pg_relation_size(t.table_oid)::bigint AS heap_bytes,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    coalesce(ds.shadow_heap_dirty, false) AS shadow_heap_dirty,
    (
        pg_relation_size(t.table_oid) = 0
        OR coalesce(ds.shadow_heap_retained AND NOT ds.shadow_heap_dirty, false)
    ) AS parquet_authoritative,
    (SELECT max(o.started_at) FROM rvbbit.acceleration_operations o WHERE o.table_oid = t.table_oid) AS last_operation_at
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
LEFT JOIN rvbbit.acceleration_state s ON s.table_oid = t.table_oid;

CREATE OR REPLACE FUNCTION rvbbit.shadow_heap_status(rel regclass)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    heap_bytes bigint,
    heap_total_bytes bigint,
    parquet_rows bigint,
    parquet_bytes bigint,
    row_groups bigint,
    delete_rows bigint,
    parquet_authoritative boolean,
    shadow_heap_present boolean,
    shadow_heap_retained boolean,
    shadow_heap_dirty boolean
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        rel::oid,
        rel::text,
        pg_relation_size(rel)::bigint,
        pg_total_relation_size(rel)::bigint,
        coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT sum(rg.n_bytes)::bigint FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups_visible rg WHERE rg.table_oid = rel), 0),
        rvbbit.visible_tombstone_count(rel),
        rvbbit.visible_tombstone_count(rel) = 0
            AND (
                pg_relation_size(rel) = 0
                OR rvbbit.shadow_heap_clean_retained(rel::oid)
            ),
        pg_relation_size(rel) > 0
            AND coalesce((SELECT t.shadow_heap_retained FROM rvbbit.tables t WHERE t.table_oid = rel), false),
        coalesce((SELECT t.shadow_heap_retained FROM rvbbit.tables t WHERE t.table_oid = rel), false),
        rvbbit.shadow_heap_dirty_effective(rel::oid);
$$;
