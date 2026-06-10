//! Provider model/rate catalog refresh and coarse maintenance scheduling.
//!
//! Provider APIs are inconsistent: model availability is usually API-backed,
//! while pricing is often docs-only. This module stores live model lists when
//! keys are configured, records skipped/error states when they are not, and
//! mirrors any known rates into a richer rate-card table while preserving the
//! existing `rvbbit.model_rates` compatibility table.

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use pgrx::extension_sql;
use pgrx::prelude::*;
use pgrx::{JsonB, Spi};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::specialists::{gemini, SpecialistSpec};

extension_sql!(
    r#"
CREATE TABLE rvbbit.provider_catalog (
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

CREATE TABLE rvbbit.provider_models (
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

CREATE INDEX provider_models_model_idx
    ON rvbbit.provider_models (model);
CREATE INDEX provider_models_provider_available_idx
    ON rvbbit.provider_models (provider, available);

CREATE TABLE rvbbit.model_rate_cards (
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

CREATE INDEX model_rate_cards_model_idx
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
    logs_reaped jsonb := '[]'::jsonb;
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

    -- resources-02/ops-02: trim the append-only telemetry logs on the same
    -- maintenance heartbeat. Isolated so a reap failure never fails maintenance.
    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object('table', table_name, 'rows', rows_reaped)),
                   '[]'::jsonb)
          INTO logs_reaped
          FROM rvbbit.reap_logs();
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_logs', 'error', SQLERRM)
        );
    END;

    RETURN jsonb_build_object(
        'compacted', compacted,
        'refreshed_variants', refreshed,
        'logs_reaped', logs_reaped,
        'errors', errors
    );
END $$;
"#,
    name = "provider_catalog_schema",
    requires = ["rvbbit_bootstrap", "create_model_rates"]
);

