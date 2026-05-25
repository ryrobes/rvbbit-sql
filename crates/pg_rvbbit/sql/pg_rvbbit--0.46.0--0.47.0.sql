-- pg_rvbbit 0.46.0 -> 0.47.0
-- Warren deployment control plane for remote capability/model hosts.

CREATE TABLE IF NOT EXISTS rvbbit.warren_nodes (
    node_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL UNIQUE,
    base_url         text,
    labels           jsonb NOT NULL DEFAULT '{}'::jsonb,
    capacity         jsonb NOT NULL DEFAULT '{}'::jsonb,
    inventory        jsonb NOT NULL DEFAULT '[]'::jsonb,
    status           text NOT NULL DEFAULT 'registered',
    version          text,
    shared_key_hash  text,
    auth_config      jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_heartbeat   timestamptz,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT warren_nodes_status_check CHECK (
        status IN ('registered', 'ready', 'busy', 'draining', 'offline', 'error')
    ),
    CONSTRAINT warren_nodes_labels_is_object CHECK (jsonb_typeof(labels) = 'object'),
    CONSTRAINT warren_nodes_capacity_is_object CHECK (jsonb_typeof(capacity) = 'object'),
    CONSTRAINT warren_nodes_inventory_is_array CHECK (jsonb_typeof(inventory) = 'array'),
    CONSTRAINT warren_nodes_auth_config_is_object CHECK (jsonb_typeof(auth_config) = 'object')
);

CREATE INDEX IF NOT EXISTS warren_nodes_status_idx
    ON rvbbit.warren_nodes (status, last_heartbeat DESC);
CREATE INDEX IF NOT EXISTS warren_nodes_labels_idx
    ON rvbbit.warren_nodes USING gin (labels);

CREATE OR REPLACE FUNCTION rvbbit.touch_warren_nodes_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS warren_nodes_touch_updated_at ON rvbbit.warren_nodes;
CREATE TRIGGER warren_nodes_touch_updated_at
    BEFORE UPDATE ON rvbbit.warren_nodes
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_warren_nodes_updated_at();

CREATE TABLE IF NOT EXISTS rvbbit.warren_jobs (
    job_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL,
    desired_state    text NOT NULL DEFAULT 'running',
    name             text NOT NULL,
    manifest         jsonb NOT NULL,
    target_selector  jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'queued',
    claimed_by       text,
    claimed_at       timestamptz,
    attempts         int NOT NULL DEFAULT 0,
    endpoint_url     text,
    backend_name     text,
    operator_name    text,
    error            text,
    logs             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CONSTRAINT warren_jobs_kind_check CHECK (
        kind IN ('capability', 'trained_model', 'mcp_server', 'compose', 'custom')
    ),
    CONSTRAINT warren_jobs_desired_state_check CHECK (
        desired_state IN ('running', 'stopped', 'removed')
    ),
    CONSTRAINT warren_jobs_status_check CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT warren_jobs_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT warren_jobs_target_selector_is_object CHECK (jsonb_typeof(target_selector) = 'object'),
    CONSTRAINT warren_jobs_logs_is_object CHECK (jsonb_typeof(logs) = 'object')
);

