-- 0042_route_optimize_runs
--
-- History of auto-optimizer passes (rvbbit.route_optimize_auto), so the Lens can show what the
-- nightly job did: how many hot shapes it benchmarked, how many produced a divergent pin, how
-- long it took, and the per-shape outcome. One row per run.

CREATE TABLE IF NOT EXISTS rvbbit.route_optimize_runs (
    run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    trigger       text NOT NULL DEFAULT 'auto'    -- auto | manual
                  CHECK (trigger IN ('auto', 'manual')),
    shapes_tested int NOT NULL DEFAULT 0,
    pinned        int NOT NULL DEFAULT 0,
    errors        int NOT NULL DEFAULT 0,
    elapsed_sec   int,
    detail        jsonb     -- [{shape_key, pinned, winner, margin_pct}, ...]
);

CREATE INDEX IF NOT EXISTS route_optimize_runs_started_idx ON rvbbit.route_optimize_runs (started_at DESC);
