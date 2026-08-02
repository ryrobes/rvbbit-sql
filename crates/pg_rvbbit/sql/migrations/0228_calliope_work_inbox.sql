-- 0228: Calliope Work Inbox
--
-- Hermes remains the agent/scheduler/goal runtime.  This table is the humane
-- handoff surface: a scheduled job, persistent goal, proactive suggestion, or
-- blocked turn can publish a compact user-owned item without copying an agent
-- transcript into the warehouse UI.  Semantic-watch transitions remain in
-- calliope_watch_events and are projected into the same Inbox at read time.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_work_items (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    kind text NOT NULL,
    source text NOT NULL DEFAULT 'hermes',
    source_ref text,
    dedupe_key text,
    title text NOT NULL,
    summary text NOT NULL DEFAULT '',
    urgency text NOT NULL DEFAULT 'normal',
    state text NOT NULL DEFAULT 'unread',
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    action_prompt text NOT NULL DEFAULT '',
    due_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    seen_at timestamptz,
    resolved_at timestamptz,
    CONSTRAINT calliope_work_items_kind_check
        CHECK (kind IN ('suggestion','scheduled','goal','blocked','result')),
    CONSTRAINT calliope_work_items_urgency_check
        CHECK (urgency IN ('low','normal','high','critical')),
    CONSTRAINT calliope_work_items_state_check
        CHECK (state IN ('unread','seen','done','dismissed')),
    CONSTRAINT calliope_work_items_owner_source_dedupe_key
        UNIQUE (owner_email, source, dedupe_key)
);

CREATE INDEX IF NOT EXISTS calliope_work_items_owner_state_idx
    ON rvbbit.calliope_work_items (owner_email, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS calliope_work_items_session_idx
    ON rvbbit.calliope_work_items (session_id, updated_at DESC)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_work_items_due_idx
    ON rvbbit.calliope_work_items (due_at)
    WHERE state IN ('unread','seen') AND due_at IS NOT NULL;

COMMENT ON TABLE rvbbit.calliope_work_items IS
    'Private action-ready handoffs from Hermes cron, goals, suggestions, and blocked Calliope work. Watch transitions join this surface at read time.';
COMMENT ON COLUMN rvbbit.calliope_work_items.session_id IS
    'Originating Calliope session. Its unguessable UUID acts as the scoped handoff capability for the Hermes MCP publisher.';
COMMENT ON COLUMN rvbbit.calliope_work_items.context IS
    'Bounded inert context and evidence handles; never an executable agent transcript.';