extension_sql!(
    r#"
CREATE OR REPLACE FUNCTION rvbbit.register_self_hosted_model(
    provider text,
    model text,
    backend_name text DEFAULT NULL,
    display_name text DEFAULT NULL,
    family text DEFAULT NULL,
    capabilities jsonb DEFAULT '["chat"]'::jsonb,
    context_window bigint DEFAULT NULL,
    output_token_limit bigint DEFAULT NULL,
    input_per_mtok numeric DEFAULT NULL,
    output_per_mtok numeric DEFAULT NULL,
    currency text DEFAULT 'USD',
    cost_policy text DEFAULT 'free',
    raw jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    p text := nullif(btrim(provider), '');
    m text := nullif(btrim(model), '');
    b text := nullif(btrim(backend_name), '');
    policy text := nullif(btrim(cost_policy), '');
    v_models_count bigint;
    v_rates_count bigint;
    catalog_row jsonb;
BEGIN
    IF p IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: provider cannot be empty';
    END IF;
    IF m IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: model cannot be empty';
    END IF;
    IF jsonb_typeof(coalesce(capabilities, '[]'::jsonb)) <> 'array' THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: capabilities must be a JSON array';
    END IF;
    IF jsonb_typeof(coalesce(raw, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: raw must be a JSON object';
    END IF;
    IF (input_per_mtok IS NULL) <> (output_per_mtok IS NULL) THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: input_per_mtok and output_per_mtok must be supplied together';
    END IF;
    IF policy IS NOT NULL
       AND policy NOT IN ('free', 'fixed', 'model_rate', 'provider_settled', 'unknown') THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: unsupported cost_policy "%"', policy;
    END IF;
    IF b IS NOT NULL AND NOT EXISTS (SELECT 1 FROM rvbbit.backends WHERE name = b) THEN
        RAISE EXCEPTION 'rvbbit.register_self_hosted_model: backend "%" is not registered', b;
    END IF;

    INSERT INTO rvbbit.provider_catalog
        (provider, auth_state, status, last_refresh, models_count, rates_count, raw, updated_at)
    VALUES
        (p, 'configured', 'ok', clock_timestamp(), 0, 0,
         jsonb_build_object('source', 'user', 'kind', 'self_hosted'), clock_timestamp())
    ON CONFLICT ON CONSTRAINT provider_catalog_pkey DO UPDATE SET
        auth_state = 'configured',
        status = 'ok',
        error = NULL,
        last_refresh = clock_timestamp(),
        raw = coalesce(rvbbit.provider_catalog.raw, '{}'::jsonb)
              || jsonb_build_object('source', 'user', 'kind', 'self_hosted'),
        updated_at = clock_timestamp();

    INSERT INTO rvbbit.provider_models
        (provider, model, display_name, family, capabilities, context_window,
         output_token_limit, available, source, raw, fetched_at, updated_at)
    VALUES
        (p, m, display_name, family, coalesce(capabilities, '[]'::jsonb),
         context_window, output_token_limit, true, 'user',
         coalesce(raw, '{}'::jsonb) || jsonb_build_object('backend', b),
         clock_timestamp(), clock_timestamp())
    ON CONFLICT ON CONSTRAINT provider_models_pkey DO UPDATE SET
        display_name = EXCLUDED.display_name,
        family = EXCLUDED.family,
        capabilities = EXCLUDED.capabilities,
        context_window = EXCLUDED.context_window,
        output_token_limit = EXCLUDED.output_token_limit,
        available = true,
        source = EXCLUDED.source,
        raw = EXCLUDED.raw,
        updated_at = clock_timestamp();

    IF input_per_mtok IS NOT NULL THEN
        INSERT INTO rvbbit.model_rate_cards
            (provider, model, rate_kind, input_per_mtok, output_per_mtok,
             currency, source, confidence, raw, updated_at)
        VALUES
            (p, m, 'standard', input_per_mtok, output_per_mtok,
             coalesce(currency, 'USD'), 'user', 'manual',
             jsonb_build_object('source', 'rvbbit.register_self_hosted_model'),
             clock_timestamp())
        ON CONFLICT ON CONSTRAINT model_rate_cards_pkey DO UPDATE SET
            input_per_mtok = EXCLUDED.input_per_mtok,
            output_per_mtok = EXCLUDED.output_per_mtok,
            currency = EXCLUDED.currency,
            source = EXCLUDED.source,
            confidence = EXCLUDED.confidence,
            raw = EXCLUDED.raw,
            updated_at = clock_timestamp();

        PERFORM rvbbit.set_model_rate(m, input_per_mtok, output_per_mtok, coalesce(currency, 'USD'));
    END IF;

    IF b IS NOT NULL AND policy IS NOT NULL THEN
        PERFORM rvbbit.set_cost_policy(
            target_kind => 'backend',
            target_name => b,
            policy => policy,
            input_per_mtok => CASE WHEN policy = 'model_rate' THEN input_per_mtok ELSE NULL END,
            output_per_mtok => CASE WHEN policy = 'model_rate' THEN output_per_mtok ELSE NULL END,
            model => CASE WHEN policy = 'model_rate' THEN m ELSE NULL END,
            notes => 'Self-hosted model registered through rvbbit.register_self_hosted_model.'
        );
    END IF;

    SELECT count(*) INTO v_models_count
    FROM rvbbit.provider_models pm
    WHERE pm.provider = p;

    SELECT count(*) INTO v_rates_count
    FROM rvbbit.model_rate_cards mrc
    WHERE mrc.provider = p;

    UPDATE rvbbit.provider_catalog pc
    SET models_count = v_models_count,
        rates_count = v_rates_count,
        updated_at = clock_timestamp()
    WHERE pc.provider = p;

    SELECT to_jsonb(v) INTO catalog_row
    FROM rvbbit.provider_model_catalog v
    WHERE v.provider = p AND v.model = m
    ORDER BY v.rate_kind NULLS LAST
    LIMIT 1;

    RETURN jsonb_build_object(
        'provider', p,
        'model', m,
        'backend', b,
        'cost_policy', policy,
        'catalog', catalog_row
    );
END
$$;
"#,
    name = "self_hosted_provider_catalog",
    requires = ["provider_catalog_schema", "rvbbit_cost_catalog"]
);

#[derive(Debug, Clone)]
struct RefreshRow {
    provider: String,
    status: String,
    models: i64,
    rates: i64,
    error: Option<String>,
    auth_state: String,
    latency_ms: i64,
}

#[derive(Debug, Clone)]
struct RefreshStats {
    models: i64,
    rates: i64,
    auth_state: String,
    raw: Value,
}

#[pg_extern(volatile)]
fn refresh_provider_catalogs(
    providers: default!(&str, "'auto'"),
) -> TableIterator<
    'static,
    (
        name!(provider, String),
        name!(status, String),
        name!(models, i64),
        name!(rates, i64),
        name!(error, Option<String>),
        name!(auth_state, String),
        name!(latency_ms, i64),
    ),
> {
    let rows: Vec<_> = provider_list(providers)
        .into_iter()
        .map(refresh_provider)
        .collect();
    TableIterator::new(rows.into_iter().map(|r| {
        (
            r.provider,
            r.status,
            r.models,
            r.rates,
            r.error,
            r.auth_state,
            r.latency_ms,
        )
    }))
}

#[pg_extern]
fn provider_catalog_summary() -> JsonB {
    let doc = Spi::get_one::<JsonB>(
        "SELECT jsonb_build_object( \
            'providers', coalesce((SELECT jsonb_agg(to_jsonb(pc) ORDER BY provider) FROM rvbbit.provider_catalog pc), '[]'::jsonb), \
            'models', coalesce((SELECT count(*) FROM rvbbit.provider_models), 0), \
            'available_models', coalesce((SELECT count(*) FROM rvbbit.provider_models WHERE available), 0), \
            'rate_cards', coalesce((SELECT count(*) FROM rvbbit.model_rate_cards), 0), \
            'uncosted_available_models', coalesce(( \
                SELECT count(*) \
                FROM rvbbit.provider_models pm \
                WHERE pm.available \
                  AND NOT EXISTS ( \
                    SELECT 1 FROM rvbbit.model_rate_cards mrc \
                    WHERE mrc.provider = pm.provider \
                      AND mrc.model = pm.model \
                      AND mrc.rate_kind = 'standard' \
                  ) \
            ), 0) \
        )",
    )
    .ok()
    .flatten()
    .unwrap_or_else(|| JsonB(json!({})));
    doc
}

#[pg_extern(volatile)]
fn maintain(
    queue_limit: default!(i64, 10000),
    backfill_limit: default!(i64, 10000),
    reconcile_limit: default!(i64, 1000),
    refresh_catalogs: default!(bool, true),
    storage_tables: default!(i64, 0),
) -> JsonB {
    let cost_sql = format!(
        "SELECT rvbbit.maintain_cost_audit({}, {}, {})",
        queue_limit.max(0),
        backfill_limit.max(0),
        reconcile_limit.max(0)
    );
    let cost = Spi::get_one::<JsonB>(&cost_sql)
        .ok()
        .flatten()
        .map(|v| v.0)
        .unwrap_or_else(|| json!({"error": "rvbbit.maintain_cost_audit returned no row"}));

    let catalogs = if refresh_catalogs {
        Spi::get_one::<JsonB>(
            "SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb) \
             FROM rvbbit.refresh_provider_catalogs('auto') r",
        )
        .ok()
        .flatten()
        .map(|v| v.0)
        .unwrap_or_else(
            || json!([{"status": "error", "error": "provider catalog refresh returned no row"}]),
        )
    } else {
        json!({"skipped": true})
    };

    let storage = if storage_tables > 0 {
        let storage_sql = format!(
            "SELECT rvbbit.maintain_storage({}, true)",
            storage_tables.max(0)
        );
        Spi::get_one::<JsonB>(&storage_sql)
            .ok()
            .flatten()
            .map(|v| v.0)
            .unwrap_or_else(|| json!({"error": "rvbbit.maintain_storage returned no row"}))
    } else {
        json!({"skipped": "storage_tables is zero"})
    };

    JsonB(json!({
        "costs": cost,
        "provider_catalogs": catalogs,
        "storage": storage,
    }))
}

#[pg_extern(volatile)]
fn install_maintenance_jobs(
    maintenance_schedule: default!(&str, "'*/15 * * * *'"),
    storage_schedule: default!(&str, "'0 * * * *'"),
    storage_tables: default!(i64, 2),
) -> JsonB {
    let cron_available = Spi::get_one::<bool>(
        "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron')",
    )
    .ok()
    .flatten()
    .unwrap_or(false);
    let cron_preloaded = Spi::get_one::<bool>(
        "SELECT coalesce(current_setting('shared_preload_libraries', true), '') ILIKE '%pg_cron%'",
    )
    .ok()
    .flatten()
    .unwrap_or(false);
    // pg_cron's cron.* functions live only in its home database (cron.database_name,
    // default 'postgres'). When that differs from THIS database, cron.* is not callable
    // here and CREATE EXTENSION pg_cron hard-errors ("can only create extension in
    // database <home>"). Detect that up front — BEFORE attempting CREATE EXTENSION —
    // and return the exact cron.schedule_in_database SQL to run from the home db (or via
    // the Scheduler UI), so this stays non-destructive instead of throwing.
    let cron_home = Spi::get_one::<String>("SELECT current_setting('cron.database_name', true)")
        .ok()
        .flatten()
        .unwrap_or_default();
    // current_database() returns `name`, which pgrx's get_one::<String> can't decode;
    // cast to text so this_db is populated (not the empty string).
    let this_db = Spi::get_one::<String>("SELECT current_database()::text")
        .ok()
        .flatten()
        .unwrap_or_default();
    if !cron_home.is_empty() && cron_home != this_db {
        let maintain_cmd = format!(
            "SELECT cron.schedule_in_database('rvbbit-maintain', {}, 'SELECT rvbbit.maintain();', {});",
            sql_lit(maintenance_schedule),
            sql_lit(&this_db),
        );
        let storage_cmd = format!(
            "SELECT cron.schedule_in_database('rvbbit-storage-maintain', {}, {}, {});",
            sql_lit(storage_schedule),
            sql_lit(&format!(
                "SELECT rvbbit.maintain(storage_tables => {});",
                storage_tables.max(0)
            )),
            sql_lit(&this_db),
        );
        return JsonB(json!({
            "ok": false,
            "status": "pg_cron_not_home_db",
            "cron_home": cron_home,
            "this_db": this_db,
            "message": format!(
                "pg_cron's home database is '{cron_home}'; cron.* is only callable there. \
                 Use the Scheduler UI, or connect to '{cron_home}' and run the SQL in 'schedule_sql' \
                 to schedule jobs that run in '{this_db}'."
            ),
            "schedule_sql": [maintain_cmd, storage_cmd],
            "manual_maintenance_sql": "SELECT rvbbit.maintain();"
        }));
    }

    // Home db == this db (or cron.database_name unset): safe to create + schedule here.
    if cron_available && cron_preloaded {
        let _ = Spi::run("CREATE EXTENSION IF NOT EXISTS pg_cron");
    }
    let cron_ready = Spi::get_one::<bool>(
        "SELECT to_regnamespace('cron') IS NOT NULL \
            AND to_regprocedure('cron.schedule(text,text,text)') IS NOT NULL",
    )
    .ok()
    .flatten()
    .unwrap_or(false);
    if !cron_ready {
        return JsonB(json!({
            "ok": false,
            "status": "pg_cron_unavailable",
            "message": "Install and preload pg_cron, then run SELECT rvbbit.install_maintenance_jobs();",
            "self_hosted_hint": "shared_preload_libraries = 'pg_rvbbit,pg_cron'; CREATE EXTENSION IF NOT EXISTS pg_cron;",
            "manual_maintenance_sql": "SELECT rvbbit.maintain();"
        }));
    }

    let _ = Spi::run(
        "DO $$ \
         DECLARE r record; \
         BEGIN \
           FOR r IN SELECT jobid FROM cron.job WHERE jobname IN ('rvbbit-maintain', 'rvbbit-storage-maintain') LOOP \
             PERFORM cron.unschedule(r.jobid); \
           END LOOP; \
         END $$",
    );

    let maintain_sql = format!(
        "SELECT cron.schedule('rvbbit-maintain', {}, 'SELECT rvbbit.maintain();')",
        sql_lit(maintenance_schedule)
    );
    let maintain_job = Spi::get_one::<i64>(&maintain_sql).ok().flatten();

    let storage_job = if storage_tables > 0 {
        let command = format!(
            "SELECT rvbbit.maintain(storage_tables => {});",
            storage_tables.max(0)
        );
        let storage_sql = format!(
            "SELECT cron.schedule('rvbbit-storage-maintain', {}, {})",
            sql_lit(storage_schedule),
            sql_lit(&command)
        );
        Spi::get_one::<i64>(&storage_sql).ok().flatten()
    } else {
        None
    };

    JsonB(json!({
        "ok": maintain_job.is_some(),
        "maintenance_job_id": maintain_job,
        "maintenance_schedule": maintenance_schedule,
        "storage_job_id": storage_job,
        "storage_schedule": if storage_job.is_some() { Some(storage_schedule) } else { None::<&str> },
        "storage_tables": storage_tables.max(0),
    }))
}

fn refresh_provider(provider: String) -> RefreshRow {
    let start = Instant::now();
    let result = match provider.as_str() {
        "openrouter" => refresh_openrouter(),
        "openai" => refresh_openai(),
        "anthropic" => refresh_anthropic(),
        "gemini" => refresh_gemini(),
        other => Err(format!("unknown provider '{other}'")),
    };
    let latency_ms = start.elapsed().as_millis().min(i64::MAX as u128) as i64;
    match result {
        Ok(stats) => {
            let status = if stats.auth_state == "missing" {
                "skipped"
            } else {
                "ok"
            };
            upsert_provider_status(
                &provider,
                &stats.auth_state,
                status,
                stats.models,
                stats.rates,
                None,
                Some(&stats.raw),
            );
            RefreshRow {
                provider,
                status: status.to_string(),
                models: stats.models,
                rates: stats.rates,
                error: None,
                auth_state: stats.auth_state,
                latency_ms,
            }
        }
        Err(error) => {
            let auth_state = provider_auth_state(&provider);
            upsert_provider_status(&provider, &auth_state, "error", 0, 0, Some(&error), None);
            RefreshRow {
                provider,
                status: "error".to_string(),
                models: 0,
                rates: 0,
                error: Some(error),
                auth_state,
                latency_ms,
            }
        }
    }
}

fn provider_list(raw: &str) -> Vec<String> {
    let trimmed = raw.trim();
    let mut out = BTreeSet::new();
    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("auto") {
        for p in ["openrouter", "openai", "anthropic", "gemini"] {
            out.insert(p.to_string());
        }
    } else {
        for p in trimmed.split(',') {
            let p = p.trim().to_ascii_lowercase();
            if !p.is_empty() {
                out.insert(p);
            }
        }
    }
    out.into_iter().collect()
}

fn refresh_openrouter() -> Result<RefreshStats, String> {
    let client = http_client()?;
    let mut req = client.get("https://openrouter.ai/api/v1/models");
    let auth_state = if let Ok(key) = std::env::var("OPENROUTER_API_KEY") {
        if !key.trim().is_empty() {
            req = req.bearer_auth(key);
            "configured"
        } else {
            "public"
        }
    } else {
        "public"
    };
    let resp = req.send().map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!(
            "OpenRouter /models returned HTTP {}",
            resp.status().as_u16()
        ));
    }
    let body: OpenRouterModelsResponse = resp.json().map_err(|e| e.to_string())?;
    let mut models = 0_i64;
    let mut rates = 0_i64;
    for m in &body.data {
        let raw = serde_json::to_value(m).unwrap_or_else(|_| json!({}));
        let capabilities = m.supported_parameters.clone().unwrap_or_else(|| json!([]));
        upsert_provider_model(
            "openrouter",
            &m.id,
            m.name.as_deref(),
            openrouter_family(m),
            &capabilities,
            m.context_length,
            None,
            true,
            "openrouter_api",
            &raw,
        )?;
        models += 1;
        if let Some(pricing) = &m.pricing {
            if let (Some(input), Some(output)) = (
                parse_per_token_to_mtok(pricing.prompt.as_deref()),
                parse_per_token_to_mtok(pricing.completion.as_deref()),
            ) {
                upsert_rate_card(
                    "openrouter",
                    &m.id,
                    "standard",
                    Some(input),
                    Some(output),
                    None,
                    None,
                    "USD",
                    "openrouter_api",
                    "provider",
                    &raw,
                    true,
                )?;
                rates += 1;
            }
        }
    }
    Ok(RefreshStats {
        models,
        rates,
        auth_state: auth_state.to_string(),
        raw: json!({"endpoint": "https://openrouter.ai/api/v1/models"}),
    })
}

fn refresh_openai() -> Result<RefreshStats, String> {
    let Some(key) = env_token("OPENAI_API_KEY") else {
        return Ok(skipped("openai", "OPENAI_API_KEY"));
    };
    let client = http_client()?;
    let resp = client
        .get("https://api.openai.com/v1/models")
        .bearer_auth(key)
        .send()
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!(
            "OpenAI /v1/models returned HTTP {}: {}",
            resp.status().as_u16(),
            resp.text().unwrap_or_default()
        ));
    }
    let body: OpenAiModelsResponse = resp.json().map_err(|e| e.to_string())?;
    let mut models = 0_i64;
    let mut rates = 0_i64;
    for m in &body.data {
        let raw = serde_json::to_value(m).unwrap_or_else(|_| json!({}));
        upsert_provider_model(
            "openai",
            &m.id,
            Some(&m.id),
            m.owned_by.as_deref(),
            &json!([]),
            None,
            None,
            true,
            "openai_api",
            &raw,
        )?;
        models += 1;
        rates += mirror_existing_rate("openai", &m.id, "openai_seed")?;
    }
    Ok(RefreshStats {
        models,
        rates,
        auth_state: "configured".to_string(),
        raw: json!({"endpoint": "https://api.openai.com/v1/models"}),
    })
}

