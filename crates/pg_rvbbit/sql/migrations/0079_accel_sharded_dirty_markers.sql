-- Reduce same-table writer contention on rvbbit.tables dirty-marker rows.
--
-- Writers record a bounded sharded marker first, then best-effort coalesce the
-- legacy rvbbit.tables flags with NOWAIT. Readers that make routing/freshness
-- decisions use rvbbit.table_dirty_state so correctness no longer depends on
-- every writer updating the same catalog row.

CREATE TABLE IF NOT EXISTS rvbbit.table_dirty_markers (
    table_oid       oid NOT NULL,
    shard           smallint NOT NULL,
    dirty_op        char(1) NOT NULL,
    marked_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (table_oid, shard, dirty_op),
    CHECK (shard >= 0),
    CHECK (dirty_op IN ('I', 'U', 'D', 'T'))
);

CREATE INDEX IF NOT EXISTS table_dirty_markers_table_time_idx
    ON rvbbit.table_dirty_markers (table_oid, marked_at DESC);

CREATE SEQUENCE IF NOT EXISTS rvbbit.generation_seq AS bigint;

SELECT setval(
    'rvbbit.generation_seq',
    greatest(
        coalesce((SELECT max(next_generation) FROM rvbbit.tables), 1),
        coalesce((SELECT max(generation) FROM rvbbit.row_groups), 0),
        coalesce((SELECT max(generation) FROM rvbbit.generations), 0),
        coalesce((SELECT max(deleted_generation) FROM rvbbit.delete_log), 0),
        1
    ),
    true
);

CREATE OR REPLACE FUNCTION rvbbit.allocate_generation(reloid regclass)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    gen bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.tables WHERE table_oid = reloid) THEN
        RAISE EXCEPTION 'rvbbit.allocate_generation: table % is not registered with the rvbbit access method', reloid;
    END IF;
    SELECT nextval('rvbbit.generation_seq') INTO gen;
    RETURN gen;
END $$;

CREATE OR REPLACE VIEW rvbbit.table_dirty_state AS
SELECT
    t.table_oid,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    (coalesce(t.shadow_heap_dirty, false) OR coalesce(dm.has_marker, false))
        AS shadow_heap_dirty,
    (coalesce(t.dirty_has_insert, false) OR coalesce(dm.has_insert, false))
        AS dirty_has_insert,
    (coalesce(t.dirty_has_update, false) OR coalesce(dm.has_update, false))
        AS dirty_has_update,
    (coalesce(t.dirty_has_delete, false) OR coalesce(dm.has_delete, false))
        AS dirty_has_delete,
    (coalesce(t.dirty_has_truncate, false) OR coalesce(dm.has_truncate, false))
        AS dirty_has_truncate,
    CASE
        WHEN NOT (coalesce(t.shadow_heap_dirty, false) OR coalesce(dm.has_marker, false))
            THEN NULL
        WHEN t.dirty_since IS NULL THEN dm.dirty_since
        WHEN dm.dirty_since IS NULL THEN t.dirty_since
        ELSE least(t.dirty_since, dm.dirty_since)
    END AS dirty_since,
    CASE
        WHEN t.last_write_at IS NULL THEN dm.last_write_at
        WHEN dm.last_write_at IS NULL THEN t.last_write_at
        ELSE greatest(t.last_write_at, dm.last_write_at)
    END AS last_write_at
FROM rvbbit.tables t
LEFT JOIN LATERAL (
    SELECT count(*) > 0 AS has_marker,
           bool_or(m.dirty_op = 'I') AS has_insert,
           bool_or(m.dirty_op = 'U') AS has_update,
           bool_or(m.dirty_op = 'D') AS has_delete,
           bool_or(m.dirty_op = 'T') AS has_truncate,
           min(m.marked_at) AS dirty_since,
           max(m.marked_at) AS last_write_at
      FROM rvbbit.table_dirty_markers m
     WHERE m.table_oid = t.table_oid
) dm ON true;

