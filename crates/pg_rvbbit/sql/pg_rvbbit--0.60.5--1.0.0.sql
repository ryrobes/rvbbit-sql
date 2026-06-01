-- pg_rvbbit 0.60.5 -> 1.0.0
--
-- V1 pre-release cleanup: keep one supported upgrade path from the last dev
-- build to the release build. Fresh installs use pg_rvbbit--1.0.0.sql.

-- Warren install progress ---------------------------------------------------

ALTER TABLE IF EXISTS rvbbit.warren_jobs
    ADD COLUMN IF NOT EXISTS phase text,
    ADD COLUMN IF NOT EXISTS progress jsonb,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz;

UPDATE rvbbit.warren_jobs
SET phase = coalesce(
        nullif(phase, ''),
        CASE status
            WHEN 'queued' THEN 'queued'
            WHEN 'running' THEN 'running'
            WHEN 'completed' THEN 'ready'
            WHEN 'failed' THEN 'failed'
            ELSE status
        END
    ),
    progress = coalesce(progress, '{}'::jsonb),
    updated_at = coalesce(updated_at, clock_timestamp());

ALTER TABLE IF EXISTS rvbbit.warren_jobs
    ALTER COLUMN phase SET DEFAULT 'queued',
    ALTER COLUMN phase SET NOT NULL,
    ALTER COLUMN progress SET DEFAULT '{}'::jsonb,
    ALTER COLUMN progress SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT clock_timestamp(),
    ALTER COLUMN updated_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE connamespace = 'rvbbit'::regnamespace
          AND conrelid = 'rvbbit.warren_jobs'::regclass
          AND conname = 'warren_jobs_phase_check'
    ) THEN
        ALTER TABLE rvbbit.warren_jobs
            ADD CONSTRAINT warren_jobs_phase_check CHECK (phase <> '');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE connamespace = 'rvbbit'::regnamespace
          AND conrelid = 'rvbbit.warren_jobs'::regclass
          AND conname = 'warren_jobs_progress_is_object'
    ) THEN
        ALTER TABLE rvbbit.warren_jobs
            ADD CONSTRAINT warren_jobs_progress_is_object CHECK (jsonb_typeof(progress) = 'object');
    END IF;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.touch_warren_jobs_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS warren_jobs_touch_updated_at ON rvbbit.warren_jobs;
CREATE TRIGGER warren_jobs_touch_updated_at
    BEFORE UPDATE ON rvbbit.warren_jobs
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_warren_jobs_updated_at();

DO $$
BEGIN
    IF to_regclass('rvbbit.warren_deployments') IS NOT NULL THEN
        ALTER TABLE rvbbit.warren_deployments
            DROP CONSTRAINT IF EXISTS warren_deployments_status_check;
        ALTER TABLE rvbbit.warren_deployments
            ADD CONSTRAINT warren_deployments_status_check CHECK (
                status IN ('starting', 'running', 'stopping', 'stopped',
                           'failed', 'removed', 'drifted', 'orphaned')
            );
    END IF;
END $$;

WITH ranked_active_deployments AS (
    SELECT
        deployment_id,
        row_number() OVER (
            PARTITION BY node_name, kind, name
            ORDER BY updated_at DESC, created_at DESC, deployment_id DESC
        ) AS rn
    FROM rvbbit.warren_deployments
    WHERE status IN ('starting', 'running', 'stopping')
)
UPDATE rvbbit.warren_deployments AS d
SET status = 'removed',
    stopped_at = coalesce(d.stopped_at, clock_timestamp()),
    error = coalesce(d.error, 'superseded by newer deployment record')
FROM ranked_active_deployments AS r
WHERE d.deployment_id = r.deployment_id
  AND r.rn > 1;

DROP INDEX IF EXISTS rvbbit.warren_deployments_active_unique_idx;
CREATE UNIQUE INDEX warren_deployments_active_unique_idx
    ON rvbbit.warren_deployments (node_name, kind, name)
    WHERE status IN ('starting', 'running', 'stopping');

-- Warren GPU capacity and capability resource reservations ------------------

ALTER TABLE IF EXISTS rvbbit.capability_catalog
    ADD COLUMN IF NOT EXISTS resource_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS gpu_required boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS gpu_placement text,
    ADD COLUMN IF NOT EXISTS model_size_bytes bigint,
    ADD COLUMN IF NOT EXISTS vram_required_bytes bigint,
    ADD COLUMN IF NOT EXISTS vram_headroom_pct numeric;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE connamespace = 'rvbbit'::regnamespace
          AND conrelid = 'rvbbit.capability_catalog'::regclass
          AND conname = 'capability_catalog_resource_profile_is_object'
    ) THEN
        ALTER TABLE rvbbit.capability_catalog
            ADD CONSTRAINT capability_catalog_resource_profile_is_object CHECK (jsonb_typeof(resource_profile) = 'object');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE connamespace = 'rvbbit'::regnamespace
          AND conrelid = 'rvbbit.capability_catalog'::regclass
          AND conname = 'capability_catalog_model_size_nonnegative'
    ) THEN
        ALTER TABLE rvbbit.capability_catalog
            ADD CONSTRAINT capability_catalog_model_size_nonnegative CHECK (model_size_bytes IS NULL OR model_size_bytes >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE connamespace = 'rvbbit'::regnamespace
          AND conrelid = 'rvbbit.capability_catalog'::regclass
          AND conname = 'capability_catalog_vram_required_nonnegative'
    ) THEN
        ALTER TABLE rvbbit.capability_catalog
            ADD CONSTRAINT capability_catalog_vram_required_nonnegative CHECK (vram_required_bytes IS NULL OR vram_required_bytes >= 0);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS capability_catalog_resource_idx ON rvbbit.capability_catalog
    (gpu_required, vram_required_bytes)
    WHERE gpu_required OR vram_required_bytes IS NOT NULL;

