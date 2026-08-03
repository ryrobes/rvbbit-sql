-- 0236: Calliope first-class metrics
--
-- Metrics become stable, parameter-aware semantic handles alongside artifacts
-- and named artifact objects.  Home placement and quiet following are kept as
-- separate user intents: a pin controls layout, while a follow contributes to
-- the user's Personal Brief without implicitly creating an alert/watch.

ALTER TABLE rvbbit.calliope_board_items
    DROP CONSTRAINT IF EXISTS calliope_board_items_kind_check;

ALTER TABLE rvbbit.calliope_board_items
    ADD CONSTRAINT calliope_board_items_kind_check
        CHECK (item_kind IN ('artifact', 'artifact_object', 'metric'));

CREATE TABLE IF NOT EXISTS rvbbit.calliope_metric_follows (
    id uuid PRIMARY KEY,
    owner_email text NOT NULL,
    execution_subject text NOT NULL,
    metric_name text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    canonical_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_metric_follows_owner_key
        UNIQUE (owner_email, canonical_key),
    CONSTRAINT calliope_metric_follows_params_check
        CHECK (jsonb_typeof(params) = 'object')
);

CREATE INDEX IF NOT EXISTS calliope_metric_follows_owner_updated_idx
    ON rvbbit.calliope_metric_follows (owner_email, updated_at DESC);

CREATE INDEX IF NOT EXISTS calliope_metric_follows_metric_idx
    ON rvbbit.calliope_metric_follows (metric_name);

COMMENT ON TABLE rvbbit.calliope_metric_follows IS
    'Private, quiet metric subscriptions used by Gallery and Personal Brief. A follow is not a threshold alert.';

COMMENT ON COLUMN rvbbit.calliope_metric_follows.canonical_key IS
    'Stable identity derived from metric name plus canonical JSON parameters.';
