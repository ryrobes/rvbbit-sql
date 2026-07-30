-- 0222: Calliope — private session index + immutable visual surface ledger
--
-- Hermes owns conversation and shared company memory. These tables own only
-- the warehouse Hub's user-facing session index and the tangible history that
-- links tool results back to the turns that produced them. Published artifacts
-- remain in dashboards/dashboard_versions and are shared in the pilot.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_sessions (
    id                uuid PRIMARY KEY,
    owner_email       text NOT NULL,
    hermes_session_id text UNIQUE NOT NULL,
    title             text NOT NULL,
    archived          boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS calliope_sessions_owner_updated_idx
    ON rvbbit.calliope_sessions (owner_email, archived, updated_at DESC);

COMMENT ON TABLE rvbbit.calliope_sessions IS
    'Calliope Hub sessions. owner_email is the signed browser identity; Hermes still uses one shared/default company profile and memory.';

CREATE TABLE IF NOT EXISTS rvbbit.calliope_turns (
    id                   uuid PRIMARY KEY,
    session_id           uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    ordinal              integer NOT NULL,
    user_message         text NOT NULL,
    assistant_message    text,
    attachments          jsonb NOT NULL DEFAULT '[]'::jsonb,
    selected_surface_id  uuid,
    hermes_message_id    text,
    status               text NOT NULL DEFAULT 'running',
    error                text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    completed_at         timestamptz,
    UNIQUE (session_id, ordinal)
);

CREATE INDEX IF NOT EXISTS calliope_turns_session_created_idx
    ON rvbbit.calliope_turns (session_id, created_at);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_surfaces (
    id                  uuid PRIMARY KEY,
    session_id          uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    turn_id             uuid NOT NULL REFERENCES rvbbit.calliope_turns(id) ON DELETE CASCADE,
    ordinal             integer NOT NULL,
    kind                text NOT NULL,
    title               text NOT NULL,
    tool_name           text NOT NULL,
    tool_call_id        text NOT NULL,
    lineage_key         text NOT NULL,
    parent_surface_id   uuid REFERENCES rvbbit.calliope_surfaces(id) ON DELETE SET NULL,
    artifact_slug       text,
    artifact_version    integer,
    payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
    source              jsonb NOT NULL DEFAULT '{}'::jsonb,
    presentation        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, tool_call_id, lineage_key)
);

CREATE INDEX IF NOT EXISTS calliope_surfaces_session_created_idx
    ON rvbbit.calliope_surfaces (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_surfaces_lineage_idx
    ON rvbbit.calliope_surfaces (session_id, lineage_key, created_at DESC);

COMMENT ON TABLE rvbbit.calliope_surfaces IS
    'Append-only Calliope surface events projected from Hermes tool results. Updates link backward through parent_surface_id instead of replacing history.';

CREATE TABLE IF NOT EXISTS rvbbit.calliope_attachments (
    id            uuid PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    turn_id       uuid NOT NULL REFERENCES rvbbit.calliope_turns(id) ON DELETE CASCADE,
    original_name text,
    mime_type     text NOT NULL,
    storage_path  text NOT NULL,
    bytes         integer NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS calliope_attachments_session_idx
    ON rvbbit.calliope_attachments (session_id, created_at);

