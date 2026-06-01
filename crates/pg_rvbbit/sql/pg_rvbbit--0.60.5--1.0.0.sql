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
            phase = 'claimed',
            claimed_by = claim_warren_job.node_name,
            claimed_at = clock_timestamp(),
            started_at = COALESCE(started_at, clock_timestamp()),
            attempts = attempts + 1,
            progress = progress || jsonb_build_object(
                'phase', 'claimed',
                'node_name', claim_warren_job.node_name,
                'claimed_at', clock_timestamp()
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
