//! Accelerator freshness control plane.
//!
//! See `docs/ACCELERATOR_FRESHNESS_PLAN.md` for the full design. The key
//! reframe: because the planner degrades a stale table to a correct-but-slow
//! native heap scan (`parquet_authoritative_for_oid()` simply omits the
//! custom-scan path when `shadow_heap_dirty`), accelerator freshness is a
//! *performance/cost* optimization, never a correctness one. So this layer is
//! a value-vs-cost policy engine, not a cache-coherence protocol.
//!
//! Layer 1 (this block): make freshness legible. `rvbbit.accel_freshness`
//! fuses the supply side (dirty bit, watermark, parquet rows, rebuild cost)
//! with cheap per-table demand (`pg_stat_get_numscans` — seq scans on the heap
//! are the slow-path / "eligible-but-unused" signal). All columns are catalog
//! + pg_stat lookups; no heap scans.
//!
//! The auto-delta primitive already exists — `rvbbit.refresh_acceleration()`
//! closes the xid-watermark loop — so Layer 1 adds no refresh function; it just
//! measures. Layers 2 (policy) and 3 (executor) build on this view.

use pgrx::extension_sql;

extension_sql!(
    r#"
-- Layer 1: freshness legibility -------------------------------------------------
--
-- One row per registered rvbbit table. seconds_* are clamped to >= 0 because in
-- a single-statement-transaction edge case clock_timestamp() stamps can lead
-- now() (transaction start); in production the dirty episode began in a prior
-- transaction so the elapsed values are naturally positive.

CREATE OR REPLACE VIEW rvbbit.accel_freshness AS
SELECT
    t.table_oid,
    c.oid::regclass::text                   AS table_name,
    coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
    coalesce(t.shadow_heap_dirty, false)    AS shadow_heap_dirty,
    (
        pg_relation_size(t.table_oid) = 0
        OR coalesce(t.shadow_heap_retained AND NOT t.shadow_heap_dirty, false)
    )                                       AS parquet_authoritative,

    -- staleness aging. dirty_since is NULLed when the table is clean, so the
    -- refresh/rebuild clear-sites don't need to reset the column themselves.
    CASE WHEN coalesce(t.shadow_heap_dirty, false) THEN t.dirty_since END
                                            AS dirty_since,
    CASE WHEN coalesce(t.shadow_heap_dirty, false) AND t.dirty_since IS NOT NULL
         THEN greatest(0, extract(epoch FROM now() - t.dirty_since))
    END                                     AS seconds_dirty,
    t.last_write_at,

    s.last_refresh_at,
    CASE WHEN s.last_refresh_at IS NOT NULL
         THEN greatest(0, extract(epoch FROM now() - s.last_refresh_at))
    END                                     AS seconds_since_refresh,
    coalesce(s.last_refresh_xid, 0)         AS last_refresh_xid,

    -- supply side
    coalesce(rg.parquet_rows, 0)            AS parquet_rows,
    coalesce(rg.row_groups, 0)              AS row_groups,
    coalesce(rg.parquet_bytes, 0)           AS parquet_bytes,
    pg_stat_get_live_tuples(t.table_oid)    AS heap_live_tuples,
    greatest(0, pg_stat_get_live_tuples(t.table_oid) - coalesce(rg.parquet_rows, 0))
                                            AS est_unmirrored_rows,
    coalesce(dl.tombstones, 0)              AS tombstones,

    -- drift = un-mirrored inserts + tombstoned (updated/deleted) rows. This is
    -- the LSM "how far has L0 grown past the runs" signal that drives the
    -- delta-vs-full decision in Layer 3.
    (greatest(0, pg_stat_get_live_tuples(t.table_oid) - coalesce(rg.parquet_rows, 0))
        + coalesce(dl.tombstones, 0))       AS drift_rows,
    CASE WHEN coalesce(rg.parquet_rows, 0) > 0
         THEN (greatest(0, pg_stat_get_live_tuples(t.table_oid) - coalesce(rg.parquet_rows, 0))
                  + coalesce(dl.tombstones, 0))::float8 / rg.parquet_rows
    END                                     AS drift_ratio,

    -- demand: sequential scans on the heap. On an accelerated table these are
    -- queries on the SLOW path (the planner declined the custom scan) — i.e.
    -- the "eligible-but-unused" signal. High here + dirty = high refresh value.
    pg_stat_get_numscans(t.table_oid)       AS heap_seq_scans,

    -- last successful rebuild cost
    op.last_rebuild_ms,
    op.last_rebuild_rows,

    EXISTS (
        SELECT 1 FROM rvbbit.acceleration_operations o
         WHERE o.table_oid = t.table_oid AND o.status = 'running'
    )                                       AS op_running,
    (t.lance_url IS NOT NULL)               AS lance_accelerated
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
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

COMMENT ON VIEW rvbbit.accel_freshness IS
    'Per-table accelerator freshness + value signals (Layer 1 of the freshness control plane). '
    'Fuses supply-side staleness (dirty bit, watermark, drift, rebuild cost) with cheap per-table '
    'demand (heap_seq_scans = slow-path queries). Drives rvbbit.accel_policy / rvbbit.accel_tick.';
"#,
    name = "accel_freshness_layer1",
    requires = ["rvbbit_bootstrap"],
);

