-- 0266: Split low-latency freshness from optional major compaction and make
-- both lanes observable without reading mutable executor state.
--
-- accel_activity_log is an append-only event stream for committed history.
-- It intentionally is not the live-progress mechanism: events written by a
-- long rebuild share that rebuild's transaction and cannot become visible
-- before commit. accel_live_activity reads pg_stat_activity/application_name
-- and advisory locks instead, so the current table remains visible while the
-- transaction is busy or waiting on a lock.

CREATE SEQUENCE IF NOT EXISTS rvbbit.accel_sweep_id_seq;

CREATE TABLE IF NOT EXISTS rvbbit.accel_activity_log (
    id                 bigserial PRIMARY KEY,
    sweep_id           bigint NOT NULL,
    event_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    lane               text NOT NULL,
    event_type         text NOT NULL,
    backend_pid        integer NOT NULL DEFAULT pg_backend_pid(),
    table_oid          oid,
    table_name         text,
    strategy           text,
    action             text,
    reason             text,
    status             text,
    operation_id       bigint,
    elapsed_ms         double precision,
    rows_written       bigint,
    row_groups_written bigint,
    details            jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (event_type IN (
        'sweep_started', 'table_started', 'table_finished', 'sweep_finished'
    )),
    CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CHECK (rows_written IS NULL OR rows_written >= 0),
    CHECK (row_groups_written IS NULL OR row_groups_written >= 0)
);

CREATE INDEX IF NOT EXISTS accel_activity_log_time_idx
    ON rvbbit.accel_activity_log (event_at DESC);
CREATE INDEX IF NOT EXISTS accel_activity_log_sweep_idx
    ON rvbbit.accel_activity_log (sweep_id, id);
CREATE INDEX IF NOT EXISTS accel_activity_log_table_time_idx
    ON rvbbit.accel_activity_log (table_oid, event_at DESC)
    WHERE table_oid IS NOT NULL;
CREATE INDEX IF NOT EXISTS accel_activity_log_operation_idx
    ON rvbbit.accel_activity_log (operation_id)
    WHERE operation_id IS NOT NULL;

ALTER TABLE rvbbit.accel_activity_log SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 1000,
    autovacuum_analyze_scale_factor = 0.05,
    autovacuum_analyze_threshold = 1000,
    toast.autovacuum_vacuum_scale_factor = 0.02,
    toast.autovacuum_vacuum_threshold = 1000
);

COMMENT ON TABLE rvbbit.accel_activity_log IS
    'Retention-bounded append-only history for accelerator freshness/fold sweeps. '
    'Use accel_live_activity for in-flight work because executor events commit only with the worker transaction.';

