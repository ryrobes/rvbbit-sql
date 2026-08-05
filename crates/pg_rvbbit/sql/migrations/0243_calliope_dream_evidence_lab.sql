-- 0243: Give Dreaming a bounded, auditable Evidence Lab.
--
-- Hermes proposes hypotheses and read-only probes. The Warehouse validates and
-- executes them, persists only bounded/redacted results, and attaches the
-- relevant receipts to the resulting Dream. Raw sampled inputs are ephemeral.

ALTER TABLE rvbbit.calliope_dream_cycles
    ADD COLUMN IF NOT EXISTS probe_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS probe_success_count integer NOT NULL DEFAULT 0;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dream_cycles_probe_count_check'
           AND conrelid='rvbbit.calliope_dream_cycles'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dream_cycles
            ADD CONSTRAINT calliope_dream_cycles_probe_count_check
            CHECK (
                probe_count >= 0
                AND probe_success_count >= 0
                AND probe_success_count <= probe_count
            );
    END IF;
END
$do$;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_probes (
    id uuid PRIMARY KEY,
    cycle_id uuid NOT NULL REFERENCES rvbbit.calliope_dream_cycles(id) ON DELETE CASCADE,
    probe_key text NOT NULL,
    kind text NOT NULL,
    operator text,
    hypothesis text NOT NULL,
    falsifier text NOT NULL,
    purpose text NOT NULL DEFAULT '',
    observation_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    sql_text text NOT NULL,
    sql_sha256 text NOT NULL,
    operator_args jsonb NOT NULL DEFAULT '{}'::jsonb,
    execution_status text NOT NULL DEFAULT 'planned',
    verdict text NOT NULL DEFAULT 'untested',
    result_summary text NOT NULL DEFAULT '',
    result_preview jsonb NOT NULL DEFAULT '{}'::jsonb,
    row_count integer NOT NULL DEFAULT 0,
    elapsed_ms integer NOT NULL DEFAULT 0,
    error text,
    cache_source_id uuid REFERENCES rvbbit.calliope_dream_probes(id) ON DELETE SET NULL,
    executed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_dream_probes_kind_check CHECK (kind IN ('sql','clover')),
    CONSTRAINT calliope_dream_probes_execution_check
        CHECK (execution_status IN ('planned','complete','error','skipped')),
    CONSTRAINT calliope_dream_probes_verdict_check
        CHECK (verdict IN ('supported','contradicted','inconclusive','untested')),
    CONSTRAINT calliope_dream_probes_observation_refs_check
        CHECK (jsonb_typeof(observation_refs)='array'),
    CONSTRAINT calliope_dream_probes_operator_args_check
        CHECK (jsonb_typeof(operator_args)='object'),
    CONSTRAINT calliope_dream_probes_result_preview_check
        CHECK (jsonb_typeof(result_preview)='object'),
    CONSTRAINT calliope_dream_probes_counts_check CHECK (row_count >= 0 AND elapsed_ms >= 0),
    CONSTRAINT calliope_dream_probes_cycle_key UNIQUE (cycle_id,probe_key)
);

CREATE INDEX IF NOT EXISTS calliope_dream_probes_cycle_idx
    ON rvbbit.calliope_dream_probes (cycle_id,execution_status,executed_at);
CREATE INDEX IF NOT EXISTS calliope_dream_probes_cache_idx
    ON rvbbit.calliope_dream_probes (sql_sha256,operator,executed_at DESC)
    WHERE execution_status='complete';

ALTER TABLE rvbbit.calliope_dreams
    ADD COLUMN IF NOT EXISTS probe_receipts jsonb NOT NULL DEFAULT '[]'::jsonb;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='calliope_dreams_probe_receipts_check'
           AND conrelid='rvbbit.calliope_dreams'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_dreams
            ADD CONSTRAINT calliope_dreams_probe_receipts_check
            CHECK (jsonb_typeof(probe_receipts)='array');
    END IF;
END
$do$;

COMMENT ON TABLE rvbbit.calliope_dream_probes IS
    'Bounded SQL and allowlisted Clover experiments proposed during a Dream cycle; raw inputs are never retained.';
COMMENT ON COLUMN rvbbit.calliope_dreams.probe_receipts IS
    'Small public receipts for the Evidence Lab tests relevant to the latest Dream version.';