extension_sql!(
    r#"
-- Layer 2: per-table refresh policy ---------------------------------------------
--
-- A policy is a *freshness target + a budget*, not a schedule. Absence of a row
-- means 'manual' — nothing changes until a table opts in — so this table is
-- empty by default and accel_policy_effective supplies the manual fallback.

CREATE TABLE rvbbit.accel_policy (
    table_oid                oid PRIMARY KEY REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    -- manual:     executor never touches it (explicit user calls only).
    -- scheduled:  refresh whenever dirty on the heartbeat (subject to min_interval).
    -- target:     refresh when stale longer than freshness_target_secs (a freshness SLO).
    -- demand:     refresh when dirty AND being queried on the slow path (value-driven).
    -- continuous: refresh every tick while dirty (target=0; min_interval still applies).
    strategy                 text NOT NULL DEFAULT 'manual',
    freshness_target_secs    integer,            -- 'target': keep within N seconds stale
    min_interval_secs        integer NOT NULL DEFAULT 60,   -- floor on auto-refresh frequency
    daily_refresh_budget     integer,            -- max auto-refreshes / rolling 24h (NULL = unlimited)
    -- Below this drift ratio the executor does a cheap delta refresh; at/above it
    -- escalates to a full rebuild (the LSM major-compaction trigger).
    full_rebuild_drift_ratio double precision NOT NULL DEFAULT 0.5,
    -- Lance datasets are always full-overwrite (expensive). When true, the
    -- executor refreshes them under a stricter, separate sub-budget.
    lance_separate           boolean NOT NULL DEFAULT true,
    active                   boolean NOT NULL DEFAULT true,
    note                     text,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT accel_policy_strategy_check
        CHECK (strategy IN ('manual', 'scheduled', 'target', 'demand', 'continuous')),
    CHECK (freshness_target_secs IS NULL OR freshness_target_secs > 0),
    CHECK (min_interval_secs >= 0),
    CHECK (daily_refresh_budget IS NULL OR daily_refresh_budget >= 0),
    CHECK (full_rebuild_drift_ratio >= 0)
);

COMMENT ON TABLE rvbbit.accel_policy IS
    'Per-table accelerator refresh policy (Layer 2). Absent row = manual. A policy expresses '
    'a freshness target + a budget; rvbbit.accel_tick (Layer 3) turns it into delta/full/skip actions.';

-- Effective policy: every registered table, with the manual fallback applied for
-- tables that have no explicit row. `explicit` distinguishes a real policy from
-- the default so the UI can show "(default)".
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
    (p.table_oid IS NOT NULL)                      AS explicit,
    p.note,
    p.updated_at
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
LEFT JOIN rvbbit.accel_policy p ON p.table_oid = t.table_oid;

-- Upsert helper. Defaults to 'target' since calling it at all signals intent to
-- automate (the no-row default is already 'manual'). Returns the effective row.
CREATE OR REPLACE FUNCTION rvbbit.set_accel_policy(
    rel                      regclass,
    strategy                 text DEFAULT 'target',
    freshness_target_secs    integer DEFAULT NULL,
    min_interval_secs        integer DEFAULT 60,
    daily_refresh_budget     integer DEFAULT NULL,
    full_rebuild_drift_ratio double precision DEFAULT 0.5,
    lance_separate           boolean DEFAULT true,
    active                   boolean DEFAULT true,
    note                     text DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    result jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.tables WHERE table_oid = rel) THEN
        RAISE EXCEPTION '% is not a registered rvbbit table (no acceleration catalog entry)', rel;
    END IF;

    INSERT INTO rvbbit.accel_policy AS ap (
        table_oid, strategy, freshness_target_secs, min_interval_secs,
        daily_refresh_budget, full_rebuild_drift_ratio, lance_separate, active, note, updated_at
    ) VALUES (
        rel, strategy, freshness_target_secs, min_interval_secs,
        daily_refresh_budget, full_rebuild_drift_ratio, lance_separate, active, note, now()
    )
    ON CONFLICT (table_oid) DO UPDATE SET
        strategy                 = EXCLUDED.strategy,
        freshness_target_secs    = EXCLUDED.freshness_target_secs,
        min_interval_secs        = EXCLUDED.min_interval_secs,
        daily_refresh_budget     = EXCLUDED.daily_refresh_budget,
        full_rebuild_drift_ratio = EXCLUDED.full_rebuild_drift_ratio,
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

-- Reset a table back to the manual default (drops the explicit row).
CREATE OR REPLACE FUNCTION rvbbit.clear_accel_policy(rel regclass)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
    n_deleted integer;
BEGIN
    DELETE FROM rvbbit.accel_policy WHERE table_oid = rel;
    GET DIAGNOSTICS n_deleted = ROW_COUNT;
    RETURN n_deleted > 0;
END;
$$;
"#,
    name = "accel_policy_layer2",
    requires = ["rvbbit_bootstrap"],
);