CREATE OR REPLACE FUNCTION rvbbit._accel_log_event(
    p_sweep_id bigint,
    p_lane text,
    p_event_type text,
    p_table_oid oid DEFAULT NULL,
    p_table_name text DEFAULT NULL,
    p_strategy text DEFAULT NULL,
    p_action text DEFAULT NULL,
    p_reason text DEFAULT NULL,
    p_status text DEFAULT NULL,
    p_operation_id bigint DEFAULT NULL,
    p_elapsed_ms double precision DEFAULT NULL,
    p_rows_written bigint DEFAULT NULL,
    p_row_groups_written bigint DEFAULT NULL,
    p_details jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    event_id bigint;
BEGIN
    INSERT INTO rvbbit.accel_activity_log (
        sweep_id, lane, event_type, table_oid, table_name, strategy,
        action, reason, status, operation_id, elapsed_ms, rows_written,
        row_groups_written, details
    ) VALUES (
        p_sweep_id, p_lane, p_event_type, p_table_oid, p_table_name,
        p_strategy, p_action, p_reason, p_status, p_operation_id,
        p_elapsed_ms, p_rows_written, p_row_groups_written,
        coalesce(p_details, '{}'::jsonb)
    )
    RETURNING id INTO event_id;
    RETURN event_id;
EXCEPTION WHEN OTHERS THEN
    -- Observability must never make an accelerator unavailable. A failed
    -- telemetry insert is deliberately fail-open; the canonical operation log
    -- and cron history remain available for diagnosis.
    RETURN NULL;
END;
$$;

COMMENT ON FUNCTION rvbbit._accel_log_event(
    bigint, text, text, oid, text, text, text, text, text, bigint,
    double precision, bigint, bigint, jsonb
) IS 'Internal fail-open append helper for accelerator sweep events.';

-- One row per policy/table with fold pressure shown independently of
-- freshness. A clean fragmented accelerator remains authoritative and usable;
-- this view merely says whether the optional maintenance lane should fold it.
CREATE OR REPLACE VIEW rvbbit.accel_fold_candidates AS
SELECT f.table_oid,
       f.table_name,
       e.strategy,
       e.active,
       f.parquet_authoritative,
       f.shadow_heap_dirty,
       replacement.pending AS current_replacement_pending,
       f.last_refresh_at,
       f.seconds_since_refresh,
       f.row_groups,
       e.max_row_groups_before_rebuild,
       CASE
           WHEN e.max_row_groups_before_rebuild IS NULL THEN NULL
           ELSE f.row_groups::double precision
                / e.max_row_groups_before_rebuild::double precision
       END AS row_group_pressure,
       f.tombstones,
       e.max_tombstones_before_rebuild,
       CASE
           WHEN e.max_tombstones_before_rebuild IS NULL THEN NULL
           ELSE f.tombstones::double precision
                / e.max_tombstones_before_rebuild::double precision
       END AS tombstone_pressure,
       concat_ws(
           '; ',
           CASE
               WHEN e.max_row_groups_before_rebuild IS NOT NULL
                AND f.row_groups >= e.max_row_groups_before_rebuild
               THEN format(
                   'row_group_fanout %s >= %s',
                   f.row_groups,
                   e.max_row_groups_before_rebuild
               )
           END,
           CASE
               WHEN e.max_tombstones_before_rebuild IS NOT NULL
                AND f.tombstones >= e.max_tombstones_before_rebuild
               THEN format(
                   'tombstone_count %s >= %s',
                   f.tombstones,
                   e.max_tombstones_before_rebuild
               )
           END
       ) AS fold_reason,
       (
           e.active
           AND e.strategy <> 'manual'
           AND f.row_groups > 0
           AND NOT f.shadow_heap_dirty
           AND NOT replacement.pending
           AND (
               (e.max_row_groups_before_rebuild IS NOT NULL
                AND f.row_groups >= e.max_row_groups_before_rebuild)
               OR
               (e.max_tombstones_before_rebuild IS NOT NULL
                AND f.tombstones >= e.max_tombstones_before_rebuild)
           )
       ) AS fold_due,
       CASE
           WHEN NOT e.active THEN 'policy inactive'
           WHEN e.strategy = 'manual' THEN 'manual policy'
           WHEN f.row_groups = 0 THEN 'no accelerator baseline'
           WHEN replacement.pending THEN 'current replacement pending; freshness lane owns it'
           WHEN f.shadow_heap_dirty THEN 'dirty; freshness lane owns it'
           WHEN NOT (
               (e.max_row_groups_before_rebuild IS NOT NULL
                AND f.row_groups >= e.max_row_groups_before_rebuild)
               OR
               (e.max_tombstones_before_rebuild IS NOT NULL
                AND f.tombstones >= e.max_tombstones_before_rebuild)
           ) THEN 'below fold thresholds'
       END AS blocked_reason,
       f.last_rebuild_ms,
       f.last_rebuild_rows
  FROM rvbbit.accel_freshness f
  JOIN rvbbit.accel_policy_effective e ON e.table_oid = f.table_oid
  CROSS JOIN LATERAL (
      SELECT rvbbit.current_replacement_pending(f.table_oid) AS pending
  ) replacement;

COMMENT ON VIEW rvbbit.accel_fold_candidates IS
    'Optional major-compaction pressure. fold_due never includes dirty/current-replacement tables; those belong to accel_tick freshness work.';

-- Exact live state. application_name is updated before each long operation and
-- is immediately visible to other sessions, unlike rows written in the same
-- worker transaction. Advisory locks are included both as corroboration and as
-- a fallback for legacy/variant workers that do not yet stamp a table name.
CREATE OR REPLACE VIEW rvbbit.accel_live_activity AS
WITH worker_locks AS (
    SELECT a.pid,
           bool_or(
               l.locktype = 'advisory'
               AND l.classid = 1381187156::oid
               AND l.objid = 7::oid
               AND l.objsubid = 2
               AND l.granted
           ) AS has_canonical_sweep_lock,
           bool_or(
               l.locktype = 'advisory'
               AND l.classid = 1381187156::oid
               AND l.objid = 8::oid
               AND l.objsubid = 2
               AND l.granted
           ) AS has_variant_sweep_lock,
           array_agg(l.objid::oid ORDER BY l.objid)
               FILTER (
                   WHERE l.locktype = 'advisory'
                     AND l.classid = 1380336724::oid
                     AND l.objsubid = 1
                     AND l.granted
               ) AS locked_table_oids
      FROM pg_stat_activity a
      LEFT JOIN pg_locks l ON l.pid = a.pid
     GROUP BY a.pid
), parsed AS (
    SELECT a.pid,
           a.usename,
           a.datname,
           a.application_name,
           a.client_addr,
           a.backend_start,
           a.xact_start,
           a.query_start,
           a.state,
           a.wait_event_type,
           a.wait_event,
           pg_blocking_pids(a.pid) AS blocking_pids,
           wl.has_canonical_sweep_lock,
           wl.has_variant_sweep_lock,
           wl.locked_table_oids,
           substring(a.application_name FROM '^rvbbit/([^ ]+)') AS app_lane,
           nullif(substring(a.application_name FROM ' s=([0-9]+)'), '')::bigint
               AS app_sweep_id,
           nullif(substring(a.application_name FROM ' o=([0-9]+)'), '')::oid
               AS app_table_oid,
           nullif(substring(a.application_name FROM ' a=([^ ]+)'), '') AS app_action,
           to_timestamp(
               nullif(substring(a.application_name FROM ' t=([0-9.]+)'), '')::double precision
           ) AS app_table_started_at,
           CASE
               WHEN coalesce(wl.has_variant_sweep_lock, false) THEN 'build_vortex'
               WHEN a.query ~* 'rvbbit\\.rebuild_acceleration' THEN 'full'
               WHEN a.query ~* 'rvbbit\\.refresh_acceleration' THEN 'delta'
               WHEN a.query ~* 'rvbbit\\.compact_acceleration' THEN 'compact'
           END AS inferred_action,
           a.query ~* 'rvbbit\\.(refresh_acceleration|rebuild_acceleration|compact_acceleration)'
               AS query_is_accel_operation
      FROM pg_stat_activity a
      JOIN worker_locks wl ON wl.pid = a.pid
     WHERE a.application_name LIKE 'rvbbit/%'
        OR coalesce(wl.has_canonical_sweep_lock, false)
        OR coalesce(wl.has_variant_sweep_lock, false)
        OR (
            cardinality(coalesce(wl.locked_table_oids, '{}'::oid[])) > 0
            AND a.query ~* 'rvbbit\\.(refresh_acceleration|rebuild_acceleration|compact_acceleration)'
        )
), resolved AS (
    SELECT p.*,
           coalesce(
               p.app_table_oid,
               CASE
                   WHEN cardinality(p.locked_table_oids) = 1
                   THEN p.locked_table_oids[1]
               END
           ) AS resolved_table_oid
      FROM parsed p
)
SELECT r.pid,
       CASE r.app_lane
           WHEN 'fresh' THEN 'freshness'
           WHEN 'fold' THEN 'fold'
           WHEN 'variant' THEN 'variant'
           ELSE CASE
               WHEN coalesce(r.has_variant_sweep_lock, false) THEN 'variant'
               WHEN coalesce(r.has_canonical_sweep_lock, false) THEN 'freshness_or_fold'
               WHEN r.query_is_accel_operation THEN 'manual'
               ELSE r.app_lane
           END
       END AS lane,
       r.app_sweep_id AS sweep_id,
       r.resolved_table_oid AS table_oid,
       c.oid::regclass::text AS table_name,
       coalesce(r.app_action, r.inferred_action) AS action,
       coalesce(r.app_table_started_at, r.query_start) AS current_work_started_at,
       greatest(
           0,
           extract(epoch FROM (
               clock_timestamp() - coalesce(r.app_table_started_at, r.query_start)
           )) * 1000.0
       ) AS current_work_elapsed_ms,
       r.state,
       r.wait_event_type,
       r.wait_event,
       r.blocking_pids,
       r.locked_table_oids AS transaction_locked_table_oids,
       r.usename,
       r.datname,
       r.application_name,
       r.client_addr,
       r.backend_start,
       r.xact_start,
       r.query_start
  FROM resolved r
  LEFT JOIN pg_class c ON c.oid = r.resolved_table_oid;

COMMENT ON VIEW rvbbit.accel_live_activity IS
    'In-flight accelerator worker/table from pg_stat_activity plus advisory locks. Unlike activity-log rows, this is visible before the worker transaction commits.';

CREATE OR REPLACE VIEW rvbbit.accel_sweep_history AS
SELECT l.sweep_id,
       l.lane,
       min(l.event_at) FILTER (WHERE l.event_type = 'sweep_started') AS started_at,
       max(l.event_at) FILTER (WHERE l.event_type = 'sweep_finished') AS finished_at,
       max(l.backend_pid) FILTER (WHERE l.event_type = 'sweep_started') AS backend_pid,
       count(*) FILTER (WHERE l.event_type = 'table_started')::bigint AS tables_started,
       count(*) FILTER (
           WHERE l.event_type = 'table_finished' AND l.status <> 'failed'
       )::bigint AS tables_succeeded,
       count(*) FILTER (
           WHERE l.event_type = 'table_finished' AND l.status = 'failed'
       )::bigint AS tables_failed,
       coalesce(sum(l.rows_written) FILTER (WHERE l.event_type = 'table_finished'), 0)::bigint
           AS rows_written,
       max(l.elapsed_ms) FILTER (WHERE l.event_type = 'sweep_finished') AS elapsed_ms,
       max(l.status) FILTER (WHERE l.event_type = 'sweep_finished') AS status,
       coalesce(
           (array_agg(l.details ORDER BY l.id DESC)
               FILTER (WHERE l.event_type = 'sweep_finished'))[1],
           '{}'::jsonb
       ) AS details
  FROM rvbbit.accel_activity_log l
 GROUP BY l.sweep_id, l.lane;

COMMENT ON VIEW rvbbit.accel_sweep_history IS
    'Committed freshness/fold heartbeat history, summarized from append-only activity events.';

CREATE OR REPLACE VIEW rvbbit.accel_table_runtime_profile AS
SELECT l.table_oid,
       max(l.table_name) AS table_name,
       l.lane,
       count(*)::bigint AS runs,
       count(*) FILTER (WHERE l.status = 'failed')::bigint AS failed_runs,
       max(l.event_at) AS last_finished_at,
       avg(l.elapsed_ms) FILTER (WHERE l.elapsed_ms IS NOT NULL) AS avg_elapsed_ms,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY l.elapsed_ms)
           FILTER (WHERE l.elapsed_ms IS NOT NULL) AS p50_elapsed_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY l.elapsed_ms)
           FILTER (WHERE l.elapsed_ms IS NOT NULL) AS p95_elapsed_ms,
       max(l.elapsed_ms) AS max_elapsed_ms,
       coalesce(sum(l.rows_written), 0)::bigint AS rows_written,
       coalesce(sum(l.row_groups_written), 0)::bigint AS row_groups_written
  FROM rvbbit.accel_activity_log l
 WHERE l.event_type = 'table_finished'
 GROUP BY l.table_oid, l.lane;

