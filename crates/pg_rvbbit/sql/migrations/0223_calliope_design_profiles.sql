-- 0223: Calliope Design Profiles
--
-- Design Profiles are company-visible authoring contracts for dashboards,
-- apps, charts, and decks. The profile record is mutable metadata; every
-- usable style document is an immutable version so old Calliope turns and
-- surfaces retain the exact creative direction that produced them.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_design_profiles (
    id              uuid PRIMARY KEY,
    owner_email     text NOT NULL,
    name            text NOT NULL,
    description     text NOT NULL DEFAULT '',
    current_version integer NOT NULL DEFAULT 1,
    archived        boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS calliope_design_profiles_owner_name_idx
    ON rvbbit.calliope_design_profiles (owner_email, lower(name))
    WHERE NOT archived;
CREATE INDEX IF NOT EXISTS calliope_design_profiles_updated_idx
    ON rvbbit.calliope_design_profiles (archived, updated_at DESC);

COMMENT ON TABLE rvbbit.calliope_design_profiles IS
    'Company-visible Calliope dashboard design profiles. owner_email controls editing; all authenticated pilot users may read and fork.';

CREATE TABLE IF NOT EXISTS rvbbit.calliope_design_profile_versions (
    id              uuid PRIMARY KEY,
    profile_id      uuid NOT NULL REFERENCES rvbbit.calliope_design_profiles(id) ON DELETE CASCADE,
    version         integer NOT NULL,
    markdown        text NOT NULL,
    tokens          jsonb NOT NULL DEFAULT '{}'::jsonb,
    compiled_prompt text NOT NULL,
    source_summary  text NOT NULL DEFAULT '',
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_id, version)
);

CREATE INDEX IF NOT EXISTS calliope_design_profile_versions_profile_idx
    ON rvbbit.calliope_design_profile_versions (profile_id, version DESC);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_design_profile_assets (
    id               uuid PRIMARY KEY,
    profile_version_id uuid NOT NULL REFERENCES rvbbit.calliope_design_profile_versions(id) ON DELETE CASCADE,
    ordinal          integer NOT NULL,
    source_kind      text NOT NULL,
    original_name    text,
    source_url       text,
    mime_type        text,
    storage_path     text,
    bytes            integer,
    sha256           text,
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_version_id, ordinal)
);

CREATE INDEX IF NOT EXISTS calliope_design_profile_assets_version_idx
    ON rvbbit.calliope_design_profile_assets (profile_version_id, ordinal);

ALTER TABLE rvbbit.calliope_sessions
    ADD COLUMN IF NOT EXISTS design_profile_version_id uuid
    REFERENCES rvbbit.calliope_design_profile_versions(id) ON DELETE SET NULL;
ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS design_profile_version_id uuid
    REFERENCES rvbbit.calliope_design_profile_versions(id) ON DELETE SET NULL;
ALTER TABLE rvbbit.calliope_surfaces
    ADD COLUMN IF NOT EXISTS design_profile_version_id uuid
    REFERENCES rvbbit.calliope_design_profile_versions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS calliope_turns_design_profile_idx
    ON rvbbit.calliope_turns (design_profile_version_id)
    WHERE design_profile_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_surfaces_design_profile_idx
    ON rvbbit.calliope_surfaces (design_profile_version_id)
    WHERE design_profile_version_id IS NOT NULL;
