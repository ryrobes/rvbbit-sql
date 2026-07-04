-- 0124_gqe_warm_prior_routing
--
-- GPU/GQE warm-prior routing support (2026-07-03). The auto-router never picked
-- GQE because it is absent from the cold-path FALLBACK_* candidate orders; and
-- the training pass measured GQE cold (inflated median -> no overlay pin). This
-- adds the runtime pieces the router prior needs:
--   * rvbbit.gqe_warm_state  - cross-backend "GQE is warm & functional" signal.
--   * rvbbit.warm_gpu_gqe()  - self-gating warm probe; records warm only when a
--                              forced-GQE query actually ran (fail-open OFF).
--   * accel_tick             - calls warm on the heartbeat when the prior is on
--                              (reuses the existing per-minute cron; no new job).
-- The router-side prior (choose_no_profile_route) fires only when this warm state
-- is fresh, so no user query ever pays a GQE cold-start.

CREATE TABLE IF NOT EXISTS rvbbit.gqe_warm_state (
    id       integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    warm_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE rvbbit.gqe_warm_state IS
    'Singleton: last time rvbbit.warm_gpu_gqe() confirmed the GQE server served a query. '
    'The router GQE prior only routes to GQE when this is fresh, so prior-routed queries never cold-start.';

CREATE OR REPLACE FUNCTION rvbbit.warm_gpu_gqe() RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_tbl   regclass;
    v_ok    boolean := false;
    v_err   text := NULL;
    v_route text;
    v_sql   text;
    v_prior text := lower(coalesce(nullif(current_setting('rvbbit.route_gpu_gqe_prior', true), ''), 'off'));
BEGIN
    -- Self-gating: the heartbeat calls this unconditionally, so it must be a
    -- cheap no-op when the prior is disabled.
    IF v_prior IN ('off','false','0','no') THEN
        RETURN jsonb_build_object('status', 'disabled');
    END IF;

    -- Smallest accelerated table: a trivial forced-GQE count(*) over it starts
    -- the GQE server and proves it can serve a query.
    SELECT rg.table_oid::regclass INTO v_tbl
    FROM rvbbit.row_groups rg
    GROUP BY rg.table_oid
    ORDER BY sum(rg.n_rows) ASC NULLS LAST
    LIMIT 1;

    IF v_tbl IS NULL THEN
        RETURN jsonb_build_object('status', 'no_accelerated_tables');
    END IF;

    v_sql := format('SELECT count(*) FROM %s', v_tbl);
    PERFORM set_config('rvbbit.route_force_candidate', 'gpu_gqe', true);

    -- Confirm GQE would ACTUALLY serve this query (eligible + gate on). A
    -- forced-but-ineligible GQE silently falls back to native, which must NOT be
    -- recorded as warm. route_explain re-runs candidate_availability without
    -- executing, so its `route` is the true engine that would run.
    SELECT rvbbit.route_explain(v_sql)->>'route' INTO v_route;
    IF v_route IS DISTINCT FROM 'gpu_gqe' THEN
        PERFORM set_config('rvbbit.route_force_candidate', '', true);
        RETURN jsonb_build_object('status', 'unavailable', 'table', v_tbl::text, 'route', v_route);
    END IF;

    BEGIN
        -- fail-open OFF so a GQE failure ERRORS (caught below) instead of
        -- silently falling back to native and falsely reporting warm.
        PERFORM set_config('rvbbit.duck_backend_fail_open', 'off', true);
        EXECUTE v_sql;
        v_ok := true;
    EXCEPTION WHEN OTHERS THEN
        v_err := SQLERRM;
    END;
    -- Restore (is_local, but accel_tick calls us inside its own txn).
    PERFORM set_config('rvbbit.route_force_candidate', '', true);
    PERFORM set_config('rvbbit.duck_backend_fail_open', 'on', true);

    IF v_ok THEN
        INSERT INTO rvbbit.gqe_warm_state (id, warm_at) VALUES (1, clock_timestamp())
        ON CONFLICT (id) DO UPDATE SET warm_at = excluded.warm_at;
    END IF;

    RETURN jsonb_build_object(
        'status', CASE WHEN v_ok THEN 'warm' ELSE 'failed' END,
        'table', v_tbl::text,
        'error', v_err
    );
END $fn$;
COMMENT ON FUNCTION rvbbit.warm_gpu_gqe() IS
    'Best-effort GPU/GQE warm probe. No-op unless rvbbit.route_gpu_gqe_prior is enabled. '
    'Runs a forced-GQE count(*) (fail-open off) and records rvbbit.gqe_warm_state on success.';

CREATE OR REPLACE FUNCTION rvbbit.accel_tick(budget integer DEFAULT NULL::integer, dry_run boolean DEFAULT false, lance_budget integer DEFAULT 1)
 RETURNS TABLE(table_oid oid, table_name text, strategy text, action text, reason text, drift_rows bigint, drift_ratio double precision, seconds_dirty double precision, heap_seq_scans bigint, executed boolean, status text, rows_written bigint, error text)
 LANGUAGE plpgsql
AS $function$
DECLARE
    cand        record;
    acted       integer := 0;
    lance_acted integer := 0;
    do_execute  boolean;
    should_act  boolean;
    prop_action text;
    prop_reason text;
    act_reason  text;
    maintenance_pressure boolean;
    res         jsonb;
    last_scans  bigint;
    used_today  integer;
    is_lance    boolean;
BEGIN
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 7) THEN
        RETURN;
    END IF;

    -- Warm-prior support (audit 2026-07-03): keep GPU/GQE warm on the heartbeat
    -- when the router's GQE prior is enabled, so prior-routed queries never pay a
    -- cold-start and route training measures GQE fairly. Best-effort; a warm
    -- failure never breaks the tick. rvbbit.warm_gpu_gqe() self-gates (cheap
    -- no-op) when the prior is disabled.
    IF NOT dry_run
       AND lower(coalesce(nullif(current_setting('rvbbit.route_gpu_gqe_prior', true), ''), 'off'))
           NOT IN ('off','false','0','no') THEN
        BEGIN
            PERFORM rvbbit.warm_gpu_gqe();
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END IF;

    FOR cand IN
        SELECT f.table_oid        AS f_oid,
               f.table_name       AS f_name,
               f.shadow_heap_dirty,
               f.seconds_dirty,
               f.seconds_since_refresh,
               f.row_groups,
               f.tombstones,
               f.drift_rows,
               f.drift_ratio,
               f.heap_seq_scans,
               f.lance_accelerated,
               e.strategy,
               e.freshness_target_secs,
               e.min_interval_secs,
               e.daily_refresh_budget,
               e.full_rebuild_drift_ratio,
               e.max_row_groups_before_rebuild,
               e.max_tombstones_before_rebuild,
               e.lance_separate
          FROM rvbbit.accel_freshness f
          JOIN rvbbit.accel_policy_effective e ON e.table_oid = f.table_oid
         WHERE e.active
           AND e.strategy <> 'manual'
         ORDER BY (f.drift_rows * (1 + f.heap_seq_scans)) DESC,
                  f.seconds_dirty DESC NULLS LAST
    LOOP
        IF cand.max_row_groups_before_rebuild IS NOT NULL
           AND cand.row_groups >= cand.max_row_groups_before_rebuild THEN
            prop_action := 'full';
            prop_reason := format(
                'row_group_fanout %s >= %s',
                cand.row_groups,
                cand.max_row_groups_before_rebuild
            );
        ELSIF cand.max_tombstones_before_rebuild IS NOT NULL
              AND cand.tombstones >= cand.max_tombstones_before_rebuild THEN
            prop_action := 'full';
            prop_reason := format(
                'tombstone_count %s >= %s',
                cand.tombstones,
                cand.max_tombstones_before_rebuild
            );
        ELSIF cand.shadow_heap_dirty THEN
            IF cand.drift_ratio IS NULL THEN
                prop_action := 'full';
                prop_reason := 'no accelerator baseline';
            ELSIF cand.drift_ratio >= cand.full_rebuild_drift_ratio THEN
                prop_action := 'full';
                prop_reason := format(
                    'drift_ratio %s >= %s',
                    round(cand.drift_ratio::numeric, 6),
                    round(cand.full_rebuild_drift_ratio::numeric, 6)
                );
            ELSE
                prop_action := 'delta';
                prop_reason := 'dirty';
            END IF;
        ELSE
            prop_action := 'skip';
            prop_reason := 'clean';
        END IF;

        is_lance   := coalesce(cand.lance_accelerated, false) AND coalesce(cand.lance_separate, true);
        should_act := false;
        act_reason := prop_reason;
        maintenance_pressure := prop_action = 'full'
            AND (prop_reason LIKE 'row_group_fanout %' OR prop_reason LIKE 'tombstone_count %');

        IF prop_action = 'skip' THEN
            act_reason := prop_reason;
        ELSIF cand.seconds_since_refresh IS NOT NULL
              AND cand.seconds_since_refresh < cand.min_interval_secs THEN
            act_reason := format('min_interval %ss not elapsed', cand.min_interval_secs);
        ELSIF maintenance_pressure THEN
            should_act := true;
            act_reason := prop_reason;
        ELSE
            IF cand.strategy IN ('scheduled', 'continuous') THEN
                should_act := true;
                act_reason := prop_reason;
            ELSIF cand.strategy = 'target' THEN
                IF cand.freshness_target_secs IS NULL
                   OR coalesce(cand.seconds_dirty, 0) >= cand.freshness_target_secs THEN
                    should_act := true;
                    act_reason := format('%s; stale %ss >= target %ss',
                        prop_reason,
                        round(coalesce(cand.seconds_dirty, 0))::int,
                        coalesce(cand.freshness_target_secs, 0));
                ELSE
                    act_reason := format('within target (%ss < %ss)',
                        round(coalesce(cand.seconds_dirty, 0))::int, cand.freshness_target_secs);
                END IF;
            ELSIF cand.strategy = 'demand' THEN
                SELECT r.heap_seq_scans INTO last_scans
                  FROM rvbbit.accel_tick_runs r
                 WHERE r.table_oid = cand.f_oid
                 ORDER BY r.ran_at DESC LIMIT 1;
                IF last_scans IS NOT NULL AND cand.heap_seq_scans > last_scans THEN
                    should_act := true;
                    act_reason := prop_reason || '; demand grew on slow path';
                ELSE
                    act_reason := CASE WHEN last_scans IS NULL
                                       THEN 'demand baseline' ELSE 'no new slow-path demand' END;
                END IF;
            END IF;

            IF should_act AND cand.daily_refresh_budget IS NOT NULL THEN
                SELECT count(*) INTO used_today
                  FROM rvbbit.accel_tick_runs r
                 WHERE r.table_oid = cand.f_oid
                   AND r.executed
                   AND r.ran_at > now() - interval '24 hours';
                IF used_today >= cand.daily_refresh_budget THEN
                    should_act := false;
                    act_reason := format('daily budget %s exhausted', cand.daily_refresh_budget);
                END IF;
            END IF;
        END IF;

        do_execute := should_act;
        IF do_execute AND budget IS NOT NULL AND acted >= budget THEN
            do_execute := false;
            act_reason := 'tick budget reached';
        ELSIF do_execute AND is_lance AND lance_acted >= lance_budget THEN
            do_execute := false;
            act_reason := 'lance budget reached';
        END IF;

        accel_tick.table_oid      := cand.f_oid;
        accel_tick.table_name     := cand.f_name;
        accel_tick.strategy       := cand.strategy;
        accel_tick.drift_rows     := cand.drift_rows;
        accel_tick.drift_ratio    := cand.drift_ratio;
        accel_tick.seconds_dirty  := cand.seconds_dirty;
        accel_tick.heap_seq_scans := cand.heap_seq_scans;
        accel_tick.rows_written   := NULL;
        accel_tick.error          := NULL;

        IF NOT do_execute THEN
            accel_tick.action   := CASE WHEN should_act THEN prop_action ELSE 'skip' END;
            accel_tick.reason   := act_reason;
            accel_tick.executed := false;
            accel_tick.status   := CASE WHEN should_act THEN 'deferred' ELSE 'skip' END;
            IF NOT dry_run THEN
                INSERT INTO rvbbit.accel_tick_runs
                    (table_oid, table_name, strategy, action, reason, drift_rows, heap_seq_scans, executed, status)
                VALUES (cand.f_oid, cand.f_name, cand.strategy, accel_tick.action, act_reason,
                        cand.drift_rows, cand.heap_seq_scans, false, accel_tick.status);
            END IF;
            RETURN NEXT;
            CONTINUE;
        END IF;

        acted := acted + 1;
        IF is_lance THEN lance_acted := lance_acted + 1; END IF;

        IF dry_run THEN
            accel_tick.action   := prop_action;
            accel_tick.reason   := act_reason;
            accel_tick.executed := false;
            accel_tick.status   := 'planned';
            RETURN NEXT;
            CONTINUE;
        END IF;

        BEGIN
            IF prop_action = 'full' THEN
                res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
            ELSE
                BEGIN
                    res := rvbbit.refresh_acceleration(cand.f_oid::regclass, true);
                EXCEPTION WHEN OTHERS THEN
                    prop_action := 'full';
                    act_reason  := act_reason || ' (delta->full: ' || SQLERRM || ')';
                    res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
                END;
            END IF;
            accel_tick.action       := prop_action;
            accel_tick.reason       := act_reason;
            accel_tick.executed     := true;
            accel_tick.status       := coalesce(res->>'status', 'ok');
            accel_tick.rows_written := coalesce((res->>'rows_written')::bigint, 0);
        EXCEPTION WHEN OTHERS THEN
            accel_tick.action   := prop_action;
            accel_tick.reason   := act_reason;
            accel_tick.executed := true;
            accel_tick.status   := 'failed';
            accel_tick.error    := SQLERRM;
        END;

        INSERT INTO rvbbit.accel_tick_runs
            (table_oid, table_name, strategy, action, reason, drift_rows, heap_seq_scans,
             executed, status, rows_written, error)
        VALUES (cand.f_oid, cand.f_name, cand.strategy, accel_tick.action, accel_tick.reason,
                cand.drift_rows, cand.heap_seq_scans, true, accel_tick.status,
                accel_tick.rows_written, accel_tick.error);
        RETURN NEXT;
    END LOOP;

    RETURN;
END;
$function$;
