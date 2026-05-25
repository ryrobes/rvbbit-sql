-- pg_rvbbit 0.22.0 -> 0.23.0
-- Native adaptive query routing control plane.

CREATE TABLE IF NOT EXISTS rvbbit.route_profiles (
    name          text PRIMARY KEY,
    active        boolean NOT NULL DEFAULT false,
    profile       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.route_observations (
    id            bigserial PRIMARY KEY,
    observed_at   timestamptz NOT NULL DEFAULT now(),
    source        text NOT NULL DEFAULT 'manual',
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    features      jsonb NOT NULL,
    candidate     text NOT NULL,
    elapsed_ms    double precision NOT NULL,
    status        text NOT NULL DEFAULT 'ok',
    CHECK (candidate IN ('duck_vector', 'rvbbit_native', 'pg_rowstore')),
    CHECK (elapsed_ms >= 0)
);

CREATE INDEX IF NOT EXISTS route_observations_shape_idx
    ON rvbbit.route_observations (shape_key, candidate, observed_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS route_profiles_one_active_idx
    ON rvbbit.route_profiles ((active))
    WHERE active;

CREATE OR REPLACE FUNCTION rvbbit.route_profiles_touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS route_profiles_touch_updated_at ON rvbbit.route_profiles;
CREATE TRIGGER route_profiles_touch_updated_at
    BEFORE UPDATE ON rvbbit.route_profiles
    FOR EACH ROW EXECUTE FUNCTION rvbbit.route_profiles_touch_updated_at();

CREATE FUNCTION rvbbit.route_explain(
    query text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_explain_wrapper';

CREATE FUNCTION rvbbit.route_features(
    query text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_features_wrapper';

CREATE FUNCTION rvbbit.route_record_observation(
    query text,
    candidate text,
    elapsed_ms double precision,
    status text,
    source text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_record_observation_wrapper';

CREATE FUNCTION rvbbit.route_set_profile(
    profile_name text,
    profile jsonb,
    active boolean
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_set_profile_wrapper';

CREATE FUNCTION rvbbit.route_train(
    profile_name text,
    min_observations bigint,
    min_gain_pct double precision
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_train_wrapper';
