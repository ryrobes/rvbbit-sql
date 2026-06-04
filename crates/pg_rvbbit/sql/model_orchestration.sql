-- =====================================================================
-- Unified Inference Plane — model orchestration / lifecycle / ergonomics.
-- See docs/MODELS_UNIFIED_PLAN.md.
--
-- SQL-first surface that brings the model subsystem (ml_models /
-- ml_training_runs / ml_evaluations + the predict_<model> operator) up to
-- parity with the rest of rvbbit: lifecycle helpers, versioning, monitoring,
-- declarative ergonomics, Warren-ified train+serve, and LLM distillation.
--
-- Idempotent; psql -f loadable and compiled in via src/model_orchestration.rs.
-- Read-only/DML over existing catalog tables; no new heavy infra.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Step 1 — lifecycle, versioning, monitoring
-- ---------------------------------------------------------------------

-- Disable a model (UI/serving filter flag; the operator is left intact).
CREATE OR REPLACE FUNCTION rvbbit.disable_model(p_model text)
RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
BEGIN
    UPDATE rvbbit.ml_models SET status = 'disabled' WHERE name = p_model;
    IF NOT FOUND THEN RAISE EXCEPTION 'disable_model: model % not found', p_model; END IF;
END $fn$;

-- Re-enable: active when an artifact exists, else registered.
CREATE OR REPLACE FUNCTION rvbbit.enable_model(p_model text)
RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
BEGIN
    UPDATE rvbbit.ml_models
       SET status = CASE WHEN artifact_uri IS NOT NULL THEN 'active' ELSE 'registered' END
     WHERE name = p_model;
    IF NOT FOUND THEN RAISE EXCEPTION 'enable_model: model % not found', p_model; END IF;
END $fn$;

-- Cancel a queued/running training run; reset its model to a stable status.
CREATE OR REPLACE FUNCTION rvbbit.cancel_model_training(p_run_id uuid)
RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_model text;
BEGIN
    UPDATE rvbbit.ml_training_runs
       SET status = 'cancelled', finished_at = clock_timestamp()
     WHERE run_id = p_run_id AND status IN ('queued', 'running')
     RETURNING model_name INTO v_model;
    IF v_model IS NULL THEN RETURN; END IF;
    UPDATE rvbbit.ml_models m
       SET status = CASE WHEN m.artifact_uri IS NOT NULL THEN 'active' ELSE 'registered' END
     WHERE m.name = v_model AND m.status IN ('queued', 'running');
END $fn$;

