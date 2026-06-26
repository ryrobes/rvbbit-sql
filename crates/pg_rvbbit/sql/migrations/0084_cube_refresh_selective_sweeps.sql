-- 0084_cube_refresh_selective_sweeps
--
-- Cubes are materialized snapshots. A cube does not receive new source rows
-- until rvbbit.refresh_cube(name) runs, and that refresh is still a full
-- snapshot_load of the cube SQL into cubes.<name>.
--
-- The previous "auto" policy only tuned how an explicit refresh executed. This
-- migration gives the scheduled/bulk runner real sweep semantics:
--
--   * track whether a cube's rvbbit source tables are dirty;
--   * add a per-cube refresh interval used as an age-based fallback;
--   * make refresh_all_cubes() skip cubes that are clean and not due;
--   * keep the old rebuild-everything behavior behind p_force => true.

ALTER TABLE rvbbit.cube_refresh_policy
    ADD COLUMN IF NOT EXISTS refresh_interval_seconds integer
        CHECK (refresh_interval_seconds IS NULL OR refresh_interval_seconds > 0);

ALTER TABLE rvbbit.cube_control
    ADD COLUMN IF NOT EXISTS last_refresh_started_at timestamptz;

DROP VIEW IF EXISTS rvbbit.cube_refresh_status;
DROP VIEW IF EXISTS rvbbit.cube_source_refresh_status;
DROP TABLE IF EXISTS rvbbit.cube_source_write_markers;
CREATE TABLE rvbbit.cube_source_write_markers (
    table_oid oid NOT NULL,
    shard smallint NOT NULL,
    marked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (table_oid, shard),
    CHECK (shard >= 0)
);
CREATE INDEX cube_source_write_markers_table_time_idx
    ON rvbbit.cube_source_write_markers (table_oid, marked_at DESC);

DROP FUNCTION IF EXISTS rvbbit.cube_refresh_policy_effective(text);
DROP FUNCTION IF EXISTS rvbbit._install_cube_source_dirty_tracking(text);
DROP FUNCTION IF EXISTS rvbbit.mark_cube_source_write() CASCADE;
DROP FUNCTION IF EXISTS rvbbit.set_cube_refresh_policy(
    text, text, integer, integer, integer, text, text, text
);
DROP PROCEDURE IF EXISTS rvbbit.refresh_all_cubes(text, text, numeric);

CREATE OR REPLACE FUNCTION rvbbit.cube_refresh_policy_effective(p_name text)
RETURNS TABLE (
    cube_name text,
    mode text,
    query_threads integer,
    writer_threads integer,
    scan_chunk_rows integer,
    metadata_profile text,
    refresh_variants text,
    refresh_interval_seconds integer
) LANGUAGE sql STABLE AS $fn$
WITH raw AS (
    SELECT *
    FROM rvbbit.cube_refresh_policy
    WHERE cube_name = p_name
),
base AS (
    SELECT
        p_name AS cube_name,
        coalesce(
            (SELECT mode FROM raw),
            rvbbit._text_setting('rvbbit.cube_refresh_mode'),
            'auto'
        ) AS mode
)
SELECT
    base.cube_name,
    base.mode,
    coalesce(
        (SELECT query_threads FROM raw),
        rvbbit._int_setting('rvbbit.refresh_max_threads'),
        CASE base.mode WHEN 'conservative' THEN 1 WHEN 'bulk' THEN 4 ELSE 2 END
    ) AS query_threads,
    coalesce(
        (SELECT writer_threads FROM raw),
        rvbbit._int_setting('rvbbit.cube_compact_writer_threads'),
        CASE base.mode WHEN 'conservative' THEN 2 ELSE 8 END
    ) AS writer_threads,
    coalesce(
        (SELECT scan_chunk_rows FROM raw),
        rvbbit._int_setting('rvbbit.cube_compact_scan_chunk_rows'),
        250000
    ) AS scan_chunk_rows,
    coalesce(
        (SELECT metadata_profile FROM raw),
        rvbbit._text_setting('rvbbit.cube_compact_metadata_profile'),
        CASE base.mode WHEN 'conservative' THEN NULL ELSE 'minimal' END
    ) AS metadata_profile,
    coalesce(
        (SELECT refresh_variants FROM raw),
        rvbbit._text_setting('rvbbit.cube_refresh_variants'),
        'deferred'
    ) AS refresh_variants,
    CASE
        WHEN base.mode = 'manual' THEN NULL
        ELSE coalesce(
            (SELECT refresh_interval_seconds FROM raw),
            rvbbit._int_setting('rvbbit.cube_refresh_interval_seconds'),
            CASE base.mode
                WHEN 'bulk' THEN 3600
                WHEN 'conservative' THEN 21600
                ELSE 14400
            END
        )
    END AS refresh_interval_seconds