COMMENT ON VIEW rvbbit.accel_table_runtime_profile IS
    'Per-table duration/error profile over retained append-only accelerator activity history.';

COMMENT ON COLUMN rvbbit.accel_policy.max_row_groups_before_rebuild IS
    'Optional off-hours accel_fold_tick threshold; it no longer forces a full rebuild in the freshness heartbeat.';
COMMENT ON COLUMN rvbbit.accel_policy.max_tombstones_before_rebuild IS
    'Optional off-hours accel_fold_tick threshold; it no longer forces a full rebuild in the freshness heartbeat.';

-- The daytime/high-frequency lane. This restores canonical freshness as
-- cheaply as correctness permits. Clean row-group/tombstone pressure is not a
-- reason to make an authoritative accelerator disappear into a long rebuild;
-- accel_fold_tick owns that optional work.
CREATE OR REPLACE FUNCTION rvbbit.accel_tick(
    budget integer DEFAULT NULL::integer,
    dry_run boolean DEFAULT false,
    lance_budget integer DEFAULT 1
)
RETURNS TABLE(
    table_oid oid,
    table_name text,
    strategy text,
    action text,
    reason text,
    drift_rows bigint,
    drift_ratio double precision,
    seconds_dirty double precision,
    heap_seq_scans bigint,
    executed boolean,
    status text,
    rows_written bigint,
    error text
)
LANGUAGE plpgsql
AS $function$
DECLARE
    cand record;
    acted integer := 0;
    lance_acted integer := 0;
    do_execute boolean;
    should_act boolean;
    prop_action text;
    prop_reason text;
    act_reason text;
    baseline_missing boolean;
    res jsonb;
    last_scans bigint;
    used_today integer;
    is_lance boolean;
    sweep_id bigint;
    sweep_started timestamptz;
    table_started timestamptz;
    original_application_name text;
    operation_id bigint;
    written_row_groups bigint;
    candidate_count integer := 0;
    deferred_count integer := 0;
    skipped_count integer := 0;
    failed_count integer := 0;
    total_rows_written bigint := 0;