extension_sql!(
    r#"
-- Layer 3: the executor ---------------------------------------------------------
--
-- rvbbit.accel_tick() is the brain a pg_cron heartbeat calls. It reads the
-- freshness rollup + effective policy, decides skip/delta/full per table within
-- budget, prefers the cheap auto-delta (refresh_acceleration), and escalates to
-- a full rebuild on drift or when delta refuses. pg_cron is only the clock.

CREATE TABLE rvbbit.accel_tick_runs (
    id            bigserial PRIMARY KEY,
    table_oid     oid,                 -- not a FK: keep history even if the table is dropped
    table_name    text NOT NULL,
    ran_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    strategy      text,
    action        text NOT NULL,       -- delta | full | skip
    reason        text,
    drift_rows    bigint,
    heap_seq_scans bigint,
    executed      boolean NOT NULL DEFAULT false,
    status        text,                -- ok | noop | failed | deferred | skip
    rows_written  bigint,
    error         text
);

CREATE INDEX accel_tick_runs_table_time_idx ON rvbbit.accel_tick_runs (table_oid, ran_at DESC);
CREATE INDEX accel_tick_runs_time_idx       ON rvbbit.accel_tick_runs (ran_at DESC);

COMMENT ON TABLE rvbbit.accel_tick_runs IS
    'Per-table action log for each rvbbit.accel_tick() heartbeat. Feeds the daily-budget '
    'counter and the demand baseline, and gives the cockpit historical run data.';

CREATE OR REPLACE FUNCTION rvbbit.accel_tick(
    budget       integer DEFAULT NULL,   -- max tables to refresh this tick (NULL = all eligible)
    dry_run      boolean DEFAULT false,  -- plan only; no execution, no history written
    lance_budget integer DEFAULT 1       -- max lance (full-overwrite) tables this tick
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
    act_reason  text;
    res         jsonb;
    last_scans  bigint;
    used_today  integer;
    is_lance    boolean;
BEGIN
    -- One *executing* tick at a time across the cluster. dry_run is read-only
    -- planning (the cockpit preview) and is safe to run concurrently, so it
    -- skips the singleton guard. Re-entrant within the same backend, so calling
    -- accel_tick() twice in one session/txn is fine.
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 7) THEN
        RETURN;  -- another tick holds the lock
    END IF;

    FOR cand IN
        SELECT f.table_oid        AS f_oid,
               f.table_name       AS f_name,
               f.shadow_heap_dirty,
               f.seconds_dirty,
               f.seconds_since_refresh,
               f.drift_rows,
               f.drift_ratio,
               f.heap_seq_scans,
               f.lance_accelerated,
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
         ORDER BY (f.drift_rows * (1 + f.heap_seq_scans)) DESC,
                  f.seconds_dirty DESC NULLS LAST
    LOOP
        -- Proposed kind: full when there's no baseline or drift crossed the
        -- threshold (LSM major compaction); otherwise a cheap delta.
        prop_action := CASE
            WHEN cand.drift_ratio IS NULL OR cand.drift_ratio >= cand.full_rebuild_drift_ratio
                THEN 'full' ELSE 'delta' END;
        is_lance   := coalesce(cand.lance_accelerated, false) AND coalesce(cand.lance_separate, true);
        should_act := false;
        act_reason := '';

        IF NOT cand.shadow_heap_dirty THEN
            act_reason := 'clean';
        ELSIF cand.seconds_since_refresh IS NOT NULL
              AND cand.seconds_since_refresh < cand.min_interval_secs THEN
            act_reason := format('min_interval %ss not elapsed', cand.min_interval_secs);
        ELSE
            -- Strategy gate.
            IF cand.strategy IN ('scheduled', 'continuous') THEN
                should_act := true;
                act_reason := 'dirty';
            ELSIF cand.strategy = 'target' THEN
                IF cand.freshness_target_secs IS NULL
                   OR coalesce(cand.seconds_dirty, 0) >= cand.freshness_target_secs THEN
                    should_act := true;
                    act_reason := format('stale %ss >= target %ss',
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
                    act_reason := 'demand grew on slow path';
                ELSE
                    act_reason := CASE WHEN last_scans IS NULL
                                       THEN 'demand baseline' ELSE 'no new slow-path demand' END;
                END IF;
            END IF;

            -- Daily budget (counts executed tick refreshes in the last 24h).
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

        -- Per-tick budget gating.
        do_execute := should_act;
        IF do_execute AND budget IS NOT NULL AND acted >= budget THEN
            do_execute := false;
            act_reason := 'tick budget reached';
        ELSIF do_execute AND is_lance AND lance_acted >= lance_budget THEN
            do_execute := false;
            act_reason := 'lance budget reached';
        END IF;

        -- Assemble the OUT row.
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
            -- Not acting (clean / gated / deferred). 'deferred' = wanted to but
            -- a budget/interval blocked it; 'skip' = nothing to do.
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

        -- We will act. Count it against budgets up front.
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

        -- Execute. Each table is isolated in its own subtransaction so one
        -- failure doesn't abort the whole tick.
        BEGIN
            IF prop_action = 'full' THEN
                res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
            ELSE
                BEGIN
                    res := rvbbit.refresh_acceleration(cand.f_oid::regclass, true);
                EXCEPTION WHEN OTHERS THEN
                    -- Delta refused (e.g. dirty bootstrap) -> escalate to full.
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

COMMENT ON FUNCTION rvbbit.accel_tick(integer, boolean, integer) IS
    'Policy-driven accelerator refresh executor (Layer 3). Call on a heartbeat (pg_cron). '
    'dry_run=true returns the plan without executing — the cockpit "projected consequence" preview.';

-- Convenience: register the heartbeat as a pg_cron job. Errors clearly if
-- pg_cron is absent. Built with EXECUTE so the extension installs without a
-- hard dependency on the cron schema.
CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_tick(
    cron_schedule text DEFAULT '* * * * *',
    budget        integer DEFAULT 4
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    jobid bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule the accelerator heartbeat. '
            'Install pg_cron (shared_preload_libraries) then retry, or call rvbbit.accel_tick() manually.';
    END IF;
    EXECUTE format(
        'SELECT cron.schedule(%L, %L, %L)',
        'rvbbit_accel_tick',
        cron_schedule,
        format('SELECT rvbbit.accel_tick(%s)', budget)
    ) INTO jobid;
    RETURN jobid;
END;
$$;
"#,
    name = "accel_tick_layer3",
    requires = ["rvbbit_bootstrap", "accel_policy_layer2"],
);

#[cfg(any(test, feature = "pg_test"))]
#[pgrx::pg_schema]
mod tests {
    use pgrx::prelude::*;

    // Seed catalog rows on a real relation oid so we can exercise the view's
    // math + the dirty trigger deterministically, without driving a live
    // compaction (which writes parquet to disk and is covered by the live E2E).

    #[pg_test]
    fn accel_freshness_reports_rows_and_drift() {
        Spi::run("CREATE TABLE fresh_demo (id int, v text)").unwrap();
        Spi::run(
            "INSERT INTO rvbbit.tables (table_oid, shadow_heap_retained, shadow_heap_dirty) \
             VALUES ('fresh_demo'::regclass, true, false)",
        )
        .unwrap();
        // Pretend a compaction wrote 100 rows in one row group at generation 1.
        Spi::run(
            "INSERT INTO rvbbit.row_groups (table_oid, rg_id, path, n_rows, n_bytes, generation) \
             VALUES ('fresh_demo'::regclass, 0, '/tmp/fresh_demo-0.parquet', 100, 4096, 1)",
        )
        .unwrap();
        Spi::run(
            "INSERT INTO rvbbit.acceleration_state \
             (table_oid, last_refresh_xid, last_refresh_generation, last_refresh_rows, last_refresh_at) \
             VALUES ('fresh_demo'::regclass, 50, 1, 100, now())",
        )
        .unwrap();

        // Clean + authoritative, 100 parquet rows visible.
        let dirty: bool = Spi::get_one(
            "SELECT shadow_heap_dirty FROM rvbbit.accel_freshness WHERE table_name = 'fresh_demo'",
        )
        .unwrap()
        .unwrap();
        assert!(!dirty, "freshly refreshed table should be clean");
        let auth: bool = Spi::get_one(
            "SELECT parquet_authoritative FROM rvbbit.accel_freshness WHERE table_name = 'fresh_demo'",
        )
        .unwrap()
        .unwrap();
        assert!(auth, "clean retained heap => parquet authoritative");
        let pr: i64 = Spi::get_one(
            "SELECT parquet_rows FROM rvbbit.accel_freshness WHERE table_name = 'fresh_demo'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(pr, 100);
        let rgs: i64 = Spi::get_one(
            "SELECT row_groups FROM rvbbit.accel_freshness WHERE table_name = 'fresh_demo'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(rgs, 1);

        // Tombstones count toward drift.
        Spi::run(
            "INSERT INTO rvbbit.delete_log (table_oid, rg_id, ordinal, deleted_xid) VALUES \
             ('fresh_demo'::regclass, 0, 1, '60'::xid8), \
             ('fresh_demo'::regclass, 0, 2, '61'::xid8)",
        )
        .unwrap();
        let drift: i64 = Spi::get_one(
            "SELECT drift_rows FROM rvbbit.accel_freshness WHERE table_name = 'fresh_demo'",
        )
        .unwrap()
        .unwrap();
        assert!(drift >= 2, "two tombstones should make drift >= 2, got {drift}");
    }

    #[pg_test]
    fn dirty_trigger_stamps_and_holds_dirty_since() {
        Spi::run("CREATE TABLE dt_demo (id int)").unwrap();
        Spi::run(
            "INSERT INTO rvbbit.tables (table_oid, shadow_heap_retained, shadow_heap_dirty) \
             VALUES ('dt_demo'::regclass, true, false)",
        )
        .unwrap();
        Spi::run(
            "CREATE TRIGGER rvbbit_shadow_heap_dirty \
             AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON dt_demo \
             FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()",
        )
        .unwrap();

        // First write: goes dirty, stamps dirty_since + last_write_at.
        Spi::run("INSERT INTO dt_demo VALUES (1)").unwrap();
        let dirty: bool = Spi::get_one(
            "SELECT shadow_heap_dirty FROM rvbbit.tables WHERE table_oid = 'dt_demo'::regclass",
        )
        .unwrap()
        .unwrap();
        assert!(dirty, "write should flip the dirty bit");
        let stamped: bool = Spi::get_one(
            "SELECT dirty_since IS NOT NULL AND last_write_at IS NOT NULL \
               FROM rvbbit.tables WHERE table_oid = 'dt_demo'::regclass",
        )
        .unwrap()
        .unwrap();
        assert!(stamped, "first write should stamp dirty_since and last_write_at");
        let since1: String = Spi::get_one(
            "SELECT dirty_since::text FROM rvbbit.tables WHERE table_oid = 'dt_demo'::regclass",
        )
        .unwrap()
        .unwrap();

        // Second write in the same dirty episode: dirty_since must NOT move
        // (the onset is sticky); last_write_at advances (>= dirty_since).
        Spi::run("INSERT INTO dt_demo VALUES (2)").unwrap();
        let since2: String = Spi::get_one(
            "SELECT dirty_since::text FROM rvbbit.tables WHERE table_oid = 'dt_demo'::regclass",
        )
        .unwrap()
        .unwrap();
        assert_eq!(since1, since2, "dirty_since must be stable across writes in one episode");
        let ordered: bool = Spi::get_one(
            "SELECT last_write_at >= dirty_since FROM rvbbit.tables WHERE table_oid = 'dt_demo'::regclass",
        )
        .unwrap()
        .unwrap();
        assert!(ordered, "last_write_at should be at/after the dirty onset");

        // The view NULLs dirty_since once the table is marked clean again,
        // even though the underlying column still holds the old value.
        Spi::run(
            "UPDATE rvbbit.tables SET shadow_heap_dirty = false WHERE table_oid = 'dt_demo'::regclass",
        )
        .unwrap();
        let view_since: Option<String> = Spi::get_one(
            "SELECT dirty_since::text FROM rvbbit.accel_freshness WHERE table_name = 'dt_demo'",
        )
        .unwrap();
        assert!(view_since.is_none(), "accel_freshness should NULL dirty_since when clean");
        let view_dirty: bool = Spi::get_one(
            "SELECT shadow_heap_dirty FROM rvbbit.accel_freshness WHERE table_name = 'dt_demo'",
        )
        .unwrap()
        .unwrap();
        assert!(!view_dirty);
    }

    fn register(rel: &str) {
        Spi::run(&format!("CREATE TABLE {rel} (id int)")).unwrap();
        Spi::run(&format!(
            "INSERT INTO rvbbit.tables (table_oid, shadow_heap_retained, shadow_heap_dirty) \
             VALUES ('{rel}'::regclass, true, false)"
        ))
        .unwrap();
    }

    #[pg_test]
    fn policy_defaults_to_manual_when_absent() {
        register("pol_demo");
        // No explicit policy => effective view reports manual, not explicit.
        let strategy: String = Spi::get_one(
            "SELECT strategy FROM rvbbit.accel_policy_effective WHERE table_name = 'pol_demo'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(strategy, "manual");
        let explicit: bool = Spi::get_one(
            "SELECT explicit FROM rvbbit.accel_policy_effective WHERE table_name = 'pol_demo'",
        )
        .unwrap()
        .unwrap();
        assert!(!explicit, "absent policy must not read as explicit");
        // Manual default also fills the guard columns.
        let min_int: i32 = Spi::get_one(
            "SELECT min_interval_secs FROM rvbbit.accel_policy_effective WHERE table_name = 'pol_demo'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(min_int, 60);
    }

    #[pg_test]
    fn set_accel_policy_upserts_and_is_effective() {
        register("pol_up");
        // First set: target SLO of 300s.
        let j: pgrx::JsonB = Spi::get_one(
            "SELECT rvbbit.set_accel_policy('pol_up'::regclass, 'target', 300)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(j.0.get("strategy").and_then(|v| v.as_str()), Some("target"));
        assert_eq!(
            j.0.get("freshness_target_secs").and_then(|v| v.as_i64()),
            Some(300)
        );
        assert_eq!(j.0.get("explicit").and_then(|v| v.as_bool()), Some(true));

        let n: i64 = Spi::get_one(
            "SELECT count(*) FROM rvbbit.accel_policy WHERE table_oid = 'pol_up'::regclass",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 1, "one policy row");

        // Second set: upsert, not duplicate — switch to continuous.
        Spi::run("SELECT rvbbit.set_accel_policy('pol_up'::regclass, 'continuous')").unwrap();
        let n2: i64 = Spi::get_one(
            "SELECT count(*) FROM rvbbit.accel_policy WHERE table_oid = 'pol_up'::regclass",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n2, 1, "upsert must not duplicate");
        let strat: String = Spi::get_one(
            "SELECT strategy FROM rvbbit.accel_policy_effective WHERE table_name = 'pol_up'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(strat, "continuous");

        // Clear resets to the manual default.
        let cleared: bool =
            Spi::get_one("SELECT rvbbit.clear_accel_policy('pol_up'::regclass)").unwrap().unwrap();
        assert!(cleared);
        let strat2: String = Spi::get_one(
            "SELECT strategy FROM rvbbit.accel_policy_effective WHERE table_name = 'pol_up'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(strat2, "manual", "cleared policy falls back to manual");
    }

    #[pg_test]
    #[should_panic(expected = "accel_policy_strategy_check")]
    fn set_accel_policy_rejects_bad_strategy() {
        register("pol_bad");
        // Violates the CHECK on strategy.
        Spi::run("SELECT rvbbit.set_accel_policy('pol_bad'::regclass, 'turbo')").unwrap();
    }

    // --- Layer 3 (executor) decision-logic tests. All dry_run, so deterministic
    // and side-effect-free; live delta/full execution is covered by the E2E. ---

    fn seed_accel(rel: &str, parquet_rows: i64) {
        Spi::run(&format!("CREATE TABLE {rel} (id int)")).unwrap();
        Spi::run(&format!(
            "INSERT INTO rvbbit.tables (table_oid, shadow_heap_retained, shadow_heap_dirty) \
             VALUES ('{rel}'::regclass, true, false)"
        ))
        .unwrap();
        if parquet_rows > 0 {
            Spi::run(&format!(
                "INSERT INTO rvbbit.row_groups (table_oid, rg_id, path, n_rows, n_bytes, generation) \
                 VALUES ('{rel}'::regclass, 0, '/tmp/{rel}-0.parquet', {parquet_rows}, 4096, 1)"
            ))
            .unwrap();
        }
        // last_refresh an hour ago so the 60s min_interval never blocks tests.
        Spi::run(&format!(
            "INSERT INTO rvbbit.acceleration_state \
             (table_oid, last_refresh_xid, last_refresh_generation, last_refresh_rows, last_refresh_at) \
             VALUES ('{rel}'::regclass, 50, 1, {parquet_rows}, now() - interval '1 hour')"
        ))
        .unwrap();
    }

    fn make_dirty(rel: &str, dirty_age_secs: i64, tombstones: i64) {
        Spi::run(&format!(
            "UPDATE rvbbit.tables \
                SET shadow_heap_dirty = true, dirty_since = now() - make_interval(secs => {dirty_age_secs}) \
              WHERE table_oid = '{rel}'::regclass"
        ))
        .unwrap();
        if tombstones > 0 {
            Spi::run(&format!(
                "INSERT INTO rvbbit.delete_log (table_oid, rg_id, ordinal, deleted_xid) \
                 SELECT '{rel}'::regclass, 0, g, '60'::xid8 FROM generate_series(1, {tombstones}) g"
            ))
            .unwrap();
        }
    }

    #[pg_test]
    fn accel_tick_skips_manual_and_clean() {
        seed_accel("tk_manual", 100);
        make_dirty("tk_manual", 600, 10); // dirty + drifted, but policy stays manual

        seed_accel("tk_clean", 100);
        Spi::run("SELECT rvbbit.set_accel_policy('tk_clean'::regclass, 'scheduled')").unwrap();
        // not dirtied

        // Manual table is excluded entirely.
        let manual_rows: i64 = Spi::get_one(
            "SELECT count(*) FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_manual'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(manual_rows, 0, "manual tables are not candidates");

        // Clean scheduled table appears but is skipped.
        let action: String = Spi::get_one(
            "SELECT action FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_clean'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(action, "skip");
        let reason: String = Spi::get_one(
            "SELECT reason FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_clean'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(reason, "clean");
    }

    #[pg_test]
    fn accel_tick_plans_delta_vs_full_by_drift() {
        seed_accel("tk_delta", 100);
        Spi::run("SELECT rvbbit.set_accel_policy('tk_delta'::regclass, 'scheduled')").unwrap();
        make_dirty("tk_delta", 600, 10); // 10/100 = 0.1 < 0.5 -> delta

        seed_accel("tk_full", 100);
        Spi::run("SELECT rvbbit.set_accel_policy('tk_full'::regclass, 'scheduled')").unwrap();
        make_dirty("tk_full", 600, 60); // 60/100 = 0.6 >= 0.5 -> full

        seed_accel("tk_nobaseline", 0); // no row groups -> drift_ratio NULL -> full
        Spi::run("SELECT rvbbit.set_accel_policy('tk_nobaseline'::regclass, 'scheduled')").unwrap();
        make_dirty("tk_nobaseline", 600, 0);

        let a_delta: String = Spi::get_one(
            "SELECT action FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_delta'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(a_delta, "delta", "low drift -> delta");
        let a_full: String = Spi::get_one(
            "SELECT action FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_full'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(a_full, "full", "drift past threshold -> full");
        let a_nb: String = Spi::get_one(
            "SELECT action FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_nobaseline'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(a_nb, "full", "no parquet baseline -> full");
        // All planned (dry run), none executed, nothing logged.
        let planned: i64 = Spi::get_one(
            "SELECT count(*) FROM rvbbit.accel_tick(NULL, true) WHERE status = 'planned'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(planned, 3);
        let logged: i64 = Spi::get_one("SELECT count(*) FROM rvbbit.accel_tick_runs").unwrap().unwrap();
        assert_eq!(logged, 0, "dry_run must not write history");
    }

    #[pg_test]
    fn accel_tick_target_slo_gate() {
        seed_accel("tk_target", 100);
        Spi::run("SELECT rvbbit.set_accel_policy('tk_target'::regclass, 'target', 300)").unwrap();

        // 100s stale < 300s target -> within target, skip.
        make_dirty("tk_target", 100, 0);
        let action: String = Spi::get_one(
            "SELECT action FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_target'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(action, "skip", "within freshness target -> skip");
        let reason: String = Spi::get_one(
            "SELECT reason FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_target'",
        )
        .unwrap()
        .unwrap();
        assert!(reason.contains("within target"), "reason was: {reason}");

        // Age it past the SLO -> should act (delta, drift 0).
        Spi::run(
            "UPDATE rvbbit.tables SET dirty_since = now() - interval '400 seconds' \
              WHERE table_oid = 'tk_target'::regclass",
        )
        .unwrap();
        let action2: String = Spi::get_one(
            "SELECT action FROM rvbbit.accel_tick(NULL, true) WHERE table_name = 'tk_target'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(action2, "delta", "past SLO -> act (delta, no drift)");
    }

    #[pg_test]
    fn accel_tick_budget_defers_lower_value() {
        seed_accel("tk_b1", 100);
        Spi::run("SELECT rvbbit.set_accel_policy('tk_b1'::regclass, 'scheduled')").unwrap();
        make_dirty("tk_b1", 600, 50); // higher drift -> higher value -> goes first

        seed_accel("tk_b2", 100);
        Spi::run("SELECT rvbbit.set_accel_policy('tk_b2'::regclass, 'scheduled')").unwrap();
        make_dirty("tk_b2", 600, 5); // lower value -> deferred under budget 1

        let planned: i64 = Spi::get_one(
            "SELECT count(*) FROM rvbbit.accel_tick(1, true) WHERE status = 'planned'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(planned, 1, "budget 1 plans exactly one");
        let planned_name: String = Spi::get_one(
            "SELECT table_name FROM rvbbit.accel_tick(1, true) WHERE status = 'planned'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(planned_name, "tk_b1", "higher-value table is chosen first");
        let deferred_reason: String = Spi::get_one(
            "SELECT reason FROM rvbbit.accel_tick(1, true) WHERE status = 'deferred'",
        )
        .unwrap()
        .unwrap();
        assert!(deferred_reason.contains("budget"), "reason was: {deferred_reason}");
    }
}
