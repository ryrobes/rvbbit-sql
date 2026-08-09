-- 0277: Scope daily refresh budgets to one automation lifecycle.
--
-- accel_tick_runs is deliberately durable across accelerator retirement so it
-- remains useful for audit and workload analysis.  The rolling daily-budget
-- gate previously counted every receipt in the prior 24 hours, though, which
-- meant a manual table re-enrolled through "Baseline + auto" could immediately
-- inherit an exhausted budget from its previous lifecycle.  Preserve those
-- receipts but start a new accounting epoch whenever a non-executing policy
-- (manual or inactive) becomes active automation.

ALTER TABLE rvbbit.accel_policy
    ADD COLUMN IF NOT EXISTS budget_epoch_at timestamptz;

-- updated_at is the best existing enrollment boundary for upgraded databases.
-- In particular, reset-to-manual followed by Baseline + auto already stamped
-- the new policy time even though the old tick receipts were retained.
UPDATE rvbbit.accel_policy
   SET budget_epoch_at = coalesce(updated_at, clock_timestamp())
 WHERE budget_epoch_at IS NULL;

ALTER TABLE rvbbit.accel_policy
    ALTER COLUMN budget_epoch_at SET DEFAULT clock_timestamp();
ALTER TABLE rvbbit.accel_policy
    ALTER COLUMN budget_epoch_at SET NOT NULL;

COMMENT ON COLUMN rvbbit.accel_policy.budget_epoch_at IS
    'Start of the current active automation lifecycle. Older accel_tick_runs remain auditable but do not spend this policy budget.';

CREATE OR REPLACE FUNCTION rvbbit.accel_policy_budget_epoch_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.budget_epoch_at := coalesce(NEW.budget_epoch_at, clock_timestamp());
    ELSIF (
        (OLD.strategy = 'manual' OR NOT OLD.active)
        AND NEW.strategy <> 'manual'
        AND NEW.active
    ) THEN
        -- Crossing into executable automation starts a new budget lifecycle.
        -- Changes between active automation strategies retain the epoch.
        NEW.budget_epoch_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS accel_policy_budget_epoch_guard
    ON rvbbit.accel_policy;
CREATE TRIGGER accel_policy_budget_epoch_guard
BEFORE INSERT OR UPDATE ON rvbbit.accel_policy
FOR EACH ROW EXECUTE FUNCTION rvbbit.accel_policy_budget_epoch_guard();

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
    p.max_tombstones_before_rebuild,
    p.budget_epoch_at
FROM rvbbit.tables t
JOIN pg_class c ON c.oid = t.table_oid
LEFT JOIN rvbbit.accel_policy p ON p.table_oid = t.table_oid;

DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
    changed boolean := false;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit._accel_tick_batch(integer,boolean,integer)'::regprocedure
    );

    -- Fresh installs copy the current base accel_tick body into the private
    -- worker in 0268, so the planner half may already be epoch-aware while the
    -- post-claim worker gate added in 0269 is not. Patch every contract
    -- independently rather than treating the function as wholly old or new.
    IF position('cand.budget_epoch_at' IN definition) = 0 THEN
        needle := $needle$
               e.min_interval_secs,
               e.daily_refresh_budget,
               e.full_rebuild_drift_ratio,
$needle$;
        replacement := $replacement$
               e.min_interval_secs,
               e.daily_refresh_budget,
               e.budget_epoch_at,
               e.full_rebuild_drift_ratio,
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION
                '0277 could not add budget_epoch_at to the tick candidate snapshot';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('greatest(
                       now() - interval ''24 hours'',
                       cand.budget_epoch_at' IN definition) = 0 THEN
        needle := $needle$
            IF should_act AND cand.daily_refresh_budget IS NOT NULL THEN
                SELECT count(*)
                  INTO used_today
                  FROM rvbbit.accel_tick_runs r
                 WHERE r.table_oid = cand.f_oid
                   AND r.executed
                   AND r.ran_at > now() - interval '24 hours';
$needle$;
        replacement := $replacement$
            -- Policy lifecycle budget epoch: keep historical receipts without
            -- charging a newly enrolled automation policy for old work.
            IF should_act AND cand.daily_refresh_budget IS NOT NULL THEN
                SELECT count(*)
                  INTO used_today
                  FROM rvbbit.accel_tick_runs r
                 WHERE r.table_oid = cand.f_oid
                   AND r.executed
                   AND r.ran_at > greatest(
                       now() - interval '24 hours',
                       cand.budget_epoch_at
                   );
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION
                '0277 could not scope the tick planner budget to its policy epoch';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('e.budget_epoch_at
                  INTO live_candidate' IN definition) = 0 THEN
        needle := $needle$
                       e.strategy,
                       e.min_interval_secs,
                       e.daily_refresh_budget
                  INTO live_candidate
$needle$;
        replacement := $replacement$
                       e.strategy,
                       e.min_interval_secs,
                       e.daily_refresh_budget,
                       e.budget_epoch_at
                  INTO live_candidate
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION
                '0277 could not add budget_epoch_at to the claimed-table snapshot';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('live_candidate.budget_epoch_at' IN definition) = 0 THEN
        needle := $needle$
                    IF live_candidate.daily_refresh_budget IS NOT NULL THEN
                        SELECT count(*)
                          INTO used_today
                          FROM rvbbit.accel_tick_runs r
                         WHERE r.table_oid = cand.f_oid
                           AND r.executed
                           AND r.ran_at > now() - interval '24 hours';
$needle$;
        replacement := $replacement$
                    IF live_candidate.daily_refresh_budget IS NOT NULL THEN
                        SELECT count(*)
                          INTO used_today
                          FROM rvbbit.accel_tick_runs r
                         WHERE r.table_oid = cand.f_oid
                           AND r.executed
                           AND r.ran_at > greatest(
                               now() - interval '24 hours',
                               live_candidate.budget_epoch_at
                           );
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION
                '0277 could not scope the claimed-table budget to its policy epoch';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF changed THEN
        EXECUTE definition;
    END IF;
END;
$migration$;

COMMENT ON FUNCTION rvbbit._accel_tick_batch(integer, boolean, integer) IS
    'Internal one-transaction freshness body. Daily refresh budgets count durable tick receipts only within the current active automation lifecycle.';