BEGIN
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 7) THEN
        RETURN;
    END IF;

    IF NOT dry_run THEN
        original_application_name := current_setting('application_name', true);
        sweep_id := nextval('rvbbit.accel_sweep_id_seq');
        sweep_started := clock_timestamp();
        PERFORM set_config(
            'application_name',
            left(format('rvbbit/fresh s=%s scan', sweep_id), 63),
            true
        );
        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'freshness',
            p_event_type => 'sweep_started',
            p_status => 'running',
            p_details => jsonb_build_object(
                'budget', budget,
                'lance_budget', lance_budget,
                'mode', 'freshness_only'
            )
        );
    END IF;

    -- Preserve the warm-prior heartbeat added by migration 0124.
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
        SELECT f.table_oid AS f_oid,
               f.table_name AS f_name,
               f.shadow_heap_dirty,
               f.seconds_dirty,
               f.seconds_since_refresh,
               f.row_groups,
               f.heap_live_tuples,
               f.tombstones,
               f.drift_rows,
               f.drift_ratio,
               f.heap_seq_scans,
               f.lance_accelerated,
               rvbbit.current_replacement_pending(f.table_oid)
                   AS current_replacement_pending,
               e.strategy,
               e.freshness_target_secs,
               e.min_interval_secs,
               e.daily_refresh_budget,
               e.full_rebuild_drift_ratio,
               e.lance_separate
          FROM rvbbit.accel_freshness f
          JOIN rvbbit.accel_policy_effective e ON e.table_oid = f.table_oid
         WHERE e.active
           AND e.strategy <> 'manual'
         ORDER BY rvbbit.current_replacement_pending(f.table_oid) DESC,
                  (f.drift_rows * (1 + f.heap_seq_scans)) DESC,
                  f.seconds_dirty DESC NULLS LAST
    LOOP
        candidate_count := candidate_count + 1;
        baseline_missing := cand.row_groups = 0 AND cand.heap_live_tuples > 0;

        IF cand.current_replacement_pending THEN
            -- A current-only TRUNCATE is an invalidated baseline, not a delta.
            prop_action := 'full';
            prop_reason := 'current replacement pending';
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
        ELSIF baseline_missing THEN
            prop_action := 'delta';
            prop_reason := 'no accelerator baseline';
        ELSE
            prop_action := 'skip';
            prop_reason := 'clean';
        END IF;

        is_lance := coalesce(cand.lance_accelerated, false)
            AND coalesce(cand.lance_separate, true);
        should_act := false;
        act_reason := prop_reason;

        IF prop_action = 'skip' THEN
            act_reason := prop_reason;
        ELSIF cand.seconds_since_refresh IS NOT NULL
              AND cand.seconds_since_refresh < cand.min_interval_secs THEN
            act_reason := format('min_interval %ss not elapsed', cand.min_interval_secs);
        ELSE
            IF cand.strategy IN ('scheduled', 'continuous') THEN
                should_act := true;
                act_reason := prop_reason;
            ELSIF cand.strategy = 'target' THEN
                IF baseline_missing THEN
                    should_act := true;
                    act_reason := prop_reason || '; target requires a baseline';
                ELSIF cand.freshness_target_secs IS NULL
                   OR coalesce(cand.seconds_dirty, 0) >= cand.freshness_target_secs THEN
                    should_act := true;
                    act_reason := format(
                        '%s; stale %ss >= target %ss',
                        prop_reason,
                        round(coalesce(cand.seconds_dirty, 0))::int,
                        coalesce(cand.freshness_target_secs, 0)
                    );
                ELSE
                    act_reason := format(
                        'within target (%ss < %ss)',
                        round(coalesce(cand.seconds_dirty, 0))::int,
                        cand.freshness_target_secs
                    );
                END IF;
            ELSIF cand.strategy = 'demand' THEN
                SELECT r.heap_seq_scans
                  INTO last_scans
                  FROM rvbbit.accel_tick_runs r
                 WHERE r.table_oid = cand.f_oid
                 ORDER BY r.ran_at DESC
                 LIMIT 1;

                IF baseline_missing
                   AND cand.heap_seq_scans > coalesce(last_scans, 0) THEN
                    should_act := true;
                    act_reason := prop_reason || '; observed slow-path demand';
                ELSIF last_scans IS NOT NULL
                      AND cand.heap_seq_scans > last_scans THEN
                    should_act := true;
                    act_reason := prop_reason || '; demand grew on slow path';
                ELSE
                    act_reason := CASE
                        WHEN last_scans IS NULL THEN 'demand baseline'
                        ELSE 'no new slow-path demand'
                    END;
                END IF;
            END IF;

            IF should_act AND cand.daily_refresh_budget IS NOT NULL THEN
                SELECT count(*)
                  INTO used_today
                  FROM rvbbit.accel_tick_runs r
                 WHERE r.table_oid = cand.f_oid
                   AND r.executed
                   AND r.ran_at > now() - interval '24 hours';
                IF used_today >= cand.daily_refresh_budget THEN
                    should_act := false;
                    act_reason := format(
                        'daily budget %s exhausted',
                        cand.daily_refresh_budget
                    );
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

        accel_tick.table_oid := cand.f_oid;
        accel_tick.table_name := cand.f_name;
        accel_tick.strategy := cand.strategy;
        accel_tick.drift_rows := cand.drift_rows;
        accel_tick.drift_ratio := cand.drift_ratio;
        accel_tick.seconds_dirty := cand.seconds_dirty;
        accel_tick.heap_seq_scans := cand.heap_seq_scans;
        accel_tick.rows_written := NULL;
        accel_tick.error := NULL;

        IF NOT do_execute THEN
            accel_tick.action := CASE
                WHEN should_act THEN prop_action
                ELSE 'skip'
            END;
            accel_tick.reason := act_reason;
            accel_tick.executed := false;
            accel_tick.status := CASE
                WHEN should_act THEN 'deferred'
                ELSE 'skip'
            END;
            IF should_act THEN
                deferred_count := deferred_count + 1;
            ELSE
                skipped_count := skipped_count + 1;
            END IF;
            IF NOT dry_run THEN
                INSERT INTO rvbbit.accel_tick_runs (
                    table_oid, table_name, strategy, action, reason,
                    drift_rows, heap_seq_scans, executed, status
                ) VALUES (
                    cand.f_oid, cand.f_name, cand.strategy,
                    accel_tick.action, act_reason, cand.drift_rows,
                    cand.heap_seq_scans, false, accel_tick.status
                );
            END IF;
            RETURN NEXT;
            CONTINUE;
        END IF;

        acted := acted + 1;
        IF is_lance THEN
            lance_acted := lance_acted + 1;
        END IF;

        IF dry_run THEN
            accel_tick.action := prop_action;
            accel_tick.reason := act_reason;
            accel_tick.executed := false;
            accel_tick.status := 'planned';
            RETURN NEXT;
            CONTINUE;
        END IF;

        table_started := clock_timestamp();
        res := NULL;
        operation_id := NULL;
        written_row_groups := NULL;
        PERFORM set_config(
            'application_name',
            left(
                format(
                    'rvbbit/fresh s=%s o=%s a=%s t=%s %s',
                    sweep_id,
                    cand.f_oid,
                    prop_action,
                    round(extract(epoch FROM table_started)::numeric, 3),
                    cand.f_name
                ),
                63
            ),
            true
        );
        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'freshness',
            p_event_type => 'table_started',
            p_table_oid => cand.f_oid,
            p_table_name => cand.f_name,
            p_strategy => cand.strategy,
            p_action => prop_action,
            p_reason => act_reason,
            p_status => 'running',
            p_details => jsonb_strip_nulls(jsonb_build_object(
                'drift_rows', cand.drift_rows,
                'drift_ratio', cand.drift_ratio,
                'seconds_dirty', cand.seconds_dirty,
                'heap_seq_scans', cand.heap_seq_scans,
                'row_groups', cand.row_groups,
                'tombstones', cand.tombstones
            ))
        );

        BEGIN
            IF prop_action = 'full' THEN
                res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
            ELSE
                BEGIN
                    res := rvbbit.refresh_acceleration(cand.f_oid::regclass, true);
                EXCEPTION WHEN OTHERS THEN
                    prop_action := 'full';
                    act_reason := act_reason || ' (delta->full: ' || SQLERRM || ')';
                    PERFORM set_config(
                        'application_name',
                        left(
                            format(
                                'rvbbit/fresh s=%s o=%s a=full t=%s %s',
                                sweep_id,
                                cand.f_oid,
                                round(extract(epoch FROM table_started)::numeric, 3),
                                cand.f_name
                            ),
                            63
                        ),
                        true
                    );
                    res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
                END;
            END IF;
            accel_tick.action := prop_action;
            accel_tick.reason := act_reason;
            accel_tick.executed := true;
            accel_tick.status := coalesce(res->>'status', 'ok');
            accel_tick.rows_written := coalesce((res->>'rows_written')::bigint, 0);
            operation_id := nullif(res->>'operation_id', '')::bigint;
            written_row_groups := nullif(res->>'row_groups_written', '')::bigint;
        EXCEPTION WHEN OTHERS THEN
            accel_tick.action := prop_action;
            accel_tick.reason := act_reason;
            accel_tick.executed := true;
            accel_tick.status := 'failed';
            accel_tick.error := SQLERRM;
        END;

        IF accel_tick.status = 'failed' THEN
            failed_count := failed_count + 1;
        END IF;
        total_rows_written := total_rows_written + coalesce(accel_tick.rows_written, 0);

        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'freshness',
            p_event_type => 'table_finished',
            p_table_oid => cand.f_oid,
            p_table_name => cand.f_name,
            p_strategy => cand.strategy,
            p_action => accel_tick.action,
            p_reason => accel_tick.reason,
            p_status => accel_tick.status,
            p_operation_id => operation_id,
            p_elapsed_ms => greatest(
                0,
                extract(epoch FROM (clock_timestamp() - table_started)) * 1000.0
            ),
            p_rows_written => accel_tick.rows_written,
            p_row_groups_written => written_row_groups,
            p_details => jsonb_strip_nulls(jsonb_build_object(
                'error', accel_tick.error,
                'result', res
            ))
        );
        PERFORM set_config(
            'application_name',
            left(format('rvbbit/fresh s=%s scan', sweep_id), 63),
            true
        );

        INSERT INTO rvbbit.accel_tick_runs (
            table_oid, table_name, strategy, action, reason, drift_rows,
            heap_seq_scans, executed, status, rows_written, error
        ) VALUES (
            cand.f_oid, cand.f_name, cand.strategy, accel_tick.action,
            accel_tick.reason, cand.drift_rows, cand.heap_seq_scans, true,
            accel_tick.status, accel_tick.rows_written, accel_tick.error
        );
        RETURN NEXT;
    END LOOP;

    IF NOT dry_run THEN
        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'freshness',
            p_event_type => 'sweep_finished',
            p_status => CASE WHEN failed_count > 0 THEN 'partial' ELSE 'ok' END,
            p_elapsed_ms => greatest(
                0,
                extract(epoch FROM (clock_timestamp() - sweep_started)) * 1000.0
            ),
            p_rows_written => total_rows_written,
            p_details => jsonb_build_object(
                'candidates', candidate_count,
                'tables_executed', acted,
                'tables_failed', failed_count,
                'tables_deferred', deferred_count,
                'tables_skipped', skipped_count,
                'lance_tables_executed', lance_acted
            )
        );
        PERFORM set_config(
            'application_name',
            coalesce(original_application_name, ''),
            true
        );
    END IF;

    RETURN;
