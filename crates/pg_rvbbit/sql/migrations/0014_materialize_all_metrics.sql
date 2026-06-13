-- 0014_materialize_all_metrics
--
-- A one-shot "materialize EVERY defined metric, now, at one timestamp" — the
-- bulk sibling of materialize_metric (one metric) and materialize_tick (only
-- metrics whose tables just compacted). Snapshots all current metrics into
-- rvbbit.metric_observations at a single def/data timestamp, so it's a clean
-- pg_cron job:
--   SELECT cron.schedule('rvbbit_materialize_all', '0 * * * *',
--                        $$SELECT rvbbit.materialize_all_metrics()$$);
-- or, scoped to a category:
--   SELECT rvbbit.materialize_all_metrics(p_category => 'Finance');
--
-- Returns one row per metric so the caller (cron log / UI) sees what happened;
-- a failing metric is caught and reported, never aborting the rest.

CREATE OR REPLACE FUNCTION rvbbit.materialize_all_metrics(
    p_category    text        DEFAULT NULL,   -- NULL = every category (incl. uncategorized)
    p_subcategory text        DEFAULT NULL,
    p_def_as_of   timestamptz DEFAULT NULL,   -- NULL = now() (definition-time axis)
    p_data_as_of  timestamptz DEFAULT NULL,   -- NULL = now() (data-time / AS OF axis)
    p_trigger     text        DEFAULT 'bulk'
) RETURNS TABLE(metric_name text, observation_id bigint, status text, error text)
LANGUAGE plpgsql AS $fn$
DECLARE
    rec   record;
    -- Captured once so the whole batch shares ONE timestamp (a consistent
    -- snapshot), independent of how long the loop takes.
    v_def  timestamptz := coalesce(p_def_as_of, now());
    v_data timestamptz := coalesce(p_data_as_of, now());
    v_obs  bigint;
BEGIN
    FOR rec IN
        SELECT name
          FROM rvbbit.metric_catalog
         WHERE (p_category    IS NULL OR category    = p_category)
           AND (p_subcategory IS NULL OR subcategory = p_subcategory)
         ORDER BY name
    LOOP
        BEGIN
            v_obs := rvbbit.materialize_metric(rec.name, '{}'::jsonb, v_def, v_data, NULL, p_trigger);
            metric_name := rec.name; observation_id := v_obs; status := 'ok'; error := NULL;
        EXCEPTION WHEN others THEN
            -- isolate a failing metric to its own savepoint; report and continue
            metric_name := rec.name; observation_id := NULL; status := 'error'; error := left(SQLERRM, 500);
        END;
        RETURN NEXT;
    END LOOP;
END $fn$;
