-- =====================================================================
-- Model Studio: SQL-native model evaluation (predictions-vs-actuals).
-- See docs/MODEL_STUDIO_PLAN.md §3.
--
-- Builds on the existing model lifecycle (rvbbit.ml_models / ml_training_runs /
-- the auto-generated predict_<model> operator). Adds a first-class, recorded,
-- re-runnable evaluation: run a model over a labeled SQL query and compute
-- confusion (classification) or residual metrics (regression). Pure plpgsql
-- over the predict operator, so it runs in psql/DataGrip too, and every
-- evaluation also populates rvbbit.receipts (predictions are operators).
--
-- Idempotent; psql -f loadable and compiled in via src/model_studio.rs.
-- =====================================================================

CREATE TABLE IF NOT EXISTS rvbbit.ml_evaluations (
    eval_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name   text NOT NULL,
    task         text,
    eval_name    text,
    eval_sql     text NOT NULL,
    label_column text,
    n_rows       bigint,
    metrics      jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'ok',   -- ok | failed | running
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ml_evaluations_model_idx
    ON rvbbit.ml_evaluations (model_name, created_at DESC);

-- ---------------------------------------------------------------------
-- Evaluate a registered model against a labeled SQL query.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.evaluate_model(
    model_name   text,
    eval_sql     text,
    label_column text DEFAULT NULL,
    eval_name    text DEFAULT NULL,
    opts         jsonb DEFAULT '{}'::jsonb)
RETURNS uuid
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    m          record;
    v_op       text;
    v_task     text;
    v_label    text;
    v_is_class boolean;
    v_eval_id  uuid;
    v_metrics  jsonb;
BEGIN
    SELECT * INTO m FROM rvbbit.ml_models WHERE name = model_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'evaluate_model: model % not found', model_name;
    END IF;

    v_op := m.operator_name;
    IF v_op IS NULL OR btrim(v_op) = '' THEN
        RAISE EXCEPTION 'evaluate_model: model % has no predict operator (operator_name)', model_name;
    END IF;
    v_task  := COALESCE(NULLIF(btrim(m.task), ''), 'classification');
    v_label := COALESCE(NULLIF(btrim(COALESCE(label_column, '')), ''), m.target_column);
    IF v_label IS NULL OR btrim(v_label) = '' THEN
        RAISE EXCEPTION 'evaluate_model: label_column not supplied and model has no target_column';
    END IF;
    v_is_class := v_task IN ('classification', 'tabular_classification');

    INSERT INTO rvbbit.ml_evaluations
        (model_name, task, eval_name, eval_sql, label_column, status)
    VALUES (model_name, v_task, eval_name, eval_sql, v_label, 'running')
    RETURNING eval_id INTO v_eval_id;

    BEGIN
        IF v_is_class THEN
            EXECUTE format($q$
                WITH raw AS (
                    SELECT (_e.%I)::text AS actual,
                           rvbbit.%I(to_jsonb(_e)) AS pj
                    FROM (%s) _e
                ),
                p AS (
                    SELECT actual, COALESCE(pj->>'label', pj->>'prediction') AS pred
                    FROM raw
                )
                SELECT jsonb_build_object(
                    'n', count(*),
                    'accuracy', avg((actual IS NOT DISTINCT FROM pred)::int)::float8,
                    'labels', (SELECT COALESCE(to_jsonb(array_agg(DISTINCT l ORDER BY l)), '[]'::jsonb)
                                 FROM (SELECT actual AS l FROM p WHERE actual IS NOT NULL
                                       UNION SELECT pred FROM p WHERE pred IS NOT NULL) u),
                    'confusion', COALESCE((
                        SELECT jsonb_agg(jsonb_build_object('actual', actual, 'predicted', pred, 'n', c)
                                         ORDER BY actual, pred)
                        FROM (SELECT actual, pred, count(*) AS c FROM p GROUP BY 1, 2) z), '[]'::jsonb)
                ) FROM p
            $q$, v_label, v_op, eval_sql) INTO v_metrics;
        ELSE
            EXECUTE format($q$
                WITH raw AS (
                    SELECT (_e.%I)::float8 AS actual,
                           rvbbit.%I(to_jsonb(_e)) AS pj
                    FROM (%s) _e
                ),
                p AS (
                    SELECT actual,
                           COALESCE(pj->>'value', pj->>'prediction', pj->>'score')::float8 AS pred
                    FROM raw
                    WHERE actual IS NOT NULL
                ),
                a AS (SELECT avg(actual) AS mean FROM p)
                SELECT jsonb_build_object(
                    'n', count(*),
                    'rmse', sqrt(avg((p.actual - p.pred) ^ 2)),
                    'mae', avg(abs(p.actual - p.pred)),
                    'r2', 1 - (sum((p.actual - p.pred) ^ 2)
                               / NULLIF(sum((p.actual - a.mean) ^ 2), 0)),
                    'residual_sample', COALESCE((
                        SELECT jsonb_agg(jsonb_build_object(
                                 'actual', round(actual::numeric, 6),
                                 'pred', round(pred::numeric, 6)))
                        FROM (SELECT actual, pred FROM p LIMIT 1000) s), '[]'::jsonb)
                ) FROM p, a
            $q$, v_label, v_op, eval_sql) INTO v_metrics;
        END IF;

        UPDATE rvbbit.ml_evaluations
           SET n_rows = (v_metrics->>'n')::bigint,
               metrics = COALESCE(v_metrics, '{}'::jsonb),
               status = 'ok'
         WHERE eval_id = v_eval_id;
    EXCEPTION WHEN others THEN
        UPDATE rvbbit.ml_evaluations
           SET status = 'failed', error = SQLERRM
         WHERE eval_id = v_eval_id;
        RAISE;
    END;

    RETURN v_eval_id;
END $fn$;