END;
$function$;

COMMENT ON FUNCTION rvbbit.accel_tick(integer, boolean, integer) IS
    'High-frequency freshness lane. Restores dirty/current-replacement tables with delta or correctness-required full rebuilds; clean fragmentation is deferred to accel_fold_tick. Writes committed sweep history to accel_activity_log and live identity to application_name.';

-- The deliberately slow lane. It only touches clean, authoritative
-- accelerators whose explicit policy thresholds say consolidation is due.
-- Sharing singleton key 7 with accel_tick guarantees freshness and folds do
-- not overlap. A one-table default also bounds lock accumulation per txn.
CREATE OR REPLACE FUNCTION rvbbit.accel_fold_tick(
    budget integer DEFAULT 1,
    dry_run boolean DEFAULT false
)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    strategy text,
    action text,
    reason text,
    row_groups bigint,
    tombstones bigint,
    executed boolean,
    status text,
    elapsed_ms double precision,
    operation_id bigint,
    rows_written bigint,
    error text
)
LANGUAGE plpgsql
AS $function$
DECLARE
    cand record;
    acted integer := 0;
    deferred_count integer := 0;
    skipped_count integer := 0;
    failed_count integer := 0;
    total_rows_written bigint := 0;
    sweep_id bigint;
    sweep_started timestamptz;
    table_started timestamptz;
    original_application_name text;
    res jsonb;
    written_row_groups bigint;
    still_due boolean;
    live_blocked_reason text;
    live_fold_reason text;
    live_row_groups bigint;
    live_tombstones bigint;
