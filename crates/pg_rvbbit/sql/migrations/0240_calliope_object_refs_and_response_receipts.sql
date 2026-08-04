-- 0240: Exact composer object references and durable response receipts.
--
-- The visible user message stays readable while these columns retain the
-- permission-checked identities selected in the composer and the compact
-- provenance summary produced by the completed turn.

ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS object_refs jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS response_receipt jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN rvbbit.calliope_turns.object_refs IS
    'Exact, viewer-authorized company objects selected in the Calliope composer.';

COMMENT ON COLUMN rvbbit.calliope_turns.response_receipt IS
    'Bounded durable summary of evidence, object references, tools, and outputs used by a consequential response.';
