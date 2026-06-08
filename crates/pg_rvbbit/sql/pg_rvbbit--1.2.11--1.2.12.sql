-- =====================================================================
-- rvbbit 1.2.11 -> 1.2.12 : relative-time metric refs (rolling baselines)
-- =====================================================================
-- {metric:NAME.OFFSET} / {metric:self.OFFSET} = NAME's (or this metric's) SCALAR
-- headline at a SHIFTED data-time (base ± OFFSET), with the def held fixed. A
-- single statement can't carry two rvbbit AS-OFs, so a relative ref is
-- EAGER-EVALUATED (run the target at the shifted instant) and spliced inline as a
-- numeric literal. Makes rolling/delta checks one-liners, e.g.:
--   SELECT total, total >= {metric:self.-1day} AS ok FROM metric
--   SELECT total::numeric / {metric:self.-7days} - 1 AS wow FROM metric
-- OFFSET: signed N + unit (-1day, -12hours, +1week, -1month) or yesterday/lastweek.
-- The headline = a 'value' field if present, else the first numeric result field
-- (relative refs target SCALAR metrics). Refs don't nest (depth-guarded).

CREATE OR REPLACE FUNCTION rvbbit._parse_offset(p_off text) RETURNS interval
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    v text := lower(btrim(p_off));
    n text;
    u text;
BEGIN
    IF v IN ('yesterday','yday')        THEN RETURN interval '-1 day';  END IF;
    IF v IN ('lastweek','lastwk','lwk')  THEN RETURN interval '-7 days'; END IF;
    IF v IN ('lastmonth','lastmo')       THEN RETURN interval '-1 month'; END IF;
    n := (regexp_match(v, '^([+-]?[0-9]+)'))[1];
    u := (regexp_match(v, '([a-z]+)$'))[1];
    IF n IS NULL OR u IS NULL THEN
        RAISE EXCEPTION 'rvbbit: bad relative-time offset "%" (e.g. -1day, -12hours, yesterday)', p_off;
    END IF;
    u := CASE
        WHEN u IN ('s','sec','secs','second','seconds') THEN 'seconds'
        WHEN u IN ('h','hr','hrs','hour','hours')   THEN 'hours'
        WHEN u IN ('d','day','days')                THEN 'days'
        WHEN u IN ('w','wk','wks','week','weeks')    THEN 'weeks'
        WHEN u IN ('min','mins','minute','minutes')  THEN 'minutes'
        WHEN u IN ('mo','mon','month','months')      THEN 'months'
        ELSE u
    END;
    RETURN (n || ' ' || u)::interval;
END;
$fn$;

-- Splice {metric:NAME.OFFSET} tokens in p_sql with the target's scalar headline at
-- the shifted data-time. p_self_sql is the resolved body used for `self`.
CREATE OR REPLACE FUNCTION rvbbit._resolve_relative_refs(
    p_sql        text,
    p_self_sql   text,
    p_params     jsonb,
    p_def_as_of  timestamptz,
    p_data_as_of timestamptz
) RETURNS text
LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql     text := p_sql;
    v_base    timestamptz := coalesce(p_data_as_of, now());
    v_depth   integer := coalesce(nullif(current_setting('rvbbit.relref_depth', true), ''), '0')::integer;
    v_token   text;
    v_name    text;
    v_off     text;
    v_shifted timestamptz;
    v_obj     jsonb;
    v_scalar  text;
    v_saved   text;
    v_self_clean text;
