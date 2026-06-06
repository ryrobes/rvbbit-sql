-- Phase 4 (native+vortex router candidate) schema deltas for EXISTING deployments.
-- Fresh installs get these from the extension_sql! block in router.rs; this idempotent
-- script applies them to a DB already created at an older extension version. Safe to
-- re-run. (Fold into the next versioned pg_rvbbit--X--Y.sql migration at release time.)

-- Widen the candidate/choice CHECK constraints to accept 'rvbbit_native_vortex'.
ALTER TABLE IF EXISTS rvbbit.route_observations
    DROP CONSTRAINT IF EXISTS route_observations_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_observations
    ADD CONSTRAINT route_observations_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_training_results
    DROP CONSTRAINT IF EXISTS route_training_results_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_training_results
    ADD CONSTRAINT route_training_results_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_decisions
    DROP CONSTRAINT IF EXISTS route_decisions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_decisions
    ADD CONSTRAINT route_decisions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_executions
    DROP CONSTRAINT IF EXISTS route_executions_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_executions
    ADD CONSTRAINT route_executions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem', 'datafusion_vector', 'datafusion_hive', 'datafusion_vortex', 'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'));

-- Per-candidate ms columns for the learned cost model.
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS native_vortex_ms double precision;
ALTER TABLE IF EXISTS rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS native_vortex_ms double precision;
