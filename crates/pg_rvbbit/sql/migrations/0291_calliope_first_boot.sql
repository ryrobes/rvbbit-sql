-- 0291: durable reviewed state for the hosted Calliope first-boot path.
--
-- Conversation turns and Stage surfaces remain the human-readable audit trail.
-- These two singleton rows are the typed current state used by normal
-- Calliope administration after the setup presentation is closed.

CREATE TABLE IF NOT EXISTS rvbbit.company_profile (
    profile_key text PRIMARY KEY DEFAULT 'company',
    version bigint NOT NULL DEFAULT 1,
    company_name text NOT NULL,
    summary text NOT NULL,
    timezone text NOT NULL DEFAULT 'UTC',
    reporting_calendar jsonb NOT NULL DEFAULT '{}'::jsonb,
    terminology jsonb NOT NULL DEFAULT '[]'::jsonb,
    business_questions text[] NOT NULL DEFAULT ARRAY[]::text[],
    status text NOT NULL DEFAULT 'reviewed',
    reviewed_by text,
    reviewed_at timestamptz,
    created_by text NOT NULL DEFAULT session_user,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by text NOT NULL DEFAULT session_user,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT company_profile_singleton_check CHECK (profile_key = 'company'),
    CONSTRAINT company_profile_version_check CHECK (version >= 1),
    CONSTRAINT company_profile_name_check
        CHECK (char_length(btrim(company_name)) BETWEEN 1 AND 160),
    CONSTRAINT company_profile_summary_check
        CHECK (char_length(btrim(summary)) BETWEEN 1 AND 4000),
    CONSTRAINT company_profile_timezone_check
        CHECK (char_length(btrim(timezone)) BETWEEN 1 AND 100),
    CONSTRAINT company_profile_calendar_check
        CHECK (jsonb_typeof(reporting_calendar) = 'object'),
    CONSTRAINT company_profile_terminology_check
        CHECK (jsonb_typeof(terminology) = 'array'),
    CONSTRAINT company_profile_questions_check
        CHECK (cardinality(business_questions) BETWEEN 0 AND 20),
    CONSTRAINT company_profile_status_check CHECK (status IN ('draft','reviewed')),
    CONSTRAINT company_profile_review_check CHECK (
        status = 'draft' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    )
);

COMMENT ON TABLE rvbbit.company_profile IS
    'The one reviewed company context for this trusted single-company appliance; contains no credentials.';
COMMENT ON COLUMN rvbbit.company_profile.terminology IS
    'Reviewed array of {term,meaning} objects used to interpret company language.';

CREATE TABLE IF NOT EXISTS rvbbit.appliance_setup (
    setup_key text PRIMARY KEY DEFAULT 'hosted',
    launched_by text,
    launched_at timestamptz,
    launch_revision bigint NOT NULL DEFAULT 0,
    launch_receipt jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT appliance_setup_singleton_check CHECK (setup_key = 'hosted'),
    CONSTRAINT appliance_setup_revision_check CHECK (launch_revision >= 0),
    CONSTRAINT appliance_setup_receipt_check CHECK (jsonb_typeof(launch_receipt) = 'object'),
    CONSTRAINT appliance_setup_launch_check CHECK (
        (launched_by IS NULL AND launched_at IS NULL)
        OR (launched_by IS NOT NULL AND launched_at IS NOT NULL)
    )
);

INSERT INTO rvbbit.appliance_setup (setup_key)
VALUES ('hosted')
ON CONFLICT (setup_key) DO NOTHING;

COMMENT ON TABLE rvbbit.appliance_setup IS
    'Current hosted first-boot launch state. Detailed redacted receipts remain in the setup notebook.';
