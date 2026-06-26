-- One operator-facing maintenance surface over table accelerators and cubes.
--
-- Lower-level primitives stay available for debugging:
--   refresh_acceleration = catch up accelerator files from heap changes
--   rebuild_acceleration = compact current table state into clean files
--   refresh_cube         = recompute a cube snapshot
--   refresh_layout_variants = build optional file layouts
--
-- The public mental model should be simpler: inspect rvbbit.maintenance_status
-- and run rvbbit.maintain(...).

DROP VIEW IF EXISTS rvbbit.maintenance_status;

CREATE OR REPLACE VIEW rvbbit.maintenance_status AS
WITH table_status AS (
    SELECT
        'table'::text AS target_kind,
        f.table_name AS target_name,
        CASE
            WHEN f.op_running THEN 'running'
            WHEN NOT e.active THEN 'disabled'
            WHEN e.strategy = 'manual' THEN 'manual'
            WHEN f.shadow_heap_dirty
                 AND coalesce(f.drift_ratio, 1.0) >= coalesce(e.full_rebuild_drift_ratio, 0.5)
                THEN 'needs_compaction'
            WHEN e.max_row_groups_before_rebuild IS NOT NULL
                 AND coalesce(f.row_groups, 0) >= e.max_row_groups_before_rebuild
                THEN 'needs_compaction'
            WHEN e.max_tombstones_before_rebuild IS NOT NULL
                 AND coalesce(f.tombstones, 0) >= e.max_tombstones_before_rebuild
                THEN 'needs_compaction'
            WHEN f.shadow_heap_dirty THEN 'lagging'
            WHEN coalesce(f.row_groups, 0) = 0 AND coalesce(f.heap_live_tuples, 0) > 0 THEN 'lagging'
            ELSE 'current'
        END AS lifecycle_state,
        CASE
            WHEN f.op_running THEN 'wait'
            WHEN NOT e.active THEN 'none'
            WHEN e.strategy = 'manual' THEN 'none'
            WHEN f.shadow_heap_dirty
                 AND coalesce(f.drift_ratio, 1.0) >= coalesce(e.full_rebuild_drift_ratio, 0.5)
                THEN 'compact'
            WHEN e.max_row_groups_before_rebuild IS NOT NULL
                 AND coalesce(f.row_groups, 0) >= e.max_row_groups_before_rebuild
                THEN 'compact'
            WHEN e.max_tombstones_before_rebuild IS NOT NULL
                 AND coalesce(f.tombstones, 0) >= e.max_tombstones_before_rebuild
                THEN 'compact'
            WHEN f.shadow_heap_dirty THEN 'catch_up'
            WHEN coalesce(f.row_groups, 0) = 0 AND coalesce(f.heap_live_tuples, 0) > 0 THEN 'catch_up'
            ELSE 'none'
        END AS maintenance_action,
        CASE
            WHEN f.op_running THEN false
            WHEN NOT e.active THEN false
            WHEN e.strategy = 'manual' THEN false
            WHEN f.shadow_heap_dirty THEN true
            WHEN coalesce(f.row_groups, 0) = 0 AND coalesce(f.heap_live_tuples, 0) > 0 THEN true
            WHEN e.max_row_groups_before_rebuild IS NOT NULL
                 AND coalesce(f.row_groups, 0) >= e.max_row_groups_before_rebuild THEN true
            WHEN e.max_tombstones_before_rebuild IS NOT NULL
                 AND coalesce(f.tombstones, 0) >= e.max_tombstones_before_rebuild THEN true
            ELSE false
        END AS needs_maintenance,
        CASE
            WHEN f.op_running THEN 'maintenance is already running'
            WHEN NOT e.active THEN 'policy disabled'
            WHEN e.strategy = 'manual' THEN 'manual policy'
            WHEN f.shadow_heap_dirty
                 AND coalesce(f.drift_ratio, 1.0) >= coalesce(e.full_rebuild_drift_ratio, 0.5)
                THEN format('drift ratio %s reached compaction threshold %s',
                    round(coalesce(f.drift_ratio, 1.0)::numeric, 4),
                    round(coalesce(e.full_rebuild_drift_ratio, 0.5)::numeric, 4))
            WHEN e.max_row_groups_before_rebuild IS NOT NULL
                 AND coalesce(f.row_groups, 0) >= e.max_row_groups_before_rebuild
                THEN format('row-group fanout %s reached %s',
                    coalesce(f.row_groups, 0),
                    e.max_row_groups_before_rebuild)
            WHEN e.max_tombstones_before_rebuild IS NOT NULL
                 AND coalesce(f.tombstones, 0) >= e.max_tombstones_before_rebuild
                THEN format('tombstone count %s reached %s',
                    coalesce(f.tombstones, 0),
                    e.max_tombstones_before_rebuild)
            WHEN f.shadow_heap_dirty THEN 'heap changes are not fully reflected in accelerator files'
            WHEN coalesce(f.row_groups, 0) = 0 AND coalesce(f.heap_live_tuples, 0) > 0 THEN 'no accelerator files exist yet'
            ELSE 'current'
        END AS reason,
        CASE
            WHEN f.op_running THEN 90
            WHEN e.strategy = 'manual' OR NOT e.active THEN 80
            WHEN f.shadow_heap_dirty
                 AND coalesce(f.drift_ratio, 1.0) >= coalesce(e.full_rebuild_drift_ratio, 0.5) THEN 10
            WHEN f.shadow_heap_dirty THEN 20
            WHEN coalesce(f.row_groups, 0) = 0 AND coalesce(f.heap_live_tuples, 0) > 0 THEN 30
            WHEN e.max_row_groups_before_rebuild IS NOT NULL
                 AND coalesce(f.row_groups, 0) >= e.max_row_groups_before_rebuild THEN 40
            WHEN e.max_tombstones_before_rebuild IS NOT NULL
                 AND coalesce(f.tombstones, 0) >= e.max_tombstones_before_rebuild THEN 40
            ELSE 100
        END AS priority,
        f.parquet_rows AS current_rows,
        f.heap_live_tuples,
        f.row_groups,
        NULL::bigint AS variant_files,
        false AS variants_pending,
        f.seconds_dirty AS seconds_lag,
        f.last_write_at,
        f.last_refresh_at AS last_maintained_at,
        jsonb_build_object(
            'strategy', e.strategy,
            'freshness_target_secs', e.freshness_target_secs,
            'min_interval_secs', e.min_interval_secs,
            'full_rebuild_drift_ratio', e.full_rebuild_drift_ratio,
            'max_row_groups_before_rebuild', e.max_row_groups_before_rebuild,
            'max_tombstones_before_rebuild', e.max_tombstones_before_rebuild,
            'explicit', e.explicit
        ) AS policy,
        jsonb_build_object(
            'shadow_heap_dirty', f.shadow_heap_dirty,
            'parquet_authoritative', f.parquet_authoritative,
            'drift_rows', f.drift_rows,
            'drift_ratio', f.drift_ratio,
            'tombstones', f.tombstones,
            'heap_seq_scans', f.heap_seq_scans
        ) AS raw_status
    FROM rvbbit.accel_freshness f
    JOIN rvbbit.accel_policy_effective e ON e.table_oid = f.table_oid
),
cube_status AS (
    SELECT
        'cube'::text AS target_kind,
        s.name AS target_name,
        CASE
            WHEN NOT coalesce(s.enabled, true) THEN 'disabled'
            WHEN s.last_error IS NOT NULL THEN 'broken'
            WHEN s.refresh_mode = 'manual' THEN 'manual'
            WHEN s.refreshed_at IS NULL THEN 'refresh_due'
            WHEN s.source_dirty OR s.cube_dirty THEN 'refresh_due'
            WHEN s.refresh_interval_seconds IS NOT NULL
                 AND coalesce(s.seconds_since_refresh, 999999999) >= s.refresh_interval_seconds
                THEN 'refresh_due'
            WHEN s.variants_pending THEN 'layouts_pending'
            ELSE 'current'
        END AS lifecycle_state,
        CASE
            WHEN NOT coalesce(s.enabled, true) THEN 'none'
            WHEN s.last_error IS NOT NULL THEN 'refresh_snapshot'
            WHEN s.refresh_mode = 'manual' THEN 'none'
            WHEN s.refreshed_at IS NULL THEN 'refresh_snapshot'
            WHEN s.source_dirty OR s.cube_dirty THEN 'refresh_snapshot'
            WHEN s.refresh_interval_seconds IS NOT NULL
                 AND coalesce(s.seconds_since_refresh, 999999999) >= s.refresh_interval_seconds
                THEN 'refresh_snapshot'
            WHEN s.variants_pending THEN 'build_layouts'
            ELSE 'none'
        END AS maintenance_action,
        CASE
            WHEN NOT coalesce(s.enabled, true) THEN false
            WHEN s.refresh_mode = 'manual' THEN false
            WHEN s.last_error IS NOT NULL THEN true
            WHEN s.refreshed_at IS NULL THEN true
            WHEN s.source_dirty OR s.cube_dirty THEN true
            WHEN s.refresh_interval_seconds IS NOT NULL
                 AND coalesce(s.seconds_since_refresh, 999999999) >= s.refresh_interval_seconds THEN true
            WHEN s.variants_pending THEN true
            ELSE false
        END AS needs_maintenance,
        CASE
            WHEN NOT coalesce(s.enabled, true) THEN 'cube disabled'
            WHEN s.last_error IS NOT NULL THEN 'last refresh failed'
            WHEN s.refresh_mode = 'manual' THEN 'manual policy'
            WHEN s.refreshed_at IS NULL THEN 'cube snapshot has not been built'
            WHEN s.source_dirty THEN 'source tables changed since the last cube snapshot'
            WHEN s.cube_dirty THEN 'cube table changed outside the snapshot workflow'
            WHEN s.refresh_interval_seconds IS NOT NULL
                 AND coalesce(s.seconds_since_refresh, 999999999) >= s.refresh_interval_seconds
                THEN format('snapshot age %ss reached interval %ss',
                    round(coalesce(s.seconds_since_refresh, 0))::bigint,
                    s.refresh_interval_seconds)
            WHEN s.variants_pending THEN 'optional layouts are missing or older than the canonical files'
            ELSE 'current'
        END AS reason,
        CASE
            WHEN s.last_error IS NOT NULL THEN 5
            WHEN s.refreshed_at IS NULL OR s.source_dirty OR s.cube_dirty THEN 15
            WHEN s.refresh_interval_seconds IS NOT NULL
                 AND coalesce(s.seconds_since_refresh, 999999999) >= s.refresh_interval_seconds THEN 25
            WHEN s.variants_pending THEN 50
            WHEN s.refresh_mode = 'manual' THEN 80
            ELSE 100
        END AS priority,
        s.last_rows AS current_rows,
        NULL::bigint AS heap_live_tuples,
        s.row_groups,
        s.variant_files,
        s.variants_pending,
        s.source_seconds_dirty AS seconds_lag,
        s.source_last_write_at AS last_write_at,
        s.refreshed_at AS last_maintained_at,
        jsonb_build_object(
            'mode', s.refresh_mode,
            'query_threads', s.query_threads,
            'writer_threads', s.writer_threads,
            'scan_chunk_rows', s.scan_chunk_rows,
            'metadata_profile', s.metadata_profile,
            'refresh_variants', s.refresh_variants,
            'refresh_interval_seconds', s.refresh_interval_seconds
        ) AS policy,
        to_jsonb(s) - 'last_refresh_policy' AS raw_status
    FROM rvbbit.cube_refresh_status s
)
SELECT * FROM table_status
UNION ALL
SELECT * FROM cube_status;

