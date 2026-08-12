-- 0288: One durable, private setup notebook per Calliope owner.
--
-- Setup uses the real Calliope session/turn/surface machinery, but its notebook
-- is intentionally absent from the ordinary session rail.  The dedicated
-- /calliope/setup shell resolves it through the purpose marker below.

ALTER TABLE rvbbit.calliope_sessions
    ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'chat';

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'calliope_sessions_purpose_check'
          AND conrelid = 'rvbbit.calliope_sessions'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_sessions
            ADD CONSTRAINT calliope_sessions_purpose_check
            CHECK (purpose IN ('chat', 'setup'));
    END IF;
END
$do$;

CREATE UNIQUE INDEX IF NOT EXISTS calliope_sessions_owner_setup_idx
    ON rvbbit.calliope_sessions (lower(owner_email))
    WHERE purpose = 'setup';

COMMENT ON COLUMN rvbbit.calliope_sessions.purpose IS
    'Presentation purpose. setup notebooks reuse normal turns and surfaces but stay out of the ordinary session rail.';
