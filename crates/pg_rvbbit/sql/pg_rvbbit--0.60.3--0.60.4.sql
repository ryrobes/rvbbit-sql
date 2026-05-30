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

CREATE TABLE IF NOT EXISTS rvbbit.capability_catalog (
    id                 text PRIMARY KEY,
    manifest_path      text,
    name               text NOT NULL,
    title              text NOT NULL,
    description        text,
    tags               text[] NOT NULL DEFAULT ARRAY[]::text[],
    kind               text NOT NULL,
    license            text,
    source_provider    text,
    source_model       text,
    source_revision    text,
    backend_name       text,
    backend_transport  text,
    runtime_name       text,
    runtime_language   text,
    runtime_template   text,
    runtime_handler    text,
    endpoint_path      text,
    device             text,
    operators          text[] NOT NULL DEFAULT ARRAY[]::text[],
    manifest           jsonb NOT NULL,
    catalog_entry      jsonb NOT NULL DEFAULT '{}'::jsonb,
    catalog_source     text NOT NULL DEFAULT 'manual',
    active             boolean NOT NULL DEFAULT true,
    created_by         oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT capability_catalog_id_check CHECK (id ~ '^[A-Za-z0-9_./-]+$'),
    CONSTRAINT capability_catalog_kind_check CHECK (kind <> ''),
    CONSTRAINT capability_catalog_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT capability_catalog_entry_is_object CHECK (jsonb_typeof(catalog_entry) = 'object')
);

CREATE INDEX IF NOT EXISTS capability_catalog_active_idx ON rvbbit.capability_catalog (active, kind, name);
CREATE INDEX IF NOT EXISTS capability_catalog_tags_idx ON rvbbit.capability_catalog USING gin (tags);
CREATE INDEX IF NOT EXISTS capability_catalog_manifest_idx ON rvbbit.capability_catalog USING gin (manifest);
CREATE INDEX IF NOT EXISTS capability_catalog_backend_idx ON rvbbit.capability_catalog (backend_name)
    WHERE backend_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS capability_catalog_runtime_idx ON rvbbit.capability_catalog (runtime_name)
    WHERE runtime_name IS NOT NULL;

CREATE OR REPLACE FUNCTION rvbbit.touch_capability_catalog_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS capability_catalog_touch_updated_at ON rvbbit.capability_catalog;
CREATE TRIGGER capability_catalog_touch_updated_at
    BEFORE UPDATE ON rvbbit.capability_catalog
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_capability_catalog_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.require_capability_catalog_admin()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) THEN
        RAISE EXCEPTION 'rvbbit capability catalog changes require a superuser in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.upsert_capability_catalog_entry(
    catalog_entry jsonb,
    capability_manifest jsonb,
    catalog_source text DEFAULT 'curated',
    entry_active boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $ucc$