CREATE OR REPLACE FUNCTION rvbbit.maintain(
    p_target_kind text DEFAULT NULL,
    p_target_name text DEFAULT NULL,
    p_dry_run boolean DEFAULT false,
    p_budget integer DEFAULT NULL,
    p_force boolean DEFAULT false
) RETURNS TABLE (
    target_kind text,
    target_name text,
    lifecycle_state text,
    maintenance_action text,
    executed boolean,
    status text,
    rows_written bigint,
    details jsonb,
    error text
) LANGUAGE plpgsql AS $$
DECLARE
    rec record;
    v_action text;
    v_rel regclass;
    v_result jsonb;
    v_layout_rows bigint;
    v_rows bigint;
    v_count integer := 0;
BEGIN
    FOR rec IN
        SELECT *
        FROM rvbbit.maintenance_status s
        WHERE (p_target_kind IS NULL OR s.target_kind = lower(p_target_kind))
          AND (p_target_name IS NULL OR s.target_name = p_target_name)
          AND (
              coalesce(p_force, false)
              OR s.needs_maintenance
              OR p_target_name IS NOT NULL
          )
        ORDER BY s.priority, s.target_kind, s.target_name
    LOOP
        v_action := rec.maintenance_action;

        IF coalesce(p_force, false) AND v_action = 'none' THEN
            IF rec.target_kind = 'cube' THEN
                v_action := 'refresh_snapshot';
            ELSIF rec.target_kind = 'table' THEN
                v_action := CASE
                    WHEN rec.lifecycle_state = 'needs_compaction' THEN 'compact'
                    ELSE 'catch_up'
                END;
            END IF;
        END IF;

        target_kind := rec.target_kind;
        target_name := rec.target_name;
        lifecycle_state := rec.lifecycle_state;
        maintenance_action := v_action;
        rows_written := NULL;
        details := jsonb_build_object('reason', rec.reason, 'policy', rec.policy);
        error := NULL;

        IF p_budget IS NOT NULL AND v_count >= p_budget AND v_action <> 'none' THEN
            executed := false;
            status := 'deferred';
            details := details || jsonb_build_object('deferred_reason', 'maintenance budget reached');
            RETURN NEXT;
            CONTINUE;
        END IF;

        IF v_action IN ('none', 'wait') THEN
            executed := false;
            status := CASE WHEN v_action = 'wait' THEN 'deferred' ELSE 'skip' END;
            RETURN NEXT;
            CONTINUE;
        END IF;

        IF coalesce(p_dry_run, false) THEN
            executed := false;
            status := 'planned';
            RETURN NEXT;
            CONTINUE;
        END IF;

        v_count := v_count + 1;

        BEGIN
            IF rec.target_kind = 'table' THEN
                v_rel := to_regclass(rec.target_name);
                IF v_rel IS NULL THEN
                    RAISE EXCEPTION 'table % no longer exists', rec.target_name;
                END IF;

                IF v_action = 'compact' THEN
                    SELECT rvbbit.rebuild_acceleration(v_rel, true) INTO v_result;
                ELSIF v_action = 'catch_up' THEN
                    SELECT rvbbit.refresh_acceleration(v_rel, true) INTO v_result;
                ELSE
                    RAISE EXCEPTION 'unsupported table maintenance action %', v_action;
                END IF;

                rows_written := coalesce((v_result->>'rows_written')::bigint, 0);
                status := coalesce(v_result->>'status', 'ok');
                details := details || jsonb_build_object('result', v_result);
                executed := true;
                RETURN NEXT;
            ELSIF rec.target_kind = 'cube' THEN
                v_rel := to_regclass('cubes.' || quote_ident(rec.target_name));
                IF v_action = 'refresh_snapshot' THEN
                    SELECT rvbbit.refresh_cube(rec.target_name) INTO v_rows;
                    rows_written := coalesce(v_rows, 0);
                    details := details || jsonb_build_object('snapshot_rows', rows_written);
                    v_rel := to_regclass('cubes.' || quote_ident(rec.target_name));
                    IF v_rel IS NOT NULL
                       AND coalesce(rec.policy->>'refresh_variants', 'deferred') <> 'skip' THEN
                        SELECT rvbbit.refresh_layout_variants(v_rel::oid) INTO v_layout_rows;
                        details := details || jsonb_build_object('layout_rows', coalesce(v_layout_rows, 0));
                    END IF;
                    status := 'ok';
                    executed := true;
                    RETURN NEXT;
                ELSIF v_action = 'build_layouts' THEN
                    IF v_rel IS NULL THEN
                        RAISE EXCEPTION 'cube table cubes.% does not exist', rec.target_name;
                    END IF;
                    SELECT rvbbit.refresh_layout_variants(v_rel::oid) INTO v_layout_rows;
                    rows_written := coalesce(v_layout_rows, 0);
                    details := details || jsonb_build_object('layout_rows', rows_written);
                    status := 'ok';
                    executed := true;
                    RETURN NEXT;
                ELSE
                    RAISE EXCEPTION 'unsupported cube maintenance action %', v_action;
                END IF;
            ELSE
                RAISE EXCEPTION 'unsupported maintenance target kind %', rec.target_kind;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            executed := true;
            status := 'failed';
            rows_written := NULL;
            error := SQLERRM;
            RETURN NEXT;
        END;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.maintain_cube(
    p_name text,
    p_dry_run boolean DEFAULT false,
    p_force boolean DEFAULT false
) RETURNS TABLE (
    target_kind text,
    target_name text,
    lifecycle_state text,
    maintenance_action text,
    executed boolean,
    status text,
    rows_written bigint,
    details jsonb,
    error text
) LANGUAGE sql AS $$
    SELECT *
    FROM rvbbit.maintain('cube', p_name, p_dry_run, NULL, p_force);
