-- 0256: Durable, observable scheduling for Calliope Dreaming.
--
-- pg_cron is only the clock.  It enqueues one lightweight sweep and returns;
-- Warehouse expands that sweep into a company job plus one owner-scoped job
-- per active user.  The queue is durable, restart-safe, and deliberately
-- separate from the private Dream/dossier payloads so operations telemetry can
-- be inspected without granting access to what anyone dreamed about.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    processing_paused boolean NOT NULL DEFAULT false,
    company_enabled boolean NOT NULL DEFAULT true,
    personal_enabled boolean NOT NULL DEFAULT true,
    active_window_days integer NOT NULL DEFAULT 30,
    min_chat_turns integer NOT NULL DEFAULT 2,
    min_tool_calls integer NOT NULL DEFAULT 3,
    max_personal_users integer NOT NULL DEFAULT 200,
    telemetry_retention_days integer NOT NULL DEFAULT 90,
    updated_by text NOT NULL DEFAULT 'calliope@system',
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_dream_settings_window_check
        CHECK (active_window_days BETWEEN 7 AND 90),
    CONSTRAINT calliope_dream_settings_chat_check
        CHECK (min_chat_turns BETWEEN 1 AND 20),
    CONSTRAINT calliope_dream_settings_calls_check
        CHECK (min_tool_calls BETWEEN 1 AND 50),
    CONSTRAINT calliope_dream_settings_users_check
        CHECK (max_personal_users BETWEEN 1 AND 500),
    CONSTRAINT calliope_dream_settings_retention_check
        CHECK (telemetry_retention_days BETWEEN 14 AND 730)
);