BEGIN
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 7) THEN
        RETURN;
    END IF;

    IF NOT dry_run THEN
        original_application_name := current_setting('application_name', true);
        sweep_id := nextval('rvbbit.accel_sweep_id_seq');
        sweep_started := clock_timestamp();
        PERFORM set_config(
            'application_name',
            left(format('rvbbit/fold s=%s scan', sweep_id), 63),
            true
        );
        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'fold',
            p_event_type => 'sweep_started',
            p_status => 'running',
            p_details => jsonb_build_object(
                'budget', greatest(coalesce(budget, 1), 0),
                'mode', 'clean_major_compaction'
            )
        );
    END IF;

    FOR cand IN
        SELECT fc.*
          FROM rvbbit.accel_fold_candidates fc
         WHERE fc.fold_due
         ORDER BY greatest(
                      coalesce(fc.row_group_pressure, 0),
                      coalesce(fc.tombstone_pressure, 0)
                  ) DESC,
                  fc.last_refresh_at NULLS FIRST,
                  fc.table_oid
    LOOP
        accel_fold_tick.table_oid := cand.table_oid;
        accel_fold_tick.table_name := cand.table_name;
        accel_fold_tick.strategy := cand.strategy;
        accel_fold_tick.action := 'full';
        accel_fold_tick.reason := cand.fold_reason;
        accel_fold_tick.row_groups := cand.row_groups;
        accel_fold_tick.tombstones := cand.tombstones;
        accel_fold_tick.executed := false;
        accel_fold_tick.status := 'planned';
        accel_fold_tick.elapsed_ms := NULL;
        accel_fold_tick.operation_id := NULL;
        accel_fold_tick.rows_written := NULL;
        accel_fold_tick.error := NULL;

        IF cand.seconds_since_refresh IS NOT NULL
           AND cand.seconds_since_refresh < (
               SELECT e.min_interval_secs
                 FROM rvbbit.accel_policy_effective e
                WHERE e.table_oid = cand.table_oid
           ) THEN
            accel_fold_tick.status := 'deferred';
            accel_fold_tick.reason := format(
                'min_interval %ss not elapsed',
                (SELECT e.min_interval_secs
                   FROM rvbbit.accel_policy_effective e
                  WHERE e.table_oid = cand.table_oid)
            );
            deferred_count := deferred_count + 1;
            RETURN NEXT;
            CONTINUE;
        END IF;

        IF acted >= greatest(coalesce(budget, 1), 0) THEN
            accel_fold_tick.status := 'deferred';
            accel_fold_tick.reason := 'fold budget reached';
            deferred_count := deferred_count + 1;
            RETURN NEXT;
            CONTINUE;
        END IF;

        IF dry_run THEN
            acted := acted + 1;
            RETURN NEXT;
            CONTINUE;
        END IF;

        -- Never wait behind a manual refresh/rebuild. A later off-hours sweep
        -- can retry without turning a monitoring query into a lock convoy.
        IF NOT pg_try_advisory_xact_lock(
            (1380336724::bigint << 32) | cand.table_oid::bigint
        ) THEN
            accel_fold_tick.status := 'deferred';
            accel_fold_tick.reason := 'table maintenance lock busy';
            deferred_count := deferred_count + 1;
            RETURN NEXT;
            CONTINUE;
        END IF;

        -- Re-read after acquiring the table lock. A freshness/manual action may
        -- have committed since the candidate snapshot was taken.
        SELECT fc.fold_due,
               fc.blocked_reason,
               fc.fold_reason,
               fc.row_groups,
               fc.tombstones
          INTO still_due,
               live_blocked_reason,
               live_fold_reason,
               live_row_groups,
               live_tombstones
          FROM rvbbit.accel_fold_candidates fc
         WHERE fc.table_oid = cand.table_oid;

        IF NOT FOUND OR NOT coalesce(still_due, false) THEN
            accel_fold_tick.action := 'skip';
            accel_fold_tick.status := 'skip';
            accel_fold_tick.reason := coalesce(
                live_blocked_reason,
                'table or policy changed before fold'
            );
            skipped_count := skipped_count + 1;
            RETURN NEXT;
            CONTINUE;
        END IF;

        accel_fold_tick.reason := live_fold_reason;
        accel_fold_tick.row_groups := live_row_groups;
        accel_fold_tick.tombstones := live_tombstones;
        acted := acted + 1;
        table_started := clock_timestamp();
        res := NULL;
        written_row_groups := NULL;
        PERFORM set_config(
            'application_name',
            left(
                format(
                    'rvbbit/fold s=%s o=%s a=full t=%s %s',
                    sweep_id,
                    cand.table_oid,
                    round(extract(epoch FROM table_started)::numeric, 3),
                    cand.table_name
                ),
                63
            ),
            true
        );
        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'fold',
            p_event_type => 'table_started',
            p_table_oid => cand.table_oid,
            p_table_name => cand.table_name,
            p_strategy => cand.strategy,
            p_action => 'full',
            p_reason => live_fold_reason,
            p_status => 'running',
            p_details => jsonb_build_object(
                'row_groups', live_row_groups,
                'tombstones', live_tombstones,
                'parquet_authoritative', true
            )
        );

        BEGIN
            res := rvbbit.rebuild_acceleration(cand.table_oid::regclass, true);
            accel_fold_tick.executed := true;
            accel_fold_tick.status := coalesce(res->>'status', 'ok');
            accel_fold_tick.operation_id := nullif(res->>'operation_id', '')::bigint;
            accel_fold_tick.rows_written := coalesce((res->>'rows_written')::bigint, 0);
            written_row_groups := nullif(res->>'row_groups_written', '')::bigint;
        EXCEPTION WHEN OTHERS THEN
            accel_fold_tick.executed := true;
            accel_fold_tick.status := 'failed';
            accel_fold_tick.error := SQLERRM;
        END;

        accel_fold_tick.elapsed_ms := greatest(
            0,
            extract(epoch FROM (clock_timestamp() - table_started)) * 1000.0
        );
        IF accel_fold_tick.status = 'failed' THEN
            failed_count := failed_count + 1;
        END IF;
        total_rows_written := total_rows_written
            + coalesce(accel_fold_tick.rows_written, 0);

        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'fold',
            p_event_type => 'table_finished',
            p_table_oid => cand.table_oid,
            p_table_name => cand.table_name,
            p_strategy => cand.strategy,
            p_action => 'full',
            p_reason => accel_fold_tick.reason,
            p_status => accel_fold_tick.status,
            p_operation_id => accel_fold_tick.operation_id,
            p_elapsed_ms => accel_fold_tick.elapsed_ms,
            p_rows_written => accel_fold_tick.rows_written,
            p_row_groups_written => written_row_groups,
            p_details => jsonb_strip_nulls(jsonb_build_object(
                'error', accel_fold_tick.error,
                'result', res
            ))
        );
        PERFORM set_config(
            'application_name',
            left(format('rvbbit/fold s=%s scan', sweep_id), 63),
            true
        );
        RETURN NEXT;
    END LOOP;

    IF NOT dry_run THEN
        PERFORM rvbbit._accel_log_event(
            p_sweep_id => sweep_id,
            p_lane => 'fold',
            p_event_type => 'sweep_finished',
            p_status => CASE WHEN failed_count > 0 THEN 'partial' ELSE 'ok' END,
            p_elapsed_ms => greatest(
                0,
                extract(epoch FROM (clock_timestamp() - sweep_started)) * 1000.0
            ),
            p_rows_written => total_rows_written,
            p_details => jsonb_build_object(
                'tables_executed', acted,
                'tables_failed', failed_count,
                'tables_deferred', deferred_count,
                'tables_skipped_after_lock', skipped_count
            )
        );
        PERFORM set_config(
            'application_name',
            coalesce(original_application_name, ''),
            true
        );
    END IF;

    RETURN;
