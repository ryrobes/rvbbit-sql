-- pg_rvbbit 0.51.0 -> 0.52.0
-- Provider model/rate catalog and coarse maintenance scheduler entry points.

CREATE TABLE IF NOT EXISTS rvbbit.provider_catalog (
    provider       text PRIMARY KEY,
    auth_state     text NOT NULL DEFAULT 'unknown',
    status         text NOT NULL DEFAULT 'never',
    last_refresh   timestamptz,
    models_count   bigint NOT NULL DEFAULT 0,
    rates_count    bigint NOT NULL DEFAULT 0,
    error          text,
    raw            jsonb,
    updated_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT provider_catalog_auth_state_check
        CHECK (auth_state IN ('configured', 'public', 'missing', 'unknown')),
    CONSTRAINT provider_catalog_status_check
        CHECK (status IN ('ok', 'skipped', 'error', 'never'))
);

CREATE TABLE IF NOT EXISTS rvbbit.provider_models (
    provider           text NOT NULL,
    model              text NOT NULL,
    display_name       text,
    family             text,
    capabilities       jsonb NOT NULL DEFAULT '[]'::jsonb,
    context_window     bigint,
    output_token_limit bigint,
    available          boolean NOT NULL DEFAULT true,
    source             text NOT NULL DEFAULT 'provider_api',
    raw                jsonb,
    fetched_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (provider, model)
);

CREATE INDEX IF NOT EXISTS provider_models_model_idx
    ON rvbbit.provider_models (model);
CREATE INDEX IF NOT EXISTS provider_models_provider_available_idx
    ON rvbbit.provider_models (provider, available);

CREATE TABLE IF NOT EXISTS rvbbit.model_rate_cards (
    provider                 text NOT NULL,
    model                    text NOT NULL,
    rate_kind                text NOT NULL DEFAULT 'standard',
    input_per_mtok           numeric(18, 9),
    output_per_mtok          numeric(18, 9),
    cached_input_per_mtok    numeric(18, 9),
    cache_write_per_mtok     numeric(18, 9),
    currency                 text NOT NULL DEFAULT 'USD',
    source                   text NOT NULL,
    confidence               text NOT NULL DEFAULT 'seeded',
    raw                      jsonb,
    updated_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (provider, model, rate_kind),
    CONSTRAINT model_rate_cards_confidence_check
        CHECK (confidence IN ('actual', 'provider', 'seeded', 'manual', 'unknown'))
);

CREATE INDEX IF NOT EXISTS model_rate_cards_model_idx
    ON rvbbit.model_rate_cards (model);

CREATE OR REPLACE VIEW rvbbit.provider_model_catalog AS
SELECT
    pm.provider,
    pm.model,
    pm.display_name,
    pm.family,
    pm.capabilities,
    pm.context_window,
    pm.output_token_limit,
    pm.available,
    mrc.rate_kind,
    mrc.input_per_mtok,
    mrc.output_per_mtok,
    mrc.cached_input_per_mtok,
    mrc.cache_write_per_mtok,
    mrc.currency,
    mrc.source AS rate_source,
    mrc.confidence AS rate_confidence,
    pm.updated_at AS model_updated_at,
    mrc.updated_at AS rate_updated_at
FROM rvbbit.provider_models pm
LEFT JOIN rvbbit.model_rate_cards mrc
  ON mrc.provider = pm.provider
 AND mrc.model = pm.model;

CREATE OR REPLACE FUNCTION rvbbit.maintain_storage(
    max_tables bigint DEFAULT 4,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
    n bigint;
    compacted jsonb := '[]'::jsonb;
    refreshed jsonb := '[]'::jsonb;
    errors jsonb := '[]'::jsonb;
    cap bigint := greatest(coalesce(max_tables, 0), 0);
BEGIN
    IF cap = 0 THEN
        RETURN jsonb_build_object(
            'compacted', compacted,
            'refreshed_variants', refreshed,
            'errors', errors,
            'skipped', 'max_tables is zero'
        );
    END IF;

    FOR rec IN
        SELECT t.table_oid::regclass AS rel
        FROM rvbbit.tables t
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE t.shadow_heap_dirty
        ORDER BY t.created_at
        LIMIT cap
    LOOP
        BEGIN
            SELECT count(*) INTO n FROM rvbbit.compact(rec.rel);
            compacted := compacted || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'row_groups', n)
            );
        EXCEPTION WHEN OTHERS THEN
            errors := errors || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'phase', 'compact', 'error', SQLERRM)
            );
        END;
    END LOOP;

    IF refresh_variants THEN
        FOR rec IN
            WITH candidates AS (
                SELECT
                    t.table_oid,
                    t.table_oid::regclass AS rel,
                    coalesce(max(rg.created_at), '-infinity'::timestamptz) AS newest_rg,
                    coalesce(max(rgv.created_at), '-infinity'::timestamptz) AS newest_variant,
                    count(rg.*) AS row_groups,
                    count(rgv.*) AS variants
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              AND (variants = 0 OR newest_variant < newest_rg)
            ORDER BY newest_rg DESC
            LIMIT cap
        LOOP
            BEGIN
                SELECT rvbbit.refresh_layout_variants(rec.rel) INTO n;
                refreshed := refreshed || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'variants', n)
                );
            EXCEPTION WHEN OTHERS THEN
                errors := errors || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'phase', 'refresh_variants', 'error', SQLERRM)
                );
            END;
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'compacted', compacted,
        'refreshed_variants', refreshed,
        'errors', errors
    );
END $$;

CREATE OR REPLACE FUNCTION rvbbit.refresh_provider_catalogs(
    providers text DEFAULT 'auto'
) RETURNS TABLE (
    provider text,
    status text,
    models bigint,
    rates bigint,
    error text,
    auth_state text,
    latency_ms bigint
)
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'refresh_provider_catalogs_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.provider_catalog_summary()
RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'provider_catalog_summary_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.maintain(
    queue_limit bigint DEFAULT 10000,
    backfill_limit bigint DEFAULT 10000,
    reconcile_limit bigint DEFAULT 1000,
    refresh_catalogs boolean DEFAULT true,
    storage_tables bigint DEFAULT 0
) RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'maintain_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.install_maintenance_jobs(
    maintenance_schedule text DEFAULT '*/15 * * * *',
    storage_schedule text DEFAULT '0 * * * *',
    storage_tables bigint DEFAULT 2
) RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'install_maintenance_jobs_wrapper';
