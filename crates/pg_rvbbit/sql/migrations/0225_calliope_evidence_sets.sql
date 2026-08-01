-- 0225: Calliope evidence sets
--
-- Resolver searches are durable scratchpad strata, not pretend chat messages.
-- turn_kind keeps them out of the Hermes prose rail while preserving one
-- time-ordered session ledger. evidence_refs freezes the exact, user-selected
-- source handles and bounded excerpts used by a later Calliope turn.

ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS turn_kind text NOT NULL DEFAULT 'chat';

ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN rvbbit.calliope_turns.turn_kind IS
    'chat for Hermes conversation turns; evidence_search for resolver-only scratchpad strata.';

COMMENT ON COLUMN rvbbit.calliope_turns.evidence_refs IS
    'Bounded snapshots of evidence-set items explicitly attached to this turn. Raw resolver sets remain in calliope_surfaces.';