FROM base
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.set_cube_refresh_policy(
    p_name text,
    p_mode text DEFAULT NULL,
    p_query_threads integer DEFAULT NULL,
    p_writer_threads integer DEFAULT NULL,
    p_scan_chunk_rows integer DEFAULT NULL,
    p_metadata_profile text DEFAULT NULL,
    p_refresh_variants text DEFAULT NULL,
    p_note text DEFAULT NULL,
    p_refresh_interval_seconds integer DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_mode text := coalesce(nullif(btrim(p_mode), ''), 'auto');
    v_effective jsonb;
BEGIN
    IF p_name IS NULL OR p_name !~ '^[a-z_][a-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.set_cube_refresh_policy: cube name must be a lowercase identifier (got %)', p_name;
    END IF;

    INSERT INTO rvbbit.cube_refresh_policy (
        cube_name, mode, query_threads, writer_threads, scan_chunk_rows,
        metadata_profile, refresh_variants, refresh_interval_seconds, note, updated_at
    ) VALUES (
        p_name, v_mode, p_query_threads, p_writer_threads, p_scan_chunk_rows,
        nullif(btrim(p_metadata_profile), ''),
        nullif(btrim(p_refresh_variants), ''),
        p_refresh_interval_seconds,
        p_note, now()
    )
    ON CONFLICT (cube_name) DO UPDATE SET
        mode = EXCLUDED.mode,
        query_threads = EXCLUDED.query_threads,
        writer_threads = EXCLUDED.writer_threads,
        scan_chunk_rows = EXCLUDED.scan_chunk_rows,
        metadata_profile = EXCLUDED.metadata_profile,
        refresh_variants = EXCLUDED.refresh_variants,
        refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
        note = EXCLUDED.note,
        updated_at = now();

    SELECT to_jsonb(p) INTO v_effective
    FROM rvbbit.cube_refresh_policy_effective(p_name) AS p;
    RETURN v_effective;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.mark_cube_source_write()
RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    marker_shard smallint := mod(pg_backend_pid(), 1024)::smallint;
BEGIN
    INSERT INTO rvbbit.cube_source_write_markers (table_oid, shard, marked_at)
    VALUES (TG_RELID, marker_shard, clock_timestamp())
    ON CONFLICT (table_oid, shard) DO UPDATE
        SET marked_at = EXCLUDED.marked_at;
    RETURN NULL;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._install_cube_source_dirty_tracking(p_sql text)
RETURNS integer
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    rec record;
    v_installed integer := 0;
BEGIN
    FOR rec IN
        SELECT DISTINCT to_regclass(src.source_table) AS rel
        FROM unnest(coalesce(rvbbit._cube_source_tables(p_sql), '{}'::text[])) AS src(source_table)
        WHERE to_regclass(src.source_table) IS NOT NULL
    LOOP
        IF EXISTS (SELECT 1 FROM rvbbit.tables t WHERE t.table_oid = rec.rel::oid) THEN
            EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_cube_source_write ON %s', rec.rel);
            EXECUTE format(
                'CREATE TRIGGER rvbbit_cube_source_write
                     AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s
                     FOR EACH STATEMENT
                     EXECUTE FUNCTION rvbbit.mark_cube_source_write()',
                rec.rel
            );
            v_installed := v_installed + 1;
        END IF;
    END LOOP;
    RETURN v_installed;
END;
$fn$;

CREATE OR REPLACE VIEW rvbbit.cube_source_refresh_status AS
WITH cube_sources AS (
    SELECT
        c.name,
        coalesce(rvbbit._cube_source_tables(c.sql), '{}'::text[]) AS source_tables
    FROM rvbbit.cube_catalog c
),
expanded AS (
    SELECT
        cs.name,
        src.source_table,
        to_regclass(src.source_table) AS source_oid
    FROM cube_sources cs
    LEFT JOIN LATERAL unnest(cs.source_tables) AS src(source_table) ON true
),
source_rows AS (
    SELECT
        e.name,
        e.source_table,
        e.source_oid,
        EXISTS (SELECT 1 FROM rvbbit.tables t WHERE t.table_oid = e.source_oid) AS source_tracked,
        f.table_oid,
        f.shadow_heap_dirty,
        f.dirty_since,
        f.seconds_dirty,
        CASE
            WHEN f.last_write_at IS NULL THEN cwm.marked_at
            WHEN cwm.marked_at IS NULL THEN f.last_write_at
            ELSE greatest(f.last_write_at, cwm.marked_at)
        END AS source_last_write_at
    FROM expanded e
    LEFT JOIN rvbbit.accel_freshness f
           ON f.table_oid = e.source_oid OR f.table_name = e.source_table
    LEFT JOIN LATERAL (
        SELECT max(m.marked_at) AS marked_at
        FROM rvbbit.cube_source_write_markers m
        WHERE m.table_oid = e.source_oid
    ) cwm ON true
)
SELECT
    cs.name,
    cs.source_tables,
    count(sr.source_table)::integer AS source_count,
    count(sr.source_table) FILTER (WHERE sr.source_tracked)::integer AS tracked_source_count,
    coalesce(
        jsonb_agg(sr.source_table ORDER BY sr.source_table)
            FILTER (WHERE sr.source_table IS NOT NULL AND NOT sr.source_tracked),
        '[]'::jsonb
    ) AS untracked_sources,
    coalesce(
        jsonb_agg(sr.source_table ORDER BY sr.source_table)
            FILTER (WHERE sr.source_table IS NOT NULL AND coalesce(sr.shadow_heap_dirty, false)),
        '[]'::jsonb
    ) AS dirty_sources,
    count(sr.source_table) FILTER (WHERE coalesce(sr.shadow_heap_dirty, false))::integer
        AS dirty_source_count,
    coalesce(bool_or(coalesce(sr.shadow_heap_dirty, false)), false) AS source_dirty,
    min(sr.dirty_since) FILTER (WHERE coalesce(sr.shadow_heap_dirty, false)) AS source_dirty_since,
    max(sr.seconds_dirty) FILTER (WHERE coalesce(sr.shadow_heap_dirty, false)) AS source_seconds_dirty,
    max(sr.source_last_write_at) AS source_last_write_at
FROM cube_sources cs
LEFT JOIN source_rows sr ON sr.name = cs.name
GROUP BY cs.name, cs.source_tables;

CREATE OR REPLACE FUNCTION rvbbit.refresh_cube(p_name text)
RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    v_dest regclass := to_regclass('cubes.' || quote_ident(p_name));
    v_sql text;
    v_rows bigint;
    v_policy record;
    v_policy_json jsonb;
    v_started timestamptz := clock_timestamp();
    v_seconds numeric;
BEGIN
    IF v_dest IS NULL THEN
        RAISE EXCEPTION 'rvbbit.refresh_cube: cube % does not exist (define it first)', p_name;
    END IF;
    SELECT sql INTO v_sql FROM rvbbit.cube_catalog WHERE name = p_name;
    IF v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.refresh_cube: no definition for cube %', p_name;
    END IF;

    SELECT * INTO v_policy
    FROM rvbbit.cube_refresh_policy_effective(p_name);
    v_policy_json := to_jsonb(v_policy);

    PERFORM set_config('rvbbit.df_inprocess', 'off', true);
    PERFORM set_config('rvbbit.duck_threads', v_policy.query_threads::text, true);
    PERFORM set_config('rvbbit.compact_writer_threads', v_policy.writer_threads::text, true);
    PERFORM set_config('rvbbit.compact_scan_chunk_rows', v_policy.scan_chunk_rows::text, true);
    IF v_policy.metadata_profile IS NOT NULL THEN
        PERFORM set_config('rvbbit.compact_metadata_profile', v_policy.metadata_profile, true);
    END IF;
    PERFORM set_config(
        'rvbbit.compact_variants_sync',
        CASE WHEN v_policy.refresh_variants = 'sync' THEN 'on' ELSE 'off' END,
        true
    );

    BEGIN
        PERFORM rvbbit._install_cube_source_dirty_tracking(v_sql);
        SELECT rows_loaded INTO v_rows FROM rvbbit.snapshot_load(v_dest, v_sql);
        v_seconds := extract(epoch FROM clock_timestamp() - v_started);
        UPDATE rvbbit.cube_control
           SET refreshed_at = now(),
               last_refresh_started_at = v_started,
               last_rows = v_rows,
               last_error = NULL,
               last_refresh_policy = v_policy_json,
               last_refresh_seconds = v_seconds,
               updated_at = now()
         WHERE cube_name = p_name;
    EXCEPTION WHEN OTHERS THEN
        UPDATE rvbbit.cube_control
           SET last_error = SQLERRM,
               last_refresh_started_at = v_started,
               last_refresh_policy = v_policy_json,
               last_refresh_seconds = extract(epoch FROM clock_timestamp() - v_started),
               updated_at = now()
         WHERE cube_name = p_name;
        RAISE;
    END;
    RETURN v_rows;
END;
$fn$;

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

CREATE OR REPLACE PROCEDURE rvbbit.refresh_all_cubes(
    p_category        text    DEFAULT NULL,
    p_subcategory     text    DEFAULT NULL,
    p_sleep_seconds   numeric DEFAULT 0.5,
    p_force           boolean DEFAULT false,
    p_max_cubes       integer DEFAULT NULL,
    p_max_age_seconds integer DEFAULT NULL,
    p_retry_errors    boolean DEFAULT false
) LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
    v_seen integer := 0;
BEGIN
    FOR rec IN
        SELECT s.name
          FROM rvbbit.cube_refresh_status s
         WHERE (p_category    IS NULL OR s.category    = p_category)
           AND (p_subcategory IS NULL OR s.subcategory = p_subcategory)
           AND s.refresh_mode <> 'manual'
           AND (
                coalesce(p_force, false)
                OR s.recommended_action = 'refresh_cube'
                OR (
                    coalesce(p_retry_errors, false)
                    AND s.recommended_action = 'fix_error'
                )
                OR (
                    p_max_age_seconds IS NOT NULL
                    AND coalesce(s.seconds_since_refresh, 999999999) >= p_max_age_seconds
                )
           )
         ORDER BY
           CASE WHEN s.source_dirty THEN 0 ELSE 1 END,
           s.source_seconds_dirty DESC NULLS LAST,
           s.seconds_since_refresh DESC NULLS LAST,
           s.name
    LOOP
        EXIT WHEN p_max_cubes IS NOT NULL AND v_seen >= p_max_cubes;
        v_seen := v_seen + 1;
        BEGIN
            PERFORM rvbbit.refresh_cube(rec.name);
        EXCEPTION WHEN others THEN
            UPDATE rvbbit.cube_control
               SET last_error = SQLERRM, updated_at = now()
             WHERE cube_name = rec.name;
        END;
        COMMIT;
        IF coalesce(p_sleep_seconds, 0) > 0 THEN
            PERFORM pg_sleep(p_sleep_seconds);
        END IF;
    END LOOP;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.cube_health(p_name text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_reg regclass; v_key text; v_out jsonb; v_policy jsonb; v_status jsonb;
BEGIN
    v_reg := to_regclass('cubes.' || quote_ident(p_name));
    IF v_reg IS NULL THEN
        RETURN jsonb_build_object('cube', p_name, 'status', 'missing');
    END IF;
    v_key := v_reg::text;

    SELECT to_jsonb(p) - 'cube_name' INTO v_policy
    FROM rvbbit.cube_refresh_policy_effective(p_name) AS p;

    SELECT to_jsonb(s) - 'last_refresh_policy' INTO v_status
    FROM rvbbit.cube_refresh_status s
    WHERE s.name = p_name;

    SELECT jsonb_build_object(
        'cube', p_name,
        'freshness', jsonb_build_object(
            'last_refreshed_at',     ctl.refreshed_at,
            'last_refresh_at',       f.last_refresh_at,
            'seconds_since_refresh', coalesce(extract(epoch FROM (now() - ctl.refreshed_at))::bigint,
                                              f.seconds_since_refresh),
            'last_refresh_rows',     ctl.last_rows,
            'current_parquet_rows',  f.parquet_rows,
            'row_delta',             coalesce(f.parquet_rows, 0) - coalesce(ctl.last_rows, 0),
            'status', CASE
                WHEN ctl.last_error IS NOT NULL THEN 'error'
                WHEN coalesce((v_status->>'source_dirty')::boolean, false) THEN 'dirty'
                WHEN f.shadow_heap_dirty THEN 'dirty'
                WHEN coalesce(extract(epoch FROM (now() - ctl.refreshed_at)),
                              f.seconds_since_refresh, 999999999)
                     > coalesce((v_policy->>'refresh_interval_seconds')::integer, 86400)
                     THEN 'stale'
                ELSE 'fresh' END),
        'staleness', jsonb_build_object(
            'dirty_since',  coalesce((v_status->>'source_dirty_since')::timestamptz, f.dirty_since),
            'seconds_dirty', coalesce((v_status->>'source_seconds_dirty')::double precision, f.seconds_dirty),
            'dirty',         coalesce((v_status->>'dirty')::boolean, false)),
        'sources', jsonb_build_object(
            'tables',             coalesce(v_status->'source_tables', '[]'::jsonb),
            'source_count',       coalesce((v_status->>'source_count')::integer, 0),
            'tracked_count',      coalesce((v_status->>'tracked_source_count')::integer, 0),
            'dirty_count',        coalesce((v_status->>'dirty_source_count')::integer, 0),
            'dirty_sources',      coalesce(v_status->'dirty_sources', '[]'::jsonb),
            'untracked_sources',  coalesce(v_status->'untracked_sources', '[]'::jsonb),
            'last_write_at',      v_status->>'source_last_write_at'),
        'drift', jsonb_build_object(
            'unmirrored_rows', f.est_unmirrored_rows,
            'drift_rows',      f.drift_rows,
            'drift_ratio',     f.drift_ratio,
            'recommendation', CASE
                WHEN coalesce((v_status->>'source_dirty')::boolean, false) THEN 'refresh cube'
                WHEN f.drift_ratio IS NULL  THEN 'unknown'
                WHEN f.drift_ratio < 0.1    THEN 'skip'
                WHEN f.drift_ratio < 0.5    THEN 'delta'
                ELSE 'full rebuild' END),
        'usage', jsonb_build_object(
            'heap_seq_scans',   f.heap_seq_scans,
            'last_rebuild_ms',  f.last_rebuild_ms,
            'last_rebuild_rows', f.last_rebuild_rows),
        'refresh_policy', coalesce(v_policy, '{}'::jsonb),
        'autopilot', coalesce(v_status, '{}'::jsonb),
        'last_error', ctl.last_error)
    INTO v_out
    FROM rvbbit.cube_control ctl
    LEFT JOIN rvbbit.accel_freshness f ON f.table_name = v_key
    WHERE ctl.cube_name = p_name;

    RETURN coalesce(v_out, jsonb_build_object('cube', p_name, 'status', 'unknown'));
END $fn$;
