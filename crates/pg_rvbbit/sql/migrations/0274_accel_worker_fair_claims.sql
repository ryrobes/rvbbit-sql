-- 0274: Keep capacity-losing freshness workers useful and cheap.
--
-- A worker that lost the bounded heavy-maintenance race previously claimed a
-- table before checking heavy capacity, retained every such transaction lock,
-- and scanned/logged the entire registry.  With a large current-only backlog
-- that turned one useful rebuild into thousands of skip/defer rows and let a
-- capacity loser temporarily hide work from the actual heavy workers.
--
-- Execution now:
--   * considers only structurally stale accelerator candidates;
--   * checks shared heavy capacity before claiming a heavy table;
--   * keeps scanning for delta work when heavy capacity is exhausted;
--   * reports only a bounded diagnostic sample of deferred candidates; and
--   * stops the invocation immediately after its one permitted table action.

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

    IF position('heavy_capacity_deferred boolean' IN definition) = 0 THEN
        needle := $needle$
    delta_error text;
$needle$;
        replacement := $replacement$
    delta_error text;
    claimed_heavy_slot integer := 0;
    heavy_capacity_deferred boolean := false;
    deferred_rows_reported integer := 0;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0274 could not add fairness state';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('capacity-eligible candidates only' IN definition) = 0 THEN
        needle := $needle$
         WHERE e.active
           AND e.strategy <> 'manual'
         ORDER BY CASE
                      WHEN parallel_worker THEN
                          (mod(f.table_oid::bigint, worker_count::bigint)
                              + 1 = worker_slot)::integer
                      ELSE 1
                  END DESC,
                  rvbbit.current_replacement_pending(f.table_oid) DESC,
                  (f.drift_rows * (1 + f.heap_seq_scans)) DESC,
                  f.seconds_dirty DESC NULLS LAST
$needle$;
        replacement := $replacement$
         WHERE e.active
           AND e.strategy <> 'manual'
           -- capacity-eligible candidates only: clean accelerators do not
           -- belong in every worker's execution scan.
           AND (
               coalesce(
                   rvbbit.current_replacement_pending(f.table_oid),
                   false
               )
               OR coalesce(f.shadow_heap_dirty, false)
               OR (
                   coalesce(f.row_groups, 0) = 0
                   AND coalesce(f.heap_live_tuples, 0) > 0
               )
           )
         ORDER BY rvbbit.current_replacement_pending(f.table_oid) DESC,
                  CASE
                      WHEN parallel_worker THEN
                          (mod(f.table_oid::bigint, worker_count::bigint)
                              + 1 = worker_slot)::integer
                      ELSE 1
                  END DESC,
                  (f.drift_rows * (1 + f.heap_seq_scans)) DESC NULLS LAST,
                  f.seconds_dirty DESC NULLS LAST,
                  f.table_oid
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0274 could not bound the freshness candidate scan';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('Claim shared heavy capacity before the table claim' IN definition) = 0 THEN
        needle := $needle$
        IF do_execute AND NOT dry_run AND parallel_worker THEN
$needle$;
        replacement := $replacement$
        -- Claim shared heavy capacity before the table claim.  A worker that
        -- loses capacity must not retain advisory locks on every heavy table
        -- while it searches for independent delta work.
        IF do_execute
           AND NOT dry_run
           AND parallel_worker
           AND (prop_action = 'full' OR is_lance) THEN
            IF claimed_heavy_slot = 0 AND NOT heavy_capacity_deferred THEN
                claimed_heavy_slot :=
                    rvbbit._try_claim_accel_heavy_slot();
                heavy_capacity_deferred := claimed_heavy_slot = 0;
            END IF;
            IF heavy_capacity_deferred THEN
                do_execute := false;
                act_reason := prop_reason
                    || '; heavy maintenance slots busy';
            END IF;
        END IF;

        IF do_execute AND NOT dry_run AND parallel_worker THEN
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0274 could not add the pre-table heavy claim';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
                    -- Full rebuilds and separate Lance construction are the
                    -- heavy lane. Two deltas may overlap, but only one heavy
                    -- action may consume I/O at a time.
                    IF do_execute
                       AND (prop_action = 'full' OR is_lance)
                       AND rvbbit._try_claim_accel_heavy_slot() = 0 THEN
                        do_execute := false;
                        act_reason := prop_reason || '; heavy maintenance slots busy';
                    END IF;
$needle$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0274 could not remove the post-table heavy claim';
        END IF;
        definition := replace(definition, needle, '');
        changed := true;
    END IF;

    IF position('deferred_rows_reported >= 8' IN definition) = 0 THEN
        needle := $needle$
            IF NOT dry_run THEN
                INSERT INTO rvbbit.accel_tick_runs (
$needle$;
        replacement := $replacement$
            IF NOT dry_run THEN
                -- Sweep totals retain the complete counts, while the row API
                -- and append-only run log receive only a small diagnostic
                -- sample.  This preserves useful direct-call evidence without
                -- multiplying one blocked pass into thousands of rows.
                IF NOT should_act THEN
                    CONTINUE;
                END IF;
                IF deferred_rows_reported >= 8 THEN
                    CONTINUE;
                END IF;
                deferred_rows_reported := deferred_rows_reported + 1;

                INSERT INTO rvbbit.accel_tick_runs (
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0274 could not bound deferred row reporting';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('One worker invocation owns at most one table action' IN definition) = 0 THEN
        needle := $needle$
        RETURN NEXT;
    END LOOP;
$needle$;
        replacement := $replacement$
        RETURN NEXT;
        -- One worker invocation owns at most one table action.  The outer
        -- worker-pass procedure commits before requesting the next table.
        EXIT;
    END LOOP;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0274 could not stop after one table action';
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
    'Internal one-transaction freshness body. Workers scan only stale candidates, claim heavy capacity before tables, steal delta work, emit bounded deferral evidence, and stop after one action.';

