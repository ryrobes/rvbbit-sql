-- 0125_route_model
--
-- ML routing layer (docs/ML_ROUTING_PLAN.md). Per-engine latency models produced
-- by scripts/train_route_model.py and consumed by the router's ml_route_decision
-- hook (gated on rvbbit.route_ml_enabled, default off). One row per engine; the
-- router loads all rows, memoized per backend. Untrained / absent = the ML layer
-- is a no-op, so this table is inert until a model is written and the GUC flipped.

CREATE TABLE IF NOT EXISTS rvbbit.route_model (
    engine         text PRIMARY KEY,          -- native | duck_vortex | gpu_gqe | ... (RouteCurveSample engine names)
    params         jsonb NOT NULL,            -- {base, feature_names, trees:[{nodes:[...]}]} — log-latency GBM
    feature_schema integer NOT NULL DEFAULT 1,
    n_samples      bigint,                     -- training rows for this engine (provenance)
    trained_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    notes          text
);

COMMENT ON TABLE rvbbit.route_model IS
    'Per-engine gradient-boosted latency models for the ML routing layer. Written by '
    'scripts/train_route_model.py; read by the router when rvbbit.route_ml_enabled is on. '
    'params = {base, feature_names, trees}; prediction is log-ms, smaller = faster.';
