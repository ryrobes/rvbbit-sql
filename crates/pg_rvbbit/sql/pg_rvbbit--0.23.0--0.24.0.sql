-- pg_rvbbit 0.23.0 -> 0.24.0
-- Adaptive query routing source-of-truth and normalized profile storage.

ALTER TABLE rvbbit.route_observations
    ADD COLUMN IF NOT EXISTS shape_family text NOT NULL DEFAULT '';

UPDATE rvbbit.route_observations
SET shape_family = regexp_replace(
        regexp_replace(shape_key, '(^|\|)table_rows=[^|]*', '', 'g'),
        '^\|', ''
    )
WHERE shape_family = '';

CREATE INDEX IF NOT EXISTS route_observations_family_idx
    ON rvbbit.route_observations (shape_family, candidate, observed_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.route_profile_entries (
    profile_name  text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    shape_key     text NOT NULL,
    choice        text NOT NULL,
    confidence    double precision NOT NULL DEFAULT 0,
    reason        text NOT NULL DEFAULT '',
    observations  bigint NOT NULL DEFAULT 0,
    native_ms     double precision,
    duck_ms       double precision,
    pg_ms         double precision,
    entry         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_name, shape_key),
    CHECK (choice IN ('duck_vector', 'rvbbit_native', 'pg_rowstore')),
    CHECK (confidence >= 0)
);

CREATE INDEX IF NOT EXISTS route_profile_entries_choice_idx
    ON rvbbit.route_profile_entries (choice, confidence DESC);

CREATE TABLE IF NOT EXISTS rvbbit.route_profile_points (
    id            bigserial PRIMARY KEY,
    profile_name  text NOT NULL REFERENCES rvbbit.route_profiles(name) ON DELETE CASCADE,
    shape_family  text NOT NULL,
    table_rows    bigint NOT NULL,
    native_ms     double precision NOT NULL,
    duck_ms       double precision NOT NULL,
    point         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (table_rows >= 0),
    CHECK (native_ms > 0),
    CHECK (duck_ms > 0)
);

CREATE INDEX IF NOT EXISTS route_profile_points_family_idx
    ON rvbbit.route_profile_points (profile_name, shape_family, table_rows);

DROP TRIGGER IF EXISTS route_profile_entries_touch_updated_at ON rvbbit.route_profile_entries;
CREATE TRIGGER route_profile_entries_touch_updated_at
    BEFORE UPDATE ON rvbbit.route_profile_entries
    FOR EACH ROW EXECUTE FUNCTION rvbbit.route_profiles_touch_updated_at();

INSERT INTO rvbbit.route_profile_entries
    (profile_name, shape_key, choice, confidence, reason, observations,
     native_ms, duck_ms, pg_ms, entry)
SELECT rp.name,
       e.key,
       CASE e.value->>'choice'
           WHEN 'native' THEN 'rvbbit_native'
           WHEN 'duck' THEN 'duck_vector'
           ELSE e.value->>'choice'
       END,
       coalesce(nullif(e.value->>'confidence', '')::double precision, 0),
       coalesce(e.value->>'reason', ''),
       coalesce(nullif(e.value->>'observations', '')::bigint, 0),
       coalesce(nullif(e.value->>'native_ms_median', '')::double precision, (
           SELECT nullif(m->>'median_ms', '')::double precision
           FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
           WHERE m->>'candidate' = 'rvbbit_native'
           LIMIT 1
       )),
       coalesce(nullif(e.value->>'duck_ms_median', '')::double precision, (
           SELECT nullif(m->>'median_ms', '')::double precision
           FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
           WHERE m->>'candidate' = 'duck_vector'
           LIMIT 1
       )),
       (
           SELECT nullif(m->>'median_ms', '')::double precision
           FROM jsonb_array_elements(coalesce(e.value->'candidate_medians', '[]'::jsonb)) AS m
           WHERE m->>'candidate' = 'pg_rowstore'
           LIMIT 1
       ),
       e.value
FROM rvbbit.route_profiles rp
CROSS JOIN LATERAL jsonb_each(coalesce(rp.profile->'entries', '{}'::jsonb)) AS e(key, value)
WHERE e.value ? 'choice'
  AND e.value->>'choice' IN ('duck', 'native', 'duck_vector', 'rvbbit_native', 'pg_rowstore')
ON CONFLICT (profile_name, shape_key) DO UPDATE SET
    choice = EXCLUDED.choice,
    confidence = EXCLUDED.confidence,
    reason = EXCLUDED.reason,
    observations = EXCLUDED.observations,
    native_ms = EXCLUDED.native_ms,
    duck_ms = EXCLUDED.duck_ms,
    pg_ms = EXCLUDED.pg_ms,
    entry = EXCLUDED.entry;

INSERT INTO rvbbit.route_profile_points
    (profile_name, shape_family, table_rows, native_ms, duck_ms, point)
SELECT rp.name,
       regexp_replace(
           regexp_replace(coalesce(obs->'features'->>'shape_key', ''),
                          '(^|\|)table_rows=[^|]*', '', 'g'),
           '^\|', ''
       ),
       coalesce(nullif(obs->'features'->>'table_rows', '')::bigint, 0),
       nullif(obs->>'native_ms', '')::double precision,
       nullif(obs->>'duck_ms', '')::double precision,
       obs
FROM rvbbit.route_profiles rp
CROSS JOIN LATERAL jsonb_array_elements(coalesce(rp.profile->'observations', '[]'::jsonb)) AS obs
WHERE obs ? 'features'
  AND obs ? 'native_ms'
  AND obs ? 'duck_ms'
  AND coalesce(nullif(obs->'features'->>'table_rows', '')::bigint, 0) > 0
  AND nullif(obs->>'native_ms', '')::double precision > 0
  AND nullif(obs->>'duck_ms', '')::double precision > 0;

UPDATE rvbbit.route_profiles
SET profile = profile
    - 'observations'
    || jsonb_build_object(
        'observation_count', coalesce(jsonb_array_length(profile->'observations'), 0),
        'observations_persisted', true
    )
WHERE profile ? 'observations';

CREATE FUNCTION rvbbit.route_explain_text(
    query text
) RETURNS text
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_explain_text_wrapper';