CREATE OR REPLACE FUNCTION rvbbit.shadow_heap_dirty_effective(rel oid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce((
        SELECT s.shadow_heap_dirty
        FROM rvbbit.table_dirty_state s
        WHERE s.table_oid = rel
    ), false)
$$;

CREATE OR REPLACE FUNCTION rvbbit.shadow_heap_clean_retained(rel oid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce((
        SELECT s.shadow_heap_retained AND NOT s.shadow_heap_dirty
        FROM rvbbit.table_dirty_state s
        WHERE s.table_oid = rel
    ), false)
$$;

CREATE OR REPLACE FUNCTION rvbbit.clear_table_dirty_markers(rel oid)
RETURNS void
LANGUAGE sql
AS $$
    DELETE FROM rvbbit.table_dirty_markers WHERE table_oid = rel
$$;

CREATE OR REPLACE FUNCTION rvbbit.clear_shadow_heap_dirty_flags()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.shadow_heap_dirty IS FALSE THEN
        NEW.dirty_has_insert := false;
        NEW.dirty_has_update := false;
        NEW.dirty_has_delete := false;
        NEW.dirty_has_truncate := false;
        DELETE FROM rvbbit.table_dirty_markers WHERE table_oid = NEW.table_oid;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS rvbbit_clear_shadow_heap_dirty_flags ON rvbbit.tables;
CREATE TRIGGER rvbbit_clear_shadow_heap_dirty_flags
    BEFORE INSERT OR UPDATE OF shadow_heap_dirty ON rvbbit.tables
    FOR EACH ROW EXECUTE FUNCTION rvbbit.clear_shadow_heap_dirty_flags();

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    key_expr text;
    tombstone_gen bigint := 0;
    overlay_ready boolean := false;
    identity_mode text;
    marker_op char(1);
    marker_shard smallint;
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
      FROM rvbbit.row_groups r WHERE r.table_oid = t.table_oid
) rg ON true
LEFT JOIN LATERAL (
    SELECT count(*)::bigint AS tombstones
      FROM rvbbit.delete_log d WHERE d.table_oid = t.table_oid
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

CREATE OR REPLACE FUNCTION rvbbit.refresh_acceleration(
    reloid regclass,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql AS $$
<<accel_refresh>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    last_xid numeric;
    safe_upper_xid numeric;
    rows_written bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    max_rg_id_pre bigint;
    existing_rgs bigint;
    generation_after bigint := 0;
    shadow_retained boolean := false;
    shadow_dirty boolean := false;
    dirty_update boolean := false;
    dirty_delete boolean := false;
    dirty_truncate boolean := false;
    overlay_ready boolean := false;
    heap_bytes bigint := 0;
    phase_id bigint;
    phase_bytes_before bigint := 0;
    phase_bytes_after bigint := 0;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);

    INSERT INTO rvbbit.acceleration_state (table_oid)
    VALUES (reloid)
    ON CONFLICT (table_oid) DO NOTHING;

    SELECT s.last_refresh_xid
      INTO last_xid
      FROM rvbbit.acceleration_state s
     WHERE s.table_oid = reloid
     FOR UPDATE;

    safe_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1
    );

    SELECT count(*)::bigint, coalesce(max(rg_id), -1)::bigint,
           coalesce(max(generation), 0)::bigint
      INTO existing_rgs, max_rg_id_pre, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(ds.shadow_heap_retained, false),
           coalesce(ds.shadow_heap_dirty, false),
           coalesce(ds.dirty_has_update, false),
           coalesce(ds.dirty_has_delete, false),
           coalesce(ds.dirty_has_truncate, false)
      INTO shadow_retained, shadow_dirty, dirty_update, dirty_delete, dirty_truncate
      FROM rvbbit.table_dirty_state ds
     WHERE ds.table_oid = reloid;

    heap_bytes := pg_relation_size(reloid);
    overlay_ready := rvbbit.accel_overlay_ready(reloid);

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'refresh_acceleration', 'running',
        last_xid, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'watermark', 'heap xmin <= pg_snapshot_xmin(pg_current_snapshot()) - 1',
            'heap_guard', 'LOCK TABLE IN SHARE MODE'
        )
    )
    RETURNING id INTO op_id;

    IF existing_rgs > 0
       AND shadow_dirty
       AND (dirty_update OR dirty_delete OR dirty_truncate)
       AND NOT overlay_ready THEN
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = 'non-append dirty episode requires rebuild or complete row identity overlay',
               settings = settings || jsonb_build_object(
                   'dirty_has_update', dirty_update,
                   'dirty_has_delete', dirty_delete,
                   'dirty_has_truncate', dirty_truncate,
                   'overlay_ready', overlay_ready,
                   'recommended_action', 'rebuild_acceleration'
               )
         WHERE id = op_id;
        RAISE EXCEPTION
            'rvbbit.refresh_acceleration: % has UPDATE/DELETE/TRUNCATE changes since the last refresh; run rvbbit.rebuild_acceleration(%) or use an overlay-capable path',
            reloid, quote_literal(reloid::text);
    END IF;

    IF last_xid = 0 AND existing_rgs > 0 AND heap_bytes > 0 THEN
        IF shadow_retained AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false,
                   dirty_has_insert = false,
                   dirty_has_update = false,
                   dirty_has_delete = false,
                   dirty_has_truncate = false
             WHERE table_oid = reloid;
            PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
            PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
            UPDATE rvbbit.acceleration_state
               SET last_refresh_xid = safe_upper_xid,
                   last_refresh_generation = generation_after,
                   last_refresh_at = clock_timestamp(),
                   updated_at = clock_timestamp()
             WHERE table_oid = reloid;
            UPDATE rvbbit.acceleration_operations
               SET status = 'noop',
                   finished_at = clock_timestamp(),
                   rows_written = 0,
                   row_groups_written = 0,
                   generation_after = accel_refresh.generation_after,
                   settings = settings || jsonb_build_object('bootstrap', 'clean shadow heap already covered by existing row groups')
             WHERE id = op_id;
            RETURN jsonb_build_object(
                'status', 'noop',
                'operation_id', op_id,
                'table', table_name_text,
                'watermark_before', last_xid,
                'watermark_after', safe_upper_xid,
                'rows_written', 0,
                'row_groups_written', 0,
                'bootstrap', true
            );
        ELSIF shadow_dirty THEN
            RAISE EXCEPTION
                'rvbbit.refresh_acceleration: % has existing row groups and a dirty retained heap; run rvbbit.rebuild_acceleration(%) before incremental refresh',
                reloid, quote_literal(reloid::text);
        END IF;
    END IF;

    IF safe_upper_xid <= last_xid THEN
        IF existing_rgs > 0 AND NOT shadow_dirty THEN
            UPDATE rvbbit.tables
               SET shadow_heap_retained = true,
                   shadow_heap_dirty = false,
                   dirty_has_insert = false,
                   dirty_has_update = false,
                   dirty_has_delete = false,
                   dirty_has_truncate = false
             WHERE table_oid = reloid;
            PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
            PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
        END IF;
        UPDATE rvbbit.acceleration_operations
           SET status = 'noop',
               finished_at = clock_timestamp(),
               rows_written = 0,
               row_groups_written = 0,
               generation_after = accel_refresh.generation_after
         WHERE id = op_id;
        RETURN jsonb_build_object(
            'status', 'noop',
            'operation_id', op_id,
            'table', table_name_text,
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid,
            'rows_written', 0,
            'row_groups_written', 0
        );
    END IF;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_before
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_delta_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'watermark_delta',
            'watermark_before', last_xid,
            'watermark_after', safe_upper_xid
        )
    )
    RETURNING id INTO phase_id;

    PERFORM set_config('rvbbit.acceleration_phase_id', phase_id::text, true);
    SELECT rvbbit.export_to_parquet_xid_range(
        reloid::oid,
        last_xid::text,
        safe_upper_xid::text
    ) INTO rows_written;
    PERFORM set_config('rvbbit.acceleration_phase_id', '', true);

    SELECT count(*)::bigint, coalesce(max(generation), generation_after)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id > max_rg_id_pre;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = accel_refresh.rows_written,
           row_groups_written = accel_refresh.row_groups_written,
           files_written = accel_refresh.row_groups_written::integer,
           bytes_written = greatest(0, phase_bytes_after - phase_bytes_before),
           expected_rows = accel_refresh.rows_written,
           actual_rows = accel_refresh.rows_written
     WHERE id = phase_id;

    IF refresh_variants AND rows_written > 0 THEN
        PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
        SELECT rvbbit.refresh_layout_variants_xid_range(
            reloid::oid,
            last_xid::text,
            safe_upper_xid::text
        ) INTO variants_rows;
        PERFORM set_config('rvbbit.acceleration_operation_id', '', true);
    END IF;

    IF existing_rgs > 0 OR row_groups_written > 0 THEN
        UPDATE rvbbit.tables
           SET shadow_heap_retained = true,
               shadow_heap_dirty = false,
               dirty_has_insert = false,
               dirty_has_update = false,
               dirty_has_delete = false,
               dirty_has_truncate = false
         WHERE table_oid = reloid;
        PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);
        PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);
    END IF;

    UPDATE rvbbit.acceleration_state
       SET last_refresh_xid = safe_upper_xid,
           last_refresh_generation = generation_after,
           last_refresh_rows = coalesce(last_refresh_rows, 0) + coalesce(rows_written, 0),
           last_refresh_row_groups = coalesce(last_refresh_row_groups, 0) + coalesce(row_groups_written, 0),
           last_refresh_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE table_oid = reloid;

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = accel_refresh.rows_written,
           row_groups_written = accel_refresh.row_groups_written,
           variants_rows = accel_refresh.variants_rows,
           generation_after = accel_refresh.generation_after
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'watermark_before', last_xid,
        'watermark_after', safe_upper_xid,
        'rows_written', rows_written,
        'row_groups_written', row_groups_written,
        'variants_rows', variants_rows,
        'generation_after', generation_after
    );
