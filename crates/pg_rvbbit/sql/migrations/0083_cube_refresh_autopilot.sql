-- 0083_cube_refresh_autopilot
--
-- Cubes are still materialized snapshots: refresh_cube runs the cube SQL into
-- cubes.<name>, then snapshot_load writes a new canonical accelerator
-- generation. The recent accelerator work makes that second half much cheaper
-- when it is treated as a bulk-load profile:
--
--   * keep cube query execution capped so foreground work keeps CPU;
--   * let the parquet writer overlap completed canonical chunks;
--   * defer layout variants off the critical refresh path by default;
--   * expose the effective policy/status for the UI and automation.
--
-- This does not claim incremental cubes yet. It makes full cube refreshes
-- easier to leave on autopilot.

ALTER TABLE rvbbit.cube_control
    ADD COLUMN IF NOT EXISTS last_refresh_policy jsonb,
    ADD COLUMN IF NOT EXISTS last_refresh_seconds numeric;

CREATE TABLE IF NOT EXISTS rvbbit.cube_refresh_policy (
    cube_name text PRIMARY KEY,
    mode text NOT NULL DEFAULT 'auto'
        CHECK (mode IN ('auto', 'conservative', 'bulk', 'manual')),
    query_threads integer CHECK (query_threads IS NULL OR query_threads BETWEEN 1 AND 64),
    writer_threads integer CHECK (writer_threads IS NULL OR writer_threads BETWEEN 1 AND 64),
    scan_chunk_rows integer CHECK (scan_chunk_rows IS NULL OR scan_chunk_rows > 0),
    metadata_profile text CHECK (
        metadata_profile IS NULL OR metadata_profile IN ('rich', 'minimal')
    ),
    refresh_variants text CHECK (
        refresh_variants IS NULL OR refresh_variants IN ('sync', 'deferred')
    ),
    note text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION rvbbit._int_setting(p_name text)
RETURNS integer
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_raw text;
BEGIN
    v_raw := nullif(current_setting(p_name, true), '');
    IF v_raw IS NULL OR v_raw !~ '^[0-9]+$' THEN
        RETURN NULL;
    END IF;
    RETURN v_raw::integer;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit._text_setting(p_name text)
RETURNS text
LANGUAGE sql STABLE AS $fn$
    SELECT nullif(btrim(current_setting(p_name, true)), '')
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.cube_refresh_policy_effective(p_name text)
RETURNS TABLE (
    cube_name text,
    mode text,
    query_threads integer,
    writer_threads integer,
    scan_chunk_rows integer,
    metadata_profile text,
    refresh_variants text
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
        CASE base.mode WHEN 'conservative' THEN 'deferred' ELSE 'deferred' END
    ) AS refresh_variants
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
    p_note text DEFAULT NULL
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
        metadata_profile, refresh_variants, note, updated_at
    ) VALUES (
        p_name, v_mode, p_query_threads, p_writer_threads, p_scan_chunk_rows,
        nullif(btrim(p_metadata_profile), ''),
        nullif(btrim(p_refresh_variants), ''),
        p_note, now()
    )
    ON CONFLICT (cube_name) DO UPDATE SET
        mode = EXCLUDED.mode,
        query_threads = EXCLUDED.query_threads,
        writer_threads = EXCLUDED.writer_threads,
        scan_chunk_rows = EXCLUDED.scan_chunk_rows,
        metadata_profile = EXCLUDED.metadata_profile,
        refresh_variants = EXCLUDED.refresh_variants,
        note = EXCLUDED.note,
        updated_at = now();

    SELECT to_jsonb(p) INTO v_effective
    FROM rvbbit.cube_refresh_policy_effective(p_name) AS p;
    RETURN v_effective;
