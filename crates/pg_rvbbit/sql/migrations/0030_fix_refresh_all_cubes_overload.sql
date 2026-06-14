-- 0030_fix_refresh_all_cubes_overload
--
-- 0029 added refresh_all_cubes(text, text, numeric) as an OVERLOAD instead of
-- replacing the 0026 refresh_all_cubes(text, text) — so a 0-arg call
-- `CALL rvbbit.refresh_all_cubes()` (what the cron preset uses) became ambiguous
-- ("procedure is not unique"). Drop the stale 2-arg version; the 3-arg one (with
-- the pacing sleep, all defaults) then resolves uniquely.
DROP PROCEDURE IF EXISTS rvbbit.refresh_all_cubes(text, text);
