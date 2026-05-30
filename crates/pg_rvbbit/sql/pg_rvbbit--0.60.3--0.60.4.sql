-- pg_rvbbit 0.60.3 -> 0.60.4
-- Warren-managed Python runtime capabilities.

CREATE TABLE IF NOT EXISTS rvbbit.python_runtimes (
    name                  text PRIMARY KEY,
    endpoint_url          text NOT NULL,
    language              text NOT NULL DEFAULT 'python',
    status                text NOT NULL DEFAULT 'ready',
    labels                jsonb NOT NULL DEFAULT '{}'::jsonb,
    runtime_source        text NOT NULL DEFAULT 'manual',
    warren_deployment_id  uuid,
    install_manifest      jsonb NOT NULL DEFAULT '{}'::jsonb,
    health                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT python_runtimes_name_check CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT python_runtimes_language_check CHECK (language = 'python'),
    CONSTRAINT python_runtimes_status_check CHECK (
        status IN ('starting', 'ready', 'failed', 'disabled')
    ),
    CONSTRAINT python_runtimes_endpoint_check CHECK (endpoint_url ~ '^https?://'),
    CONSTRAINT python_runtimes_labels_is_object CHECK (jsonb_typeof(labels) = 'object'),
    CONSTRAINT python_runtimes_manifest_is_object CHECK (jsonb_typeof(install_manifest) = 'object'),
    CONSTRAINT python_runtimes_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

ALTER TABLE IF EXISTS rvbbit.python_envs
    ADD COLUMN IF NOT EXISTS runtime_name text;

CREATE OR REPLACE FUNCTION rvbbit.touch_python_runtimes_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_runtimes_touch_updated_at ON rvbbit.python_runtimes;
CREATE TRIGGER python_runtimes_touch_updated_at
    BEFORE UPDATE ON rvbbit.python_runtimes
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_python_runtimes_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.register_python_runtime(
    runtime_name text,
    endpoint_url text,
    runtime_status text DEFAULT 'ready',
    runtime_labels jsonb DEFAULT '{}'::jsonb,
    runtime_source text DEFAULT 'manual',
    warren_deployment_id uuid DEFAULT NULL,
    install_manifest jsonb DEFAULT '{}'::jsonb,
    health jsonb DEFAULT '{}'::jsonb,
    set_default boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(runtime_name), '');
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_status text := coalesce(nullif(btrim(runtime_status), ''), 'ready');
    normalized_source text := coalesce(nullif(btrim(runtime_source), ''), 'manual');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: runtime_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: runtime_name must be an identifier-like name';
    END IF;
    IF normalized_endpoint IS NULL OR normalized_endpoint !~ '^https?://' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: endpoint_url must be an http(s) URL';
    END IF;
    IF normalized_status NOT IN ('starting', 'ready', 'failed', 'disabled') THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: unsupported status "%"', runtime_status;
    END IF;
    IF jsonb_typeof(coalesce(runtime_labels, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: runtime_labels must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(install_manifest, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: install_manifest must be a JSON object';
    END IF;
    IF jsonb_typeof(coalesce(health, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.register_python_runtime: health must be a JSON object';
    END IF;

    INSERT INTO rvbbit.python_runtimes
        (name, endpoint_url, language, status, labels, runtime_source,
         warren_deployment_id, install_manifest, health)
    VALUES
        (normalized_name, normalized_endpoint, 'python', normalized_status,
         coalesce(runtime_labels, '{}'::jsonb), normalized_source,
         register_python_runtime.warren_deployment_id,
         coalesce(install_manifest, '{}'::jsonb), coalesce(health, '{}'::jsonb))
    ON CONFLICT (name) DO UPDATE SET
        endpoint_url = EXCLUDED.endpoint_url,
        status = EXCLUDED.status,
        labels = EXCLUDED.labels,
        runtime_source = EXCLUDED.runtime_source,
        warren_deployment_id = EXCLUDED.warren_deployment_id,
        install_manifest = EXCLUDED.install_manifest,
        health = EXCLUDED.health;

    IF coalesce(set_default, true) AND normalized_status = 'ready' THEN
        PERFORM rvbbit.set_python_runtime_endpoint(normalized_endpoint);
    ELSE
        BEGIN
            PERFORM rvbbit.reload_python_runtime();
        EXCEPTION WHEN undefined_function THEN
            NULL;
        END;
    END IF;

    SELECT to_jsonb(r) INTO row_doc FROM rvbbit.python_runtimes r WHERE r.name = normalized_name;
    RETURN row_doc;
END
$$;

DROP FUNCTION IF EXISTS rvbbit.create_python_env(text, text, text[], text, int);
CREATE OR REPLACE FUNCTION rvbbit.create_python_env(
    env_name text,
    python_version text DEFAULT '3.12',
    requirements text[] DEFAULT ARRAY[]::text[],
    endpoint_url text DEFAULT NULL,
    timeout_ms int DEFAULT 1000,
    runtime_name text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(env_name), '');
    normalized_version text := coalesce(nullif(btrim(python_version), ''), '3.12');
    normalized_requirements text[];
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    normalized_runtime text := nullif(btrim(runtime_name), '');
    resolved_runtime_endpoint text;
    computed_hash text;
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: env_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: env_name must be an identifier-like name';
    END IF;
    normalized_requirements := ARRAY(
        SELECT btrim(req)
        FROM unnest(coalesce(requirements, ARRAY[]::text[])) AS r(req)
        WHERE btrim(req) <> ''
        ORDER BY btrim(req)
    );
    IF EXISTS (
        SELECT 1 FROM unnest(normalized_requirements) AS r(req)
        WHERE req ~ E'[\\r\\n]'
    ) THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: requirements cannot contain newlines';
    END IF;
    IF normalized_runtime IS NOT NULL THEN
        IF normalized_endpoint IS NOT NULL THEN
            RAISE EXCEPTION 'rvbbit.create_python_env: pass endpoint_url or runtime_name, not both';
        END IF;
        SELECT r.endpoint_url INTO resolved_runtime_endpoint
        FROM rvbbit.python_runtimes r
        WHERE r.name = normalized_runtime
          AND r.status = 'ready';
        IF resolved_runtime_endpoint IS NULL THEN
            RAISE EXCEPTION 'rvbbit.create_python_env: python runtime "%" is not registered or ready',
                runtime_name;
        END IF;
    END IF;
    computed_hash := rvbbit.python_env_hash(normalized_version, normalized_requirements);

    INSERT INTO rvbbit.python_envs
        (name, runtime_name, python_version, requirements, env_hash, endpoint_url, timeout_ms,
         status, status_message)
    VALUES
        (normalized_name, normalized_runtime, normalized_version, normalized_requirements,
         computed_hash, normalized_endpoint,
         greatest(coalesce(timeout_ms, 1000), 1), 'registered', NULL)
    ON CONFLICT (name) DO UPDATE SET
        runtime_name = EXCLUDED.runtime_name,
        python_version = EXCLUDED.python_version,
        requirements = EXCLUDED.requirements,
        env_hash = EXCLUDED.env_hash,
        endpoint_url = EXCLUDED.endpoint_url,
        timeout_ms = EXCLUDED.timeout_ms,
        status = 'registered',
        status_message = NULL;

    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;

    SELECT to_jsonb(e) INTO row_doc FROM rvbbit.python_envs e WHERE e.name = normalized_name;
    RETURN row_doc;
END
$$;

ALTER TABLE IF EXISTS rvbbit.warren_jobs
    ADD COLUMN IF NOT EXISTS runtime_name text;

ALTER TABLE IF EXISTS rvbbit.warren_deployments
    ADD COLUMN IF NOT EXISTS runtime_name text;

DROP VIEW IF EXISTS rvbbit.warren_inventory;
CREATE OR REPLACE VIEW rvbbit.warren_inventory AS
SELECT
    n.node_id,
    n.name AS node_name,
    n.base_url,
    n.labels,
    n.capacity,
    n.status AS node_status,
    n.version,
    n.last_heartbeat,
    lm.collected_at AS latest_metrics_at,
    lm.cpu_pct,
    lm.load1,
    lm.mem_used_bytes,
    lm.mem_total_bytes,
    lm.gpu_count,
    lm.gpu_util_pct,
    lm.gpu_mem_used_bytes,
    lm.gpu_mem_total_bytes,
    lm.metrics AS latest_metrics,
    d.deployment_id,
    d.kind,
    d.name AS deployment_name,
    d.status AS deployment_status,
    d.endpoint_url,
    d.backend_name,
    d.operator_name,
    d.runtime_name,
    d.health,
    d.error,
    d.updated_at AS deployment_updated_at
FROM rvbbit.warren_nodes n
LEFT JOIN rvbbit.warren_node_latest_metrics lm
  ON lm.node_id = n.node_id
LEFT JOIN rvbbit.warren_deployments d
  ON d.node_id = n.node_id
 AND d.status IN ('starting', 'running', 'failed');

DROP FUNCTION IF EXISTS rvbbit.complete_warren_job(
    uuid, text, text, text, text, text, jsonb, text, text, jsonb, jsonb
);
CREATE OR REPLACE FUNCTION rvbbit.complete_warren_job(
    job_id            uuid,
    node_name         text,
    deployment_status text DEFAULT 'running',
    endpoint_url      text DEFAULT NULL,
    backend_name      text DEFAULT NULL,
    operator_name     text DEFAULT NULL,
    deploy_manifest   jsonb DEFAULT '{}'::jsonb,
    compose_project   text DEFAULT NULL,
    work_dir          text DEFAULT NULL,
    health            jsonb DEFAULT '{}'::jsonb,
    logs              jsonb DEFAULT '{}'::jsonb,
    runtime_name      text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $cwjd$
DECLARE
    actual_node_id uuid;
    actual_kind text;
    actual_name text;
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = complete_warren_job.node_name;

    IF actual_node_id IS NULL THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;

    SELECT kind, name INTO actual_kind, actual_name
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    IF actual_kind IS NULL THEN
        RAISE EXCEPTION 'warren job % not found', job_id;
    END IF;

    UPDATE rvbbit.warren_jobs
    SET status = 'completed',
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        logs = complete_warren_job.logs,
        error = NULL,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    INSERT INTO rvbbit.warren_deployments
        (job_id, node_id, node_name, kind, name, status, endpoint_url,
         backend_name, operator_name, runtime_name, manifest, compose_project, work_dir,
         health, error)
    VALUES
        (complete_warren_job.job_id, actual_node_id, complete_warren_job.node_name,
         actual_kind, actual_name, deployment_status, endpoint_url,
         backend_name, operator_name, runtime_name, deploy_manifest, compose_project, work_dir,
         health, NULL)
    ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
        node_id = EXCLUDED.node_id,
        node_name = EXCLUDED.node_name,
        status = EXCLUDED.status,
        endpoint_url = EXCLUDED.endpoint_url,
        backend_name = EXCLUDED.backend_name,
        operator_name = EXCLUDED.operator_name,
        runtime_name = EXCLUDED.runtime_name,
        manifest = EXCLUDED.manifest,
        compose_project = EXCLUDED.compose_project,
        work_dir = EXCLUDED.work_dir,
        health = EXCLUDED.health,
        error = NULL;
END
$cwjd$;
