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

WITH ranked_active_deployments AS (
    SELECT
        deployment_id,
        row_number() OVER (
            PARTITION BY node_name, kind, name
            ORDER BY updated_at DESC, created_at DESC, deployment_id DESC
        ) AS rn
    FROM rvbbit.warren_deployments
    WHERE status IN ('starting', 'running')
)
UPDATE rvbbit.warren_deployments AS d
SET status = 'removed',
    stopped_at = coalesce(d.stopped_at, clock_timestamp()),
    error = coalesce(d.error, 'superseded by newer deployment record')
FROM ranked_active_deployments AS r
WHERE d.deployment_id = r.deployment_id
  AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS warren_deployments_active_unique_idx
    ON rvbbit.warren_deployments (node_name, kind, name)
    WHERE status IN ('starting', 'running');

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
    WHERE d.status IN ('starting', 'running')
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
    d.updated_at AS deployment_updated_at,
    cap.gpu_names,
    cap.vram_usable_ratio,
    cap.gpu_mem_usable_bytes,
    cap.single_gpu_mem_usable_bytes,
    cap.gpu_provisioned_bytes,
    cap.gpu_available_bytes
FROM rvbbit.warren_nodes n
LEFT JOIN rvbbit.warren_node_latest_metrics lm
  ON lm.node_id = n.node_id
LEFT JOIN rvbbit.warren_gpu_capacity cap
  ON cap.node_id = n.node_id
LEFT JOIN rvbbit.warren_deployments d
  ON d.node_id = n.node_id
 AND d.status IN ('starting', 'running', 'failed');

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
                (
                    rvbbit.capability_gpu_required(j.manifest)
                    OR coalesce(NULLIF(j.target_selector->>'gpu', '')::boolean, false)
                ) AS gpu_reservation_required,
                rvbbit.capability_vram_required_bytes(j.manifest) AS vram_required_bytes,
                coalesce(NULLIF(j.manifest #>> '{resources,gpu,placement}', ''), 'single_gpu') AS gpu_placement
        ) req
        WHERE j.status = 'queued'
          AND n.labels @> j.target_selector
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
        phase = 'ready',
        endpoint_url = complete_warren_job.endpoint_url,
        backend_name = complete_warren_job.backend_name,
        operator_name = complete_warren_job.operator_name,
        runtime_name = complete_warren_job.runtime_name,
        progress = progress || jsonb_build_object(
            'phase', 'ready',
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
            WHEN deployment_status IN ('starting', 'running') THEN NULL
            ELSE coalesce(d.stopped_at, clock_timestamp())
        END
    WHERE d.node_name = complete_warren_job.node_name
      AND d.kind = actual_kind
      AND d.name = actual_name
      AND d.status IN ('starting', 'running');

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
                WHEN EXCLUDED.status IN ('starting', 'running') THEN NULL
                ELSE coalesce(rvbbit.warren_deployments.stopped_at, clock_timestamp())
            END;
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
BEGIN
    SELECT node_id INTO actual_node_id
    FROM rvbbit.warren_nodes
    WHERE name = fail_warren_job.node_name;

    SELECT kind, name INTO actual_kind, actual_name
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
             actual_kind, actual_name, 'failed', '{}'::jsonb,
             fail_warren_job.error, fail_warren_job.logs)
        ON CONFLICT ON CONSTRAINT warren_deployments_job_id_key DO UPDATE SET
            status = 'failed',
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
