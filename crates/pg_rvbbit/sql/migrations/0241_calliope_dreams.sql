-- 0241: Evidence-backed company reflection and durable Dream portfolio.
--
-- Raw conversation snippets are never stored here.  Cycles retain bounded
-- aggregate receipts, observations retain de-identified evidence summaries,
-- and recurring ideas version one durable Dream instead of creating clutter.

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_cycles (
    id uuid PRIMARY KEY,
    cycle_date date NOT NULL,
    cycle_kind text NOT NULL DEFAULT 'nightly',
    timezone text NOT NULL DEFAULT 'UTC',
    lens text NOT NULL,
    status text NOT NULL DEFAULT 'running',
    generated_by text NOT NULL DEFAULT 'calliope@system',
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    source_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_receipt jsonb NOT NULL DEFAULT '{}'::jsonb,
    observation_count integer NOT NULL DEFAULT 0,
    dream_count integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT calliope_dream_cycles_kind_check
        CHECK (cycle_kind IN ('nightly','manual')),
    CONSTRAINT calliope_dream_cycles_status_check
        CHECK (status IN ('running','complete','failed')),
    CONSTRAINT calliope_dream_cycles_window_check CHECK (window_end > window_start),
    CONSTRAINT calliope_dream_cycles_counts_check
        CHECK (observation_count >= 0 AND dream_count >= 0),
    CONSTRAINT calliope_dream_cycles_source_check
        CHECK (jsonb_typeof(source_summary)='object'),
    CONSTRAINT calliope_dream_cycles_receipt_check
        CHECK (jsonb_typeof(model_receipt)='object')
);

CREATE UNIQUE INDEX IF NOT EXISTS calliope_dream_cycles_nightly_date_idx
    ON rvbbit.calliope_dream_cycles (cycle_date) WHERE cycle_kind='nightly';

CREATE INDEX IF NOT EXISTS calliope_dream_cycles_started_idx
    ON rvbbit.calliope_dream_cycles (started_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_observations (
    id uuid PRIMARY KEY,
    cycle_id uuid NOT NULL REFERENCES rvbbit.calliope_dream_cycles(id) ON DELETE CASCADE,
    fingerprint text NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    entities jsonb NOT NULL DEFAULT '[]'::jsonb,
    signal_count integer NOT NULL DEFAULT 1,
    confidence numeric(5,4) NOT NULL DEFAULT 0.5,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_dream_observations_kind_check
        CHECK (kind IN ('friction','repetition','connection','gap','success','change')),
    CONSTRAINT calliope_dream_observations_evidence_check
        CHECK (jsonb_typeof(evidence)='array'),
    CONSTRAINT calliope_dream_observations_entities_check
        CHECK (jsonb_typeof(entities)='array'),
    CONSTRAINT calliope_dream_observations_signal_check CHECK (signal_count >= 1),
    CONSTRAINT calliope_dream_observations_confidence_check
        CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT calliope_dream_observations_cycle_key UNIQUE (cycle_id,fingerprint)
);

CREATE INDEX IF NOT EXISTS calliope_dream_observations_cycle_idx
    ON rvbbit.calliope_dream_observations (cycle_id,confidence DESC,created_at);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dreams (
    id uuid PRIMARY KEY,
    fingerprint text NOT NULL UNIQUE,
    first_cycle_id uuid REFERENCES rvbbit.calliope_dream_cycles(id) ON DELETE SET NULL,
    latest_cycle_id uuid REFERENCES rvbbit.calliope_dream_cycles(id) ON DELETE SET NULL,
    version integer NOT NULL DEFAULT 1,
    dream_type text NOT NULL,
    output_kind text NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    title text NOT NULL,
    thesis text NOT NULL,
    rationale text NOT NULL DEFAULT '',
    output jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    entities jsonb NOT NULL DEFAULT '[]'::jsonb,
    novelty numeric(5,4) NOT NULL DEFAULT 0.5,
    confidence numeric(5,4) NOT NULL DEFAULT 0.5,
    impact text NOT NULL DEFAULT 'medium',
    effort text NOT NULL DEFAULT 'medium',
    recurrence_count integer NOT NULL DEFAULT 1,
    adopted_by text,
    adopted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_dreams_version_check CHECK (version >= 1),
    CONSTRAINT calliope_dreams_type_check
        CHECK (dream_type IN ('quick_win','connection','automation','strategic','question')),
    CONSTRAINT calliope_dreams_output_kind_check
        CHECK (output_kind IN ('prototype','project_plan','question')),
    CONSTRAINT calliope_dreams_status_check
        CHECK (status IN ('proposed','exploring','adopted','retired')),
    CONSTRAINT calliope_dreams_output_check CHECK (jsonb_typeof(output)='object'),
    CONSTRAINT calliope_dreams_evidence_check CHECK (jsonb_typeof(evidence)='array'),
    CONSTRAINT calliope_dreams_entities_check CHECK (jsonb_typeof(entities)='array'),
    CONSTRAINT calliope_dreams_score_check
        CHECK (novelty BETWEEN 0 AND 1 AND confidence BETWEEN 0 AND 1),
    CONSTRAINT calliope_dreams_impact_check CHECK (impact IN ('low','medium','high')),
    CONSTRAINT calliope_dreams_effort_check CHECK (effort IN ('small','medium','large')),
    CONSTRAINT calliope_dreams_recurrence_check CHECK (recurrence_count >= 1)
);

CREATE INDEX IF NOT EXISTS calliope_dreams_status_updated_idx
    ON rvbbit.calliope_dreams (status,updated_at DESC);

CREATE INDEX IF NOT EXISTS calliope_dreams_latest_cycle_idx
    ON rvbbit.calliope_dreams (latest_cycle_id,updated_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dream_id uuid NOT NULL REFERENCES rvbbit.calliope_dreams(id) ON DELETE CASCADE,
    actor_email text NOT NULL,
    event_kind text NOT NULL,
    note text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_dream_events_kind_check
        CHECK (event_kind IN ('viewed','exploring','adopted','dismissed','sleeping','reopened')),
    CONSTRAINT calliope_dream_events_payload_check CHECK (jsonb_typeof(payload)='object')
);

CREATE INDEX IF NOT EXISTS calliope_dream_events_actor_idx
    ON rvbbit.calliope_dream_events (actor_email,created_at DESC,event_id DESC);

CREATE INDEX IF NOT EXISTS calliope_dream_events_dream_idx
    ON rvbbit.calliope_dream_events (dream_id,created_at DESC,event_id DESC);

COMMENT ON TABLE rvbbit.calliope_dream_cycles IS
    'Bounded nightly or manual company reflection receipts.';

COMMENT ON TABLE rvbbit.calliope_dream_observations IS
    'De-identified, evidence-linked observations retained by a Dream cycle.';

COMMENT ON TABLE rvbbit.calliope_dreams IS
    'Versioned company hypotheses, prototypes, questions, and project plans.';

COMMENT ON TABLE rvbbit.calliope_dream_events IS
    'Per-viewer Dream feedback plus company adoption and exploration events.';
