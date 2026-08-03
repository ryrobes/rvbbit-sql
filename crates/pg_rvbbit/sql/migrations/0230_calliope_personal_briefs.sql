-- 0230: Calliope Personal Briefs
--
-- A Brief is a private, deterministic orientation surface: the signed-in
-- person's identity + a bounded time window resolve source-backed observations
-- before an agent is asked to interpret anything.  The actual snapshot remains
-- an immutable calliope_surfaces row; these tables only provide a stable daily
-- session pointer and durable user corrections.

ALTER TABLE rvbbit.brain_doc_providers
    ADD COLUMN IF NOT EXISTS observation_map jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN rvbbit.brain_doc_providers.observation_map IS
    'Optional JSON-path map for projecting source props into Brief fields such as status, due_at, starts_at, URL, assignees, and participants.';

-- Adding a ninth defaulted argument requires dropping the eight-argument form
-- first or shorter calls become ambiguous.  Existing provider definitions keep
-- working; new definitions can describe their temporal/person projection.
DROP FUNCTION IF EXISTS rvbbit.brain_define_provider(
    text, text, text, text, text, text, jsonb, text
);
CREATE OR REPLACE FUNCTION rvbbit.brain_define_provider(
    p_provider text, p_label text, p_list_sql text,
    p_item_sql text DEFAULT NULL, p_icon text DEFAULT NULL,
    p_description text DEFAULT NULL,
    p_edge_map jsonb DEFAULT '[]'::jsonb,
    p_doc_type text DEFAULT 'document',
    p_observation_map jsonb DEFAULT '{}'::jsonb
) RETURNS text LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.brain_doc_providers
        (provider, label, list_sql, item_sql, icon, description, edge_map,
         doc_type, observation_map)
    VALUES
        (p_provider, p_label, p_list_sql, nullif(btrim(p_item_sql),''), p_icon,
         p_description, coalesce(p_edge_map, '[]'::jsonb),
         coalesce(nullif(btrim(p_doc_type),''), 'document'),
         coalesce(p_observation_map, '{}'::jsonb))
    ON CONFLICT (provider) DO UPDATE SET
        label = excluded.label,
        list_sql = excluded.list_sql,
        item_sql = excluded.item_sql,
        icon = excluded.icon,
        description = excluded.description,
        edge_map = excluded.edge_map,
        doc_type = excluded.doc_type,
        observation_map = excluded.observation_map,
        updated_at = now()
    RETURNING provider;
$fn$;

-- The built-in Linear provider already retains the complete issue JSON in
-- props.  Teach the generic Brief resolver how to read it without coupling the
-- Calliope service to Linear itself.
UPDATE rvbbit.brain_doc_providers
SET observation_map = jsonb_build_object(
    'status', jsonb_build_array('$.state.name', '$.status.name', '$.status'),
    'due_at', jsonb_build_array('$.dueDate', '$.due_at', '$.dueAt'),
    'url', jsonb_build_array('$.url', '$.webUrl'),
    'assignee_emails', jsonb_build_array('$.assignee.email'),
    'assignee_names', jsonb_build_array('$.assignee.name'),
    'participants', jsonb_build_array('$.subscribers[*].email')
)
WHERE provider = 'linear-issues'
  AND observation_map = '{}'::jsonb;

-- Fireflies' query provider retains its actual meeting API shape.  Attendees
-- are objects, while participants is a convenience array of email strings;
-- both are useful because older synchronized rows may contain only one form.
UPDATE rvbbit.brain_doc_providers
SET observation_map = jsonb_build_object(
    'starts_at', jsonb_build_array('$.dateString'),
    'url', jsonb_build_array('$.meetingLink'),
    'participants', jsonb_build_array(
        '$.meetingAttendees[*].email',
        '$.meetingAttendees[*].displayName',
        '$.participants[*]',
        '$.organizerEmail'
    ),
    'authors', jsonb_build_array('$.organizerEmail')
)
WHERE provider = 'fireflies-meetings'
  AND observation_map = '{}'::jsonb;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_briefs (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    brief_date date NOT NULL,
    timezone text NOT NULL DEFAULT 'UTC',
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    latest_surface_id uuid REFERENCES rvbbit.calliope_surfaces(id) ON DELETE SET NULL,
    item_count integer NOT NULL DEFAULT 0,
    source_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    refreshed_at timestamptz,
    CONSTRAINT calliope_briefs_owner_date_key UNIQUE (owner_email, brief_date),
    CONSTRAINT calliope_briefs_window_check CHECK (window_end > window_start),
    CONSTRAINT calliope_briefs_item_count_check CHECK (item_count >= 0),
    CONSTRAINT calliope_briefs_source_count_check CHECK (source_count >= 0)
);
CREATE INDEX IF NOT EXISTS calliope_briefs_owner_refreshed_idx
    ON rvbbit.calliope_briefs (owner_email, refreshed_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS calliope_briefs_session_idx
    ON rvbbit.calliope_briefs (session_id);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_brief_feedback (
    owner_email text NOT NULL,
    observation_key text NOT NULL,
    source text NOT NULL DEFAULT '',
    verdict text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_email, observation_key),
    CONSTRAINT calliope_brief_feedback_verdict_check
        CHECK (verdict IN ('relevant','not_mine'))
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_identity_aliases (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    source text NOT NULL DEFAULT '*',
    alias_kind text NOT NULL DEFAULT 'name',
    alias_value text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_identity_alias_kind_check
        CHECK (alias_kind IN ('email','name','external_id'))
);
CREATE UNIQUE INDEX IF NOT EXISTS calliope_identity_aliases_owner_source_value_idx
    ON rvbbit.calliope_identity_aliases
       (lower(owner_email), lower(source), lower(alias_value));

COMMENT ON TABLE rvbbit.calliope_briefs IS
    'Private daily pointers to immutable Personal Brief evidence surfaces.';
COMMENT ON TABLE rvbbit.calliope_brief_feedback IS
    'Per-user corrections to Brief relevance; never a mutation of source evidence.';
COMMENT ON TABLE rvbbit.calliope_identity_aliases IS
    'User-confirmed source identities used to resolve structured observations to OAuth email.';