CREATE INDEX IF NOT EXISTS warren_jobs_queue_idx
    ON rvbbit.warren_jobs (status, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS warren_jobs_target_selector_idx
    ON rvbbit.warren_jobs USING gin (target_selector);

CREATE TABLE IF NOT EXISTS rvbbit.warren_deployments (
    deployment_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           uuid UNIQUE REFERENCES rvbbit.warren_jobs(job_id) ON DELETE SET NULL,
    node_id          uuid REFERENCES rvbbit.warren_nodes(node_id) ON DELETE SET NULL,
    node_name        text NOT NULL,
    kind             text NOT NULL,
    name             text NOT NULL,
    status           text NOT NULL DEFAULT 'running',
    endpoint_url     text,
    backend_name     text,
    operator_name    text,
    manifest         jsonb NOT NULL DEFAULT '{}'::jsonb,
    compose_project  text,
    work_dir         text,
    health           jsonb NOT NULL DEFAULT '{}'::jsonb,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    stopped_at       timestamptz,
    CONSTRAINT warren_deployments_kind_check CHECK (
        kind IN ('capability', 'trained_model', 'mcp_server', 'compose', 'custom')
    ),
    CONSTRAINT warren_deployments_status_check CHECK (
        status IN ('starting', 'running', 'stopped', 'failed', 'removed')
    ),
    CONSTRAINT warren_deployments_manifest_is_object CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT warren_deployments_health_is_object CHECK (jsonb_typeof(health) = 'object')
);

CREATE INDEX IF NOT EXISTS warren_deployments_node_idx
    ON rvbbit.warren_deployments (node_name, status);
CREATE INDEX IF NOT EXISTS warren_deployments_backend_idx
    ON rvbbit.warren_deployments (backend_name)
    WHERE backend_name IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.warren_node_metrics (
    metric_id             bigserial PRIMARY KEY,
    node_id               uuid REFERENCES rvbbit.warren_nodes(node_id) ON DELETE SET NULL,
    node_name             text NOT NULL,
    collected_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    metrics               jsonb NOT NULL,
    cpu_pct               double precision,
    load1                 double precision,
    load5                 double precision,
    load15                double precision,
    mem_used_bytes        bigint,
    mem_total_bytes       bigint,
    gpu_count             int,
    gpu_util_pct          double precision,
    gpu_mem_used_bytes    bigint,
    gpu_mem_total_bytes   bigint,
    CONSTRAINT warren_node_metrics_metrics_is_object CHECK (jsonb_typeof(metrics) = 'object')
);

CREATE INDEX IF NOT EXISTS warren_node_metrics_node_time_idx
    ON rvbbit.warren_node_metrics (node_name, collected_at DESC);
CREATE INDEX IF NOT EXISTS warren_node_metrics_collected_at_idx
    ON rvbbit.warren_node_metrics (collected_at);

CREATE OR REPLACE VIEW rvbbit.warren_node_latest_metrics AS
SELECT DISTINCT ON (node_name)
    metric_id,
    node_id,
    node_name,
    collected_at,
    metrics,
    cpu_pct,
    load1,
    load5,
    load15,
    mem_used_bytes,
    mem_total_bytes,
    gpu_count,
    gpu_util_pct,
    gpu_mem_used_bytes,
    gpu_mem_total_bytes
FROM rvbbit.warren_node_metrics
ORDER BY node_name, collected_at DESC;

CREATE OR REPLACE FUNCTION rvbbit.touch_warren_deployments_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS warren_deployments_touch_updated_at ON rvbbit.warren_deployments;
CREATE TRIGGER warren_deployments_touch_updated_at
    BEFORE UPDATE ON rvbbit.warren_deployments
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_warren_deployments_updated_at();

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
    d.health,
    d.error,
    d.updated_at AS deployment_updated_at
FROM rvbbit.warren_nodes n
LEFT JOIN rvbbit.warren_node_latest_metrics lm
  ON lm.node_id = n.node_id
LEFT JOIN rvbbit.warren_deployments d
  ON d.node_id = n.node_id
 AND d.status IN ('starting', 'running', 'failed');

CREATE OR REPLACE FUNCTION rvbbit.register_warren_node(
    node_name        text,
    node_base_url    text DEFAULT NULL,
    node_labels      jsonb DEFAULT '{}'::jsonb,
    node_capacity    jsonb DEFAULT '{}'::jsonb,
    node_version     text DEFAULT NULL,
    node_shared_key_hash text DEFAULT NULL,
    node_auth_config jsonb DEFAULT '{}'::jsonb
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $rwn$
DECLARE
    actual_node_id uuid;
BEGIN
    IF node_name IS NULL OR btrim(node_name) = '' THEN
        RAISE EXCEPTION 'node_name is required';
    END IF;
    IF jsonb_typeof(node_labels) <> 'object' THEN
        RAISE EXCEPTION 'node_labels must be a JSON object';
    END IF;
    IF jsonb_typeof(node_capacity) <> 'object' THEN
        RAISE EXCEPTION 'node_capacity must be a JSON object';
    END IF;
    IF jsonb_typeof(node_auth_config) <> 'object' THEN
        RAISE EXCEPTION 'node_auth_config must be a JSON object';
    END IF;

    INSERT INTO rvbbit.warren_nodes
        (name, base_url, labels, capacity, version, shared_key_hash,
         auth_config, status, last_heartbeat)
    VALUES
        (node_name, node_base_url, node_labels, node_capacity, node_version,
         node_shared_key_hash, node_auth_config, 'ready', clock_timestamp())
    ON CONFLICT (name) DO UPDATE SET
        base_url = COALESCE(EXCLUDED.base_url, rvbbit.warren_nodes.base_url),
        labels = EXCLUDED.labels,
        capacity = EXCLUDED.capacity,
        version = COALESCE(EXCLUDED.version, rvbbit.warren_nodes.version),
        shared_key_hash = COALESCE(EXCLUDED.shared_key_hash, rvbbit.warren_nodes.shared_key_hash),
        auth_config = EXCLUDED.auth_config,
        status = 'ready',
        last_heartbeat = clock_timestamp()
    RETURNING node_id INTO actual_node_id;

    RETURN actual_node_id;
END
$rwn$;

CREATE OR REPLACE FUNCTION rvbbit.warren_heartbeat(
    node_name      text,
    node_status    text DEFAULT 'ready',
    node_labels    jsonb DEFAULT NULL,
    node_capacity  jsonb DEFAULT NULL,
    node_inventory jsonb DEFAULT NULL,
    node_version   text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $whb$
BEGIN
    UPDATE rvbbit.warren_nodes
    SET status = node_status,
        labels = COALESCE(node_labels, labels),
        capacity = COALESCE(node_capacity, capacity),
        inventory = COALESCE(node_inventory, inventory),
        version = COALESCE(node_version, version),
        last_heartbeat = clock_timestamp()
    WHERE name = node_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;
END
$whb$;

CREATE OR REPLACE FUNCTION rvbbit.record_warren_metrics(
    node_name  text,
    metric_doc jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $rwm$
DECLARE
    actual_node_id uuid;
BEGIN
    IF jsonb_typeof(metric_doc) <> 'object' THEN
        RAISE EXCEPTION 'metric_doc must be a JSON object';
    END IF;

    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = record_warren_metrics.node_name;

    IF actual_node_id IS NULL THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;

    INSERT INTO rvbbit.warren_node_metrics
        (node_id, node_name, metrics, cpu_pct, load1, load5, load15,
         mem_used_bytes, mem_total_bytes, gpu_count, gpu_util_pct,
         gpu_mem_used_bytes, gpu_mem_total_bytes)
    VALUES
        (actual_node_id,
         record_warren_metrics.node_name,
         metric_doc,
         NULLIF(metric_doc #>> '{system,cpu,usage_pct}', '')::double precision,
         NULLIF(metric_doc #>> '{system,load1}', '')::double precision,
         NULLIF(metric_doc #>> '{system,load5}', '')::double precision,
         NULLIF(metric_doc #>> '{system,load15}', '')::double precision,
         NULLIF(metric_doc #>> '{system,memory,used_bytes}', '')::bigint,
         NULLIF(metric_doc #>> '{system,memory,total_bytes}', '')::bigint,
         NULLIF(metric_doc #>> '{summary,gpu_count}', '')::int,
         NULLIF(metric_doc #>> '{summary,gpu_util_pct}', '')::double precision,
         NULLIF(metric_doc #>> '{summary,gpu_mem_used_bytes}', '')::bigint,
         NULLIF(metric_doc #>> '{summary,gpu_mem_total_bytes}', '')::bigint);

    IF jsonb_typeof(metric_doc->'gpus') = 'array' THEN
        UPDATE rvbbit.warren_nodes
        SET inventory = metric_doc->'gpus'
        WHERE node_id = actual_node_id;
    END IF;
END
$rwm$;

CREATE OR REPLACE FUNCTION rvbbit.prune_warren_metrics(
    retain interval DEFAULT '7 days'::interval
) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
AS $pwm$
DECLARE
    deleted_rows bigint;
BEGIN
    IF retain IS NULL OR retain <= interval '0 seconds' THEN
        RAISE EXCEPTION 'retain must be a positive interval';
    END IF;

    DELETE FROM rvbbit.warren_node_metrics
    WHERE collected_at < clock_timestamp() - retain;

    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    RETURN deleted_rows;
END
$pwm$;

CREATE OR REPLACE FUNCTION rvbbit.enqueue_warren_job(
    job_kind        text,
    job_name        text,
    job_manifest    jsonb,
    target_selector jsonb DEFAULT '{}'::jsonb,
    desired_state   text DEFAULT 'running'
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $ewj$
DECLARE
    actual_job_id uuid;
BEGIN
    IF job_name IS NULL OR btrim(job_name) = '' THEN
        RAISE EXCEPTION 'job_name is required';
    END IF;
    IF jsonb_typeof(job_manifest) <> 'object' THEN
        RAISE EXCEPTION 'job_manifest must be a JSON object';
    END IF;
    IF jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;

    INSERT INTO rvbbit.warren_jobs
        (kind, desired_state, name, manifest, target_selector)
    VALUES
        (job_kind, desired_state, job_name, job_manifest, target_selector)
    RETURNING job_id INTO actual_job_id;

    RETURN actual_job_id;
END
$ewj$;

CREATE OR REPLACE FUNCTION rvbbit.deploy_capability(
    capability_manifest jsonb,
    target_selector     jsonb DEFAULT '{}'::jsonb,
    job_name            text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $dcap$
DECLARE
    actual_name text;
BEGIN
    IF jsonb_typeof(capability_manifest) <> 'object' THEN
        RAISE EXCEPTION 'capability_manifest must be a JSON object';
    END IF;
    actual_name := COALESCE(job_name, capability_manifest->>'name');
    IF actual_name IS NULL OR btrim(actual_name) = '' THEN
        actual_name := 'capability_' || substr(md5(capability_manifest::text), 1, 12);
    END IF;

    RETURN rvbbit.enqueue_warren_job(
        'capability',
        actual_name,
        capability_manifest,
        target_selector,
        'running'
    );
END
$dcap$;

CREATE OR REPLACE FUNCTION rvbbit.claim_warren_job(
    node_name text
) RETURNS TABLE (
    job_id uuid,
    kind text,
    desired_state text,
    name text,
    manifest jsonb,
    target_selector jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $cwj$
BEGIN
    RETURN QUERY
    WITH node AS (
        SELECT n.node_id, n.name, n.labels
        FROM rvbbit.warren_nodes n
        WHERE n.name = claim_warren_job.node_name
          AND n.status IN ('ready', 'busy')
    ),
    picked AS (
        SELECT j.job_id
        FROM rvbbit.warren_jobs j
        CROSS JOIN node n
        WHERE j.status = 'queued'
          AND n.labels @> j.target_selector
        ORDER BY j.created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    updated AS (
        UPDATE rvbbit.warren_jobs j
        SET status = 'running',
            claimed_by = claim_warren_job.node_name,
            claimed_at = clock_timestamp(),
            started_at = COALESCE(started_at, clock_timestamp()),
            attempts = attempts + 1
        FROM picked
        WHERE j.job_id = picked.job_id
        RETURNING j.job_id, j.kind, j.desired_state, j.name, j.manifest,
                  j.target_selector
    )
    SELECT u.job_id, u.kind, u.desired_state, u.name, u.manifest,
           u.target_selector
    FROM updated u;
END
$cwj$;

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
    logs              jsonb DEFAULT '{}'::jsonb
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
        logs = complete_warren_job.logs,
        error = NULL,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    INSERT INTO rvbbit.warren_deployments
        (job_id, node_id, node_name, kind, name, status, endpoint_url,
         backend_name, operator_name, manifest, compose_project, work_dir,
         health, error)
    VALUES
        (complete_warren_job.job_id, actual_node_id, complete_warren_job.node_name,
         actual_kind, actual_name, deployment_status, endpoint_url,
         backend_name, operator_name, deploy_manifest, compose_project, work_dir,
         health, NULL)
    ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
        node_id = EXCLUDED.node_id,
        node_name = EXCLUDED.node_name,
        status = EXCLUDED.status,
        endpoint_url = EXCLUDED.endpoint_url,
        backend_name = EXCLUDED.backend_name,
        operator_name = EXCLUDED.operator_name,
        manifest = EXCLUDED.manifest,
        compose_project = EXCLUDED.compose_project,
        work_dir = EXCLUDED.work_dir,
        health = EXCLUDED.health,
        error = NULL;
END
$cwjd$;

CREATE OR REPLACE FUNCTION rvbbit.fail_warren_job(
    job_id uuid,
    node_name text,
    error text,
    logs jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $fwj$
DECLARE
    actual_node_id uuid;
    actual_kind text;
    actual_name text;
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = fail_warren_job.node_name;

    SELECT kind, name INTO actual_kind, actual_name
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    UPDATE rvbbit.warren_jobs
    SET status = 'failed',
        error = fail_warren_job.error,
        logs = fail_warren_job.logs,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    IF actual_kind IS NOT NULL THEN
        INSERT INTO rvbbit.warren_deployments
            (job_id, node_id, node_name, kind, name, status, manifest, error,
             health)
        VALUES
            (fail_warren_job.job_id, actual_node_id, fail_warren_job.node_name,
             actual_kind, actual_name, 'failed', '{}'::jsonb,
             fail_warren_job.error, fail_warren_job.logs)
        ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
            status = 'failed',
            error = EXCLUDED.error,
            health = EXCLUDED.health;
    END IF;
END
$fwj$;
