-- 0041_route_shape_samples
--
-- One representative SQL per routed query shape, captured opportunistically by the route
-- decision logger (route_log.rs, best-effort post-commit upsert). The auto-optimizer
-- (route_optimize_auto) reads this so it has a concrete, runnable query to benchmark for each
-- hot shape — route_decisions/route_executions only store a shape_key + hash, not the text.
--
-- Stays small: PRIMARY KEY (shape_key) + ON CONFLICT DO NOTHING means one row per distinct
-- shape (the first instance seen). Captured text is bounded (the logger skips queries > 64KB).

CREATE TABLE IF NOT EXISTS rvbbit.route_shape_samples (
    shape_key     text PRIMARY KEY,
    shape_family  text NOT NULL,
    sql           text NOT NULL,
    captured_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS route_shape_samples_family_idx ON rvbbit.route_shape_samples (shape_family);
