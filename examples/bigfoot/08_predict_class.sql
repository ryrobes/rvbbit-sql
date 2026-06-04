\set ON_ERROR_STOP on
\pset pager off
\timing on

\if :{?train_estimators}
\else
\set train_estimators 64
\endif

\if :{?train_seed}
\else
\set train_seed 13
\endif

\if :{?train_wait_seconds}
\else
\set train_wait_seconds 180
\endif

\echo
\echo ====================================================================
\echo 08. Predict the sighting class: train a tabular model from SQL
\echo ====================================================================
\echo Forest size: :train_estimators  seed: :train_seed  worker wait: :train_wait_seconds s
\echo
\echo This section trains a scikit-learn classifier from a SELECT. Training is
\echo done by the external rvbbit-trainer worker, so a worker must be running.
\echo If one is not, start it in another shell (it claims the queued run, fits it,
\echo and serves it locally):
\echo
\echo     rvbbit-trainer watch --include-unmanaged --serve-local --serve-host <db-reachable-host>
\echo

-- 1. Show the deterministic train/holdout split (stable hash of bfroid).
SELECT
    CASE WHEN mod(abs(hashtext(bfroid)), 4) < 3 THEN 'train' ELSE 'holdout' END AS split,
    count(*) AS reports,
    count(*) FILTER (WHERE class = 'Class A') AS class_a,
    count(*) FILTER (WHERE class = 'Class B') AS class_b
FROM bigfoot.sighting_docs
WHERE class IN ('Class A', 'Class B')
GROUP BY 1
ORDER BY 1;

-- 2. Start clean so the script is repeatable: drop a prior model + its operator,
--    and clear any cached predictions for the operator so the holdout eval is
--    honest on a re-run (operators memoize results by input in rvbbit.receipts).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM rvbbit.ml_models WHERE name = 'bigfoot_class') THEN
        PERFORM rvbbit.drop_model('bigfoot_class', drop_operator => true);
    END IF;
END $$;

DELETE FROM rvbbit.receipts WHERE operator = 'predict_bigfoot_class';

-- 3. Train on the 75% split. train_model queues a run and returns its id; it does
--    not block on training.
SELECT rvbbit.train_model(
    model_name    => 'bigfoot_class',
    source_sql    => $$
        SELECT
            report_year::float8           AS report_year,
            NULLIF(btrim(season), '')      AS season,
            NULLIF(btrim(state), '')       AS state,
            NULLIF(btrim(county), '')      AS county,
            NULLIF(btrim(nearesttown), '') AS nearesttown,
            NULLIF(btrim(nearestroad), '') AS nearestroad,
            NULLIF(btrim(environment), '') AS environment,
            class                          AS class_label
        FROM bigfoot.sighting_docs
        WHERE class IN ('Class A', 'Class B')
          AND mod(abs(hashtext(bfroid)), 4) < 3   -- ~75% train split
    $$,
    target_column => 'class_label',
    task          => 'classification',
    feature_schema => $$[
        {"name":"report_year","type":"float8"},
        {"name":"season","type":"text"},
        {"name":"state","type":"text"},
        {"name":"county","type":"text"},
        {"name":"nearesttown","type":"text"},
        {"name":"nearestroad","type":"text"},
        {"name":"environment","type":"text"}
    ]$$::jsonb,
    training_opts => jsonb_build_object(
        'estimator', 'random_forest',
        'n_estimators', :train_estimators,
        'random_state', :train_seed,
        'test_size', 0.25
    ),
    description   => 'Predict BFRO report class (A vs B) from location/time columns.'
) AS run_id \gset

\echo Queued training run :run_id
\echo (to fit only this run instead of a watcher:  rvbbit-trainer train-run :run_id --serve-local --serve-host <db-reachable-host>)

-- 4. Wait, bounded, for a worker to fit and serve the model (status -> active).
--    psql does not interpolate :vars inside a dollar-quoted body, so pass the
--    timeout into the block through a session setting.
SELECT set_config('bigfoot.train_wait_seconds', :'train_wait_seconds', false);

DO $$
DECLARE
    deadline timestamptz := clock_timestamp()
        + make_interval(secs => current_setting('bigfoot.train_wait_seconds')::int);
    st text;
BEGIN
    LOOP
        SELECT status INTO st FROM rvbbit.ml_model_status WHERE name = 'bigfoot_class';
        EXIT WHEN st = 'active';
        EXIT WHEN clock_timestamp() > deadline;
        PERFORM pg_sleep(2);
    END LOOP;
    RAISE NOTICE 'bigfoot_class status after wait: %', COALESCE(st, '<not found>');
END $$;

-- 5. Predict + evaluate only if a worker brought the model online. Use a scalar
--    subquery wrapped in COALESCE so this always returns exactly one boolean row
--    (even if the model row is somehow absent), keeping \if :model_ready safe.
SELECT COALESCE(
    (SELECT status = 'active' FROM rvbbit.ml_model_status WHERE name = 'bigfoot_class'),
    false
) AS model_ready \gset

\if :model_ready

\echo
\echo Model is active. Scoring a new report:
SELECT rvbbit.predict_bigfoot_class(jsonb_build_object(
    'report_year', 1998,
    'season', 'Summer',
    'state', 'Ohio',
    'county', 'Athens County',
    'nearesttown', 'Athens',
    'nearestroad', 'US-33',
    'environment', 'Forested hills and creek bottoms'
)) AS prediction;

\echo
\echo Honest evaluation on the held-out 25% the model never trained on:
SELECT rvbbit.evaluate_model(
    model_name   => 'bigfoot_class',
    eval_sql     => $$
        SELECT
            report_year::float8           AS report_year,
            NULLIF(btrim(season), '')      AS season,
            NULLIF(btrim(state), '')       AS state,
            NULLIF(btrim(county), '')      AS county,
            NULLIF(btrim(nearesttown), '') AS nearesttown,
            NULLIF(btrim(nearestroad), '') AS nearestroad,
            NULLIF(btrim(environment), '') AS environment,
            class                          AS class_label
        FROM bigfoot.sighting_docs
        WHERE class IN ('Class A', 'Class B')
          AND mod(abs(hashtext(bfroid)), 4) = 3   -- the held-out 25%
    $$,
    label_column => 'class_label',
    eval_name    => 'bigfoot_class_holdout'
) AS eval_id \gset

SELECT n_rows,
       round((metrics->>'accuracy')::numeric, 3) AS accuracy
FROM rvbbit.ml_evaluations
WHERE eval_id = :'eval_id';

\echo Confusion matrix (held-out reports). A model that just guesses the majority
\echo class lands almost everything in the Class A column.
SELECT c->>'actual'    AS actual,
       c->>'predicted' AS predicted,
       (c->>'n')::int  AS n
FROM rvbbit.ml_evaluations e,
     LATERAL jsonb_array_elements(e.metrics->'confusion') c
WHERE e.eval_id = :'eval_id'
ORDER BY actual, predicted;

\else

\echo
\echo bigfoot_class is not active yet -- no worker fit it within :train_wait_seconds s.
\echo Start a worker as shown above, then re-run this script to predict and evaluate.

\endif