BEGIN
    IF p_sql IS NULL OR strpos(p_sql, '{metric:') = 0 THEN
        RETURN p_sql;
    END IF;
    IF v_depth > 8 THEN
        RAISE EXCEPTION 'rvbbit: relative metric-ref recursion too deep (cycle?)';
    END IF;
    PERFORM set_config('rvbbit.relref_depth', (v_depth + 1)::text, true);

    -- For `self`, evaluate the body with its OWN relative tokens stripped to NULL
    -- (breaks recursion; a self-dependent derived column becomes NULL and is
    -- skipped, so the headline is the metric's primary value).
    v_self_clean := regexp_replace(coalesce(p_self_sql, ''),
        '\{metric:[a-zA-Z0-9_]+\.[+-]?[0-9a-zA-Z]+\}', 'NULL', 'g');

    FOR v_token, v_name, v_off IN
        SELECT DISTINCT '{metric:' || x[1] || '.' || x[2] || '}', x[1], x[2]
        FROM (SELECT regexp_matches(v_sql, '\{metric:([a-zA-Z0-9_]+)\.([+-]?[0-9a-zA-Z]+)\}', 'g') AS x) s
    LOOP
        v_shifted := v_base + rvbbit._parse_offset(v_off);
        v_obj := NULL;

        IF v_name = 'self' THEN
            v_saved := current_setting('rvbbit.as_of_timestamp', true);
            PERFORM set_config('rvbbit.as_of_timestamp', v_shifted::text, true);
            BEGIN
                EXECUTE format('SELECT to_jsonb(t) FROM (%s) t LIMIT 1', v_self_clean) INTO v_obj;
            EXCEPTION WHEN OTHERS THEN
                v_obj := NULL;
            END;
            PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
        ELSE
            BEGIN
                SELECT mm.obj INTO v_obj
                FROM rvbbit.metric(v_name, p_params, p_def_as_of, v_shifted) AS mm(obj) LIMIT 1;
            EXCEPTION WHEN OTHERS THEN
                v_obj := NULL;
            END;
        END IF;

        v_scalar := NULL;
        IF v_obj IS NOT NULL THEN
            SELECT coalesce(
              (SELECT je.value FROM jsonb_each_text(v_obj) je
                 WHERE je.key = 'value' AND je.value ~ '^-?[0-9]+(\.[0-9]+)?$' LIMIT 1),
              (SELECT je.value FROM jsonb_each_text(v_obj) je
                 WHERE je.value ~ '^-?[0-9]+(\.[0-9]+)?$' LIMIT 1)
            ) INTO v_scalar;
        END IF;

        v_sql := replace(v_sql, v_token, coalesce(v_scalar, 'NULL'));
    END LOOP;

    PERFORM set_config('rvbbit.relref_depth', v_depth::text, true);
    RETURN v_sql;
EXCEPTION WHEN OTHERS THEN
    PERFORM set_config('rvbbit.relref_depth', v_depth::text, true);
    RAISE;
END;
$fn$;

-- metric() now resolves relative refs in the metric body before executing.
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
    v_sql := rvbbit._resolve_relative_refs(v_sql, v_sql, p_params, p_def_as_of, p_data_as_of);

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

-- check_metric resolves relative refs in the check (self = the metric body).
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
    ORDER BY created_at DESC, version DESC LIMIT 1;

    IF v_check IS NULL OR btrim(v_check) = '' THEN
        RETURN NULL;
    END IF;

    v_eff  := v_defaults || coalesce(p_params, '{}'::jsonb);
    v_msql := rvbbit.metric_sql(p_name, v_eff, p_def_as_of);
    v_csql := rvbbit.preview_metric_sql(v_check, v_eff, p_def_as_of);
    v_csql := rvbbit._resolve_relative_refs(v_csql, v_msql, v_eff, p_def_as_of, p_data_as_of);
    v_msql := rvbbit._resolve_relative_refs(v_msql, v_msql, v_eff, p_def_as_of, p_data_as_of);
    RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
END;
$fn$;

-- preview_check_sql (draft) resolves relative refs too.
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
    v_csql := rvbbit._resolve_relative_refs(v_csql, v_msql, p_params, p_def_as_of, p_data_as_of);
    v_msql := rvbbit._resolve_relative_refs(v_msql, v_msql, p_params, p_def_as_of, p_data_as_of);
    RETURN rvbbit._run_check(v_msql, v_csql, p_data_as_of);
END;
$fn$;
