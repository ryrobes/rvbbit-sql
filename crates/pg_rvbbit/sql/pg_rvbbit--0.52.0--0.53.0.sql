-- pg_rvbbit 0.52.0 -> 0.53.0
-- SQL-configurable default LLM provider and self-hosted provider catalog rows.

CREATE TABLE IF NOT EXISTS rvbbit.settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO rvbbit.settings (key, value)
VALUES ('default_provider', to_jsonb('openrouter'::text))
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION rvbbit.default_provider()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(
        (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'default_provider'),
        'openrouter'
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.set_default_provider(provider_name text)
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized text := nullif(btrim(provider_name), '');
    backend_transport text;
    reloaded int := NULL;
BEGIN
    IF normalized IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_default_provider: provider_name cannot be empty';
    END IF;

    SELECT transport INTO backend_transport
    FROM rvbbit.backends
    WHERE name = normalized;

    IF backend_transport IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_default_provider: backend "%" is not registered', normalized;
    END IF;
    IF backend_transport NOT IN ('openai_chat', 'anthropic', 'gemini', 'stub') THEN
        RAISE EXCEPTION 'rvbbit.set_default_provider: backend "%" uses transport "%", not a chat transport',
            normalized, backend_transport;
    END IF;

    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES ('default_provider', to_jsonb(normalized), clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = clock_timestamp();

    BEGIN
        SELECT rvbbit.reload_backends() INTO reloaded;
    EXCEPTION WHEN undefined_function THEN
        reloaded := NULL;
    END;

    RETURN jsonb_build_object(
        'default_provider', normalized,
        'transport', backend_transport,
        'reloaded_backends', reloaded
    );
END
$$;

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
