-- =====================================================================
-- rvbbit 1.2.8 -> 1.2.9 : metric draft preview
-- =====================================================================
-- preview_metric_sql composes the executable SQL for an UNSAVED draft body
-- (seeded inline rather than from a saved name) so the Metric Creator UI can
-- show the resolved/composed query live, before the metric is defined.
-- {metric:NAME} refs still resolve against the saved catalog as of def-time.

CREATE OR REPLACE FUNCTION rvbbit.preview_metric_sql(
    p_sql       text,
    p_params    jsonb DEFAULT '{}'::jsonb,
    p_def_as_of timestamptz DEFAULT now()
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_sql       text := p_sql;
    v_defaults  jsonb := '{}'::jsonb;
    v_effective jsonb;
    v_child     text;
    v_rec       record;
    v_key       text;
    v_val       text;
BEGIN
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RETURN p_sql;
    END IF;

    FOR v_child IN
        SELECT DISTINCT m[1]
        FROM regexp_matches(v_sql, '\{metric:([a-zA-Z0-9_]+)\}', 'g') AS m
    LOOP
        v_rec := rvbbit.resolve_metric(v_child, p_def_as_of, ARRAY[]::text[]);
        v_sql := replace(v_sql, '{metric:' || v_child || '}', '(' || v_rec.r_sql || ')');
        v_defaults := v_defaults || v_rec.r_defaults;
    END LOOP;

    v_effective := v_defaults || coalesce(p_params, '{}'::jsonb);

    FOR v_key, v_val IN SELECT key, value FROM jsonb_each_text(v_effective)
    LOOP
        v_sql := replace(v_sql, '{' || v_key || '!}', coalesce(v_val, ''));
        v_sql := replace(v_sql, '{' || v_key || '}', quote_nullable(v_val));
    END LOOP;

    IF v_sql ~ '\{metric:[a-zA-Z0-9_]+\}' THEN
        RAISE EXCEPTION 'rvbbit.preview_metric_sql: unresolved metric reference: %',
            (regexp_match(v_sql, '\{metric:[a-zA-Z0-9_]+\}'))[1];
    END IF;

    RETURN v_sql;
END;
$fn$;
