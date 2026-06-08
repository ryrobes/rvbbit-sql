-- =====================================================================
-- rvbbit 1.2.7 -> 1.2.8 : Metrics / BI layer
-- =====================================================================
-- A metric is a named, versioned SQL template. Definitions live in a
-- PLAIN (non-accelerated) append-versioned table so def-time is a simple
-- created_at filter, fully decoupled from data-time (rvbbit AS OF on the
-- underlying tables). That decoupling is what makes bitemporal metrics
-- work: "today's definition over last-quarter's data" and "last-quarter's
-- definition over today's data" are independent axes.
--
-- Template tokens (resolved by rvbbit.metric_sql / rvbbit.metric):
--   {param}        -> safe SQL literal (quote_nullable of the value)
--   {param!}       -> raw text (identifiers / SQL fragments; caller's risk)
--   {metric:NAME}  -> the named metric inlined as a (subquery);
--                     give it an alias yourself, e.g. FROM {metric:base} b
-- =====================================================================

CREATE TABLE IF NOT EXISTS rvbbit.metric_defs (
    metric_id    bigint GENERATED ALWAYS AS IDENTITY,
    name         text        NOT NULL,
    version      integer     NOT NULL,
    sql          text        NOT NULL,
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    grain        text,
    description  text,
    owner        text,
    labels       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

CREATE INDEX IF NOT EXISTS metric_defs_name_created_idx
    ON rvbbit.metric_defs (name, created_at DESC, version DESC);

-- Current definition per metric (latest version).
CREATE OR REPLACE VIEW rvbbit.metric_catalog AS
SELECT DISTINCT ON (name)
    name, version, sql, params, grain, description, owner, labels, created_at
FROM rvbbit.metric_defs
ORDER BY name, created_at DESC, version DESC;

-- Append a new version of a metric definition. Returns the new version.
CREATE OR REPLACE FUNCTION rvbbit.define_metric(
    p_name        text,
    p_sql         text,
    p_params      jsonb DEFAULT '{}'::jsonb,
    p_grain       text  DEFAULT NULL,
    p_description text  DEFAULT NULL,
    p_owner       text  DEFAULT NULL,
    p_labels      jsonb DEFAULT '{}'::jsonb
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
    -- Serialize version allocation per metric name.
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.metric:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version
    FROM rvbbit.metric_defs WHERE name = p_name;
    INSERT INTO rvbbit.metric_defs
        (name, version, sql, params, grain, description, owner, labels)
    VALUES
        (p_name, v_version, p_sql, coalesce(p_params, '{}'::jsonb), p_grain,
         p_description, p_owner, coalesce(p_labels, '{}'::jsonb));
    RETURN v_version;
END;
$fn$;

-- Recursive resolver: inline {metric:NAME} refs as of def-time and
-- accumulate the union of every referenced metric's default params.
--   OUT r_sql      : fully-inlined SQL (params NOT yet substituted)
--   OUT r_defaults : merged default params (own wins over children)
CREATE OR REPLACE FUNCTION rvbbit.resolve_metric(
    p_name      text,
    p_def_as_of timestamptz,
    p_stack     text[],
    OUT r_sql      text,
    OUT r_defaults jsonb
) LANGUAGE plpgsql AS $fn$
DECLARE
    v_params jsonb;
    v_child  text;
    v_rec    record;
BEGIN
    IF p_name = ANY(p_stack) THEN
        RAISE EXCEPTION 'rvbbit.resolve_metric: cycle detected: % -> %',
            array_to_string(p_stack, ' -> '), p_name;
    END IF;

    SELECT sql, coalesce(params, '{}'::jsonb)
      INTO r_sql, v_params
    FROM rvbbit.metric_defs
    WHERE name = p_name
      AND created_at <= p_def_as_of
    ORDER BY created_at DESC, version DESC
    LIMIT 1;

    IF r_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.resolve_metric: metric "%" is not defined as of %',
            p_name, p_def_as_of;
    END IF;

    r_defaults := '{}'::jsonb;

    -- Inline each distinct {metric:NAME} reference.
    FOR v_child IN
        SELECT DISTINCT m[1]
        FROM regexp_matches(r_sql, '\{metric:([a-zA-Z0-9_]+)\}', 'g') AS m
    LOOP
        v_rec := rvbbit.resolve_metric(v_child, p_def_as_of, p_stack || p_name);
        r_sql := replace(r_sql, '{metric:' || v_child || '}', '(' || v_rec.r_sql || ')');
        r_defaults := r_defaults || v_rec.r_defaults;
    END LOOP;

    -- Own defaults win over children.
    r_defaults := r_defaults || v_params;
END;
$fn$;

-- Compose the final, executable SQL for a metric: resolve composition,
-- merge params (caller wins over defaults), substitute tokens. Pure --
-- no execution, no GUC side-effects -- so the UI can preview the exact
-- query that will run. This is the "observable / debuggable" surface.
CREATE OR REPLACE FUNCTION rvbbit.metric_sql(
    p_name      text,
    p_params    jsonb DEFAULT '{}'::jsonb,
    p_def_as_of timestamptz DEFAULT now()
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_res       record;
    v_effective jsonb;
    v_sql       text;
    v_key       text;
    v_val       text;
BEGIN
    v_res := rvbbit.resolve_metric(p_name, p_def_as_of, ARRAY[]::text[]);
    v_sql := v_res.r_sql;
    v_effective := v_res.r_defaults || coalesce(p_params, '{}'::jsonb);

    -- Substitute params. Raw form {key!} first, then literal {key}.
    FOR v_key, v_val IN SELECT key, value FROM jsonb_each_text(v_effective)
    LOOP
        v_sql := replace(v_sql, '{' || v_key || '!}', coalesce(v_val, ''));
        v_sql := replace(v_sql, '{' || v_key || '}', quote_nullable(v_val));
    END LOOP;

    IF v_sql ~ '\{metric:[a-zA-Z0-9_]+\}' THEN
        RAISE EXCEPTION 'rvbbit.metric_sql: unresolved metric reference in "%": %',
            p_name, (regexp_match(v_sql, '\{metric:[a-zA-Z0-9_]+\}'))[1];
    END IF;

    RETURN v_sql;
END;
$fn$;

-- Execute a metric and return each result row as jsonb. p_def_as_of pins
-- which definition is used (def-time); p_data_as_of pins the underlying
-- rvbbit data (data-time) via the rvbbit.as_of_timestamp GUC, which --
-- unlike the leading-comment directive -- reaches the nested EXECUTE.
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
                       coalesce(p_data_as_of::text, ''), true);

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

-- Version history for a metric (newest first) -- powers the inspector.
CREATE OR REPLACE FUNCTION rvbbit.metric_versions(p_name text)
RETURNS TABLE(version integer, created_at timestamptz, sql text, params jsonb,
              grain text, description text, owner text)
LANGUAGE sql STABLE AS $fn$
    SELECT version, created_at, sql, params, grain, description, owner
    FROM rvbbit.metric_defs
    WHERE name = p_name
    ORDER BY version DESC;
$fn$;
