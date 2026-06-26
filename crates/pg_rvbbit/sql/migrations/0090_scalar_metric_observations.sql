-- 0090_scalar_metric_observations
--
-- Tighten the product contract around materialized metrics: a persisted metric
-- observation is a scalar value at a data-time, not an arbitrary rowset cache.
-- The older rvbbit.metric() rowset runner remains available for semantic SQL
-- composition and ad-hoc inspection, but materialize_metric() now persists the
-- explicit scalar shape returned by rvbbit.metric_scalar().

CREATE OR REPLACE FUNCTION rvbbit.metric_scalar(
    p_name       text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_labels jsonb := '{}'::jsonb;
    v_rows   jsonb;
    v_count  bigint := 0;
    v_row    jsonb;
    v_cols   integer := 0;
    v_col    text;
    v_value  jsonb;
BEGIN
    SELECT coalesce(labels, '{}'::jsonb)
      INTO v_labels
    FROM rvbbit.metric_defs
    WHERE name = p_name
      AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.metric_scalar: metric "%" is not defined as of %',
            p_name, p_def_as_of;
    END IF;

    SELECT count(*), jsonb_agg(obj)
      INTO v_count, v_rows
    FROM rvbbit.metric(p_name, p_params, p_def_as_of, p_data_as_of) AS m(obj);

    IF coalesce(v_count, 0) = 0 THEN
        RAISE EXCEPTION 'rvbbit.metric_scalar: metric "%" returned no rows', p_name
            USING HINT = 'A materialized metric must return exactly one row and one headline value.';
    END IF;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'rvbbit.metric_scalar: metric "%" returned % rows; materialized metrics must be scalar',
            p_name, v_count
            USING HINT = 'Aggregate the definition to one row, or keep this query as a cube/dataset instead of a metric.';
    END IF;

    v_row := v_rows->0;
    IF jsonb_typeof(v_row) <> 'object' THEN
        RETURN jsonb_build_object('value', v_row);
    END IF;

    v_col := nullif(btrim(coalesce(v_labels->>'metric_value_column', v_labels->>'value_column')), '');
    IF v_col IS NOT NULL THEN
        IF NOT (v_row ? v_col) THEN
            RAISE EXCEPTION 'rvbbit.metric_scalar: metric "%" labels.metric_value_column "%" is not present in result row %',
                p_name, v_col, v_row;
        END IF;
    ELSIF v_row ? 'value' THEN
        v_col := 'value';
    ELSE
        SELECT count(*) INTO v_cols FROM jsonb_object_keys(v_row);
        IF v_cols = 1 THEN
            SELECT key INTO v_col FROM jsonb_each(v_row) AS e(key, value) LIMIT 1;
        ELSE
            RAISE EXCEPTION 'rvbbit.metric_scalar: metric "%" returned one row with % columns and no value column: %',
                p_name, v_cols, v_row
                USING HINT = 'Return one column, alias the headline as "value", or set labels.metric_value_column.';
        END IF;
    END IF;

    v_value := v_row->v_col;
    RETURN jsonb_build_object('value', v_value, 'value_column', v_col)
        || CASE
             WHEN (v_row - v_col) <> '{}'::jsonb THEN jsonb_build_object('row', v_row)
             ELSE '{}'::jsonb
           END;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.materialize_metric(
    p_name            text,
    p_params          jsonb DEFAULT '{}'::jsonb,
    p_def_as_of       timestamptz DEFAULT now(),
    p_data_as_of      timestamptz DEFAULT NULL,
    p_data_generation bigint DEFAULT NULL,
    p_trigger         text DEFAULT 'manual'
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
    v_value   jsonb;
    v_verdict jsonb;
    v_obs_id  bigint;
BEGIN
    SELECT version INTO v_version
    FROM rvbbit.metric_defs
    WHERE name = p_name AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC LIMIT 1;
    IF v_version IS NULL THEN
        RAISE EXCEPTION 'rvbbit.materialize_metric: metric "%" not defined as of %', p_name, p_def_as_of;
    END IF;

    v_value := rvbbit.metric_scalar(p_name, p_params, p_def_as_of, p_data_as_of);
    v_verdict := rvbbit.check_metric(p_name, p_params, p_def_as_of, p_data_as_of);

    INSERT INTO rvbbit.metric_observations
        (metric_name, metric_version, def_as_of, data_as_of, data_generation,
         params, value, verdict, status, trigger)
    VALUES
        (p_name, v_version, p_def_as_of, coalesce(p_data_as_of, now()), p_data_generation,
         coalesce(p_params, '{}'::jsonb), v_value, v_verdict, v_verdict->>'status', p_trigger)
    RETURNING observation_id INTO v_obs_id;
    RETURN v_obs_id;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.metric_scalar_audit(
    p_metrics    text[]      DEFAULT NULL,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS TABLE(metric_name text, ok boolean, value jsonb, error text)
LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT name
        FROM rvbbit.metric_catalog
        WHERE p_metrics IS NULL OR name = ANY(p_metrics)
        ORDER BY name
    LOOP
        metric_name := rec.name;
        BEGIN
            value := rvbbit.metric_scalar(rec.name, '{}'::jsonb, p_def_as_of, p_data_as_of);
            ok := true;
            error := NULL;
        EXCEPTION WHEN OTHERS THEN
            value := NULL;
            ok := false;
            error := left(SQLERRM, 500);
        END;
        RETURN NEXT;
    END LOOP;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.scalarize_latest_metric(
    p_name         text,
    p_time_column  text,
    p_value_column text,
    p_grain        text DEFAULT NULL,
    p_description  text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    cur rvbbit.metric_defs%ROWTYPE;
    v_body text;
    v_sql text;
BEGIN
    SELECT *
      INTO cur
    FROM rvbbit.metric_defs
    WHERE name = p_name
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.scalarize_latest_metric: metric % does not exist', p_name;
    END IF;
    IF nullif(btrim(p_time_column), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.scalarize_latest_metric: time column is required';
    END IF;
    IF nullif(btrim(p_value_column), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.scalarize_latest_metric: value column is required';
    END IF;

    v_body := regexp_replace(cur.sql, E';\\s*$', '');
    v_sql := format(
        'SELECT %1$I AS value FROM (%2$s) _series WHERE %3$I IS NOT NULL ORDER BY %3$I DESC LIMIT 1',
        p_value_column,
        v_body,
        p_time_column
    );

    RETURN rvbbit.revise_metric(
        p_name,
        p_sql         => v_sql,
        p_grain       => coalesce(p_grain, 'latest available period'),
        p_description => coalesce(p_description, coalesce(cur.description, p_name) || ' (latest available period)'),
        p_params      => cur.params,
        p_check_sql   => cur.check_sql,
        p_owner       => cur.owner,
        p_labels      => coalesce(cur.labels, '{}'::jsonb) || jsonb_build_object(
            'metric_kind', 'scalar',
            'scalarized_from', 'latest_period',
            'series_time_column', p_time_column,
            'metric_value_column', 'value',
            'source_value_column', p_value_column
        )
    );
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.scalarize_metric_observations_latest(
    p_name         text,
    p_time_column  text,
    p_value_column text
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_updated integer := 0;
BEGIN
    WITH latest AS (
        SELECT o.observation_id, e.elem
        FROM rvbbit.metric_observations o
        CROSS JOIN LATERAL (
            SELECT elem
            FROM jsonb_array_elements(o.value) AS elem
            WHERE elem ? p_time_column
              AND elem ? p_value_column
              AND nullif(elem->>p_time_column, '') IS NOT NULL
            ORDER BY elem->>p_time_column DESC
            LIMIT 1
        ) e
        WHERE o.metric_name = p_name
          AND jsonb_typeof(o.value) = 'array'
    )
    UPDATE rvbbit.metric_observations o
       SET value = jsonb_build_object(
             'value', latest.elem->p_value_column,
             'value_column', 'value',
             'source_value_column', p_value_column,
             'source_time_column', p_time_column,
             'source_time', latest.elem->p_time_column
           )
      FROM latest
     WHERE o.observation_id = latest.observation_id;

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    RETURN v_updated;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.scalarize_metric_observations_count(
    p_name text
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_updated integer := 0;
BEGIN
    UPDATE rvbbit.metric_observations
       SET value = jsonb_build_object(
             'value', jsonb_array_length(value),
             'value_column', 'value',
             'source_value', 'rowset_count'
           )
     WHERE metric_name = p_name
       AND jsonb_typeof(value) = 'array';

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    RETURN v_updated;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.scalarize_metric_observations_one_row(
    p_metrics text[] DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
    v_col text;
    v_cols integer;
    v_updated integer := 0;
BEGIN
    FOR rec IN
        SELECT o.observation_id,
               o.metric_name,
               o.value->0 AS row_obj,
               coalesce(mc.labels, '{}'::jsonb) AS labels
        FROM rvbbit.metric_observations o
        LEFT JOIN rvbbit.metric_catalog mc ON mc.name = o.metric_name
        WHERE jsonb_typeof(o.value) = 'array'
          AND jsonb_array_length(o.value) = 1
          AND (p_metrics IS NULL OR o.metric_name = ANY(p_metrics))
    LOOP
        v_col := nullif(btrim(coalesce(rec.labels->>'metric_value_column', rec.labels->>'value_column')), '');
        IF v_col IS NOT NULL AND NOT (rec.row_obj ? v_col) THEN
            v_col := NULL;
        END IF;

        IF v_col IS NULL AND rec.row_obj ? 'value' THEN
            v_col := 'value';
        END IF;

        IF v_col IS NULL THEN
            SELECT count(*) INTO v_cols FROM jsonb_object_keys(rec.row_obj);
            IF v_cols = 1 THEN
                SELECT key INTO v_col FROM jsonb_each(rec.row_obj) AS e(key, value) LIMIT 1;
            END IF;
        END IF;

        IF v_col IS NOT NULL THEN
            UPDATE rvbbit.metric_observations
               SET value = jsonb_build_object('value', rec.row_obj->v_col, 'value_column', v_col)
                    || CASE
                         WHEN (rec.row_obj - v_col) <> '{}'::jsonb THEN jsonb_build_object('row', rec.row_obj)
                         ELSE '{}'::jsonb
                       END
             WHERE observation_id = rec.observation_id;
            v_updated := v_updated + 1;
        END IF;
    END LOOP;

    RETURN v_updated;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.promote_cube_to_metric(
    p_cube_name text, p_metric_name text,
    p_description text DEFAULT NULL, p_owner text DEFAULT NULL, p_grain text DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_cube_grain text;
    v_sql text;
    v_version integer;
BEGIN
    SELECT grain INTO v_cube_grain FROM rvbbit.cube_catalog WHERE name = p_cube_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.promote_cube_to_metric: cube % not found', p_cube_name;
    END IF;

    v_sql := 'SELECT count(*)::bigint AS value FROM cubes.' || quote_ident(p_cube_name);
    v_version := rvbbit.define_metric(
        p_metric_name, v_sql, '{}'::jsonb,
        coalesce(p_grain, 'cube row count'),
        coalesce(p_description, 'Row count for cube ' || p_cube_name),
        p_owner,
        jsonb_build_object(
            'cube_source', p_cube_name,
            'metric_value_column', 'value',
            'metric_kind', 'scalar'
        ),
        NULL);
    RETURN v_version;
END;
$fn$;
