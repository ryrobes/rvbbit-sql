-- =====================================================================
-- rvbbit 1.2.9 -> 1.2.10 : KPI checks / thresholds
-- =====================================================================
-- A metric becomes a KPI when its def carries a `check_sql`. The check runs
-- against the metric's result (exposed as a CTE named `metric`) and must reduce
-- to exactly ONE verdict row yielding an `ok` boolean (optionally
-- status/value/target/...). Thresholds are {param} tokens — versioned defaults,
-- overridable per call. Because check_sql lives on the versioned def row, the
-- threshold is bitemporal: def_as_of pins metric+check+threshold, data_as_of
-- pins the data.

ALTER TABLE rvbbit.metric_defs ADD COLUMN IF NOT EXISTS check_sql text;

-- Fix (applies to checks AND plain metrics): pin "latest" to an explicit now()
-- so the snapshot floor reaches the metric body, which runs nested in a subquery.
-- Empty/implicit latest is only honored for TOP-LEVEL scans; a nested scan read
-- cumulatively (all generations). now() == latest for snapshot (= latest gen) and
-- append (<= now()) tables, and an explicit AS OF propagates via the GUC.
CREATE OR REPLACE FUNCTION rvbbit.metric(
    p_name       text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS SETOF jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql   text;
    v_saved text;
BEGIN
    v_sql := rvbbit.metric_sql(p_name, p_params, p_def_as_of);

    v_saved := current_setting('rvbbit.as_of_timestamp', true);
    PERFORM set_config('rvbbit.as_of_timestamp',
                       coalesce(p_data_as_of::text, now()::text), true);

    BEGIN
        RETURN QUERY EXECUTE 'SELECT to_jsonb(t) FROM (' || v_sql || ') AS t';
    EXCEPTION WHEN OTHERS THEN
        PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
        RAISE EXCEPTION 'rvbbit.metric("%"): % | SQL: %', p_name, SQLERRM, v_sql;
    END;

    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
    RETURN;
END;
$fn$;

-- View gains check_sql (column reorder ⇒ DROP+CREATE, not OR REPLACE).
DROP VIEW IF EXISTS rvbbit.metric_catalog;
CREATE VIEW rvbbit.metric_catalog AS
SELECT DISTINCT ON (name)
    name, version, sql, params, grain, description, owner, labels, check_sql, created_at
FROM rvbbit.metric_defs
ORDER BY name, created_at DESC, version DESC;

-- define_metric gains p_check (signature change ⇒ DROP the old overload).
DROP FUNCTION IF EXISTS rvbbit.define_metric(text, text, jsonb, text, text, text, jsonb);
CREATE OR REPLACE FUNCTION rvbbit.define_metric(
    p_name        text,
    p_sql         text,
    p_params      jsonb DEFAULT '{}'::jsonb,
    p_grain       text  DEFAULT NULL,
    p_description text  DEFAULT NULL,
    p_owner       text  DEFAULT NULL,
    p_labels      jsonb DEFAULT '{}'::jsonb,
    p_check       text  DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
BEGIN
    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_metric: name is required';
    END IF;
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_metric: sql is required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.metric:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version
    FROM rvbbit.metric_defs WHERE name = p_name;
    INSERT INTO rvbbit.metric_defs
        (name, version, sql, params, grain, description, owner, labels, check_sql)
    VALUES
        (p_name, v_version, p_sql, coalesce(p_params, '{}'::jsonb), p_grain,
         p_description, p_owner, coalesce(p_labels, '{}'::jsonb),
         CASE WHEN btrim(coalesce(p_check, '')) = '' THEN NULL ELSE p_check END);
    RETURN v_version;
END;
$fn$;

-- metric_versions gains check_sql (return-type change ⇒ DROP+CREATE).
DROP FUNCTION IF EXISTS rvbbit.metric_versions(text);
CREATE OR REPLACE FUNCTION rvbbit.metric_versions(p_name text)
RETURNS TABLE(version integer, created_at timestamptz, sql text, params jsonb,
              grain text, description text, owner text, check_sql text)
LANGUAGE sql STABLE AS $fn$
    SELECT version, created_at, sql, params, grain, description, owner, check_sql
    FROM rvbbit.metric_defs
    WHERE name = p_name
    ORDER BY version DESC;
$fn$;

-- Compose + evaluate an already-resolved metric body + check body; one verdict.
CREATE OR REPLACE FUNCTION rvbbit._run_check(
    p_metric_sql text,
    p_check_sql  text,
    p_data_as_of timestamptz
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_full    text;
    v_verdict jsonb;
    v_saved   text;
BEGIN
    IF p_check_sql IS NULL OR btrim(p_check_sql) = '' THEN
        RETURN NULL;
    END IF;

    v_full := 'WITH metric AS (' || p_metric_sql || E'\n) ' || p_check_sql;

    -- Explicit instant for "latest" (now()) so the snapshot floor reaches the
    -- nested `metric` CTE (the implicit floor only applies to top-level scans).
    v_saved := current_setting('rvbbit.as_of_timestamp', true);
    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(p_data_as_of::text, now()::text), true);

    BEGIN
        EXECUTE 'SELECT to_jsonb(t) FROM (' || v_full || ') t' INTO STRICT v_verdict;
    EXCEPTION
        WHEN TOO_MANY_ROWS THEN
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
            RAISE EXCEPTION 'rvbbit check returned more than one row; reduce the metric CTE to a single verdict row. | SQL: %', v_full;
        WHEN NO_DATA_FOUND THEN
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
            RAISE EXCEPTION 'rvbbit check returned no rows. | SQL: %', v_full;
        WHEN OTHERS THEN
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
            RAISE EXCEPTION 'rvbbit check failed: % | SQL: %', SQLERRM, v_full;
    END;

    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);

    IF v_verdict IS NULL OR NOT (v_verdict ? 'ok') THEN
        RAISE EXCEPTION 'rvbbit check must yield an "ok" boolean column (got: %)',
            coalesce(v_verdict::text, 'no row');
    END IF;

    IF NOT (v_verdict ? 'status') THEN
        v_verdict := v_verdict || jsonb_build_object(
            'status', CASE WHEN (v_verdict->>'ok')::boolean IS TRUE THEN 'pass' ELSE 'fail' END);
    END IF;

    RETURN v_verdict;
END;
$fn$;

-- Evaluate a SAVED metric's KPI check across the bitemporal axes (NULL if no check).
CREATE OR REPLACE FUNCTION rvbbit.check_metric(
    p_name       text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_check    text;
    v_defaults jsonb;
    v_eff      jsonb;
    v_msql     text;
    v_csql     text;
BEGIN
    SELECT check_sql, coalesce(params, '{}'::jsonb)
      INTO v_check, v_defaults
    FROM rvbbit.metric_defs
    WHERE name = p_name AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF v_check IS NULL OR btrim(v_check) = '' THEN
        RETURN NULL;
    END IF;

    -- threshold defaults live in the metric def's params; merge under caller overrides
    v_eff := v_defaults || coalesce(p_params, '{}'::jsonb);
    v_msql := rvbbit.metric_sql(p_name, v_eff, p_def_as_of);
    v_csql := rvbbit.preview_metric_sql(v_check, v_eff, p_def_as_of);
    RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
END;
$fn$;

-- Preview a DRAFT check (Creator): inline metric + check bodies.
CREATE OR REPLACE FUNCTION rvbbit.preview_check_sql(
    p_metric_sql text,
    p_check_sql  text,
    p_params     jsonb DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_msql text;
    v_csql text;
BEGIN
    IF p_check_sql IS NULL OR btrim(p_check_sql) = '' THEN
        RETURN NULL;
    END IF;
    v_msql := rvbbit.preview_metric_sql(p_metric_sql, p_params, p_def_as_of);
    v_csql := rvbbit.preview_metric_sql(p_check_sql, p_params, p_def_as_of);
    RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
END;
$fn$;
