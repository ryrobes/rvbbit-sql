-- 0233: Calliope Workflows -- versioned, high-level, headless Instruments.
--
-- A workflow graph is intentionally small: trigger and governed context flow
-- into one agent goal, then into stage/inbox/artifact outputs.  It is not an
-- arbitrary SQL/JavaScript DAG.  Hermes chooses the concrete tools at run time;
-- the database freezes human-approved intent, schedule state, and every run.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflows (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    slug text NOT NULL,
    visibility text NOT NULL DEFAULT 'private',
    latest_version integer NOT NULL DEFAULT 1,
    published_version integer,
    archived boolean NOT NULL DEFAULT false,
    schedule_enabled boolean NOT NULL DEFAULT false,
    scheduled_version integer,
    hermes_job_id text,
    schedule_state text,
    schedule_next_run_at timestamptz,
    schedule_last_run_at timestamptz,
    schedule_last_status text,
    schedule_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    scheduled_at timestamptz,
    CONSTRAINT calliope_workflows_visibility_check
        CHECK (visibility IN ('private','company')),
    CONSTRAINT calliope_workflows_version_check
        CHECK (latest_version >= 1 AND (
            published_version IS NULL OR
            (published_version >= 1 AND published_version <= latest_version)
        ) AND (
            scheduled_version IS NULL OR
            (scheduled_version >= 1 AND scheduled_version <= latest_version)
        )),
    CONSTRAINT calliope_workflows_schedule_state_check
        CHECK (schedule_state IS NULL OR schedule_state IN ('scheduled','paused','error','completed')),
    CONSTRAINT calliope_workflows_owner_slug_key UNIQUE (owner_email,slug)
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflow_versions (
    id uuid PRIMARY KEY,
    workflow_id uuid NOT NULL REFERENCES rvbbit.calliope_workflows(id) ON DELETE CASCADE,
    version integer NOT NULL,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    goal text NOT NULL,
    graph jsonb NOT NULL,
    revision_notes text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_workflow_versions_version_check CHECK (version >= 1),
    CONSTRAINT calliope_workflow_versions_graph_check
        CHECK (jsonb_typeof(graph) = 'object'),
    CONSTRAINT calliope_workflow_versions_workflow_version_key
        UNIQUE (workflow_id,version)
);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflow_runs (
    id uuid PRIMARY KEY,
    workflow_id uuid NOT NULL REFERENCES rvbbit.calliope_workflows(id) ON DELETE CASCADE,
    workflow_version integer NOT NULL,
    owner_email text NOT NULL,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    seed_turn_id uuid REFERENCES rvbbit.calliope_turns(id) ON DELETE SET NULL,
    trigger_kind text NOT NULL,
    status text NOT NULL DEFAULT 'running',
    hermes_job_id text,
    hermes_session_id text,
    result_summary text NOT NULL DEFAULT '',
    result_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    cost_receipt_id uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT calliope_workflow_runs_trigger_check
        CHECK (trigger_kind IN ('manual','scheduled')),
    CONSTRAINT calliope_workflow_runs_status_check
        CHECK (status IN ('running','complete','blocked','failed')),
    CONSTRAINT calliope_workflow_runs_workflow_version_fkey
        FOREIGN KEY (workflow_id,workflow_version)
        REFERENCES rvbbit.calliope_workflow_versions(workflow_id,version)
);

CREATE INDEX IF NOT EXISTS calliope_workflows_owner_updated_idx
    ON rvbbit.calliope_workflows (owner_email,archived,updated_at DESC);
CREATE INDEX IF NOT EXISTS calliope_workflows_company_idx
    ON rvbbit.calliope_workflows (updated_at DESC)
    WHERE visibility='company' AND published_version IS NOT NULL AND NOT archived;
CREATE UNIQUE INDEX IF NOT EXISTS calliope_workflows_hermes_job_idx
    ON rvbbit.calliope_workflows (hermes_job_id)
    WHERE hermes_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_workflow_versions_workflow_idx
    ON rvbbit.calliope_workflow_versions (workflow_id,version DESC);
CREATE INDEX IF NOT EXISTS calliope_workflow_runs_owner_started_idx
    ON rvbbit.calliope_workflow_runs (owner_email,started_at DESC);
CREATE INDEX IF NOT EXISTS calliope_workflow_runs_workflow_started_idx
    ON rvbbit.calliope_workflow_runs (workflow_id,started_at DESC);
CREATE INDEX IF NOT EXISTS calliope_workflow_runs_cost_pending_idx
    ON rvbbit.calliope_workflow_runs (started_at)
    WHERE trigger_kind='scheduled' AND status <> 'running' AND cost_receipt_id IS NULL;

COMMENT ON TABLE rvbbit.calliope_workflows IS
    'Permission, publication, and Hermes schedule envelope for high-level agent Workflows.';
COMMENT ON TABLE rvbbit.calliope_workflow_versions IS
    'Immutable trigger/context/agent/output graph revisions; concrete tool choice remains dynamic.';
COMMENT ON TABLE rvbbit.calliope_workflow_runs IS
    'Fresh Calliope notebook and result lineage for each manual or scheduled Workflow run.';
