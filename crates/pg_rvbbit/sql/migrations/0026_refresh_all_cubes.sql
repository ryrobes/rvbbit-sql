-- 0026_refresh_all_cubes
--
-- Bulk "refresh EVERY cube now" — the cube analog of materialize_all_metrics.
-- refresh_cube(name) already does the full chain per cube (snapshot_load:
-- TRUNCATE → INSERT the cube's SELECT → rvbbit.compact(), which rebuilds the
-- parquet + vortex acceleration files); this just loops it over all cubes,
-- optionally scoped to a category.
--
-- A PROCEDURE (not a function) so it COMMITs after each cube: refresh is heavy
-- (a full reload + compaction per cube), so partial progress survives, the
-- per-table generation/advisory lock is released between cubes, and the rebuilt
-- files become visible as it goes. Cron-ready:
--   SELECT cron.schedule('rvbbit_refresh_cubes', '0 */2 * * *',
--                        $$CALL rvbbit.refresh_all_cubes()$$);
-- or scoped:  CALL rvbbit.refresh_all_cubes(p_category => 'pack_salesforce');
--
-- Per-cube results land in rvbbit.cube_control (refreshed_at / last_rows /
-- last_error), which refresh_cube maintains — inspect it (or rvbbit.cube_health)
-- after a run. A failing cube is recorded and skipped, never aborting the batch.

CREATE OR REPLACE PROCEDURE rvbbit.refresh_all_cubes(
    p_category    text DEFAULT NULL,   -- NULL = every category (incl. uncategorized)
    p_subcategory text DEFAULT NULL)
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
            -- refresh_cube records last_error before re-raising, but that update
            -- is rolled back to this block's savepoint — so re-record it here so
            -- it survives the COMMIT below, then keep going with the next cube.
            UPDATE rvbbit.cube_control
               SET last_error = SQLERRM, updated_at = now()
             WHERE cube_name = rec.name;
        END;
        COMMIT;
    END LOOP;
END $fn$;