fn refresh_anthropic() -> Result<RefreshStats, String> {
    let Some(key) = env_token("ANTHROPIC_API_KEY") else {
        return Ok(skipped("anthropic", "ANTHROPIC_API_KEY"));
    };
    let client = http_client()?;
    let mut after_id: Option<String> = None;
    let mut models = 0_i64;
    let mut rates = 0_i64;
    let mut pages = 0_i32;
    loop {
        pages += 1;
        let mut req = client
            .get("https://api.anthropic.com/v1/models")
            .header("x-api-key", &key)
            .header("anthropic-version", "2023-06-01")
            .query(&[("limit", "100")]);
        if let Some(after) = &after_id {
            req = req.query(&[("after_id", after.as_str())]);
        }
        let resp = req.send().map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!(
                "Anthropic /v1/models returned HTTP {}: {}",
                resp.status().as_u16(),
                resp.text().unwrap_or_default()
            ));
        }
        let body: AnthropicModelsResponse = resp.json().map_err(|e| e.to_string())?;
        for m in &body.data {
            let raw = serde_json::to_value(m).unwrap_or_else(|_| json!({}));
            upsert_provider_model(
                "anthropic",
                &m.id,
                m.display_name.as_deref(),
                Some("claude"),
                &json!([]),
                None,
                None,
                true,
                "anthropic_api",
                &raw,
            )?;
            models += 1;
            rates += mirror_existing_rate("anthropic", &m.id, "anthropic_seed")?;
        }
        if !body.has_more.unwrap_or(false) || pages >= 10 {
            break;
        }
        after_id = body.last_id;
        if after_id.is_none() {
            break;
        }
    }
    Ok(RefreshStats {
        models,
        rates,
        auth_state: "configured".to_string(),
        raw: json!({"endpoint": "https://api.anthropic.com/v1/models", "pages": pages}),
    })
}

