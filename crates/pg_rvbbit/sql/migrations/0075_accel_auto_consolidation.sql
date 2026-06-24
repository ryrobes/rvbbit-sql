-- 0075: Policy-driven LSM-style major compaction.
--
-- Delta refreshes remain the cheap minor-compaction path. This adds optional
-- row-group/tombstone pressure thresholds to the existing accel_tick policy
-- executor, so a scheduled table can fold accumulated accelerator fragments
-- back into a clean current export without a separate manual rebuild.

ALTER TABLE IF EXISTS rvbbit.accel_policy
    ADD COLUMN IF NOT EXISTS max_row_groups_before_rebuild integer;
ALTER TABLE IF EXISTS rvbbit.accel_policy
    ADD COLUMN IF NOT EXISTS max_tombstones_before_rebuild bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'rvbbit.accel_policy'::regclass
          AND conname = 'accel_policy_max_row_groups_before_rebuild_check'
    ) THEN
        ALTER TABLE rvbbit.accel_policy
            ADD CONSTRAINT accel_policy_max_row_groups_before_rebuild_check
            CHECK (max_row_groups_before_rebuild IS NULL OR max_row_groups_before_rebuild > 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'rvbbit.accel_policy'::regclass
          AND conname = 'accel_policy_max_tombstones_before_rebuild_check'
    ) THEN
        ALTER TABLE rvbbit.accel_policy
            ADD CONSTRAINT accel_policy_max_tombstones_before_rebuild_check
            CHECK (max_tombstones_before_rebuild IS NULL OR max_tombstones_before_rebuild > 0);
    END IF;
END $$;

CREATE OR REPLACE VIEW rvbbit.accel_policy_effective AS
SELECT
    t.table_oid,
    c.oid::regclass::text                          AS table_name,
    coalesce(p.strategy, 'manual')                 AS strategy,
    p.freshness_target_secs,
    coalesce(p.min_interval_secs, 60)              AS min_interval_secs,
    p.daily_refresh_budget,
    coalesce(p.full_rebuild_drift_ratio, 0.5)      AS full_rebuild_drift_ratio,
    coalesce(p.lance_separate, true)               AS lance_separate,
    coalesce(p.active, true)                       AS active,
    coalesce(p.denied_engines, '{}')               AS denied_engines,
    coalesce(p.denied_layouts, '{}')               AS denied_layouts,
    (p.table_oid IS NOT NULL)                      AS explicit,
    p.note,
    p.updated_at,
    p.max_row_groups_before_rebuild,
    p.max_tombstones_before_rebuild
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
LEFT JOIN rvbbit.accel_policy p ON p.table_oid = t.table_oid;

DO $$
BEGIN
    IF to_regprocedure(
        'rvbbit.set_accel_policy(regclass,text,integer,integer,integer,double precision,boolean,boolean,text)'
    ) IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit DROP FUNCTION rvbbit.set_accel_policy(
                regclass, text, integer, integer, integer, double precision, boolean, boolean, text
            );
        EXCEPTION
            WHEN undefined_object OR object_not_in_prerequisite_state THEN NULL;
        END;
    END IF;
END $$;

DROP FUNCTION IF EXISTS rvbbit.set_accel_policy(
    regclass, text, integer, integer, integer, double precision, boolean, boolean, text
);

CREATE OR REPLACE FUNCTION rvbbit.set_accel_policy(
    rel                      regclass,
    strategy                 text DEFAULT 'target',
    freshness_target_secs    integer DEFAULT NULL,
    min_interval_secs        integer DEFAULT 60,
    daily_refresh_budget     integer DEFAULT NULL,
    full_rebuild_drift_ratio double precision DEFAULT 0.5,
    lance_separate           boolean DEFAULT true,
    active                   boolean DEFAULT true,
    note                     text DEFAULT NULL,
    max_row_groups_before_rebuild integer DEFAULT NULL,
    max_tombstones_before_rebuild bigint DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    result jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.tables WHERE table_oid = rel) THEN
        RAISE EXCEPTION '% is not a registered rvbbit table (no acceleration catalog entry)', rel;
    END IF;

    INSERT INTO rvbbit.accel_policy AS ap (
        table_oid, strategy, freshness_target_secs, min_interval_secs,
        daily_refresh_budget, full_rebuild_drift_ratio,
        max_row_groups_before_rebuild, max_tombstones_before_rebuild,
        lance_separate, active, note, updated_at
    ) VALUES (
        rel, strategy, freshness_target_secs, min_interval_secs,
        daily_refresh_budget, full_rebuild_drift_ratio,
        max_row_groups_before_rebuild, max_tombstones_before_rebuild,
        lance_separate, active, note, now()
    )
    ON CONFLICT (table_oid) DO UPDATE SET
        strategy                 = EXCLUDED.strategy,
        freshness_target_secs    = EXCLUDED.freshness_target_secs,
        min_interval_secs        = EXCLUDED.min_interval_secs,
        daily_refresh_budget     = EXCLUDED.daily_refresh_budget,
        full_rebuild_drift_ratio = EXCLUDED.full_rebuild_drift_ratio,
        max_row_groups_before_rebuild = EXCLUDED.max_row_groups_before_rebuild,
        max_tombstones_before_rebuild = EXCLUDED.max_tombstones_before_rebuild,
        lance_separate           = EXCLUDED.lance_separate,
        active                   = EXCLUDED.active,
        note                     = EXCLUDED.note,
        updated_at               = now();

    SELECT to_jsonb(e) INTO result
      FROM rvbbit.accel_policy_effective e
     WHERE e.table_oid = rel;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_tick(
    budget       integer DEFAULT NULL,
    dry_run      boolean DEFAULT false,
    lance_budget integer DEFAULT 1
) RETURNS TABLE (
    table_oid      oid,
    table_name     text,
    strategy       text,
    action         text,
    reason         text,
    drift_rows     bigint,
    drift_ratio    double precision,
    seconds_dirty  double precision,
    heap_seq_scans bigint,
    executed       boolean,
    status         text,
    rows_written   bigint,
    error          text
) LANGUAGE plpgsql AS $$
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
$$;
