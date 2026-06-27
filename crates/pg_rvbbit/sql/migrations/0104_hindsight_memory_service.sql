-- 0104_hindsight_memory_service -- registry and config helpers for Warren-managed Hindsight.

CREATE TABLE IF NOT EXISTS rvbbit.memory_services (
    name                  text PRIMARY KEY,
    provider              text NOT NULL DEFAULT 'hindsight',
    endpoint_url          text NOT NULL,
    status                text NOT NULL DEFAULT 'ready',
    auth_header_env       text,
    labels                jsonb NOT NULL DEFAULT '{}'::jsonb,
    service_source        text NOT NULL DEFAULT 'manual',
    warren_deployment_id  uuid,
    install_manifest      jsonb NOT NULL DEFAULT '{}'::jsonb,
    health                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT memory_services_name_check CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT memory_services_provider_check CHECK (provider ~ '^[A-Za-z_][A-Za-z0-9_\\-]*$'),
    CONSTRAINT memory_services_status_check CHECK (
        status IN ('starting', 'ready', 'failed', 'disabled')
    ),
    CONSTRAINT memory_services_endpoint_check CHECK (endpoint_url ~ '^https?://'),
    CONSTRAINT memory_services_labels_is_object CHECK (jsonb_typeof(labels) = 'object'),
    CONSTRAINT memory_services_manifest_is_object CHECK (jsonb_typeof(install_manifest) = 'object'),
    CONSTRAINT memory_services_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

CREATE INDEX IF NOT EXISTS memory_services_provider_status_idx
    ON rvbbit.memory_services (provider, status, updated_at DESC);

CREATE OR REPLACE FUNCTION rvbbit.touch_memory_services_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS memory_services_touch_updated_at ON rvbbit.memory_services;
CREATE TRIGGER memory_services_touch_updated_at
    BEFORE UPDATE ON rvbbit.memory_services
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_memory_services_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.require_memory_service_admin()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) AND NOT (
        EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rvbbit_warren')
        AND pg_has_role(current_user, 'rvbbit_warren', 'member')
    ) THEN
        RAISE EXCEPTION 'rvbbit memory service registration requires a superuser or rvbbit_warren role membership in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.register_memory_service(
    service_name text,
    endpoint_url text,
    service_provider text DEFAULT 'hindsight',
    service_status text DEFAULT 'ready',
    auth_header_env text DEFAULT NULL,
    service_labels jsonb DEFAULT '{}'::jsonb,
    service_source text DEFAULT 'manual',
    warren_deployment_id uuid DEFAULT NULL,
    install_manifest jsonb DEFAULT '{}'::jsonb,
    health jsonb DEFAULT '{}'::jsonb,
    set_default boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(service_name), '');
    normalized_provider text := coalesce(nullif(btrim(service_provider), ''), 'hindsight');
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_status text := coalesce(nullif(btrim(service_status), ''), 'ready');
    normalized_auth_env text := nullif(btrim(auth_header_env), '');
    normalized_source text := coalesce(nullif(btrim(service_source), ''), 'manual');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_memory_service_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: service_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: service_name must be an identifier-like name';
    END IF;
    IF normalized_provider !~ '^[A-Za-z_][A-Za-z0-9_\\-]*$' THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: service_provider must be identifier-like';
    END IF;
    IF normalized_endpoint IS NULL OR normalized_endpoint !~ '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: endpoint_url must be an http(s) URL';
    END IF;
    IF normalized_status NOT IN ('starting', 'ready', 'failed', 'disabled') THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: unsupported status "%"', service_status;
    END IF;
    IF jsonb_typeof(coalesce(service_labels, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: service_labels must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(install_manifest, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: install_manifest must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(health, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_memory_service: health must be a JSON object';
    END IF;

    INSERT INTO rvbbit.memory_services
        (name, provider, endpoint_url, status, auth_header_env, labels,
         service_source, warren_deployment_id, install_manifest, health)
    VALUES
        (normalized_name, normalized_provider, normalized_endpoint, normalized_status,
         normalized_auth_env, coalesce(service_labels, '{}'::jsonb), normalized_source,
         register_memory_service.warren_deployment_id,
         coalesce(install_manifest, '{}'::jsonb), coalesce(health, '{}'::jsonb))
    ON CONFLICT (name) DO UPDATE SET
        provider = EXCLUDED.provider,
        endpoint_url = EXCLUDED.endpoint_url,
        status = EXCLUDED.status,
        auth_header_env = EXCLUDED.auth_header_env,
        labels = EXCLUDED.labels,
        service_source = EXCLUDED.service_source,
        warren_deployment_id = EXCLUDED.warren_deployment_id,
        install_manifest = EXCLUDED.install_manifest,
        health = EXCLUDED.health;

    IF coalesce(set_default, true) AND normalized_provider = 'hindsight' AND normalized_status = 'ready' THEN
        INSERT INTO rvbbit.settings (key, value, updated_at)
        VALUES ('default_hindsight_service', to_jsonb(normalized_name), clock_timestamp())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = clock_timestamp();
    END IF;

    SELECT to_jsonb(s) INTO row_doc FROM rvbbit.memory_services s WHERE s.name = normalized_name;
    RETURN row_doc;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.memory_service(service_name text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    normalized_name text := nullif(btrim(service_name), '');
    default_name text;
    row_doc jsonb;
BEGIN
    SELECT value #>> '{}' INTO default_name
    FROM rvbbit.settings
    WHERE key = 'default_hindsight_service';

    IF normalized_name IS NOT NULL THEN
        SELECT to_jsonb(s) INTO row_doc
        FROM rvbbit.memory_services s
        WHERE s.name = normalized_name;
    ELSE
        SELECT to_jsonb(s) INTO row_doc
        FROM rvbbit.memory_services s
        WHERE s.provider = 'hindsight'
          AND s.status = 'ready'
        ORDER BY (s.name = coalesce(default_name, 'hindsight_default')) DESC,
                 s.updated_at DESC,
                 s.name
        LIMIT 1;
    END IF;

    RETURN row_doc;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.hindsight_service(service_name text DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    SELECT rvbbit.memory_service(coalesce(nullif(btrim(service_name), ''), NULL))
$$;

CREATE OR REPLACE FUNCTION rvbbit.hindsight_embedding_env(backend_name text DEFAULT 'embed')
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    normalized_backend text := coalesce(nullif(btrim(backend_name), ''), 'embed');
    src rvbbit.backends%ROWTYPE;
    provider text;
    model text;
    endpoint text;
    base_url text;
    auth_ref text;
    dims text;
    env jsonb := '{}'::jsonb;
    reason text := NULL;
    compatible boolean := false;
BEGIN
    SELECT * INTO src
    FROM rvbbit.backends
    WHERE name = normalized_backend;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'compatible', false,
            'reason', format('backend "%s" is not registered', normalized_backend),
            'env', '{}'::jsonb
        );
    END IF;

    model := nullif(coalesce(src.source_model, src.transport_opts->>'model'), '');
    endpoint := nullif(btrim(coalesce(src.endpoint_url, '')), '');
    dims := nullif(src.transport_opts->>'dimensions', '');
    IF src.auth_header_env IS NOT NULL AND btrim(src.auth_header_env) <> '' THEN
        auth_ref := '${' || btrim(src.auth_header_env) || ':-}';
    END IF;

    IF src.transport = 'openai' THEN
        IF model IS NULL THEN
            reason := format('backend "%s" has openai transport but no source_model/transport_opts.model', normalized_backend);
        ELSIF lower(coalesce(src.source_provider, '') || ' ' || coalesce(src.auth_header_env, '') || ' ' || coalesce(endpoint, '')) LIKE '%openrouter%' THEN
            provider := 'openrouter';
            compatible := true;
            env := jsonb_build_object(
                'HINDSIGHT_API_EMBEDDINGS_PROVIDER', provider,
                'HINDSIGHT_API_EMBEDDINGS_OPENROUTER_MODEL', model
            );
            IF auth_ref IS NOT NULL THEN
                env := env || jsonb_build_object('HINDSIGHT_API_EMBEDDINGS_OPENROUTER_API_KEY', auth_ref);
            END IF;
        ELSE
            provider := 'openai';
            compatible := true;
            env := jsonb_build_object(
                'HINDSIGHT_API_EMBEDDINGS_PROVIDER', provider,
                'HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL', model
            );
            IF auth_ref IS NOT NULL THEN
                env := env || jsonb_build_object('HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY', auth_ref);
            END IF;
            IF endpoint IS NOT NULL
               AND endpoint !~* '^https://api\\.openai\\.com(/v1)?/embeddings/?$' THEN
                base_url := regexp_replace(endpoint, '/embeddings/?$', '', 'i');
                env := env || jsonb_build_object('HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL', base_url);
            END IF;
            IF dims IS NOT NULL THEN
                env := env || jsonb_build_object('HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS', dims);
            END IF;
        END IF;
    ELSE
        reason := format(
            'backend "%s" uses transport "%s"; Hindsight slim can only piggyback RVBBIT embedders exposed through a Hindsight-supported external provider',
            normalized_backend,
            src.transport
        );
    END IF;

    RETURN jsonb_build_object(
        'compatible', compatible,
        'reason', reason,
        'backend', normalized_backend,
        'source_backend', coalesce(src.install_manifest #>> '{rvbbit_default_embedder,source_backend}', src.name),
        'source_model', model,
        'source_transport', src.transport,
        'provider', provider,
        'env', env
    );
END
$$;
