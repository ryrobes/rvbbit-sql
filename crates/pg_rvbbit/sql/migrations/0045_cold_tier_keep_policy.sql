-- 0045_cold_tier_keep_policy
--
-- "Keep offloaded" flag. Without it, migrating a table to a cold tier is a
-- one-shot: the next compaction/refresh writes fresh LOCAL row groups (cold_url
-- NULL) and the table is back on local disk until you re-run migrate_to_cold.
--
-- rvbbit.keep_cold(table, prefix) records a per-table policy and offloads now.
-- From then on, every compaction's tail (export_to_parquet_impl) calls
-- storage::maybe_reoffload_cold, which re-runs migrate_to_cold for the table — so
-- newly written row groups are auto-uploaded and the table stays accelerated on
-- the cold tier with no manual step. Best-effort: an object-store hiccup logs a
-- warning and leaves those row groups local rather than failing the compaction.

CREATE TABLE IF NOT EXISTS rvbbit.cold_tier_policy (
    table_oid       oid PRIMARY KEY REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    cold_url_prefix text NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE rvbbit.cold_tier_policy IS
    'Per-table keep-offloaded policy. When enabled, every compaction that writes '
    'fresh local row groups auto-re-uploads them to cold_url_prefix (s3://, gs://, '
    'file://) via migrate_to_cold — so the table stays on the cold tier.';

-- Enable keep-cold for a table and offload it now (idempotent: migrate_to_cold
-- skips row groups already on the cold tier).
CREATE OR REPLACE FUNCTION rvbbit.keep_cold(reloid regclass, cold_url_prefix text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    r jsonb;
BEGIN
    INSERT INTO rvbbit.cold_tier_policy (table_oid, cold_url_prefix, enabled, updated_at)
    VALUES (reloid, rtrim(cold_url_prefix, '/'), true, now())
    ON CONFLICT (table_oid) DO UPDATE
        SET cold_url_prefix = EXCLUDED.cold_url_prefix,
            enabled         = true,
            updated_at      = now();
    r := rvbbit.migrate_to_cold(reloid, cold_url_prefix);
    RETURN coalesce(r, '{}'::jsonb) || jsonb_build_object('keep_cold', true);
END $$;

-- Stop auto-offloading this table. Existing cold row groups stay where they are;
-- future row groups are written locally again.
CREATE OR REPLACE FUNCTION rvbbit.unkeep_cold(reloid regclass)
RETURNS boolean LANGUAGE sql AS $$
    UPDATE rvbbit.cold_tier_policy
       SET enabled = false, updated_at = now()
     WHERE table_oid = reloid
    RETURNING true;
$$;
