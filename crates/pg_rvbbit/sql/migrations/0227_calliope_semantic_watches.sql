-- 0227: Calliope semantic watches
--
-- A watch is the humane, dashboard-anchored projection of RVBBIT's alert
-- reconciler.  It stores an exact replayable semantic-object handle, never
-- copied SQL, while the corresponding versioned rvbbit.alert_rules entry owns
-- the executable condition.  Watch ownership is the signed OAuth email.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_watches (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    execution_subject text NOT NULL,
    name text NOT NULL,
    source jsonb NOT NULL,
    presentation jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_name text NOT NULL UNIQUE,
    comparator text NOT NULL,
    threshold numeric NOT NULL,
    cadence text NOT NULL DEFAULT 'normal',
    consecutive_n integer NOT NULL DEFAULT 1,
    active boolean NOT NULL DEFAULT true,
    last_value numeric,
    last_status text,
    last_evaluated_at timestamptz,
    last_triggered_at timestamptz,
    last_alert_event_id bigint NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_watches_comparator_check CHECK (comparator IN ('above', 'below')),
    CONSTRAINT calliope_watches_cadence_check CHECK (cadence IN ('fast', 'normal', 'slow')),
    CONSTRAINT calliope_watches_consecutive_check CHECK (consecutive_n BETWEEN 1 AND 12),
    CONSTRAINT calliope_watches_source_check CHECK (source->>'kind' = 'artifact_object')
);

CREATE INDEX IF NOT EXISTS calliope_watches_owner_updated_idx
    ON rvbbit.calliope_watches (owner_email, active, updated_at DESC);
CREATE INDEX IF NOT EXISTS calliope_watches_due_idx
    ON rvbbit.calliope_watches (cadence, last_evaluated_at)
    WHERE active;
CREATE INDEX IF NOT EXISTS calliope_watches_artifact_object_idx
    ON rvbbit.calliope_watches (
        owner_email,
        (source->>'slug'),
        ((source->>'version')::integer),
        (source->>'object_id')
    );

CREATE TABLE IF NOT EXISTS rvbbit.calliope_watch_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    watch_id uuid NOT NULL REFERENCES rvbbit.calliope_watches(id) ON DELETE CASCADE,
    alert_event_id bigint,
    event_kind text NOT NULL DEFAULT 'triggered',
    value numeric,
    threshold numeric,
    message text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    CONSTRAINT calliope_watch_events_kind_check
        CHECK (event_kind IN ('triggered', 'recovered', 'error'))
);

CREATE UNIQUE INDEX IF NOT EXISTS calliope_watch_events_alert_event_idx
    ON rvbbit.calliope_watch_events (watch_id, alert_event_id)
    WHERE alert_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_watch_events_watch_created_idx
    ON rvbbit.calliope_watch_events (watch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_watch_events_unread_idx
    ON rvbbit.calliope_watch_events (created_at DESC)
    WHERE acknowledged_at IS NULL;

COMMENT ON TABLE rvbbit.calliope_watches IS
    'Private dashboard-value subscriptions backed by isolated Calliope cadence tiers in the RVBBIT alert reconciler.';
COMMENT ON COLUMN rvbbit.calliope_watches.source IS
    'Exact artifact version, semantic object definition hash, and resolved dashboard context. SQL remains in the versioned artifact and alert definition.';
COMMENT ON COLUMN rvbbit.calliope_watches.execution_subject IS
    'Mapped Postgres subject captured from the authenticated session. Semantic replay runs under this role in Burrow mode.';
COMMENT ON TABLE rvbbit.calliope_watch_events IS
    'User-facing snapshots of RVBBIT alert transitions, ready for the Calliope Work Inbox and optional agent follow-up.';