END;
$fn$;

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

    -- Resource cap for the materialization query. Keep this separate from the
    -- parquet writer profile so query CPU and file-write overlap can be tuned
    -- independently.
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
        SELECT rows_loaded INTO v_rows FROM rvbbit.snapshot_load(v_dest, v_sql);
        v_seconds := extract(epoch FROM clock_timestamp() - v_started);
        UPDATE rvbbit.cube_control
           SET refreshed_at = now(),
               last_rows = v_rows,
               last_error = NULL,
               last_refresh_policy = v_policy_json,
               last_refresh_seconds = v_seconds,
               updated_at = now()
         WHERE cube_name = p_name;
    EXCEPTION WHEN OTHERS THEN
        UPDATE rvbbit.cube_control
           SET last_error = SQLERRM,
               last_refresh_policy = v_policy_json,
               last_refresh_seconds = extract(epoch FROM clock_timestamp() - v_started),
               updated_at = now()
         WHERE cube_name = p_name;
        RAISE;
    END;
    RETURN v_rows;
END;
$fn$;

CREATE OR REPLACE PROCEDURE rvbbit.refresh_all_cubes(
    p_category      text    DEFAULT NULL,
    p_subcategory   text    DEFAULT NULL,
    p_sleep_seconds numeric DEFAULT 0.5
) LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT c.name
          FROM rvbbit.cube_catalog c
          CROSS JOIN LATERAL rvbbit.cube_refresh_policy_effective(c.name) p
         WHERE (p_category    IS NULL OR c.category    = p_category)
           AND (p_subcategory IS NULL OR c.subcategory = p_subcategory)
           AND p.mode <> 'manual'
         ORDER BY c.name
    LOOP
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
        count(DISTINCT (rgv.layout, rgv.rg_id))::bigint AS variant_files,
        coalesce(max(rg.created_at), '-infinity'::timestamptz) AS newest_row_group_at,
        coalesce(max(rgv.created_at), '-infinity'::timestamptz) AS newest_variant_at
    FROM cubes c
    LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.table_oid
    LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = c.table_oid
    GROUP BY c.name
)
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
    coalesce(ph.row_groups, 0) AS row_groups,
    coalesce(ph.variant_files, 0) AS variant_files,
    (
        coalesce(ph.row_groups, 0) > 0
        AND (
            coalesce(ph.variant_files, 0) = 0
            OR ph.newest_variant_at < ph.newest_row_group_at
        )
    ) AS variants_pending,
    coalesce(f.shadow_heap_dirty, false) AS dirty,
    f.seconds_since_refresh,
    CASE
        WHEN ctl.last_error IS NOT NULL THEN 'fix_error'
        WHEN p.mode = 'manual' THEN 'manual'
        WHEN coalesce(f.shadow_heap_dirty, false) THEN 'refresh_cube'
        WHEN coalesce(ph.row_groups, 0) > 0
             AND (coalesce(ph.variant_files, 0) = 0 OR ph.newest_variant_at < ph.newest_row_group_at)
             THEN 'maintain_storage'
        ELSE 'ok'
    END AS recommended_action,
    ctl.last_refresh_policy
FROM cubes c
LEFT JOIN rvbbit.cube_control ctl ON ctl.cube_name = c.name
CROSS JOIN LATERAL rvbbit.cube_refresh_policy_effective(c.name) p
LEFT JOIN physical ph ON ph.name = c.name
LEFT JOIN rvbbit.accel_freshness f ON f.table_name = c.table_oid::text;

CREATE OR REPLACE FUNCTION rvbbit.cube_health(p_name text)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
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
                WHEN ctl.last_error IS NOT NULL                              THEN 'error'
                WHEN f.shadow_heap_dirty                                     THEN 'dirty'
                WHEN coalesce(extract(epoch FROM (now() - ctl.refreshed_at)),
                              f.seconds_since_refresh, 999999) > 86400       THEN 'stale'
                ELSE 'fresh' END),
        'staleness', jsonb_build_object(
            'dirty_since',  f.dirty_since,
            'seconds_dirty', f.seconds_dirty,
            'dirty',         coalesce(f.shadow_heap_dirty, false)),
        'drift', jsonb_build_object(
            'unmirrored_rows', f.est_unmirrored_rows,
            'drift_rows',      f.drift_rows,
            'drift_ratio',     f.drift_ratio,
            'recommendation', CASE
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
