-- 0229: Calliope Instruments
--
-- An Instrument is a small, declarative UI skill co-designed in Calliope: a
-- bounded form plus a prompt contract.  Agent-authored revisions stay private
-- drafts until the human owner explicitly publishes them.  Company visibility
-- governs discovery/use of the published revision; every run still executes in
-- a fresh Calliope session under the runner's normal governed warehouse access.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_instruments (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    slug text NOT NULL,
    visibility text NOT NULL DEFAULT 'private',
    latest_version integer NOT NULL DEFAULT 1,
    published_version integer,
    archived boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    CONSTRAINT calliope_instruments_visibility_check
        CHECK (visibility IN ('private','company')),
    CONSTRAINT calliope_instruments_version_check
        CHECK (latest_version >= 1 AND (
            published_version IS NULL OR
            (published_version >= 1 AND published_version <= latest_version)
        )),
    CONSTRAINT calliope_instruments_owner_slug_key UNIQUE (owner_email, slug)
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_instrument_versions (
    id uuid PRIMARY KEY,
    instrument_id uuid NOT NULL REFERENCES rvbbit.calliope_instruments(id) ON DELETE CASCADE,
    version integer NOT NULL,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    prompt_template text NOT NULL,
    fields jsonb NOT NULL DEFAULT '[]'::jsonb,
    revision_notes text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_instrument_versions_version_check CHECK (version >= 1),
    CONSTRAINT calliope_instrument_versions_instrument_version_key
        UNIQUE (instrument_id, version)
);

CREATE INDEX IF NOT EXISTS calliope_instruments_owner_updated_idx
    ON rvbbit.calliope_instruments (owner_email, archived, updated_at DESC);
CREATE INDEX IF NOT EXISTS calliope_instruments_company_idx
    ON rvbbit.calliope_instruments (updated_at DESC)
    WHERE visibility='company' AND published_version IS NOT NULL AND NOT archived;
CREATE INDEX IF NOT EXISTS calliope_instrument_versions_instrument_idx
    ON rvbbit.calliope_instrument_versions (instrument_id, version DESC);

COMMENT ON TABLE rvbbit.calliope_instruments IS
    'Permission envelope and publication pointer for declarative Calliope UI skills.';
COMMENT ON TABLE rvbbit.calliope_instrument_versions IS
    'Immutable form and prompt-contract revisions for Calliope Instruments.';
COMMENT ON COLUMN rvbbit.calliope_instruments.published_version IS
    'Human-approved revision visible to permitted runners; agent drafts only advance latest_version.';
