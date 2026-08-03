-- 0234: Durable, bounded Workflow execution diagnostics.
--
-- These are user-visible lifecycle steps (tool started/completed/failed and
-- concise agent-reported scheduled steps), never provider chain-of-thought.

ALTER TABLE rvbbit.calliope_workflow_runs
    ADD COLUMN IF NOT EXISTS steps jsonb NOT NULL DEFAULT '[]'::jsonb;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'calliope_workflow_runs_steps_check'
          AND conrelid = 'rvbbit.calliope_workflow_runs'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_workflow_runs
            ADD CONSTRAINT calliope_workflow_runs_steps_check
            CHECK (jsonb_typeof(steps) = 'array');
    END IF;
END
$do$;

COMMENT ON COLUMN rvbbit.calliope_workflow_runs.steps IS
    'Bounded user-visible execution lifecycle; excludes hidden model reasoning and raw tool payloads.';
