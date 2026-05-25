-- Auto-run on first boot of the rvbbit container.
CREATE EXTENSION IF NOT EXISTS pg_rvbbit;

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
