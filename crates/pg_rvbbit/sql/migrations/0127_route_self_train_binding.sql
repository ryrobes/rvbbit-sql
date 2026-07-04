-- 0127_route_self_train_binding
--
-- SQL binding for rvbbit.route_self_train() — the one-call self-improving loop
-- (replay hot logged shapes across all engines -> log route_observations -> refit
-- route_model). Like 0126, bind the new #[pg_extern] to its pgrx wrapper so
-- existing installs get it via rvbbit.migrate() (fresh installs get the
-- pgrx-generated SQL). Intended for a nightly pg_cron job.

CREATE OR REPLACE FUNCTION rvbbit.route_self_train(
    top_k integer DEFAULT 20,
    max_seconds integer DEFAULT 600,
    samples integer DEFAULT 3,
    min_samples integer DEFAULT 20
) RETURNS jsonb
LANGUAGE c
AS '$libdir/pg_rvbbit', 'route_self_train_wrapper';

COMMENT ON FUNCTION rvbbit.route_self_train(integer, integer, integer, integer) IS
    'Nightly self-training loop: rvbbit.route_optimize_auto() (replay the top_k hottest '
    'real logged query shapes across every eligible engine, logging timings to '
    'rvbbit.route_observations) then rvbbit.train_route_model() (refit route_model). '
    'Read-only replays, budget-bounded by top_k/max_seconds.';
