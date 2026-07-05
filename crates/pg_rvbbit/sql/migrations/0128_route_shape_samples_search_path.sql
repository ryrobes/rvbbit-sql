-- 0128_route_shape_samples_search_path
--
-- Record the capturing session's search_path with each route shape sample so
-- the auto-optimizer (route_optimize_auto / route_self_train) can replay the
-- sample under it. Without this, samples captured from a session with a
-- non-default search_path (e.g. the TPC-DS bench schema `tpcds`) reference
-- unqualified table names that 42P01 when replayed from a default-path
-- session — and one such errant sample used to abort the whole training pass.
-- NULL / '' means "replay under the caller's current path" (pre-0128 rows).

ALTER TABLE rvbbit.route_shape_samples
    ADD COLUMN IF NOT EXISTS search_path text;

COMMENT ON COLUMN rvbbit.route_shape_samples.search_path IS
    'search_path of the session the sample was captured from; the auto-optimizer '
    'replays the SQL under it so unqualified table names resolve identically. '
    'NULL/empty = replay under the optimizer session''s own path.';