fn refresh_gemini() -> Result<RefreshStats, String> {
    let client = http_client()?;
    let mut req = client.get("https://generativelanguage.googleapis.com/v1beta/models");
    let auth_state = if let Some(key) = env_token("GEMINI_API_KEY") {
        req = req.header("x-goog-api-key", key);
        "configured"
    } else if std::env::var("GOOGLE_APPLICATION_CREDENTIALS")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .is_some()
    {
        let spec = SpecialistSpec {
            name: "gemini_catalog".to_string(),
            transport: "gemini".to_string(),
            endpoint_url: "https://generativelanguage.googleapis.com/v1beta/models".to_string(),
            batch_size: 1,
            max_concurrent: 1,
            timeout_ms: catalog_timeout().as_millis().min(u64::MAX as u128) as u64,
            auth_header_env: Some("GOOGLE_APPLICATION_CREDENTIALS".to_string()),
            transport_opts: json!({"auth_mode": "google_adc"}),
        };
        let (token, user_project) =
            gemini::google_access_token(&spec).map_err(|e| e.to_string())?;
        req = req.bearer_auth(token);
        if let Some(project) = user_project {
            req = req.header("x-goog-user-project", project);
        }
        "configured"
    } else {
        return Ok(skipped(
            "gemini",
            "GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS",
        ));
    };
    let resp = req.send().map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!(
            "Gemini /v1beta/models returned HTTP {}: {}",
            resp.status().as_u16(),
            resp.text().unwrap_or_default()
        ));
    }
    let body: GeminiModelsResponse = resp.json().map_err(|e| e.to_string())?;
    let mut models = 0_i64;
    let mut rates = 0_i64;
    for m in &body.models {
        let raw = serde_json::to_value(m).unwrap_or_else(|_| json!({}));
        let model = m.name.strip_prefix("models/").unwrap_or(&m.name);
        let capabilities =
            serde_json::to_value(&m.supported_generation_methods).unwrap_or_else(|_| json!([]));
        upsert_provider_model(
            "gemini",
            model,
            m.display_name.as_deref(),
            Some("gemini"),
            &capabilities,
            m.input_token_limit,
            m.output_token_limit,
            m.supported_generation_methods
                .iter()
                .any(|method| method == "generateContent"),
            "gemini_api",
            &raw,
        )?;
        models += 1;
        rates += mirror_existing_rate("gemini", model, "gemini_seed")?;
    }
    Ok(RefreshStats {
        models,
        rates,
        auth_state: auth_state.to_string(),
        raw: json!({"endpoint": "https://generativelanguage.googleapis.com/v1beta/models"}),
    })
}