END;
$function$;

COMMENT ON FUNCTION rvbbit.accel_fold_tick(integer, boolean) IS
    'Off-hours major-compaction lane for clean authoritative accelerators over explicit row-group/tombstone thresholds. Shares accel_tick singleton lock, defaults to one table/transaction, and never folds a dirty table.';

CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_fold_tick(
    cron_schedule text DEFAULT '17 3 * * 0',
    budget integer DEFAULT 1
) RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    jobid bigint;
    cron_home text := current_setting('cron.database_name', true);
    this_db text := current_database();
    command text := format(
        'SELECT rvbbit.accel_fold_tick(%s)',
        greatest(coalesce(budget, 1), 0)
    );
BEGIN
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format(
                'Use the Scheduler UI, or connect to %L and run: SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home,
                'rvbbit_accel_fold_tick',
                cron_schedule,
                command,
                this_db
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule the accelerator fold lane.'
            USING HINT = 'Install pg_cron, use the Scheduled Tasks UI, or call rvbbit.accel_fold_tick() manually.';
    END IF;
    EXECUTE format(
        'SELECT cron.schedule(%L, %L, %L)',
        'rvbbit_accel_fold_tick',
        cron_schedule,
        command
    ) INTO jobid;
    RETURN jobid;
END;
$$;

COMMENT ON FUNCTION rvbbit.schedule_accel_fold_tick(text, integer) IS
    'Schedule the clean major-compaction lane. Conservative default: one table at 03:17 each Sunday in the pg_cron timezone.';

-- Keep the new append-only stream bounded with the existing maintenance
-- retention contract. This is intentionally DELETE-only housekeeping; normal
-- sweep execution never updates event rows.
CREATE OR REPLACE FUNCTION rvbbit.reap_logs(max_age interval DEFAULT interval '14 days')
RETURNS TABLE (table_name text, rows_reaped bigint)
LANGUAGE plpgsql AS $$
DECLARE
    spec record;
    cutoff timestamptz := now() - max_age;
    n bigint;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('rvbbit.accel_tick_runs',    'ran_at'),
            ('rvbbit.accel_activity_log', 'event_at'),
            ('rvbbit.route_decisions',    'decided_at'),
            ('rvbbit.route_executions',   'executed_at'),
            ('rvbbit.variant_build_runs', 'started_at'),
            ('rvbbit.mcp_invocations',    'invocation_at'),
            ('rvbbit.cost_events',        'created_at'),
            ('rvbbit.sync_runs',          'started_at'),
            ('rvbbit.receipts',           'invocation_at')
        ) AS t(tbl, col)
    LOOP
        IF to_regclass(spec.tbl) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('DELETE FROM %s WHERE %I < $1', spec.tbl, spec.col) USING cutoff;
        GET DIAGNOSTICS n = ROW_COUNT;
        table_name := spec.tbl;
        rows_reaped := n;
        RETURN NEXT;
    END LOOP;
END $$;
