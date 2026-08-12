-- 0285: durable, appliance-native Business Topology excavation workflows.
--
-- The original shadow harness deliberately kept private intermediate files and
-- model execution outside PostgreSQL.  This migration adds the missing control
-- plane: SQL creates a bounded run, an always-on appliance worker leases it,
-- and DataRabbit observes progress.  The browser never owns execution and a
-- refresh, disconnect, or closed laptop cannot cancel a run.
--
-- The workflow remains proposal-only.  Completion may stage receipt-backed
-- proposal bundles, but it cannot promote or materialize governed topology.

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_workflow_workers (
    worker_id text PRIMARY KEY,
    worker_version text NOT NULL,
    worker_status text NOT NULL DEFAULT 'ready',
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT business_topology_workflow_workers_id_check CHECK (
        nullif(btrim(worker_id),'') IS NOT NULL
    ),
    CONSTRAINT business_topology_workflow_workers_status_check CHECK (
        worker_status IN ('ready','busy','error','stopping')
    ),
    CONSTRAINT business_topology_workflow_workers_details_check CHECK (
        jsonb_typeof(details)='object'
    )
);

CREATE TABLE IF NOT EXISTS rvbbit.business_topology_workflow_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_name text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    phase text NOT NULL DEFAULT 'queued',
    parameters jsonb NOT NULL,
    progress jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    requested_by text NOT NULL DEFAULT current_user,
    worker_id text,
    lease_expires_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    CONSTRAINT business_topology_workflow_runs_name_check CHECK (
        nullif(btrim(run_name),'') IS NOT NULL
    ),
    CONSTRAINT business_topology_workflow_runs_status_check CHECK (
        status IN (
            'queued','running','cancel_requested','completed','failed','cancelled'
        )
    ),
    CONSTRAINT business_topology_workflow_runs_phase_check CHECK (
        nullif(btrim(phase),'') IS NOT NULL
    ),
    CONSTRAINT business_topology_workflow_runs_attempts_check CHECK (attempts >= 0),
    CONSTRAINT business_topology_workflow_runs_json_check CHECK (
        jsonb_typeof(parameters)='object'
        AND jsonb_typeof(progress)='object'
        AND (result IS NULL OR jsonb_typeof(result)='object')
    ),
    CONSTRAINT business_topology_workflow_runs_lease_check CHECK (
        (worker_id IS NULL AND lease_expires_at IS NULL)
        OR (worker_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

-- A full excavation is deliberately serialized per database.  It can issue
-- substantial embedding/model work and its final proposal set is easiest to
-- reason about when it has one exact source snapshot.  The start function also
-- takes an advisory lock so concurrent callers return the same active run.
CREATE UNIQUE INDEX IF NOT EXISTS business_topology_workflow_runs_one_active_idx
    ON rvbbit.business_topology_workflow_runs ((true))
    WHERE status IN ('queued','running','cancel_requested');
CREATE INDEX IF NOT EXISTS business_topology_workflow_runs_time_idx
    ON rvbbit.business_topology_workflow_runs (requested_at DESC);
CREATE INDEX IF NOT EXISTS business_topology_workflow_runs_status_idx
    ON rvbbit.business_topology_workflow_runs (status,updated_at DESC);

ALTER TABLE rvbbit.business_topology_proposal_bundles
    ADD COLUMN IF NOT EXISTS workflow_run_id uuid
        REFERENCES rvbbit.business_topology_workflow_runs(run_id)
        ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS business_topology_proposal_bundles_workflow_idx
    ON rvbbit.business_topology_proposal_bundles
       (workflow_run_id,updated_at DESC)
    WHERE workflow_run_id IS NOT NULL;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_register_workflow_worker(
    p_worker_id text,
    p_worker_version text,
    p_worker_status text DEFAULT 'ready',
    p_details jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql VOLATILE
AS $fn$
BEGIN
    IF nullif(btrim(p_worker_id),'') IS NULL
       OR nullif(btrim(p_worker_version),'') IS NULL THEN
        RAISE EXCEPTION 'business topology worker id and version are required';
    END IF;
    IF p_worker_status NOT IN ('ready','busy','error','stopping') THEN
        RAISE EXCEPTION 'unsupported business topology worker status %',p_worker_status;
    END IF;
    IF p_details IS NULL OR jsonb_typeof(p_details)<>'object' THEN
        RAISE EXCEPTION 'business topology worker details must be a JSON object';
    END IF;

    INSERT INTO rvbbit.business_topology_workflow_workers (
        worker_id,worker_version,worker_status,details,started_at,last_seen_at
    ) VALUES (
        btrim(p_worker_id),btrim(p_worker_version),p_worker_status,p_details,
        clock_timestamp(),clock_timestamp()
    )
    ON CONFLICT (worker_id) DO UPDATE SET
        worker_version=EXCLUDED.worker_version,
        worker_status=EXCLUDED.worker_status,
        details=EXCLUDED.details,
        last_seen_at=clock_timestamp();
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_start_workflow(
    p_schemas text[] DEFAULT NULL,
    p_relations text[] DEFAULT NULL,
    p_sample_rows integer DEFAULT 2048,
    p_max_relations integer DEFAULT 100,
    p_max_work_items integer DEFAULT 500,
    p_max_llm_calls integer DEFAULT 128,
    p_backend text DEFAULT 'clover_llm',
    p_model text DEFAULT 'clover',
    p_include_bridges boolean DEFAULT false,
    p_run_name text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_existing uuid;
    v_run_id uuid := gen_random_uuid();
    v_relations text[] := ARRAY[]::text[];
    v_requested text;
    v_relation regclass;
    v_canonical_relation text;
    v_candidate record;
    v_parameters jsonb;
BEGIN
    IF p_sample_rows NOT BETWEEN 32 AND 50000 THEN
        RAISE EXCEPTION 'sample_rows must be between 32 and 50000';
    END IF;
    IF p_max_relations NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'max_relations must be between 1 and 1000';
    END IF;
    IF p_max_work_items NOT BETWEEN 1 AND 5000 THEN
        RAISE EXCEPTION 'max_work_items must be between 1 and 5000';
    END IF;
    IF p_max_llm_calls NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'max_llm_calls must be between 1 and 1000';
    END IF;
    IF nullif(btrim(p_backend),'') IS NULL OR nullif(btrim(p_model),'') IS NULL THEN
        RAISE EXCEPTION 'backend and model are required';
    END IF;
    IF p_schemas IS NOT NULL AND EXISTS (
        SELECT 1 FROM unnest(p_schemas) value WHERE nullif(btrim(value),'') IS NULL
    ) THEN
        RAISE EXCEPTION 'schema names must be non-empty';
    END IF;

    -- Make repeated clicks and concurrent DataRabbit windows idempotent.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'rvbbit.business_topology_start_workflow',0
    ));
    SELECT run_id INTO v_existing
      FROM rvbbit.business_topology_workflow_runs
     WHERE status IN ('queued','running','cancel_requested')
     ORDER BY requested_at
     LIMIT 1;
    IF v_existing IS NOT NULL THEN
        RETURN v_existing;
    END IF;

    IF p_relations IS NOT NULL THEN
        FOREACH v_requested IN ARRAY p_relations
        LOOP
            IF nullif(btrim(v_requested),'') IS NULL THEN
                RAISE EXCEPTION 'relation names must be non-empty';
            END IF;
            v_relation := rvbbit._safe_regclass(v_requested);
            IF v_relation IS NULL THEN
                RAISE EXCEPTION 'relation % does not exist or is not visible',v_requested;
            END IF;
            IF NOT has_table_privilege(v_relation,'SELECT') THEN
                RAISE EXCEPTION 'SELECT privilege is required for relation %',v_relation;
            END IF;
            SELECT format('%I.%I',n.nspname,c.relname)
              INTO v_canonical_relation
              FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE c.oid=v_relation;
            IF NOT v_canonical_relation=ANY(v_relations) THEN
                v_relations := array_append(v_relations,v_canonical_relation);
            END IF;
            EXIT WHEN cardinality(v_relations)>=p_max_relations;
        END LOOP;
    ELSE
        FOR v_candidate IN
            SELECT format('%I.%I',n.nspname,c.relname) AS relation_name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE c.relkind IN ('r','p','m')
               AND NOT c.relispartition
               AND n.nspname NOT IN ('pg_catalog','information_schema','rvbbit')
               AND n.nspname NOT LIKE 'pg_toast%'
               AND n.nspname NOT LIKE 'pg_temp_%'
               AND n.nspname<>ALL(rvbbit._catalog_excluded_schemas())
               AND (p_schemas IS NULL OR n.nspname=ANY(p_schemas))
               AND has_table_privilege(c.oid,'SELECT')
             ORDER BY n.nspname,c.relname
             LIMIT p_max_relations
        LOOP
            v_relations := array_append(v_relations,v_candidate.relation_name);
        END LOOP;
    END IF;

    IF cardinality(v_relations)=0 THEN
        RAISE EXCEPTION 'no caller-readable relations matched the excavation scope';
    END IF;

    v_parameters := jsonb_build_object(
        'schema_version','rvbbit.business-topology.workflow-parameters.v1',
        'corpus_id','business-topology-'||v_run_id,
        'relations',to_jsonb(v_relations),
        'schemas',to_jsonb(p_schemas),
        'sample_rows',p_sample_rows,
        'max_relations',p_max_relations,
        'max_work_items',p_max_work_items,
        'max_llm_calls',p_max_llm_calls,
        'backend',btrim(p_backend),
        'model',btrim(p_model),
        'include_bridges',coalesce(p_include_bridges,false),
        'policy',jsonb_build_object(
            'maximum_sources_per_unit',12,
            'maximum_populations_per_unit',48,
            'max_pairs_per_unit',64,
            'max_pairs_per_link',8,
            'max_population_fanout',8,
            'minimum_pair_priority',0.32,
            'max_cross_neighborhood_links',24
        )
    );

    INSERT INTO rvbbit.business_topology_workflow_runs (
        run_id,run_name,status,phase,parameters,progress,requested_by
    ) VALUES (
        v_run_id,
        coalesce(
            nullif(btrim(p_run_name),''),
            'Fresh excavation · '||to_char(clock_timestamp(),'YYYY-MM-DD HH24:MI')
        ),
        'queued','queued',v_parameters,
        jsonb_build_object(
            'phase','queued',
            'relation_count',cardinality(v_relations),
            'completed_work_items',0,
            'llm_attempts',0,
            'bundles_staged',0,
            'queued_at',clock_timestamp()
        ),
        coalesce(nullif(current_setting('rvbbit.request_user',true),''),current_user)
    );

    PERFORM pg_notify('rvbbit_business_topology_jobs',v_run_id::text);
    RETURN v_run_id;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_claim_workflow(
    p_worker_id text,
    p_lease_seconds integer DEFAULT 300
) RETURNS TABLE (
    run_id uuid,
    run_name text,
    parameters jsonb,
    progress jsonb,
    requested_by text,
    attempts integer
)
LANGUAGE plpgsql VOLATILE
AS $fn$
BEGIN
    IF nullif(btrim(p_worker_id),'') IS NULL THEN
        RAISE EXCEPTION 'worker_id is required';
    END IF;
    IF p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'lease_seconds must be between 30 and 3600';
    END IF;

    RETURN QUERY
    WITH picked AS (
        SELECT workflow.run_id
          FROM rvbbit.business_topology_workflow_runs workflow
         WHERE workflow.status='queued'
            OR (
                workflow.status='running'
                AND workflow.lease_expires_at<clock_timestamp()
            )
         ORDER BY CASE WHEN workflow.status='running' THEN 0 ELSE 1 END,
                  workflow.requested_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    ), updated AS (
        UPDATE rvbbit.business_topology_workflow_runs workflow
           SET status='running',
               phase=CASE WHEN workflow.status='queued' THEN 'claimed' ELSE 'resuming' END,
               worker_id=btrim(p_worker_id),
               lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),
               attempts=workflow.attempts+1,
               started_at=coalesce(workflow.started_at,clock_timestamp()),
               updated_at=clock_timestamp(),
               error=NULL,
               progress=workflow.progress||jsonb_build_object(
                   'phase',CASE WHEN workflow.status='queued' THEN 'claimed' ELSE 'resuming' END,
                   'worker_id',btrim(p_worker_id),
                   'claimed_at',clock_timestamp()
               )
          FROM picked
         WHERE workflow.run_id=picked.run_id
         RETURNING workflow.*
    )
    SELECT updated.run_id,updated.run_name,updated.parameters,updated.progress,
           updated.requested_by,updated.attempts
      FROM updated;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_renew_workflow_lease(
    p_run_id uuid,
    p_worker_id text,
    p_lease_seconds integer DEFAULT 300
) RETURNS boolean
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_renewed boolean;
BEGIN
    IF p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'lease_seconds must be between 30 and 3600';
    END IF;
    UPDATE rvbbit.business_topology_workflow_runs workflow
       SET lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),
           updated_at=clock_timestamp()
     WHERE workflow.run_id=p_run_id
       AND workflow.status='running'
       AND workflow.worker_id=p_worker_id;
    v_renewed := FOUND;
    RETURN v_renewed;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_update_workflow(
    p_run_id uuid,
    p_worker_id text,
    p_phase text,
    p_progress jsonb DEFAULT '{}'::jsonb,
    p_lease_seconds integer DEFAULT 300
) RETURNS boolean
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_updated boolean;
BEGIN
    IF nullif(btrim(p_phase),'') IS NULL THEN
        RAISE EXCEPTION 'workflow phase is required';
    END IF;
    IF p_progress IS NULL OR jsonb_typeof(p_progress)<>'object' THEN
        RAISE EXCEPTION 'workflow progress must be a JSON object';
    END IF;
    IF p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'lease_seconds must be between 30 and 3600';
    END IF;

    UPDATE rvbbit.business_topology_workflow_runs workflow
       SET phase=btrim(p_phase),
           progress=workflow.progress||p_progress||jsonb_build_object(
               'phase',btrim(p_phase),'updated_at',clock_timestamp()
           ),
           lease_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),
           updated_at=clock_timestamp()
     WHERE workflow.run_id=p_run_id
       AND workflow.status='running'
       AND workflow.worker_id=p_worker_id;
    v_updated := FOUND;
    RETURN v_updated;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_complete_workflow(
    p_run_id uuid,
    p_worker_id text,
    p_result jsonb
) RETURNS void
LANGUAGE plpgsql VOLATILE
AS $fn$
BEGIN
    IF p_result IS NULL OR jsonb_typeof(p_result)<>'object' THEN
        RAISE EXCEPTION 'workflow result must be a JSON object';
    END IF;
    UPDATE rvbbit.business_topology_workflow_runs workflow
       SET status='completed',phase='completed',result=p_result,error=NULL,
           progress=workflow.progress||p_result||jsonb_build_object(
               'phase','completed','completed_at',clock_timestamp()
           ),
           worker_id=NULL,lease_expires_at=NULL,
           updated_at=clock_timestamp(),finished_at=clock_timestamp()
     WHERE workflow.run_id=p_run_id
       AND workflow.status='running'
       AND workflow.worker_id=p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'running workflow % is not claimed by worker %',p_run_id,p_worker_id;
    END IF;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_fail_workflow(
    p_run_id uuid,
    p_worker_id text,
    p_error text,
    p_progress jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql VOLATILE
AS $fn$
BEGIN
    IF p_progress IS NULL OR jsonb_typeof(p_progress)<>'object' THEN
        RAISE EXCEPTION 'workflow progress must be a JSON object';
    END IF;
    UPDATE rvbbit.business_topology_workflow_runs workflow
       SET status=CASE WHEN workflow.status='cancel_requested' THEN 'cancelled' ELSE 'failed' END,
           phase=CASE WHEN workflow.status='cancel_requested' THEN 'cancelled' ELSE 'failed' END,
           error=CASE WHEN workflow.status='cancel_requested' THEN NULL
                      ELSE left(coalesce(nullif(p_error,''),'unknown workflow failure'),6000) END,
           progress=workflow.progress||p_progress||jsonb_build_object(
               'phase',CASE WHEN workflow.status='cancel_requested' THEN 'cancelled' ELSE 'failed' END,
               'finished_at',clock_timestamp()
           ),
           worker_id=NULL,lease_expires_at=NULL,
           updated_at=clock_timestamp(),finished_at=clock_timestamp()
     WHERE workflow.run_id=p_run_id
       AND workflow.status IN ('running','cancel_requested')
       AND workflow.worker_id=p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active workflow % is not claimed by worker %',p_run_id,p_worker_id;
    END IF;
END
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.business_topology_cancel_workflow(
    p_run_id uuid
) RETURNS text
LANGUAGE plpgsql VOLATILE
AS $fn$
DECLARE
    v_status text;
BEGIN
    UPDATE rvbbit.business_topology_workflow_runs workflow
       SET status=CASE WHEN workflow.status='queued' THEN 'cancelled'
                       ELSE 'cancel_requested' END,
           phase=CASE WHEN workflow.status='queued' THEN 'cancelled'
                      ELSE 'cancelling' END,
           progress=workflow.progress||jsonb_build_object(
               'phase',CASE WHEN workflow.status='queued' THEN 'cancelled'
                            ELSE 'cancelling' END,
               'cancel_requested_at',clock_timestamp()
           ),
           worker_id=CASE WHEN workflow.status='queued' THEN NULL ELSE workflow.worker_id END,
           lease_expires_at=CASE WHEN workflow.status='queued' THEN NULL
                                 ELSE workflow.lease_expires_at END,
           updated_at=clock_timestamp(),
           finished_at=CASE WHEN workflow.status='queued' THEN clock_timestamp()
                            ELSE workflow.finished_at END
     WHERE workflow.run_id=p_run_id
       AND workflow.status IN ('queued','running')
     RETURNING status INTO v_status;
    IF v_status IS NULL THEN
        SELECT status INTO v_status
          FROM rvbbit.business_topology_workflow_runs
         WHERE run_id=p_run_id;
    END IF;
    IF v_status IS NULL THEN
        RAISE EXCEPTION 'business topology workflow % was not found',p_run_id;
    END IF;
    RETURN v_status;
END
$fn$;

CREATE OR REPLACE VIEW rvbbit.business_topology_workflow_status AS
SELECT workflow.run_id,workflow.run_name,workflow.status,workflow.phase,
       workflow.parameters,workflow.progress,workflow.result,
       workflow.requested_by,workflow.worker_id,workflow.attempts,workflow.error,
       workflow.requested_at,workflow.started_at,workflow.updated_at,workflow.finished_at,
       workflow.lease_expires_at,
       coalesce(jsonb_array_length(workflow.parameters->'relations'),0)::integer
           AS relation_count,
       coalesce((workflow.progress->>'population_count')::integer,0)
           AS population_count,
       coalesce((workflow.progress->>'neighborhood_count')::integer,0)
           AS neighborhood_count,
       coalesce((workflow.progress->>'excavation_unit_count')::integer,0)
           AS excavation_unit_count,
       coalesce((workflow.progress->>'work_item_count')::integer,0)
           AS work_item_count,
       coalesce((workflow.progress->>'completed_work_items')::integer,0)
           AS completed_work_items,
       coalesce((workflow.progress->>'llm_attempts')::integer,0)
           AS llm_attempts,
       coalesce((workflow.progress->>'bundles_staged')::integer,0)
           AS bundles_staged,
       nullif(workflow.progress->>'plan_sha256','') AS plan_sha256,
       worker.worker_version,
       worker.last_seen_at AS worker_last_seen_at,
       coalesce(
           worker.last_seen_at>clock_timestamp()-interval '90 seconds',false
       ) AS assigned_worker_online,
       EXISTS (
           SELECT 1
             FROM rvbbit.business_topology_workflow_workers available
            WHERE available.worker_status IN ('ready','busy')
              AND available.last_seen_at>clock_timestamp()-interval '90 seconds'
       ) AS any_worker_online
  FROM rvbbit.business_topology_workflow_runs workflow
  LEFT JOIN rvbbit.business_topology_workflow_workers worker
    ON worker.worker_id=workflow.worker_id;

COMMENT ON TABLE rvbbit.business_topology_workflow_runs IS
    'Durable control-plane jobs for full, resumable Business Topology excavation. Runs stage shadow proposal bundles only and never promote governed topology.';
COMMENT ON VIEW rvbbit.business_topology_workflow_status IS
    'DataRabbit-facing excavation status projection with bounded progress counters and appliance-worker liveness.';
COMMENT ON FUNCTION rvbbit.business_topology_start_workflow(
    text[],text[],integer,integer,integer,integer,text,text,boolean,text
) IS
    'Queue one fresh, bounded Business Topology excavation from SQL. Repeated starts return the existing active run.';