fn skipped(provider: &str, missing: &str) -> RefreshStats {
    RefreshStats {
        models: 0,
        rates: 0,
        auth_state: "missing".to_string(),
        raw: json!({"provider": provider, "missing_env": missing}),
    }
}

fn provider_auth_state(provider: &str) -> String {
    match provider {
        "openrouter" => {
            if env_token("OPENROUTER_API_KEY").is_some() {
                "configured".to_string()
            } else {
                "public".to_string()
            }
        }
        "openai" => auth_state_for_env("OPENAI_API_KEY"),
        "anthropic" => auth_state_for_env("ANTHROPIC_API_KEY"),
        "gemini" => {
            if env_token("GEMINI_API_KEY").is_some()
                || env_token("GOOGLE_APPLICATION_CREDENTIALS").is_some()
            {
                "configured".to_string()
            } else {
                "missing".to_string()
            }
        }
        _ => "unknown".to_string(),
    }
}

fn auth_state_for_env(env_name: &str) -> String {
    if env_token(env_name).is_some() {
        "configured"
    } else {
        "missing"
    }
    .to_string()
}

fn env_token(env_name: &str) -> Option<String> {
    std::env::var(env_name)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn http_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .timeout(catalog_timeout())
        .build()
        .map_err(|e| e.to_string())
}