CREATE OR REPLACE FUNCTION rvbbit.normalize_capability_catalog_resources()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    resource_doc jsonb;
    gpu_doc jsonb;
BEGIN
    resource_doc := coalesce(
        CASE WHEN jsonb_typeof(NEW.resource_profile) = 'object'
                  AND NEW.resource_profile <> '{}'::jsonb
             THEN NEW.resource_profile END,
        CASE WHEN jsonb_typeof(NEW.catalog_entry->'resources') = 'object'
             THEN NEW.catalog_entry->'resources' END,
        CASE WHEN jsonb_typeof(NEW.manifest->'resources') = 'object'
             THEN NEW.manifest->'resources' END,
        '{}'::jsonb
    );
    gpu_doc := CASE
        WHEN jsonb_typeof(resource_doc->'gpu') = 'object' THEN resource_doc->'gpu'
        ELSE '{}'::jsonb
    END;

    NEW.resource_profile := resource_doc;
    NEW.gpu_required := CASE
        WHEN gpu_doc ? 'required' THEN coalesce((gpu_doc->>'required')::boolean, false)
        ELSE false
    END;
    NEW.gpu_placement := nullif(gpu_doc->>'placement', '');
    NEW.model_size_bytes := nullif(gpu_doc->>'model_size_bytes', '')::bigint;
    NEW.vram_required_bytes := nullif(gpu_doc->>'vram_required_bytes', '')::bigint;
    NEW.vram_headroom_pct := nullif(gpu_doc->>'headroom_pct', '')::numeric;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS capability_catalog_normalize_resources ON rvbbit.capability_catalog;
CREATE TRIGGER capability_catalog_normalize_resources
    BEFORE INSERT OR UPDATE ON rvbbit.capability_catalog
    FOR EACH ROW EXECUTE FUNCTION rvbbit.normalize_capability_catalog_resources();