DECLARE
    normalized_entry jsonb := coalesce(catalog_entry, '{}'::jsonb);
    normalized_manifest jsonb := coalesce(capability_manifest, '{}'::jsonb);
    normalized_id text;
    normalized_source text := coalesce(nullif(btrim(catalog_source), ''), 'curated');
    entry_tags text[] := ARRAY[]::text[];
    entry_operators text[] := ARRAY[]::text[];
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF jsonb_typeof(normalized_entry) <> 'object' THEN
        RAISE EXCEPTION 'catalog_entry must be a JSON object';
    END IF;
    IF jsonb_typeof(normalized_manifest) <> 'object' THEN
        RAISE EXCEPTION 'capability_manifest must be a JSON object';
    END IF;

    normalized_id := nullif(btrim(coalesce(
        normalized_entry->>'id',
        normalized_entry->>'manifest_path',
        normalized_manifest->>'name'
    )), '');
    IF normalized_id IS NULL THEN
        RAISE EXCEPTION 'catalog entry id is required';
    END IF;
    IF normalized_id !~ '^[A-Za-z0-9_./-]+$' THEN
        RAISE EXCEPTION 'catalog entry id contains unsupported characters: %', normalized_id;
    END IF;

    IF jsonb_typeof(coalesce(normalized_entry->'tags', '[]'::jsonb)) = 'array' THEN
        SELECT coalesce(array_agg(tag ORDER BY tag), ARRAY[]::text[])
        INTO entry_tags
        FROM jsonb_array_elements_text(normalized_entry->'tags') AS t(tag);
    END IF;

    IF jsonb_typeof(coalesce(normalized_entry->'operators', '[]'::jsonb)) = 'array' THEN
        SELECT coalesce(array_agg(op ORDER BY op), ARRAY[]::text[])
        INTO entry_operators
        FROM jsonb_array_elements_text(normalized_entry->'operators') AS o(op);
    END IF;

    INSERT INTO rvbbit.capability_catalog
        (id, manifest_path, name, title, description, tags, kind, license,
         source_provider, source_model, source_revision,
         backend_name, backend_transport,
         runtime_name, runtime_language, runtime_template, runtime_handler,
         endpoint_path, device, operators, manifest, catalog_entry,
         catalog_source, active)
    VALUES
        (normalized_id,
         nullif(normalized_entry->>'manifest_path', ''),
         coalesce(nullif(normalized_entry->>'name', ''), normalized_manifest->>'name', normalized_id),
         coalesce(nullif(normalized_entry->>'title', ''), normalized_manifest->>'title',
                  normalized_manifest->>'name', normalized_id),
         coalesce(normalized_entry->>'description', normalized_manifest->>'description'),
         entry_tags,
         coalesce(nullif(normalized_entry->>'kind', ''), normalized_manifest->>'kind', 'unknown'),
         coalesce(normalized_entry->>'license', normalized_manifest->>'license'),
         coalesce(normalized_entry->>'source_provider', normalized_manifest #>> '{source,provider}'),
         coalesce(normalized_entry->>'source_model', normalized_manifest #>> '{source,model}'),
         coalesce(normalized_entry->>'source_revision', normalized_manifest #>> '{source,revision}'),
         coalesce(normalized_entry->>'backend_name', normalized_manifest #>> '{backend,name}'),
         coalesce(normalized_entry->>'backend_transport', normalized_manifest #>> '{backend,transport}'),
         coalesce(normalized_entry->>'runtime_name', normalized_manifest #>> '{runtime_registration,name}'),
         coalesce(normalized_entry->>'runtime_language', normalized_manifest #>> '{runtime_registration,language}',
                  normalized_manifest #>> '{runtime,language}'),
         coalesce(normalized_entry->>'runtime_template', normalized_manifest #>> '{runtime,template}'),
         coalesce(normalized_entry->>'runtime_handler', normalized_manifest #>> '{runtime,handler}'),
         coalesce(normalized_entry->>'endpoint_path', normalized_manifest #>> '{warren,endpoint_path}',
                  normalized_manifest #>> '{runtime_registration,endpoint_path}'),
         coalesce(normalized_entry->>'device', normalized_manifest #>> '{runtime,device}'),
         entry_operators,
         normalized_manifest,
         normalized_entry,
         normalized_source,
         coalesce(entry_active, true))
    ON CONFLICT (id) DO UPDATE SET
        manifest_path = EXCLUDED.manifest_path,
        name = EXCLUDED.name,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        tags = EXCLUDED.tags,
        kind = EXCLUDED.kind,
        license = EXCLUDED.license,
        source_provider = EXCLUDED.source_provider,
        source_model = EXCLUDED.source_model,
        source_revision = EXCLUDED.source_revision,
        backend_name = EXCLUDED.backend_name,
        backend_transport = EXCLUDED.backend_transport,
        runtime_name = EXCLUDED.runtime_name,
        runtime_language = EXCLUDED.runtime_language,
        runtime_template = EXCLUDED.runtime_template,
        runtime_handler = EXCLUDED.runtime_handler,
        endpoint_path = EXCLUDED.endpoint_path,
        device = EXCLUDED.device,
        operators = EXCLUDED.operators,
        manifest = EXCLUDED.manifest,
        catalog_entry = EXCLUDED.catalog_entry,
        catalog_source = EXCLUDED.catalog_source,
        active = EXCLUDED.active;

    SELECT to_jsonb(c) INTO row_doc
    FROM rvbbit.capability_catalog c
    WHERE c.id = normalized_id;
    RETURN row_doc;
END
$ucc$;

CREATE OR REPLACE FUNCTION rvbbit.prune_capability_catalog(
    catalog_source text DEFAULT 'curated',
    keep_ids text[] DEFAULT ARRAY[]::text[]
) RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $pcc$
DECLARE
    affected integer;
    normalized_source text := coalesce(nullif(btrim(catalog_source), ''), 'curated');
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    UPDATE rvbbit.capability_catalog c
    SET active = false
    WHERE c.catalog_source = normalized_source
      AND NOT (c.id = ANY(coalesce(keep_ids, ARRAY[]::text[])))
      AND c.active;
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END
$pcc$;

CREATE OR REPLACE FUNCTION rvbbit.deploy_catalog_capability(
    catalog_id      text,
    target_selector jsonb DEFAULT '{}'::jsonb,
    job_name        text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $dcc$
DECLARE
    catalog_manifest jsonb;
    catalog_name text;
    catalog_backend_name text;
    catalog_runtime_name text;
    catalog_operator_name text;
    queued_job_id uuid;
BEGIN
    IF catalog_id IS NULL OR btrim(catalog_id) = '' THEN
        RAISE EXCEPTION 'catalog_id is required';
    END IF;
    IF jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;

    SELECT manifest, name, backend_name, runtime_name, operators[1]
    INTO catalog_manifest, catalog_name, catalog_backend_name, catalog_runtime_name, catalog_operator_name
    FROM rvbbit.capability_catalog
    WHERE id = btrim(catalog_id)
      AND active;

    IF catalog_manifest IS NULL THEN
        RAISE EXCEPTION 'active capability catalog entry % not found', catalog_id;
    END IF;

    queued_job_id := rvbbit.deploy_capability(
        catalog_manifest,
        target_selector,
        coalesce(job_name, catalog_name)
    );
    UPDATE rvbbit.warren_jobs AS j
    SET backend_name = coalesce(catalog_backend_name, j.backend_name),
        runtime_name = coalesce(catalog_runtime_name, j.runtime_name),
        operator_name = coalesce(catalog_operator_name, j.operator_name)
    WHERE j.job_id = queued_job_id;
    RETURN queued_job_id;
END
$dcc$;

CREATE OR REPLACE FUNCTION rvbbit.seed_capability_catalog()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'seed_capability_catalog_wrapper'
LANGUAGE c VOLATILE STRICT;

SELECT rvbbit.seed_capability_catalog();

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
