-- Auto-run on first boot of the rvbbit container.
CREATE EXTENSION IF NOT EXISTS pg_rvbbit;

-- Query-history evidence: preloaded in tuning.conf; the extension exposes
-- the pg_stat_statements view in this database.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Apply the stacked SQL migrations (sql/migrations/NNNN_*.sql), tracked in
-- rvbbit.schema_migrations and decoupled from the extension version. The binding
-- exists from CREATE EXTENSION on a fresh install; this is a no-op once applied.
DO $$
BEGIN
    RAISE NOTICE '%', rvbbit.migrate();
END $$;

-- Sanity check: print version into the postgres log so `docker compose logs`
-- confirms the extension actually loaded.
DO $$
BEGIN
    RAISE NOTICE 'rvbbit loaded: %', rvbbit.rvbbit_build_info();
END $$;

-- Seed provider model/rate metadata so EXPLAIN SEMANTIC, receipts/costs,
-- and UI model pickers start with useful data. No-ops gracefully when
-- provider keys or network access are unavailable at boot.
DO $$
BEGIN
    RAISE NOTICE 'rvbbit provider catalogs refreshed: %',
        (SELECT jsonb_agg(to_jsonb(r)) FROM rvbbit.refresh_provider_catalogs('auto') r);
END $$;