INSERT INTO rvbbit.calliope_dream_settings (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_sweeps (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date date NOT NULL DEFAULT current_date,
    source text NOT NULL DEFAULT 'cron',
    requested_by text NOT NULL DEFAULT 'calliope@system',
    status text NOT NULL DEFAULT 'pending',
    dedupe_key text UNIQUE,
    options jsonb NOT NULL DEFAULT '{}'::jsonb,
    active_user_count integer NOT NULL DEFAULT 0,
    planned_job_count integer NOT NULL DEFAULT 0,
    completed_job_count integer NOT NULL DEFAULT 0,
    skipped_job_count integer NOT NULL DEFAULT 0,
    failed_job_count integer NOT NULL DEFAULT 0,
    worker_id text,
    error text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT calliope_dream_sweeps_source_check
        CHECK (source IN ('cron','scheduler','api','manual','retry')),
    CONSTRAINT calliope_dream_sweeps_status_check
        CHECK (status IN ('pending','planning','running','complete','partial','failed','paused')),
    CONSTRAINT calliope_dream_sweeps_options_check
        CHECK (jsonb_typeof(options)='object'),
    CONSTRAINT calliope_dream_sweeps_counts_check CHECK (
        active_user_count >= 0 AND planned_job_count >= 0
        AND completed_job_count >= 0 AND skipped_job_count >= 0
        AND failed_job_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS calliope_dream_sweeps_status_requested_idx
    ON rvbbit.calliope_dream_sweeps (status,requested_at);
CREATE INDEX IF NOT EXISTS calliope_dream_sweeps_recent_idx
    ON rvbbit.calliope_dream_sweeps (requested_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sweep_id uuid NOT NULL REFERENCES rvbbit.calliope_dream_sweeps(id) ON DELETE CASCADE,
    scope_kind text NOT NULL,
    owner_email text,
    status text NOT NULL DEFAULT 'pending',
    outcome text,
    cycle_id uuid REFERENCES rvbbit.calliope_dream_cycles(id) ON DELETE SET NULL,
    attempt integer NOT NULL DEFAULT 0,
    worker_id text,
    error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    queued_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT calliope_dream_jobs_scope_check CHECK (
        (scope_kind='company' AND owner_email IS NULL)
        OR
        (scope_kind='personal' AND owner_email IS NOT NULL
         AND owner_email=lower(btrim(owner_email)) AND owner_email LIKE '%@%')
    ),
    CONSTRAINT calliope_dream_jobs_status_check
        CHECK (status IN ('pending','running','complete','skipped','failed')),
    CONSTRAINT calliope_dream_jobs_attempt_check CHECK (attempt >= 0),
    CONSTRAINT calliope_dream_jobs_metadata_check CHECK (jsonb_typeof(metadata)='object')
);

CREATE UNIQUE INDEX IF NOT EXISTS calliope_dream_jobs_sweep_scope_idx
    ON rvbbit.calliope_dream_jobs
       (sweep_id,scope_kind,coalesce(owner_email,''));
CREATE INDEX IF NOT EXISTS calliope_dream_jobs_queue_idx
    ON rvbbit.calliope_dream_jobs (status,queued_at);
CREATE INDEX IF NOT EXISTS calliope_dream_jobs_owner_recent_idx
    ON rvbbit.calliope_dream_jobs (owner_email,started_at DESC)
    WHERE scope_kind='personal';

CREATE OR REPLACE FUNCTION rvbbit.calliope_dream_enqueue(
    p_source text DEFAULT 'cron',
    p_requested_by text DEFAULT 'calliope@system',
    p_force boolean DEFAULT false
) RETURNS uuid
LANGUAGE plpgsql
SET search_path = pg_catalog, rvbbit
AS $fn$
DECLARE
    v_source text := lower(btrim(coalesce(p_source,'cron')));
    v_requested_by text := left(coalesce(nullif(btrim(p_requested_by),''),'calliope@system'),320);
    v_settings rvbbit.calliope_dream_settings%ROWTYPE;
    v_id uuid;
    v_key text;
BEGIN
    IF v_source NOT IN ('cron','scheduler','api','manual','retry') THEN
        RAISE EXCEPTION 'unsupported Dream sweep source: %', v_source;
    END IF;

    SELECT * INTO v_settings
      FROM rvbbit.calliope_dream_settings
     WHERE singleton;

    IF NOT FOUND THEN
        INSERT INTO rvbbit.calliope_dream_settings (singleton)
        VALUES (true)
        RETURNING * INTO v_settings;
    END IF;

    v_key := CASE WHEN p_force THEN NULL ELSE v_source || ':' || current_date::text END;

    INSERT INTO rvbbit.calliope_dream_sweeps
        (run_date,source,requested_by,status,dedupe_key,options)
    VALUES (
        current_date,
        v_source,
        v_requested_by,
        CASE WHEN v_settings.processing_paused THEN 'paused' ELSE 'pending' END,
        v_key,
        jsonb_build_object(
            'company_enabled',v_settings.company_enabled,
            'personal_enabled',v_settings.personal_enabled,
            'active_window_days',v_settings.active_window_days,
            'min_chat_turns',v_settings.min_chat_turns,
            'min_tool_calls',v_settings.min_tool_calls,
            'max_personal_users',v_settings.max_personal_users
        )
    )
    ON CONFLICT (dedupe_key) DO UPDATE
       SET requested_at=rvbbit.calliope_dream_sweeps.requested_at
    RETURNING id INTO v_id;

    RETURN v_id;
END
$fn$;

COMMENT ON TABLE rvbbit.calliope_dream_settings IS
    'Global Dreaming execution controls. Scheduling itself is the active state of the rvbbit_calliope_dreams pg_cron job.';
COMMENT ON TABLE rvbbit.calliope_dream_sweeps IS
    'Durable cron/API trigger receipts. Contains operational counts only, never private dossier or Dream content.';
COMMENT ON TABLE rvbbit.calliope_dream_jobs IS
    'Per-scope Dream execution telemetry and restart-safe work queue. Personal rows expose owner and timing, not private content.';
COMMENT ON FUNCTION rvbbit.calliope_dream_enqueue(text,text,boolean) IS
    'Enqueue one lightweight Dream sweep for Warehouse. Safe for pg_cron; same-source same-day calls deduplicate unless forced.';
