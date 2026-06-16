-- 0039_route_overlay
--
-- The routing "overlay": a single, always-on table of tested shape -> engine pins that
-- layer on top of the deterministic base rules. This replaces the named-profile machinery
-- (route_profiles / route_profile_entries / route_profile_points / route_observations) for
-- the common case: you don't assemble + activate a profile, you just benchmark a query and
-- the winning engine is pinned for that shape.
--
-- Precedence (see router.rs choose_route): regex/correctness hard-rule -> route_force_candidate
-- (GUC) -> route_overlay pin -> base rules. A pin only sets the PREFERRED engine; it still
-- passes candidate_availability + the correctness gates, so a stale/wrong pin can never
-- change results, only (briefly) which engine is chosen.
--
-- Only DIVERGENT pins are stored (engine != base_engine, margin >= threshold) — so the table
-- is "the set of shapes where the base rules are measurably wrong," and stays small. A
-- re-benchmark that finds the pin no longer beats base deletes the row (self-pruning).

CREATE TABLE IF NOT EXISTS rvbbit.route_overlay (
    shape_key     text PRIMARY KEY,
    shape_family  text NOT NULL,
    engine        text NOT NULL,
    base_engine   text NOT NULL,
    margin_pct    double precision NOT NULL,
    sample_ms     jsonb,
    n_samples     int  NOT NULL DEFAULT 1,
    source        text NOT NULL DEFAULT 'tested'
                  CHECK (source IN ('tested', 'manual', 'auto')),
    enabled       boolean NOT NULL DEFAULT true,
    tested_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz,
    hit_count     bigint NOT NULL DEFAULT 0,
    CHECK (engine IN ('duck_vector', 'duck_hive', 'duck_vortex', 'datafusion_mem',
                      'datafusion_vector', 'datafusion_hive', 'datafusion_vortex',
                      'rvbbit_native', 'rvbbit_native_vortex', 'pg_rowstore'))
);

CREATE INDEX IF NOT EXISTS route_overlay_family_idx ON rvbbit.route_overlay (shape_family);
