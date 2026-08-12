-- 0280: user-owned delegated work for Calliope.
--
-- A work order is the durable promise. Hermes is only its wake-up mechanism;
-- every attempt receives a separate Calliope run notebook and an optional
-- Work Inbox delivery. The intentionally broader assignee/trigger/executor
-- columns leave room for human todos, reminders, and Workflow-backed work
-- without making those future modes part of this first agent-schedule slice.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_work_orders (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    source_session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    title text NOT NULL,
    instruction text NOT NULL,
    assignee text NOT NULL DEFAULT 'calliope',
    trigger_kind text NOT NULL DEFAULT 'manual',
    schedule text NOT NULL DEFAULT '',
    schedule_display text NOT NULL DEFAULT '',
    timezone text NOT NULL DEFAULT 'UTC',
    execution_kind text NOT NULL DEFAULT 'agent',
    workflow_id uuid REFERENCES rvbbit.calliope_workflows(id) ON DELETE SET NULL,
    workflow_version integer,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    approval_policy text NOT NULL DEFAULT 'read_only',
    notification_policy text NOT NULL DEFAULT 'attention',
    overlap_policy text NOT NULL DEFAULT 'skip',
    definition_version integer NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'draft',
    hermes_job_id text,
    schedule_state text,
    schedule_next_run_at timestamptz,
    schedule_last_run_at timestamptz,
    schedule_last_status text,
    schedule_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT calliope_work_orders_assignee_check
        CHECK (assignee IN ('human','calliope')),
    CONSTRAINT calliope_work_orders_trigger_check
        CHECK (trigger_kind IN ('none','manual','once','recurring')),
    CONSTRAINT calliope_work_orders_execution_check
        CHECK (execution_kind IN ('notify','agent','workflow')),
    CONSTRAINT calliope_work_orders_context_check
        CHECK (jsonb_typeof(context)='object'),
    CONSTRAINT calliope_work_orders_approval_check
        CHECK (approval_policy IN ('read_only','propose_changes')),
    CONSTRAINT calliope_work_orders_notification_check
        CHECK (notification_policy IN ('always','attention','failure','never')),
    CONSTRAINT calliope_work_orders_overlap_check CHECK (overlap_policy='skip'),
    CONSTRAINT calliope_work_orders_version_check CHECK (definition_version >= 1),
    CONSTRAINT calliope_work_orders_status_check
        CHECK (status IN ('draft','active','paused','completed','cancelled','error')),
    CONSTRAINT calliope_work_orders_schedule_state_check
        CHECK (schedule_state IS NULL OR schedule_state IN ('scheduled','paused','error','completed')),
    CONSTRAINT calliope_work_orders_schedule_contract_check CHECK (
        (trigger_kind IN ('none','manual') AND schedule='') OR
        (trigger_kind IN ('once','recurring') AND length(schedule) > 0)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS calliope_work_orders_hermes_job_idx
    ON rvbbit.calliope_work_orders (hermes_job_id)
    WHERE hermes_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_work_orders_owner_status_idx
    ON rvbbit.calliope_work_orders (owner_email,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS calliope_work_orders_due_idx
    ON rvbbit.calliope_work_orders (schedule_next_run_at)
    WHERE status='active' AND schedule_next_run_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_work_order_runs (
    id uuid PRIMARY KEY,
    work_order_id uuid NOT NULL REFERENCES rvbbit.calliope_work_orders(id) ON DELETE CASCADE,
    definition_version integer NOT NULL,
    owner_email text NOT NULL,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    seed_turn_id uuid REFERENCES rvbbit.calliope_turns(id) ON DELETE SET NULL,
    trigger_kind text NOT NULL,
    execution_key text NOT NULL,
    scheduled_for timestamptz,
    status text NOT NULL DEFAULT 'running',
    hermes_job_id text,
    hermes_session_id text,
    instruction_snapshot text NOT NULL,
    context_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary text NOT NULL DEFAULT '',
    result_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    attention_required boolean NOT NULL DEFAULT true,
    changed boolean NOT NULL DEFAULT true,
    cost_receipt_id uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT calliope_work_order_runs_version_check CHECK (definition_version >= 1),
    CONSTRAINT calliope_work_order_runs_trigger_check
        CHECK (trigger_kind IN ('manual','scheduled')),
    CONSTRAINT calliope_work_order_runs_status_check
        CHECK (status IN ('running','complete','blocked','failed')),
    CONSTRAINT calliope_work_order_runs_context_check
        CHECK (jsonb_typeof(context_snapshot)='object'),
    CONSTRAINT calliope_work_order_runs_details_check
        CHECK (jsonb_typeof(result_details)='object'),
    CONSTRAINT calliope_work_order_runs_steps_check
        CHECK (jsonb_typeof(steps)='array'),
    CONSTRAINT calliope_work_order_runs_execution_key
        UNIQUE (work_order_id,execution_key)
);

CREATE INDEX IF NOT EXISTS calliope_work_order_runs_owner_started_idx
    ON rvbbit.calliope_work_order_runs (owner_email,started_at DESC);
CREATE INDEX IF NOT EXISTS calliope_work_order_runs_order_started_idx
    ON rvbbit.calliope_work_order_runs (work_order_id,started_at DESC);
CREATE INDEX IF NOT EXISTS calliope_work_order_runs_running_idx
    ON rvbbit.calliope_work_order_runs (work_order_id,started_at)
    WHERE status='running';

COMMENT ON TABLE rvbbit.calliope_work_orders IS
    'Private user-owned assignments; Hermes job ids are deployment bindings, not source of truth.';
COMMENT ON TABLE rvbbit.calliope_work_order_runs IS
    'One immutable-definition execution attempt and private Calliope notebook per assignment run.';