$$;

CREATE OR REPLACE FUNCTION rvbbit.cube_health(p_name text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_reg regclass; v_key text; v_out jsonb; v_policy jsonb; v_status jsonb; v_maintenance jsonb;
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

    SELECT to_jsonb(m) - 'raw_status' INTO v_maintenance
    FROM rvbbit.maintenance_status m
    WHERE m.target_kind = 'cube'
      AND m.target_name = p_name;

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
                WHEN coalesce((v_status->>'source_dirty')::boolean, false) THEN 'refresh snapshot'
                WHEN f.drift_ratio IS NULL  THEN 'unknown'
                WHEN f.drift_ratio < 0.1    THEN 'skip'
                WHEN f.drift_ratio < 0.5    THEN 'catch up'
                ELSE 'compact' END),
        'usage', jsonb_build_object(
            'heap_seq_scans',   f.heap_seq_scans,
            'last_rebuild_ms',  f.last_rebuild_ms,
            'last_rebuild_rows', f.last_rebuild_rows),
        'refresh_policy', coalesce(v_policy, '{}'::jsonb),
        'autopilot', coalesce(v_status, '{}'::jsonb),
        'maintenance', coalesce(v_maintenance, '{}'::jsonb),
        'last_error', ctl.last_error)
    INTO v_out
    FROM rvbbit.cube_control ctl
    LEFT JOIN rvbbit.accel_freshness f ON f.table_name = v_key
    WHERE ctl.cube_name = p_name;

    RETURN coalesce(v_out, jsonb_build_object('cube', p_name, 'status', 'unknown'));
END $fn$;
