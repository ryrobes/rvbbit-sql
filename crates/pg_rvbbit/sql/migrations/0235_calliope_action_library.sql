-- 0235: Calliope Action Library catalog and durable change receipts.
--
-- The service seeds curated outcome recipes into the catalog. Runtime cards
-- derived from capability_catalog use the same receipt table, so every typed
-- plan and apply has a stable, redacted audit record even before the future
-- organization-wide permissions model is introduced.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_action_catalog (
    id text PRIMARY KEY,
    version integer NOT NULL DEFAULT 1,
    category text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    executor text NOT NULL,
    risk text NOT NULL DEFAULT 'reversible',
    tags text[] NOT NULL DEFAULT ARRAY[]::text[],
    input_schema jsonb NOT NULL DEFAULT '{"fields":[]}'::jsonb,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    sort_order integer NOT NULL DEFAULT 100,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_action_catalog_version_check CHECK (version >= 1),
    CONSTRAINT calliope_action_catalog_input_check CHECK (jsonb_typeof(input_schema)='object'),
    CONSTRAINT calliope_action_catalog_config_check CHECK (jsonb_typeof(config)='object')
);

CREATE INDEX IF NOT EXISTS calliope_action_catalog_category_idx
    ON rvbbit.calliope_action_catalog (active, category, sort_order, title);
CREATE INDEX IF NOT EXISTS calliope_action_catalog_tags_idx
    ON rvbbit.calliope_action_catalog USING gin (tags);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_action_runs (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    session_id uuid REFERENCES rvbbit.calliope_sessions(id) ON DELETE SET NULL,
    action_id text NOT NULL,
    action_version integer NOT NULL DEFAULT 1,
    action_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'planned',
    input_values jsonb NOT NULL DEFAULT '{}'::jsonb,
    input_redacted jsonb NOT NULL DEFAULT '{}'::jsonb,
    plan jsonb NOT NULL DEFAULT '{}'::jsonb,
    steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    verification jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT calliope_action_runs_status_check
        CHECK (status IN ('planned','running','complete','failed')),
    CONSTRAINT calliope_action_runs_version_check CHECK (action_version >= 1),
    CONSTRAINT calliope_action_runs_snapshot_check CHECK (jsonb_typeof(action_snapshot)='object'),
    CONSTRAINT calliope_action_runs_values_check CHECK (jsonb_typeof(input_values)='object'),
    CONSTRAINT calliope_action_runs_redacted_check CHECK (jsonb_typeof(input_redacted)='object'),
    CONSTRAINT calliope_action_runs_plan_check CHECK (jsonb_typeof(plan)='object'),
    CONSTRAINT calliope_action_runs_steps_check CHECK (jsonb_typeof(steps)='array'),
    CONSTRAINT calliope_action_runs_result_check CHECK (jsonb_typeof(result)='object'),
    CONSTRAINT calliope_action_runs_verification_check CHECK (jsonb_typeof(verification)='object')
);

CREATE INDEX IF NOT EXISTS calliope_action_runs_owner_created_idx
    ON rvbbit.calliope_action_runs (owner_email, created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_action_runs_action_created_idx
    ON rvbbit.calliope_action_runs (action_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_action_runs_active_idx
    ON rvbbit.calliope_action_runs (status, created_at)
    WHERE status IN ('planned', 'running');

COMMENT ON TABLE rvbbit.calliope_action_catalog IS
    'Outcome-oriented Calliope recipes backed by typed executors or guided conversation handoffs.';
COMMENT ON TABLE rvbbit.calliope_action_runs IS
    'Redacted plan, execution steps, verification, and result receipts for trusted-organization Calliope changes.';
