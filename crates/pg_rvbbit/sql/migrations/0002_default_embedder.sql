-- 0002_default_embedder
--
-- Existing databases may already have applied 0001 and may already be on a
-- version whose old upgrade edge did not carry rvbbit.set_default_embedder().
-- Keep this idempotent so deploys can safely run SELECT rvbbit.migrate().

CREATE OR REPLACE FUNCTION rvbbit.set_default_embedder(
    backend_name text,
    purge_cache boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized text := nullif(btrim(backend_name), '');
    src rvbbit.backends%ROWTYPE;
    purged bigint := 0;
    reloaded int := NULL;
    source_backend text;
    source_model text;
    source_manifest jsonb;
    default_meta jsonb;
    is_embedding_backend boolean;
BEGIN
    PERFORM rvbbit.require_mcp_gateway_admin();

    IF normalized IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_default_embedder: backend_name cannot be empty';
    END IF;

    SELECT * INTO src
    FROM rvbbit.backends
    WHERE name = normalized;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_default_embedder: backend "%" is not registered', normalized;
    END IF;

    is_embedding_backend :=
        src.transport IN ('local_embed', 'openai', 'stub')
        OR lower(coalesce(src.install_manifest #>> '{runtime,handler}', '')) = 'embedding'
        OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(coalesce(src.install_manifest, '{}'::jsonb)->'tags') = 'array'
                    THEN coalesce(src.install_manifest, '{}'::jsonb)->'tags'
                    ELSE '[]'::jsonb
                END
            ) AS tag(value)
            WHERE lower(tag.value) IN ('embedding', 'embeddings', 'retrieval')
        );

    IF NOT is_embedding_backend THEN
        RAISE EXCEPTION 'rvbbit.set_default_embedder: backend "%" uses transport "%" and is not marked as an embedding capability',
            normalized, src.transport;
    END IF;

    source_backend := CASE
        WHEN src.name = 'embed' THEN coalesce(nullif(src.install_manifest #>> '{rvbbit_default_embedder,source_backend}', ''), 'embed')
        ELSE src.name
    END;
    source_model := coalesce(src.source_model, src.transport_opts->>'model');
    default_meta := jsonb_build_object(
        'source_backend', source_backend,
        'source_model', source_model,
        'source_transport', src.transport,
        'set_at', clock_timestamp()
    );
    source_manifest := coalesce(src.install_manifest, '{}'::jsonb)
        || jsonb_build_object('rvbbit_default_embedder', default_meta);

    IF src.name <> 'embed' THEN
        INSERT INTO rvbbit.backends
            (name, transport, endpoint_url, batch_size, max_concurrent,
             timeout_ms, auth_header_env, transport_opts, description,
             source_provider, source_model, source_revision, install_manifest)
        VALUES
            ('embed', src.transport, src.endpoint_url, src.batch_size, src.max_concurrent,
             src.timeout_ms, src.auth_header_env, src.transport_opts,
             coalesce(src.description, 'Default embedding backend') || ' (default embedder alias for ' || src.name || ')',
             src.source_provider, src.source_model, src.source_revision, source_manifest)
        ON CONFLICT (name) DO UPDATE SET
            transport        = EXCLUDED.transport,
            endpoint_url     = EXCLUDED.endpoint_url,
            batch_size       = EXCLUDED.batch_size,
            max_concurrent   = EXCLUDED.max_concurrent,
            timeout_ms       = EXCLUDED.timeout_ms,
            auth_header_env  = EXCLUDED.auth_header_env,
            transport_opts   = EXCLUDED.transport_opts,
            description      = EXCLUDED.description,
            source_provider  = EXCLUDED.source_provider,
            source_model     = EXCLUDED.source_model,
            source_revision  = EXCLUDED.source_revision,
            install_manifest = EXCLUDED.install_manifest;
    ELSE
        UPDATE rvbbit.backends
        SET install_manifest = source_manifest
        WHERE name = 'embed';
    END IF;

    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES (
        'default_embedder',
        jsonb_build_object(
            'backend', 'embed',
            'source_backend', source_backend,
            'source_model', source_model,
            'source_transport', src.transport
        ),
        clock_timestamp()
    )
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = clock_timestamp();

    IF purge_cache THEN
        SELECT rvbbit.embedding_purge('embed') INTO purged;
    END IF;

    BEGIN
        SELECT rvbbit.reload_backends() INTO reloaded;
    EXCEPTION WHEN undefined_function THEN
        reloaded := NULL;
    END;

    RETURN jsonb_build_object(
        'default_embedder', 'embed',
        'source_backend', source_backend,
        'source_model', source_model,
        'transport', src.transport,
        'endpoint_url', src.endpoint_url,
        'purged_entries', purged,
        'reloaded_backends', reloaded
    );
END
$$;
