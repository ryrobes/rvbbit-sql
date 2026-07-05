-- 0129_route_shape_samples_test_memory
--
-- Remember when each shape was last replayed by the auto-optimizer
-- (route_optimize_auto / route_self_train) and how it went. Without this the
-- optimizer re-benched every non-pinned shape on EVERY pass (a tested shape
-- whose base engine won leaves no overlay row, so it re-entered
-- route_optimization_candidates immediately) — pass time grew with history and
-- heavy shapes were replayed across all engines again and again.
--
-- Retest policy (enforced in route_optimize_auto): a shape is a candidate when
--   never tested, OR
--   (its cooldown elapsed — rvbbit.route_optimize_retest_hours, default 24h —
--    AND it has executed again since the last test).
-- So dormant shapes are never re-benched, and active shapes are revalidated at
-- most once per cooldown window.

ALTER TABLE rvbbit.route_shape_samples
    ADD COLUMN IF NOT EXISTS last_tested_at timestamptz;
ALTER TABLE rvbbit.route_shape_samples
    ADD COLUMN IF NOT EXISTS last_result text;

COMMENT ON COLUMN rvbbit.route_shape_samples.last_tested_at IS
    'When route_optimize_auto last replayed this shape across engines.';
COMMENT ON COLUMN rvbbit.route_shape_samples.last_result IS
    'Outcome of the last replay: pinned:<engine>, base_ok, or error:<summary>.';