CREATE OR REPLACE FUNCTION rvbbit.capability_gpu_required(
    capability_manifest jsonb
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN coalesce(NULLIF(capability_manifest #>> '{resources,gpu,required}', '')::boolean, false)
        OR coalesce(capability_manifest #>> '{runtime,device}', '') = 'cuda';
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.capability_vram_required_bytes(
    capability_manifest jsonb
) RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN coalesce(
        NULLIF(capability_manifest #>> '{resources,gpu,vram_required_bytes}', '')::bigint,
        NULLIF(capability_manifest #>> '{resource_profile,gpu,vram_required_bytes}', '')::bigint,
        NULLIF(capability_manifest #>> '{resources,vram_required_bytes}', '')::bigint
    );
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.capability_gpu_reserved(
    capability_manifest jsonb
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN rvbbit.capability_gpu_required(capability_manifest)
        OR coalesce(NULLIF(capability_manifest #>> '{resources,gpu,reserved}', '')::boolean, false);
END
$$;

CREATE OR REPLACE VIEW rvbbit.warren_gpu_capacity AS
WITH gpu_rows AS (
    SELECT
        n.node_id,
        n.name AS node_name,
        n.capacity,
        n.inventory,
        coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90) AS vram_usable_ratio,
        g.elem AS gpu
    FROM rvbbit.warren_nodes n
    LEFT JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(n.inventory) = 'array' THEN n.inventory ELSE '[]'::jsonb END
    ) AS g(elem) ON true
),
provisioned AS (
    SELECT
        d.node_id,
        coalesce(sum(rvbbit.capability_vram_required_bytes(d.manifest)), 0)::bigint
            AS gpu_provisioned_bytes
    FROM rvbbit.warren_deployments d
    WHERE d.status IN ('starting', 'running', 'stopping')
      AND rvbbit.capability_gpu_reserved(d.manifest)
      AND rvbbit.capability_vram_required_bytes(d.manifest) IS NOT NULL
    GROUP BY d.node_id
)
SELECT
    n.node_id,
    n.name AS node_name,
    n.capacity,
    n.inventory AS gpu_inventory,
    coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90)
        AS vram_usable_ratio,
    count(g.gpu)::int AS gpu_count,
    coalesce(
        array_remove(array_agg(DISTINCT g.gpu->>'name') FILTER (WHERE g.gpu ? 'name'), NULL),
        ARRAY[]::text[]
    ) AS gpu_names,
    coalesce(sum(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric), 0)::bigint
        AS gpu_mem_total_bytes,
    coalesce(
        floor(sum(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric)
            * coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90)),
        0
    )::bigint AS gpu_mem_usable_bytes,
    coalesce(
        max(floor(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric
            * coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90))),
        0
    )::bigint AS single_gpu_mem_usable_bytes,
    coalesce(p.gpu_provisioned_bytes, 0)::bigint AS gpu_provisioned_bytes,
    greatest(
        coalesce(
            floor(sum(NULLIF(g.gpu->>'memory_total_bytes', '')::numeric)
                * coalesce(NULLIF(n.capacity #>> '{gpu,vram_usable_ratio}', '')::numeric, 0.90)),
            0
        )::bigint - coalesce(p.gpu_provisioned_bytes, 0)::bigint,
        0
    )::bigint AS gpu_available_bytes
FROM rvbbit.warren_nodes n
LEFT JOIN gpu_rows g ON g.node_id = n.node_id
LEFT JOIN provisioned p ON p.node_id = n.node_id
GROUP BY n.node_id, n.name, n.capacity, n.inventory, p.gpu_provisioned_bytes;

CREATE OR REPLACE VIEW rvbbit.warren_node_effective_status AS
WITH heartbeat AS (
    SELECT
        n.*,
        CASE
            WHEN n.last_heartbeat IS NULL THEN 'unknown'
            WHEN clock_timestamp() - n.last_heartbeat < interval '30 seconds' THEN 'fresh'
            WHEN clock_timestamp() - n.last_heartbeat < interval '2 minutes' THEN 'stale'
            ELSE 'offline'
        END AS heartbeat_state
    FROM rvbbit.warren_nodes n
)
SELECT
    h.node_id,
    h.name,
    h.base_url,
    h.labels,
    h.capacity,
    h.inventory,
    h.status AS reported_status,
    h.heartbeat_state,
    CASE
        WHEN h.status = 'error' THEN 'error'
        WHEN h.status = 'draining' THEN 'draining'
        WHEN h.heartbeat_state = 'offline' THEN 'offline'
        WHEN h.heartbeat_state = 'unknown' THEN 'registered'
        WHEN h.heartbeat_state = 'stale' AND h.status IN ('ready', 'busy') THEN 'stale'
        ELSE h.status
    END AS effective_status,
    h.status IN ('ready', 'busy')
        AND h.heartbeat_state IN ('fresh', 'stale') AS is_eligible,
    h.version,
    h.last_heartbeat,
    CASE
        WHEN h.last_heartbeat IS NULL THEN NULL::interval
        ELSE clock_timestamp() - h.last_heartbeat
    END AS heartbeat_age,
    h.created_at,
    h.updated_at
FROM heartbeat h;

CREATE OR REPLACE VIEW rvbbit.warren_inventory AS
SELECT
    n.node_id,
    n.name AS node_name,
    n.base_url,
    n.labels,
    n.capacity,
    n.reported_status AS node_status,
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
    d.updated_at AS deployment_updated_at,
    cap.gpu_names,
    cap.vram_usable_ratio,
    cap.gpu_mem_usable_bytes,
    cap.single_gpu_mem_usable_bytes,
    cap.gpu_provisioned_bytes,
    cap.gpu_available_bytes,
    n.effective_status AS node_effective_status,
    n.heartbeat_state,
    n.is_eligible
FROM rvbbit.warren_node_effective_status n
LEFT JOIN rvbbit.warren_node_latest_metrics lm
  ON lm.node_id = n.node_id
LEFT JOIN rvbbit.warren_gpu_capacity cap
  ON cap.node_id = n.node_id
LEFT JOIN rvbbit.warren_deployments d
  ON d.node_id = n.node_id
 AND d.status IN ('starting', 'running', 'stopping', 'stopped', 'failed', 'drifted', 'orphaned');

CREATE OR REPLACE VIEW rvbbit.warren_backend_status AS
WITH ranked AS (
    SELECT
        d.*,
        row_number() OVER (
            PARTITION BY d.backend_name
            ORDER BY d.updated_at DESC, d.created_at DESC, d.deployment_id DESC
        ) AS rn
    FROM rvbbit.warren_deployments d
    WHERE d.backend_name IS NOT NULL
)
SELECT
    b.name,
    b.transport,
    b.endpoint_url,
    b.batch_size,
    b.max_concurrent,
    b.timeout_ms,
    b.auth_header_env,
    b.transport_opts,
    b.description,
    b.source_provider,
    b.source_model,
    b.source_revision,
    b.install_manifest,
    b.created_at,
    d.deployment_id,
    d.node_name,
    d.kind AS deployment_kind,
    d.name AS deployment_name,
    d.status AS deployment_status,
    CASE
        WHEN d.deployment_id IS NULL THEN 'external'
        WHEN d.status = 'running' THEN 'running'
        WHEN d.status IN ('starting', 'stopping') THEN d.status
        ELSE 'unavailable'
    END AS serving_status,
    (d.deployment_id IS NULL OR d.status = 'running') AS callable,
    d.error AS deployment_error,
    d.health AS deployment_health,
    d.updated_at AS deployment_updated_at,
    d.stopped_at
FROM rvbbit.backends b
LEFT JOIN ranked d
  ON d.backend_name = b.name
 AND d.rn = 1;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_state(
    deployment_id uuid,
    desired_state text
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $rwds$
DECLARE
    d rvbbit.warren_deployments%ROWTYPE;
    normalized_state text := nullif(btrim(desired_state), '');
    lifecycle_manifest jsonb;
    queued_job_id uuid;
BEGIN
    IF normalized_state NOT IN ('stopped', 'removed') THEN
        RAISE EXCEPTION 'desired_state must be stopped or removed';
    END IF;

    SELECT * INTO d
    FROM rvbbit.warren_deployments
    WHERE warren_deployments.deployment_id = request_warren_deployment_state.deployment_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Warren deployment % not found', deployment_id;
    END IF;
    IF d.status = 'removed' THEN
        RAISE EXCEPTION 'Warren deployment % is already removed', deployment_id;
    END IF;
    IF d.status = 'stopped' AND normalized_state = 'stopped' THEN
        RAISE EXCEPTION 'Warren deployment % is already stopped', deployment_id;
    END IF;
    IF d.status = 'stopping' THEN
        RAISE EXCEPTION 'Warren deployment % already has a lifecycle request in progress',
            deployment_id;
    END IF;

    lifecycle_manifest := coalesce(d.manifest, '{}'::jsonb)
        || jsonb_build_object(
            'warren_deployment',
            jsonb_build_object(
                'deployment_id', d.deployment_id,
                'node_id', d.node_id,
                'node_name', d.node_name,
                'kind', d.kind,
                'name', d.name,
                'status', d.status,
                'endpoint_url', d.endpoint_url,
                'backend_name', d.backend_name,
                'operator_name', d.operator_name,
                'runtime_name', d.runtime_name,
                'compose_project', d.compose_project,
                'work_dir', d.work_dir
            )
        );

    queued_job_id := rvbbit.enqueue_warren_job(
        d.kind,
        d.name,
        lifecycle_manifest,
        '{}'::jsonb,
        normalized_state
    );

    UPDATE rvbbit.warren_jobs AS j
    SET backend_name = d.backend_name,
        operator_name = d.operator_name,
        runtime_name = d.runtime_name,
        endpoint_url = d.endpoint_url,
        progress = jsonb_build_object(
            'phase', 'queued',
            'desired_state', normalized_state,
            'deployment_id', d.deployment_id,
            'node_name', d.node_name,
            'queued_at', clock_timestamp()
        )
    WHERE j.job_id = queued_job_id;

    UPDATE rvbbit.warren_deployments AS existing
    SET status = 'stopping',
        error = NULL,
        health = existing.health || jsonb_build_object(
            'lifecycle_request',
            jsonb_build_object(
                'desired_state', normalized_state,
                'job_id', queued_job_id,
                'requested_at', clock_timestamp()
            )
        )
    WHERE existing.deployment_id = d.deployment_id;

    RETURN queued_job_id;
END
$rwds$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_stop(
    deployment_id uuid
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    RETURN rvbbit.request_warren_deployment_state(deployment_id, 'stopped');
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_remove(
    deployment_id uuid
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    RETURN rvbbit.request_warren_deployment_state(deployment_id, 'removed');
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.request_warren_deployment_redeploy(
    deployment_id uuid,
    target_selector jsonb DEFAULT NULL,
    job_name text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $rwdr$
DECLARE
    d rvbbit.warren_deployments%ROWTYPE;
    source_job rvbbit.warren_jobs%ROWTYPE;
    redeploy_manifest jsonb;
    redeploy_selector jsonb;
    queued_job_id uuid;
    actual_job_name text;
BEGIN
    IF target_selector IS NOT NULL AND jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;

    SELECT * INTO d
    FROM rvbbit.warren_deployments
    WHERE warren_deployments.deployment_id = request_warren_deployment_redeploy.deployment_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Warren deployment % not found', deployment_id;
    END IF;

    IF d.job_id IS NOT NULL THEN
        SELECT * INTO source_job
        FROM rvbbit.warren_jobs
        WHERE warren_jobs.job_id = d.job_id;
    END IF;

    redeploy_manifest := coalesce(
        NULLIF(d.manifest - 'warren_deployment', '{}'::jsonb),
        NULLIF(source_job.manifest - 'warren_deployment', '{}'::jsonb)
    );
    IF redeploy_manifest IS NULL OR jsonb_typeof(redeploy_manifest) <> 'object' THEN
        RAISE EXCEPTION 'Warren deployment % has no reusable manifest', deployment_id;
    END IF;

    redeploy_selector := coalesce(
        target_selector,
        source_job.target_selector,
        CASE
            WHEN jsonb_typeof(d.health->'target_selector') = 'object'
            THEN d.health->'target_selector'
            ELSE NULL
        END,
        '{}'::jsonb
    );

    actual_job_name := coalesce(nullif(btrim(job_name), ''), d.name);
    queued_job_id := rvbbit.enqueue_warren_job(
        d.kind,
        actual_job_name,
        redeploy_manifest,
        redeploy_selector,
        'running'
    );

    UPDATE rvbbit.warren_jobs AS j
    SET backend_name = d.backend_name,
        operator_name = d.operator_name,
        runtime_name = d.runtime_name,
        progress = jsonb_build_object(
            'phase', 'queued',
            'desired_state', 'running',
            'redeploy_of', d.deployment_id,
            'previous_status', d.status,
            'queued_at', clock_timestamp()
        )
    WHERE j.job_id = queued_job_id;

    UPDATE rvbbit.warren_deployments AS existing
    SET health = existing.health || jsonb_build_object(
            'redeploy_request',
            jsonb_build_object(
                'job_id', queued_job_id,
                'requested_at', clock_timestamp()
            )
        )
    WHERE existing.deployment_id = d.deployment_id;

    RETURN queued_job_id;
END
$rwdr$;

CREATE OR REPLACE FUNCTION rvbbit.report_warren_deployment_observation(
    deployment_id uuid,
    node_name text,
    observed_state text,
    observation jsonb DEFAULT '{}'::jsonb,
    observation_error text DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
VOLATILE
AS $rwdo$
DECLARE
    normalized_observed text := coalesce(nullif(btrim(observed_state), ''), 'unknown');
    normalized_observation jsonb := coalesce(observation, '{}'::jsonb);
    current_status text;
    current_desired_state text;
    next_status text;
BEGIN
    IF jsonb_typeof(normalized_observation) <> 'object' THEN
        RAISE EXCEPTION 'observation must be a JSON object';
    END IF;

    SELECT status, health #>> '{lifecycle_request,desired_state}'
    INTO current_status, current_desired_state
    FROM rvbbit.warren_deployments AS d
    WHERE d.deployment_id = report_warren_deployment_observation.deployment_id
      AND d.node_name = report_warren_deployment_observation.node_name
    FOR UPDATE;

    IF current_status IS NULL THEN
        RAISE EXCEPTION 'Warren deployment % not found for node %',
            deployment_id, node_name;
    END IF;

    next_status := CASE
        WHEN current_status IN ('starting', 'running', 'drifted')
             AND normalized_observed IN ('running', 'healthy') THEN 'running'
        WHEN current_status IN ('starting', 'running', 'drifted')
             AND normalized_observed IN ('missing', 'exited', 'dead', 'stopped') THEN 'drifted'
        WHEN current_status IN ('stopped', 'removed', 'orphaned')
             AND normalized_observed IN ('running', 'healthy') THEN 'orphaned'
        WHEN current_status = 'orphaned'
             AND normalized_observed IN ('missing', 'exited', 'dead', 'stopped')
        THEN CASE WHEN current_desired_state = 'removed' THEN 'removed' ELSE 'stopped' END
        ELSE current_status
    END;

    UPDATE rvbbit.warren_deployments AS d
    SET status = next_status,
        health = d.health || jsonb_build_object(
            'last_reconcile',
            normalized_observation || jsonb_build_object(
                'observed_state', normalized_observed,
                'observed_at', clock_timestamp()
            )
        ),
        error = CASE
            WHEN next_status IN ('drifted', 'orphaned')
            THEN coalesce(observation_error, 'Warren deployment state drift detected')
            WHEN d.status IN ('drifted', 'orphaned') AND next_status = 'running'
            THEN NULL
            ELSE d.error
        END,
        stopped_at = CASE
            WHEN next_status IN ('stopped', 'removed') THEN coalesce(d.stopped_at, clock_timestamp())
            WHEN next_status = 'running' THEN NULL
            ELSE d.stopped_at
        END
    WHERE d.deployment_id = report_warren_deployment_observation.deployment_id;

    RETURN next_status;
END
$rwdo$;

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
          AND n.last_heartbeat IS NOT NULL
          AND clock_timestamp() - n.last_heartbeat < interval '2 minutes'
    ),
    picked AS (
        SELECT
            j.job_id,
            req.gpu_reservation_required,
            req.vram_required_bytes,
            req.gpu_placement,
            cap.gpu_available_bytes,
            cap.single_gpu_mem_usable_bytes
        FROM rvbbit.warren_jobs j
        CROSS JOIN node n
        LEFT JOIN rvbbit.warren_gpu_capacity cap
          ON cap.node_id = n.node_id
        CROSS JOIN LATERAL (
            SELECT
                CASE WHEN j.desired_state = 'running' THEN (
                    rvbbit.capability_gpu_required(j.manifest)
                    OR coalesce(NULLIF(j.target_selector->>'gpu', '')::boolean, false)
                ) ELSE false END AS gpu_reservation_required,
                CASE WHEN j.desired_state = 'running'
                     THEN rvbbit.capability_vram_required_bytes(j.manifest)
                     ELSE NULL::bigint
                END AS vram_required_bytes,
                coalesce(NULLIF(j.manifest #>> '{resources,gpu,placement}', ''), 'single_gpu') AS gpu_placement
        ) req
        WHERE j.status = 'queued'
          AND (
              (j.desired_state = 'running' AND n.labels @> j.target_selector)
              OR (
                  j.desired_state IN ('stopped', 'removed')
                  AND j.manifest #>> '{warren_deployment,node_name}' = n.name
              )
          )
          AND (
              NOT req.gpu_reservation_required
              OR req.vram_required_bytes IS NULL
              OR (
                  req.vram_required_bytes <= coalesce(cap.gpu_available_bytes, 0)
                  AND (
                      req.gpu_placement <> 'single_gpu'
                      OR req.vram_required_bytes <= coalesce(cap.single_gpu_mem_usable_bytes, 0)
                  )
              )
          )
        ORDER BY j.created_at
        LIMIT 1
        FOR UPDATE OF j SKIP LOCKED
    ),
    updated AS (
        UPDATE rvbbit.warren_jobs j
        SET status = 'running',
            phase = 'claimed',
            manifest = CASE
                WHEN picked.gpu_reservation_required
                     AND picked.vram_required_bytes IS NOT NULL
                THEN jsonb_set(j.manifest, '{resources,gpu,reserved}', 'true'::jsonb, true)
                ELSE j.manifest
            END,
            claimed_by = claim_warren_job.node_name,
            claimed_at = clock_timestamp(),
            started_at = COALESCE(started_at, clock_timestamp()),
            attempts = attempts + 1,
            progress = progress || jsonb_build_object(
                'phase', 'claimed',
                'desired_state', j.desired_state,
                'node_name', claim_warren_job.node_name,
                'claimed_at', clock_timestamp(),
                'gpu_reserved', picked.gpu_reservation_required,
                'gpu_placement', picked.gpu_placement,
                'vram_required_bytes', picked.vram_required_bytes,
                'gpu_available_bytes', picked.gpu_available_bytes,
                'single_gpu_mem_usable_bytes', picked.single_gpu_mem_usable_bytes
            )
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

CREATE OR REPLACE FUNCTION rvbbit.update_warren_job_progress(
    job_id       uuid,
    node_name    text,
    job_phase    text,
    progress_doc jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $uwjp$
DECLARE
    normalized_phase text := nullif(btrim(job_phase), '');
    normalized_doc jsonb := coalesce(progress_doc, '{}'::jsonb);
BEGIN
    IF normalized_phase IS NULL THEN
        RAISE EXCEPTION 'job_phase is required';
    END IF;
    IF jsonb_typeof(normalized_doc) <> 'object' THEN
        RAISE EXCEPTION 'progress_doc must be a JSON object';
    END IF;

    UPDATE rvbbit.warren_jobs j
    SET phase = normalized_phase,
        progress = j.progress
            || normalized_doc
            || jsonb_build_object(
                'phase', normalized_phase,
                'node_name', update_warren_job_progress.node_name,
                'updated_at', clock_timestamp()
            ),
        logs = j.logs || jsonb_build_object(
            'last_phase', normalized_phase,
            'last_phase_at', clock_timestamp()
        )
    WHERE j.job_id = update_warren_job_progress.job_id
      AND j.status = 'running'
      AND j.claimed_by = update_warren_job_progress.node_name;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'running Warren job % is not claimed by node %',
            job_id, node_name;
    END IF;
END
$uwjp$;

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
    actual_desired_state text;
    completion_phase text;
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = complete_warren_job.node_name;

    IF actual_node_id IS NULL THEN
        RAISE EXCEPTION 'warren node % is not registered', node_name;
    END IF;

    IF deployment_status NOT IN ('starting', 'running', 'stopping', 'stopped',
                                 'failed', 'removed', 'drifted', 'orphaned') THEN
        RAISE EXCEPTION 'unsupported Warren deployment status %', deployment_status;
    END IF;

    SELECT kind, name, desired_state
    INTO actual_kind, actual_name, actual_desired_state
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    IF actual_kind IS NULL THEN
        RAISE EXCEPTION 'warren job % not found', job_id;
    END IF;

    completion_phase := CASE
        WHEN deployment_status = 'running' THEN 'ready'
        ELSE deployment_status
    END;

    UPDATE rvbbit.warren_jobs
    SET status = 'completed',
        phase = completion_phase,
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        progress = progress || jsonb_build_object(
            'phase', completion_phase,
            'desired_state', actual_desired_state,
            'deployment_status', deployment_status,
            'endpoint_url', complete_warren_job.endpoint_url,
            'backend_name', complete_warren_job.backend_name,
            'operator_name', complete_warren_job.operator_name,
            'runtime_name', complete_warren_job.runtime_name,
            'finished_at', clock_timestamp()
        ),
        logs = complete_warren_job.logs,
        error = NULL,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = complete_warren_job.job_id;

    UPDATE rvbbit.warren_deployments AS d
    SET job_id = complete_warren_job.job_id,
        node_id = actual_node_id,
        status = deployment_status,
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        manifest = complete_warren_job.deploy_manifest,
        compose_project = complete_warren_job.compose_project,
        work_dir = complete_warren_job.work_dir,
        health = complete_warren_job.health,
        error = NULL,
        stopped_at = CASE
            WHEN deployment_status IN ('starting', 'running', 'stopping') THEN NULL
            ELSE coalesce(d.stopped_at, clock_timestamp())
        END
    WHERE d.node_name = complete_warren_job.node_name
      AND d.kind = actual_kind
      AND d.name = actual_name
      AND d.status IN ('starting', 'running', 'stopping', 'stopped',
                       'failed', 'removed', 'drifted', 'orphaned');

    IF NOT FOUND THEN
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
            error = NULL,
            stopped_at = CASE
                WHEN EXCLUDED.status IN ('starting', 'running', 'stopping') THEN NULL
                ELSE coalesce(rvbbit.warren_deployments.stopped_at, clock_timestamp())
            END;
    END IF;

    IF deployment_status IN ('stopped', 'removed') AND complete_warren_job.runtime_name IS NOT NULL THEN
        IF to_regclass('rvbbit.python_runtimes') IS NOT NULL THEN
            UPDATE rvbbit.python_runtimes AS r
            SET status = 'disabled',
                health = r.health || jsonb_build_object(
                    'warren_lifecycle', deployment_status,
                    'warren_job_id', complete_warren_job.job_id,
                    'updated_at', clock_timestamp()
                )
            WHERE r.name = complete_warren_job.runtime_name
              AND r.runtime_source = 'warren';
        END IF;
        IF to_regclass('rvbbit.mcp_gateways') IS NOT NULL THEN
            UPDATE rvbbit.mcp_gateways AS g
            SET status = 'disabled',
                health = g.health || jsonb_build_object(
                    'warren_lifecycle', deployment_status,
                    'warren_job_id', complete_warren_job.job_id,
                    'updated_at', clock_timestamp()
                )
            WHERE g.name = complete_warren_job.runtime_name
              AND g.gateway_source = 'warren';
        END IF;
    END IF;

    IF deployment_status IN ('stopped', 'removed') AND complete_warren_job.backend_name IS NOT NULL THEN
        PERFORM rvbbit.reload_backends();
    END IF;
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
    actual_manifest jsonb;
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = fail_warren_job.node_name;

    SELECT kind, name, manifest INTO actual_kind, actual_name, actual_manifest
    FROM rvbbit.warren_jobs
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    UPDATE rvbbit.warren_jobs
    SET status = 'failed',
        phase = 'failed',
        error = fail_warren_job.error,
        progress = progress || jsonb_build_object(
            'phase', 'failed',
            'error', fail_warren_job.error,
            'failed_at', clock_timestamp(),
            'node_name', fail_warren_job.node_name
        ),
        logs = fail_warren_job.logs,
        finished_at = clock_timestamp()
    WHERE warren_jobs.job_id = fail_warren_job.job_id;

    IF actual_kind IS NOT NULL THEN
        INSERT INTO rvbbit.warren_deployments
            (job_id, node_id, node_name, kind, name, status, manifest, error,
             health)
        VALUES
            (fail_warren_job.job_id, actual_node_id, fail_warren_job.node_name,
             actual_kind, actual_name, 'failed', coalesce(actual_manifest, '{}'::jsonb),
             fail_warren_job.error, fail_warren_job.logs)
        ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
            status = 'failed',
            manifest = EXCLUDED.manifest,
            error = EXCLUDED.error,
            health = EXCLUDED.health;
    END IF;
END
$fwj$;

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
    catalog_resource_doc jsonb;
    queued_job_id uuid;
BEGIN
    IF catalog_id IS NULL OR btrim(catalog_id) = '' THEN
        RAISE EXCEPTION 'catalog_id is required';
    END IF;
    IF jsonb_typeof(target_selector) <> 'object' THEN
        RAISE EXCEPTION 'target_selector must be a JSON object';
    END IF;

    SELECT manifest, name, backend_name, runtime_name, operators[1], resource_profile
    INTO catalog_manifest, catalog_name, catalog_backend_name, catalog_runtime_name,
         catalog_operator_name, catalog_resource_doc
    FROM rvbbit.capability_catalog
    WHERE id = btrim(catalog_id)
      AND active;

    IF catalog_manifest IS NULL THEN
        RAISE EXCEPTION 'active capability catalog entry % not found', catalog_id;
    END IF;
    IF jsonb_typeof(catalog_manifest->'resources') IS DISTINCT FROM 'object'
       AND jsonb_typeof(catalog_resource_doc) = 'object'
       AND catalog_resource_doc <> '{}'::jsonb THEN
        catalog_manifest := catalog_manifest || jsonb_build_object('resources', catalog_resource_doc);
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

-- Infix operator collision handling -----------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.create_operator(
    op_name        text,
    op_arg_names   text[],
    op_return_type text,
    op_system      text DEFAULT '',
    op_user        text DEFAULT '',
    op_shape       text DEFAULT 'scalar',
    op_model       text DEFAULT 'openai/gpt-5.4-mini',
    op_parser      text DEFAULT NULL,
    op_max_tokens  int  DEFAULT 256,
    op_temperature real DEFAULT NULL,
    op_arg_types   text[] DEFAULT NULL,
    op_description text DEFAULT NULL,
    op_infix_symbol text DEFAULT NULL,
    op_infix_word   text DEFAULT NULL,
    op_tests        jsonb DEFAULT NULL,
    op_steps        jsonb DEFAULT NULL
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    actual_parser    text;
    actual_arg_types text[];
    exec_fn          text;
    wrapper_args_with_opts text;
    wrapper_args_no_opts   text;
    wrapper_inputs   text;
    n_args           int;
BEGIN
    n_args := cardinality(op_arg_names);
    actual_arg_types := COALESCE(op_arg_types,
        ARRAY(SELECT 'text' FROM generate_series(1, n_args)));
    actual_parser := COALESCE(op_parser, CASE op_return_type
        WHEN 'bool'   THEN 'yes_no'
        WHEN 'float8' THEN 'score_0_1'
        WHEN 'jsonb'  THEN 'json'
        ELSE 'strip'
    END);

    INSERT INTO rvbbit.operators
        (name, shape, arg_names, arg_types, return_type, model, system_prompt, user_prompt,
         parser, max_tokens, temperature, description, infix_symbol, infix_word, tests, steps)
    VALUES
        (op_name, op_shape, op_arg_names, actual_arg_types, op_return_type, op_model,
         op_system, op_user, actual_parser, op_max_tokens, op_temperature, op_description,
         op_infix_symbol, op_infix_word, op_tests, op_steps)
    ON CONFLICT (name) DO UPDATE SET
        shape = EXCLUDED.shape,
        arg_names = EXCLUDED.arg_names,
        arg_types = EXCLUDED.arg_types,
        return_type = EXCLUDED.return_type,
        model = EXCLUDED.model,
        system_prompt = EXCLUDED.system_prompt,
        user_prompt = EXCLUDED.user_prompt,
        parser = EXCLUDED.parser,
        max_tokens = EXCLUDED.max_tokens,
        temperature = EXCLUDED.temperature,
        description = EXCLUDED.description,
        infix_symbol = EXCLUDED.infix_symbol,
        infix_word = EXCLUDED.infix_word,
        tests = EXCLUDED.tests,
        steps = EXCLUDED.steps;

    IF op_shape = 'dimension' THEN
        exec_fn := '_dim_exec_' || op_return_type;

        wrapper_inputs := 'jsonb_build_object(' || array_to_string(
            ARRAY(SELECT format('%L, $%s', a, i)
                  FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
            ', '
        ) || ')';

        wrapper_args_with_opts := array_to_string(
            ARRAY(SELECT format('%I %s', a, t)
                  FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
            ', '
        ) || ', opts jsonb DEFAULT ''{}''::jsonb';

        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS SETOF %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT * FROM rvbbit.%I(%L, %s, $%s) $wb$',
            op_name, wrapper_args_with_opts, op_return_type,
            exec_fn, op_name, wrapper_inputs, n_args + 1
        );
        RETURN;
    END IF;

    IF op_shape = 'aggregate' THEN
        wrapper_inputs := 'jsonb_build_object(' || array_to_string(
            ARRAY(SELECT format('%L, $%s', a, i + 1)
                  FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
            ', '
        ) || ')';
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb, %s, opts jsonb DEFAULT ''{}''::jsonb) RETURNS jsonb LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit._agg_append_state(state, %s) $wb$',
            '_agg_' || op_name || '_sfunc',
            array_to_string(
                ARRAY(SELECT format('%I %s', a, t)
                      FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
                ', '
            ),
            wrapper_inputs
        );

        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb) RETURNS %s LANGUAGE sql PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, state) $wb$',
            '_agg_' || op_name || '_ffunc',
            op_return_type,
            '_agg_run_op_' || op_return_type,
            op_name
        );

        EXECUTE format('DROP AGGREGATE IF EXISTS rvbbit.%I(%s, jsonb)',
            op_name,
            array_to_string(actual_arg_types, ', ')
        );
        EXECUTE format(
            'CREATE AGGREGATE rvbbit.%I(%s, jsonb) (SFUNC = rvbbit.%I, STYPE = jsonb, INITCOND = ''{}'', FINALFUNC = rvbbit.%I)',
            op_name,
            array_to_string(actual_arg_types, ', '),
            '_agg_' || op_name || '_sfunc',
            '_agg_' || op_name || '_ffunc'
        );

        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(state jsonb, %s) RETURNS jsonb LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit._agg_append_state(state, %s) $wb$',
            '_agg_' || op_name || '_sfunc_no_opts',
            array_to_string(
                ARRAY(SELECT format('%I %s', a, t)
                      FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
                ', '
            ),
            wrapper_inputs
        );
        EXECUTE format('DROP AGGREGATE IF EXISTS rvbbit.%I(%s)',
            op_name,
            array_to_string(actual_arg_types, ', ')
        );
        EXECUTE format(
            'CREATE AGGREGATE rvbbit.%I(%s) (SFUNC = rvbbit.%I, STYPE = jsonb, INITCOND = ''{}'', FINALFUNC = rvbbit.%I)',
            op_name,
            array_to_string(actual_arg_types, ', '),
            '_agg_' || op_name || '_sfunc_no_opts',
            '_agg_' || op_name || '_ffunc'
        );
        RETURN;
    END IF;

    exec_fn := '_exec_op_' || op_return_type;

    wrapper_inputs := 'jsonb_build_object(' || array_to_string(
        ARRAY(SELECT format('%L, $%s', a, i)
              FROM (SELECT a, row_number() OVER () AS i FROM unnest(op_arg_names) AS a) s),
        ', '
    ) || ')';

    wrapper_args_with_opts := array_to_string(
        ARRAY(SELECT format('%I %s', a, t) FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
        ', '
    ) || ', opts jsonb DEFAULT ''{}''::jsonb';

    EXECUTE format(
        'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, %s, $%s) $wb$',
        op_name, wrapper_args_with_opts, op_return_type, exec_fn, op_name, wrapper_inputs, n_args + 1
    );

    IF n_args = 2 THEN
        wrapper_args_no_opts := array_to_string(
            ARRAY(SELECT format('%I %s', a, t)
                  FROM unnest(op_arg_names, actual_arg_types) AS u(a,t)),
            ', '
        );
        EXECUTE format(
            'CREATE OR REPLACE FUNCTION rvbbit.%I(%s) RETURNS %s LANGUAGE sql STRICT PARALLEL SAFE AS $wb$ SELECT rvbbit.%I(%L, %s, ''{}''::jsonb) $wb$',
            '_op_' || op_name, wrapper_args_no_opts, op_return_type,
            exec_fn, op_name, wrapper_inputs
        );

        IF op_infix_symbol IS NOT NULL THEN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_operator op
                WHERE op.oprnamespace = 'rvbbit'::regnamespace
                  AND op.oprname = op_infix_symbol
                  AND op.oprleft = actual_arg_types[1]::regtype
                  AND op.oprright = actual_arg_types[2]::regtype
            ) THEN
                EXECUTE format(
                    'CREATE OPERATOR rvbbit.%s (LEFTARG = %s, RIGHTARG = %s, FUNCTION = rvbbit.%I)',
                    op_infix_symbol, actual_arg_types[1], actual_arg_types[2],
                    '_op_' || op_name
                );
            END IF;
        END IF;
    END IF;
END $$;

-- Refresh the built-in Warren capability catalog on upgrade.
SELECT rvbbit.seed_capability_catalog();

CREATE OR REPLACE FUNCTION rvbbit.require_python_admin()
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
        RAISE EXCEPTION 'rvbbit Python runtime DDL requires a superuser or rvbbit_warren role membership in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.require_mcp_gateway_admin()
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
        RAISE EXCEPTION 'rvbbit MCP gateway DDL requires a superuser or rvbbit_warren role membership in this release';
    END IF;
END
$$;
