-- 0126_train_route_model_binding
--
-- SQL binding for the in-database ML-routing trainer. rvbbit.train_route_model()
-- is a #[pg_extern] in the new .so, but new C functions only reach a database via
-- a fresh CREATE EXTENSION — so bind it here (pointing at pgrx's stable
-- <fn>_wrapper symbol) so existing installs get it through rvbbit.migrate(),
-- exactly like migrate.sql (re)binds rvbbit.migrate(). Idempotent; CREATE OR
-- REPLACE matches the signature pgrx generates on fresh installs.

CREATE OR REPLACE FUNCTION rvbbit.train_route_model(
    min_samples integer DEFAULT 20,
    include_auto boolean DEFAULT true
) RETURNS jsonb
LANGUAGE c
AS '$libdir/pg_rvbbit', 'train_route_model_wrapper';

COMMENT ON FUNCTION rvbbit.train_route_model(integer, boolean) IS
    'Train the ML routing layer''s per-engine latency models from bench_history and '
    'write rvbbit.route_model. min_samples: min rows per engine; include_auto: also '
    'learn from auto ''rvbbit'' runs (biased) in addition to forced-engine sweeps.';