EXCEPTION WHEN OTHERS THEN
    IF op_id IS NOT NULL THEN
        UPDATE rvbbit.acceleration_operation_phases
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE operation_id = op_id
           AND status = 'running';
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END;
$$;

CREATE OR REPLACE VIEW rvbbit.acceleration_status AS
SELECT
    t.table_oid,
    c.oid::regclass::text AS table_name,
    coalesce(s.last_refresh_xid, 0) AS last_refresh_xid,
    s.last_refresh_at,
    coalesce(s.last_refresh_generation, 0) AS last_refresh_generation,
    coalesce(s.last_refresh_rows, 0) AS last_refresh_rows,
    coalesce(s.last_refresh_row_groups, 0) AS last_refresh_row_groups,
    coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid), 0) AS parquet_rows,
    coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = t.table_oid), 0) AS row_groups,
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
                    count(rgv.*) AS variants
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              AND (variants = 0 OR newest_variant < newest_rg)
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
END $$;

CREATE OR REPLACE FUNCTION rvbbit.on_drop_table()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT * FROM pg_event_trigger_dropped_objects()
        WHERE object_type = 'table'
    LOOP
        DELETE FROM rvbbit.tables WHERE table_oid = obj.objid;
        DELETE FROM rvbbit.delete_log WHERE table_oid = obj.objid;
        DELETE FROM rvbbit.table_dirty_markers WHERE table_oid = obj.objid;
    END LOOP;
END;
$$;
