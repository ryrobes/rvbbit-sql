-- 0029_cube_refresh_pacing_and_accel_exclude
--
-- Two pressure-relief valves for bulk cube refresh (catalog_crawl-style "make
-- the whole DB unusable" symptom):
--
-- 1. refresh_all_cubes() gets a small pacing sleep BETWEEN cubes (after the
--    per-cube COMMIT, holding no locks), so a back-to-back compaction loop stops
--    pegging CPU/I/O and lets dashboards/metrics breathe. Tunable.
--
-- 2. accel_tick stops trying to maintain cube-schema tables. Cubes are fully
--    rebuilt by refresh_cube (snapshot_load → compact), so the freshness
--    heartbeat has nothing to do for them — and worse, if it fires while a cube
--    is mid-refresh (dirty) it races the bulk's compact on the same table. We
--    coerce any table in an excluded schema to strategy 'manual' in
--    accel_policy_effective (which accel_tick already skips), so no accel_tick /
--    accel_tick body change is needed. Configurable; default excludes 'cubes':
--      SET rvbbit.accel_exclude_schemas = 'cubes,staging';   -- add more
--      SET rvbbit.accel_exclude_schemas = '';                -- exclude nothing

-- Schemas accel_tick should leave alone (their tables are maintained elsewhere).
-- Unset → 'cubes'; explicitly '' → exclude nothing.
CREATE OR REPLACE FUNCTION rvbbit._accel_excluded_schemas()
RETURNS text[] LANGUAGE sql STABLE AS $fn$
    SELECT string_to_array(
        coalesce(current_setting('rvbbit.accel_exclude_schemas', true), 'cubes'),
        ',')
$fn$;

-- accel_policy_effective + an excluded-schema override: tables in an excluded
-- schema read as strategy 'manual' regardless of any explicit policy, so the
-- accel_tick candidate query (WHERE strategy <> 'manual') skips them.
CREATE OR REPLACE VIEW rvbbit.accel_policy_effective AS
SELECT
    t.table_oid,
    c.oid::regclass::text                          AS table_name,
    CASE WHEN n.nspname = ANY (rvbbit._accel_excluded_schemas())
         THEN 'manual'
         ELSE coalesce(p.strategy, 'manual') END   AS strategy,
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
    p.updated_at
FROM rvbbit.tables t
JOIN pg_class c     ON c.oid = t.table_oid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN rvbbit.accel_policy p ON p.table_oid = t.table_oid;

-- refresh_all_cubes with a pacing sleep between cubes (default 0.5s). The sleep
-- runs AFTER the per-cube COMMIT, so it holds no locks while it waits.
CREATE OR REPLACE PROCEDURE rvbbit.refresh_all_cubes(
    p_category      text    DEFAULT NULL,   -- NULL = every category (incl. uncategorized)
    p_subcategory   text    DEFAULT NULL,
    p_sleep_seconds numeric DEFAULT 0.5)    -- pacing pause between cubes; 0 = no pause
LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT name
          FROM rvbbit.cube_catalog
         WHERE (p_category    IS NULL OR category    = p_category)
           AND (p_subcategory IS NULL OR subcategory = p_subcategory)
         ORDER BY name
    LOOP
        BEGIN
            PERFORM rvbbit.refresh_cube(rec.name);
        EXCEPTION WHEN others THEN
            UPDATE rvbbit.cube_control
               SET last_error = SQLERRM, updated_at = now()
             WHERE cube_name = rec.name;
        END;
        COMMIT;   -- release the cube's locks before pausing
        IF coalesce(p_sleep_seconds, 0) > 0 THEN
            PERFORM pg_sleep(p_sleep_seconds);
        END IF;
    END LOOP;
END $fn$;