-- Drop a model + its runs + evaluations. Optionally drop the generated
-- predict operator (only auto-named predict_* ops, to avoid clobbering shared
-- pack operators).
CREATE OR REPLACE FUNCTION rvbbit.drop_model(p_model text, drop_operator boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_op text;
BEGIN
    SELECT operator_name INTO v_op FROM rvbbit.ml_models WHERE name = p_model;
    IF NOT FOUND THEN RAISE EXCEPTION 'drop_model: model % not found', p_model; END IF;
    DELETE FROM rvbbit.ml_evaluations WHERE model_name = p_model;
    DELETE FROM rvbbit.ml_models WHERE name = p_model;  -- cascades ml_training_runs
    IF drop_operator AND v_op IS NOT NULL AND v_op LIKE 'predict\_%' THEN
        EXECUTE format('DROP FUNCTION IF EXISTS rvbbit.%I(jsonb, jsonb)', v_op);
        DELETE FROM rvbbit.operators WHERE name = v_op;
    END IF;
END $fn$;

-- Reaper: a worker that claimed a run and died leaves it 'running' forever.
-- Mark over-lease runs failed; flip not-yet-active models to failed.
CREATE OR REPLACE FUNCTION rvbbit.reap_stale_training_runs(max_age interval DEFAULT interval '1 hour')
RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE n bigint;
BEGIN
    WITH reaped AS (
        UPDATE rvbbit.ml_training_runs
           SET status = 'failed',
               error = 'reaped: training lease exceeded ' || max_age::text,
               finished_at = clock_timestamp()
         WHERE status = 'running'
           AND started_at IS NOT NULL
           AND started_at < (clock_timestamp() - max_age)
        RETURNING model_name
    ),
    bump AS (
        UPDATE rvbbit.ml_models m SET status = 'failed'
          FROM reaped r
         WHERE m.name = r.model_name
           AND m.status IN ('queued', 'running')
           AND m.artifact_uri IS NULL
        RETURNING 1
    )
    SELECT count(*) INTO n FROM reaped;
    RETURN n;
END $fn$;

-- Versions: each training run is a version; is_active = artifact currently served.
CREATE OR REPLACE VIEW rvbbit.ml_model_versions AS
SELECT r.model_name,
       r.run_id,
       row_number() OVER (PARTITION BY r.model_name ORDER BY r.created_at) AS version_no,
       r.status,
       r.task,
       r.metrics,
       r.artifact_uri,
       r.operator_name,
       r.worker_id,
       r.created_at,
       r.started_at,
       r.finished_at,
       (r.artifact_uri IS NOT NULL AND r.artifact_uri = m.artifact_uri) AS is_active
FROM rvbbit.ml_training_runs r
JOIN rvbbit.ml_models m ON m.name = r.model_name;

-- Accuracy-over-time: the headline metric per recorded evaluation (for a
-- monitor sparkline). accuracy for classification, r2 for regression.
CREATE OR REPLACE FUNCTION rvbbit.ml_accuracy_series(p_model text)
RETURNS TABLE (
    eval_id      uuid,
    eval_name    text,
    created_at   timestamptz,
    n_rows       bigint,
    metric_name  text,
    metric_value float8)
LANGUAGE sql STABLE AS $fn$
    SELECT e.eval_id, e.eval_name, e.created_at, e.n_rows,
           CASE WHEN e.task IN ('classification', 'tabular_classification') THEN 'accuracy' ELSE 'r2' END,
           CASE WHEN e.task IN ('classification', 'tabular_classification')
                THEN (e.metrics->>'accuracy')::float8 ELSE (e.metrics->>'r2')::float8 END
    FROM rvbbit.ml_evaluations e
    WHERE e.model_name = p_model AND e.status = 'ok'
    ORDER BY e.created_at
$fn$;

-- ---------------------------------------------------------------------
-- Step 2 — declarative / AutoML ergonomics
-- ---------------------------------------------------------------------

-- Infer a trainer feature_schema from a query's output columns (numeric ->
-- float8, everything else -> text), excluding the target column. Uses a
-- WHERE-false temp view so nothing is scanned.
CREATE OR REPLACE FUNCTION rvbbit.infer_feature_schema(source_sql text, target_column text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v jsonb;
BEGIN
    EXECUTE 'DROP VIEW IF EXISTS _rvbbit_infer_cols';
    EXECUTE format('CREATE TEMP VIEW _rvbbit_infer_cols AS SELECT * FROM (%s) _q WHERE false', source_sql);
    SELECT jsonb_agg(jsonb_build_object(
               'name', a.attname,
               'type', CASE WHEN format_type(a.atttypid, a.atttypmod)
                                 ~* '(^|[^a-z])(int|float|numeric|double|real|decimal|serial)'
                            THEN 'float8' ELSE 'text' END)
             ORDER BY a.attnum)
      INTO v
      FROM pg_attribute a
     WHERE a.attrelid = '_rvbbit_infer_cols'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped
       AND (target_column IS NULL OR a.attname <> target_column);
    EXECUTE 'DROP VIEW IF EXISTS _rvbbit_infer_cols';
    RETURN COALESCE(v, '[]'::jsonb);
EXCEPTION WHEN others THEN
    BEGIN EXECUTE 'DROP VIEW IF EXISTS _rvbbit_infer_cols'; EXCEPTION WHEN others THEN NULL; END;
    RAISE;
END $fn$;

-- Pre-flight a training query: confirm it parses, that the target column is
-- produced, infer the feature schema, and estimate row count. Returns a jsonb
-- report instead of raising, so a UI can validate before queueing a run.
CREATE OR REPLACE FUNCTION rvbbit.validate_training_sql(source_sql text, target_column text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_cols           jsonb;
    v_target_present boolean;
    v_features       jsonb;
    v_est            bigint;
    rec              record;
BEGIN
    BEGIN
        EXECUTE 'DROP VIEW IF EXISTS _rvbbit_val_cols';
        EXECUTE format('CREATE TEMP VIEW _rvbbit_val_cols AS SELECT * FROM (%s) _q WHERE false', source_sql);
    EXCEPTION WHEN others THEN
        RETURN jsonb_build_object('ok', false, 'stage', 'parse', 'error', SQLERRM);
    END;

    SELECT jsonb_agg(jsonb_build_object('name', attname, 'type', format_type(atttypid, atttypmod)) ORDER BY attnum),
           bool_or(attname = target_column)
      INTO v_cols, v_target_present
      FROM pg_attribute
     WHERE attrelid = '_rvbbit_val_cols'::regclass AND attnum > 0 AND NOT attisdropped;
    EXECUTE 'DROP VIEW IF EXISTS _rvbbit_val_cols';

    v_features := rvbbit.infer_feature_schema(source_sql, target_column);

    BEGIN
        EXECUTE format('EXPLAIN (FORMAT JSON) %s', source_sql) INTO rec;
        v_est := (rec."QUERY PLAN"->0->'Plan'->>'Plan Rows')::bigint;
    EXCEPTION WHEN others THEN v_est := NULL; END;

    RETURN jsonb_build_object(
        'ok', COALESCE(v_target_present, false),
        'error', CASE WHEN NOT COALESCE(v_target_present, false)
                      THEN format('target column "%s" not found in query output', target_column) END,
        'columns', COALESCE(v_cols, '[]'::jsonb),
        'target_present', COALESCE(v_target_present, false),
        'feature_schema', v_features,
        'n_features', jsonb_array_length(v_features),
        'est_rows', v_est);
END $fn$;

-- ---------------------------------------------------------------------
-- Step 3 — Warren-ify train + serve (A1 / A2)
-- ---------------------------------------------------------------------

-- Allow a dedicated training job kind alongside the existing deploy kinds.
ALTER TABLE rvbbit.warren_jobs DROP CONSTRAINT IF EXISTS warren_jobs_kind_check;
ALTER TABLE rvbbit.warren_jobs ADD CONSTRAINT warren_jobs_kind_check
    CHECK (kind IN ('capability', 'trained_model', 'mcp_server', 'compose', 'custom', 'model_training'));

-- Managed training: queue a run AND a Warren job that targets a host/GPU
-- (target = warren target_selector). feature_schema NULL => inferred from
-- source_sql. A Warren-side trainer worker claims the job (below), trains, and
-- registers; if deploy, a serving sidecar is then stood up.
CREATE OR REPLACE FUNCTION rvbbit.train_model_managed(
    model_name     text,
    source_sql     text,
    target_column  text,
    task           text    DEFAULT 'classification',
    feature_schema jsonb   DEFAULT NULL,
    training_opts  jsonb   DEFAULT '{}'::jsonb,
    description    text    DEFAULT NULL,
    deploy         boolean DEFAULT true,
    target         jsonb   DEFAULT '{}'::jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_features jsonb;
    v_run      uuid;
    v_job      uuid;
BEGIN
    v_features := COALESCE(feature_schema, rvbbit.infer_feature_schema(source_sql, target_column));
    v_run := rvbbit.train_model(model_name, source_sql, target_column, task, v_features, training_opts, description);
    v_job := rvbbit.enqueue_warren_job(
        'model_training',
        'train:' || model_name,
        jsonb_build_object('subkind', 'model_training', 'run_id', v_run::text,
                           'model_name', model_name, 'task', task, 'deploy', deploy),
        COALESCE(target, '{}'::jsonb));
    RETURN jsonb_build_object('run_id', v_run, 'training_job_id', v_job, 'feature_schema', v_features);
END $fn$;

-- Worker-side claim of one queued training job (Warren job + its run), flipping
-- both to running. Returns the full training spec for the trainer to execute.
CREATE OR REPLACE FUNCTION rvbbit.claim_model_training_job(worker_id text DEFAULT NULL)
RETURNS TABLE (
    job_id          uuid,
    run_id          uuid,
    model_name      text,
    task            text,
    source_sql      text,
    target_column   text,
    feature_schema  jsonb,
    training_opts   jsonb,
    deploy          boolean,
    target_selector jsonb)
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_worker text := COALESCE(worker_id, current_setting('application_name', true));
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT j.job_id, j.manifest, j.target_selector
        FROM rvbbit.warren_jobs j
        WHERE j.kind = 'model_training' AND j.status = 'queued'
        ORDER BY j.created_at LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    upd_job AS (
        UPDATE rvbbit.warren_jobs j
           SET status = 'running', phase = 'training',
               claimed_by = v_worker, claimed_at = clock_timestamp(), started_at = clock_timestamp()
          FROM picked WHERE j.job_id = picked.job_id
        RETURNING j.job_id, j.manifest, j.target_selector
    ),
    upd_run AS (
        UPDATE rvbbit.ml_training_runs r
           SET status = 'running', worker_id = v_worker, started_at = clock_timestamp()
          FROM upd_job u
         WHERE r.run_id = (u.manifest->>'run_id')::uuid AND r.status = 'queued'
        RETURNING r.run_id, r.model_name, r.task, r.source_sql, r.target_column, r.feature_schema, r.training_opts
    ),
    upd_model AS (
        UPDATE rvbbit.ml_models m SET status = 'running'
          FROM upd_run ur WHERE m.name = ur.model_name
        RETURNING m.name
    )
    SELECT u.job_id, ur.run_id, ur.model_name, ur.task, ur.source_sql, ur.target_column,
           ur.feature_schema, ur.training_opts,
           COALESCE((u.manifest->>'deploy')::boolean, false), u.target_selector
    FROM upd_job u JOIN upd_run ur ON true;
END $fn$;

-- A2: (re)deploy a registered model's serving sidecar as a Warren 'trained_model'
-- job on a chosen host/GPU (target = warren target_selector). Reuses the
-- existing trained_model -> deploy_capability path.
CREATE OR REPLACE FUNCTION rvbbit.deploy_model_serving(p_model text, target jsonb DEFAULT '{}'::jsonb)
RETURNS uuid LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE m record; v_manifest jsonb;
BEGIN
    SELECT * INTO m FROM rvbbit.ml_models WHERE name = p_model;
    IF NOT FOUND THEN RAISE EXCEPTION 'deploy_model_serving: model % not found', p_model; END IF;
    v_manifest := COALESCE(m.install_manifest, '{}'::jsonb) || jsonb_build_object(
        'model_name', p_model, 'backend_name', m.backend_name, 'operator_name', m.operator_name,
        'artifact_uri', m.artifact_uri, 'task', m.task, 'feature_schema', m.feature_schema);
    RETURN rvbbit.enqueue_warren_job('trained_model', 'serve:' || p_model, v_manifest, COALESCE(target, '{}'::jsonb));
END $fn$;

-- ---------------------------------------------------------------------
-- Step 4 — LLM/operator distillation (label a sample -> train a cheap model)
-- ---------------------------------------------------------------------

-- Label up to n_label rows of unlabeled_sql with label_expr (any SQL expression
-- over the query's columns — typically a semantic/LLM operator like
-- rvbbit.classify(body, 'a,b,c')), materialize a labeled training table, and
-- train a cheap model on it. The labeling calls flow through rvbbit.receipts,
-- so the cost-to-build (LLM) vs cost-to-serve (model ~0) is queryable.
CREATE OR REPLACE FUNCTION rvbbit.distill_model(
    model_name     text,
    unlabeled_sql  text,
    label_expr     text,
    n_label        int     DEFAULT 500,
    label_column   text    DEFAULT 'distilled_label',
    task           text    DEFAULT 'classification',
    training_opts  jsonb   DEFAULT '{}'::jsonb,
    staging_schema text    DEFAULT 'rvbbit_distill',
    managed        boolean DEFAULT false,
    target         jsonb   DEFAULT '{}'::jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_table    text;
    v_n        bigint;
    v_features jsonb;
    v_src_sql  text;
    v_train    jsonb;
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', staging_schema);
    v_table := quote_ident(staging_schema) || '.'
            || quote_ident(regexp_replace(lower(model_name), '[^a-z0-9_]+', '_', 'g') || '_labeled');
    EXECUTE format('DROP TABLE IF EXISTS %s', v_table);
    -- Bound the labeler to exactly n_label rows, then label them.
    EXECUTE format(
        'CREATE TABLE %s AS SELECT _src.*, (%s) AS %I FROM (SELECT * FROM (%s) _u0 LIMIT %s) _src',
        v_table, label_expr, label_column, unlabeled_sql, n_label);
    EXECUTE format('SELECT count(*) FROM %s', v_table) INTO v_n;

    v_src_sql := format('SELECT * FROM %s', v_table);
    v_features := rvbbit.infer_feature_schema(v_src_sql, label_column);

    IF managed THEN
        v_train := rvbbit.train_model_managed(model_name, v_src_sql, label_column, task, v_features,
                       training_opts, format('Distilled from operator over %s rows', v_n), true, target);
    ELSE
        v_train := jsonb_build_object('run_id',
                       rvbbit.train_model(model_name, v_src_sql, label_column, task, v_features,
                           training_opts, format('Distilled from operator over %s rows', v_n)));
    END IF;

    RETURN jsonb_build_object('model', model_name, 'labeled_table', v_table, 'n_labeled', v_n,
                              'label_column', label_column, 'feature_schema', v_features, 'train', v_train);
END $fn$;

-- ---------------------------------------------------------------------
-- Step 5 — tabular foundation model (training-free, in-context prediction)
-- ---------------------------------------------------------------------

-- Ship a labeled support set + the rows to score to a tabular FOUNDATION model
-- (TabPFN-class) served as a GPU specialist, with NO training step. The
-- foundation model predicts the queries in-context from the support set.
--
-- The live path requires a `tabular_foundation` capability deployed (a GPU
-- micro-warren) exposing a foundation operator (default predict_tabular_foundation).
-- Use dry_run => true to inspect the assembled {support, queries} bundle without
-- a foundation backend.
CREATE OR REPLACE FUNCTION rvbbit.predict_tabular(
    support_sql         text,
    predict_sql         text,
    target_column       text,
    task                text    DEFAULT 'classification',
    foundation_operator text    DEFAULT 'predict_tabular_foundation',
    dry_run             boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_support jsonb; v_queries jsonb; v_bundle jsonb; v_result jsonb;
    v_n_sup bigint; v_n_q bigint;
BEGIN
    EXECUTE format('SELECT COALESCE(jsonb_agg(to_jsonb(_s)), ''[]''::jsonb), count(*) FROM (%s) _s', support_sql)
        INTO v_support, v_n_sup;
    EXECUTE format('SELECT COALESCE(jsonb_agg(to_jsonb(_q)), ''[]''::jsonb), count(*) FROM (%s) _q', predict_sql)
        INTO v_queries, v_n_q;

    v_bundle := jsonb_build_object('task', task, 'target', target_column,
                                   'support', v_support, 'queries', v_queries);

    IF dry_run THEN
        RETURN jsonb_build_object('n_support', v_n_sup, 'n_queries', v_n_q,
                                  'task', task, 'target', target_column,
                                  'sample_support', v_support->0, 'sample_query', v_queries->0);
    END IF;

    IF to_regprocedure('rvbbit.' || quote_ident(foundation_operator) || '(jsonb)') IS NULL
       AND to_regprocedure('rvbbit.' || quote_ident(foundation_operator) || '(jsonb, jsonb)') IS NULL THEN
        RAISE EXCEPTION 'predict_tabular: foundation operator rvbbit.% not found — deploy a tabular_foundation capability (e.g. tabpfn) first', foundation_operator;
    END IF;

    EXECUTE format('SELECT rvbbit.%I($1)', foundation_operator) USING v_bundle INTO v_result;
    RETURN jsonb_build_object('n_queries', v_n_q, 'predictions', v_result);
END $fn$;