fn catalog_timeout() -> Duration {
    std::env::var("RVBBIT_PROVIDER_CATALOG_TIMEOUT_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_millis)
        .unwrap_or_else(|| Duration::from_secs(20))
}

fn upsert_provider_status(
    provider: &str,
    auth_state: &str,
    status: &str,
    models: i64,
    rates: i64,
    error: Option<&str>,
    raw: Option<&Value>,
) {
    let raw_sql = raw
        .map(jsonb_sql)
        .unwrap_or_else(|| "NULL::jsonb".to_string());
    let sql = format!(
        "INSERT INTO rvbbit.provider_catalog \
            (provider, auth_state, status, last_refresh, models_count, rates_count, error, raw, updated_at) \
         VALUES ({provider}, {auth_state}, {status}, clock_timestamp(), {models}, {rates}, {error}, {raw}, clock_timestamp()) \
         ON CONFLICT (provider) DO UPDATE SET \
            auth_state = EXCLUDED.auth_state, \
            status = EXCLUDED.status, \
            last_refresh = EXCLUDED.last_refresh, \
            models_count = EXCLUDED.models_count, \
            rates_count = EXCLUDED.rates_count, \
            error = EXCLUDED.error, \
            raw = EXCLUDED.raw, \
            updated_at = clock_timestamp()",
        provider = sql_lit(provider),
        auth_state = sql_lit(auth_state),
        status = sql_lit(status),
        error = opt_sql_lit(error),
        raw = raw_sql
    );
    if let Err(e) = Spi::run(&sql) {
        pgrx::warning!("rvbbit.provider_catalog status upsert failed: {e}");
    }
}

#[allow(clippy::too_many_arguments)]
fn upsert_provider_model(
    provider: &str,
    model: &str,
    display_name: Option<&str>,
    family: Option<&str>,
    capabilities: &Value,
    context_window: Option<i64>,
    output_token_limit: Option<i64>,
    available: bool,
    source: &str,
    raw: &Value,
) -> Result<(), String> {
    let sql = format!(
        "INSERT INTO rvbbit.provider_models \
            (provider, model, display_name, family, capabilities, context_window, \
             output_token_limit, available, source, raw, fetched_at, updated_at) \
         VALUES ({provider}, {model}, {display_name}, {family}, {capabilities}, \
             {context_window}, {output_token_limit}, {available}, {source}, {raw}, \
             clock_timestamp(), clock_timestamp()) \
         ON CONFLICT (provider, model) DO UPDATE SET \
            display_name = EXCLUDED.display_name, \
            family = EXCLUDED.family, \
            capabilities = EXCLUDED.capabilities, \
            context_window = EXCLUDED.context_window, \
            output_token_limit = EXCLUDED.output_token_limit, \
            available = EXCLUDED.available, \
            source = EXCLUDED.source, \
            raw = EXCLUDED.raw, \
            fetched_at = EXCLUDED.fetched_at, \
            updated_at = clock_timestamp()",
        provider = sql_lit(provider),
        model = sql_lit(model),
        display_name = opt_sql_lit(display_name),
        family = opt_sql_lit(family),
        capabilities = jsonb_sql(capabilities),
        context_window = opt_i64(context_window),
        output_token_limit = opt_i64(output_token_limit),
        available = if available { "true" } else { "false" },
        source = sql_lit(source),
        raw = jsonb_sql(raw)
    );
    Spi::run(&sql).map_err(|e| e.to_string())
}

#[allow(clippy::too_many_arguments)]
fn upsert_rate_card(
    provider: &str,
    model: &str,
    rate_kind: &str,
    input_per_mtok: Option<f64>,
    output_per_mtok: Option<f64>,
    cached_input_per_mtok: Option<f64>,
    cache_write_per_mtok: Option<f64>,
    currency: &str,
    source: &str,
    confidence: &str,
    raw: &Value,
    mirror_model_rates: bool,
) -> Result<(), String> {
    let sql = format!(
        "INSERT INTO rvbbit.model_rate_cards \
            (provider, model, rate_kind, input_per_mtok, output_per_mtok, cached_input_per_mtok, \
             cache_write_per_mtok, currency, source, confidence, raw, updated_at) \
         VALUES ({provider}, {model}, {rate_kind}, {input}, {output}, {cached}, {cache_write}, \
             {currency}, {source}, {confidence}, {raw}, clock_timestamp()) \
         ON CONFLICT (provider, model, rate_kind) DO UPDATE SET \
            input_per_mtok = EXCLUDED.input_per_mtok, \
            output_per_mtok = EXCLUDED.output_per_mtok, \
            cached_input_per_mtok = EXCLUDED.cached_input_per_mtok, \
            cache_write_per_mtok = EXCLUDED.cache_write_per_mtok, \
            currency = EXCLUDED.currency, \
            source = EXCLUDED.source, \
            confidence = EXCLUDED.confidence, \
            raw = EXCLUDED.raw, \
            updated_at = clock_timestamp()",
        provider = sql_lit(provider),
        model = sql_lit(model),
        rate_kind = sql_lit(rate_kind),
        input = opt_f64(input_per_mtok),
        output = opt_f64(output_per_mtok),
        cached = opt_f64(cached_input_per_mtok),
        cache_write = opt_f64(cache_write_per_mtok),
        currency = sql_lit(currency),
        source = sql_lit(source),
        confidence = sql_lit(confidence),
        raw = jsonb_sql(raw)
    );
    Spi::run(&sql).map_err(|e| e.to_string())?;
    if mirror_model_rates && rate_kind == "standard" {
        if let (Some(input), Some(output)) = (input_per_mtok, output_per_mtok) {
            let mirror_sql = format!(
                "SELECT rvbbit.set_model_rate({}, {:.9}, {:.9}, {})",
                sql_lit(model),
                input,
                output,
                sql_lit(currency)
            );
            Spi::run(&mirror_sql).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

fn mirror_existing_rate(provider: &str, model: &str, source: &str) -> Result<i64, String> {
    let sql = format!(
        "SELECT ( \
            SELECT jsonb_build_object( \
                'input', input_per_mtok::float8, \
                'output', output_per_mtok::float8, \
                'currency', currency \
            ) \
            FROM rvbbit.model_rates WHERE model = {} LIMIT 1 \
         )",
        sql_lit(model)
    );
    let row = Spi::get_one::<JsonB>(&sql).map_err(|e| e.to_string())?;
    if let Some(row) = row {
        let input = row
            .0
            .get("input")
            .and_then(|v| v.as_f64())
            .ok_or_else(|| format!("invalid input rate for model {model}"))?;
        let output = row
            .0
            .get("output")
            .and_then(|v| v.as_f64())
            .ok_or_else(|| format!("invalid output rate for model {model}"))?;
        let currency = row
            .0
            .get("currency")
            .and_then(|v| v.as_str())
            .unwrap_or("USD");
        upsert_rate_card(
            provider,
            model,
            "standard",
            Some(input),
            Some(output),
            None,
            None,
            currency,
            source,
            "seeded",
            &json!({"source": "rvbbit.model_rates"}),
            false,
        )?;
        Ok(1)
    } else {
        Ok(0)
    }
}

fn parse_per_token_to_mtok(raw: Option<&str>) -> Option<f64> {
    let value = raw?.parse::<f64>().ok()?;
    if value.is_finite() && value >= 0.0 {
        Some(value * 1_000_000.0)
    } else {
        None
    }
}

fn openrouter_family(m: &OpenRouterModel) -> Option<&str> {
    m.id.split_once('/').map(|(family, _)| family)
}

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

fn opt_sql_lit(s: Option<&str>) -> String {
    s.map(sql_lit).unwrap_or_else(|| "NULL".to_string())
}

fn opt_i64(n: Option<i64>) -> String {
    n.map(|v| v.to_string())
        .unwrap_or_else(|| "NULL".to_string())
}

fn opt_f64(n: Option<f64>) -> String {
    n.filter(|v| v.is_finite())
        .map(|v| format!("{v:.9}"))
        .unwrap_or_else(|| "NULL".to_string())
}

fn jsonb_sql(value: &Value) -> String {
    let raw = serde_json::to_string(value).unwrap_or_else(|_| "null".to_string());
    format!("{}::jsonb", sql_lit(&raw))
}

#[derive(Debug, Deserialize)]
struct OpenRouterModelsResponse {
    #[serde(default)]
    data: Vec<OpenRouterModel>,
}

#[derive(Debug, Deserialize, Serialize)]
struct OpenRouterModel {
    id: String,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    context_length: Option<i64>,
    #[serde(default)]
    pricing: Option<OpenRouterPricing>,
    #[serde(default)]
    supported_parameters: Option<Value>,
    #[serde(default)]
    architecture: Option<Value>,
    #[serde(default)]
    top_provider: Option<Value>,
}

#[derive(Debug, Deserialize, Serialize)]
struct OpenRouterPricing {
    #[serde(default)]
    prompt: Option<String>,
    #[serde(default)]
    completion: Option<String>,
    #[serde(flatten)]
    extra: Value,
}

#[derive(Debug, Deserialize)]
struct OpenAiModelsResponse {
    #[serde(default)]
    data: Vec<OpenAiModel>,
}

#[derive(Debug, Deserialize, Serialize)]
struct OpenAiModel {
    id: String,
    #[serde(default)]
    owned_by: Option<String>,
    #[serde(default)]
    created: Option<i64>,
    #[serde(default)]
    object: Option<String>,
}

#[derive(Debug, Deserialize)]
struct AnthropicModelsResponse {
    #[serde(default)]
    data: Vec<AnthropicModel>,
    #[serde(default)]
    has_more: Option<bool>,
    #[serde(default)]
    last_id: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
struct AnthropicModel {
    id: String,
    #[serde(default)]
    display_name: Option<String>,
    #[serde(default)]
    created_at: Option<String>,
    #[serde(default, rename = "type")]
    kind: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GeminiModelsResponse {
    #[serde(default)]
    models: Vec<GeminiModel>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct GeminiModel {
    name: String,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    display_name: Option<String>,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    input_token_limit: Option<i64>,
    #[serde(default)]
    output_token_limit: Option<i64>,
    #[serde(default)]
    supported_generation_methods: Vec<String>,
}
