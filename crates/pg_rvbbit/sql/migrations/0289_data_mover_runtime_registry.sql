-- 0289: register HTTP data-mover sidecars without pretending they are Python
-- execution runtimes.
--
-- Warren already gives every runtime sidecar a stable, network-reachable
-- endpoint.  dlt mirror workers use that same lifecycle but expose a typed
-- control API rather than the Python /run contract.  Keep them in the existing
-- runtime inventory so operators and capability smoke checks have one source
-- of truth, while preserving the language boundary used by callers.

ALTER TABLE rvbbit.python_runtimes
    DROP CONSTRAINT IF EXISTS python_runtimes_language_check;

ALTER TABLE rvbbit.python_runtimes
    ADD CONSTRAINT python_runtimes_language_check
    CHECK (language IN ('python', 'data_mover'));

CREATE OR REPLACE FUNCTION rvbbit.register_data_mover_runtime(
    runtime_name text,
    endpoint_url text,
    runtime_status text DEFAULT 'ready',
    runtime_labels jsonb DEFAULT '{}'::jsonb,
    runtime_source text DEFAULT 'manual',
    warren_deployment_id uuid DEFAULT NULL,
    install_manifest jsonb DEFAULT '{}'::jsonb,
    health jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $register_data_mover_runtime$
DECLARE
    normalized_name text := nullif(btrim(runtime_name), '');
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_status text := coalesce(nullif(btrim(runtime_status), ''), 'ready');
    normalized_source text := coalesce(nullif(btrim(runtime_source), ''), 'manual');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_name IS NULL
       OR normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.register_data_mover_runtime: invalid runtime name';
    END IF;
    IF normalized_endpoint IS NULL OR normalized_endpoint !~ '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.register_data_mover_runtime: endpoint_url must be an http(s) URL';
    END IF;
    IF normalized_status NOT IN ('starting', 'ready', 'failed', 'disabled') THEN
        RAISE EXCEPTION 'rvbbit.register_data_mover_runtime: unsupported status "%"', runtime_status;
    END IF;
    IF jsonb_typeof(coalesce(runtime_labels, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(coalesce(install_manifest, '{}'::jsonb)) <> 'object'
       OR jsonb_typeof(coalesce(health, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_data_mover_runtime: labels, manifest, and health must be JSON objects';
    END IF;

    INSERT INTO rvbbit.python_runtimes (
        name, endpoint_url, language, status, labels, runtime_source,
        warren_deployment_id, install_manifest, health
    ) VALUES (
        normalized_name, normalized_endpoint, 'data_mover', normalized_status,
        coalesce(runtime_labels, '{}'::jsonb), normalized_source,
        register_data_mover_runtime.warren_deployment_id,
        coalesce(install_manifest, '{}'::jsonb), coalesce(health, '{}'::jsonb)
    )
    ON CONFLICT (name) DO UPDATE SET
        endpoint_url = EXCLUDED.endpoint_url,
        language = EXCLUDED.language,
        status = EXCLUDED.status,
        labels = EXCLUDED.labels,
        runtime_source = EXCLUDED.runtime_source,
        warren_deployment_id = EXCLUDED.warren_deployment_id,
        install_manifest = EXCLUDED.install_manifest,
        health = EXCLUDED.health;

    SELECT to_jsonb(r.*) INTO row_doc
    FROM rvbbit.python_runtimes r
    WHERE r.name = normalized_name;
    RETURN row_doc;
END
$register_data_mover_runtime$;

COMMENT ON FUNCTION rvbbit.register_data_mover_runtime(
    text, text, text, jsonb, text, uuid, jsonb, jsonb
) IS
    'Register a Warren-managed HTTP data mover such as the dlt mirror worker in the shared runtime inventory.';
