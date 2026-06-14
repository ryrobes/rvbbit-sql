-- 0026_dimensional_metrics — slice a metric by cube dimensions (the semantic layer).
--
-- A KPI metric is a scalar headline: SELECT sum(amount) AS pipeline_value FROM cubes.x WHERE ...
-- The same definition is far more useful if you can ask "...broken down by stage_name, by region".
-- A metric becomes *dimensional* by declaring its cube in labels ({"cube_source":"cubes.sf_opportunities"}).
-- rvbbit.metric_by('pipeline_value', ARRAY['stage_name']) then reuses the metric's measures verbatim
-- and grafts on the requested dimensions + GROUP BY. Dimensions are validated against the cube's REAL
-- columns (quote_ident = injection firewall), so a caller can only group by columns that exist.
-- Additive: metric_by is a NEW function (metric() is extension-owned + would be call-ambiguous if
-- overloaded), so every existing metric() call is untouched.

-- cube_dimensions: classify a cube's columns into dimension / measure / key (groupability metadata).
-- Primary signal is data_type + pg_stats cardinality; cube_columns.semantics (free-text LLM doc) is a
-- keyword hint. p_cube accepts 'cubes.x' or bare 'x'.
CREATE OR REPLACE FUNCTION rvbbit.cube_dimensions(p_cube text)
RETURNS TABLE (column_name text, data_type text, kind text, groupable boolean, distinct_est numeric, semantics text)
LANGUAGE sql STABLE AS $fn$
    WITH bare AS (SELECT regexp_replace(p_cube, '^cubes\.', '') AS nm)
    SELECT
        cc.column_name,
        cc.data_type,
        k.kind,
        (k.kind IN ('dimension', 'time', 'key')) AS groupable,
        st.n_distinct AS distinct_est,
        cc.semantics
    FROM rvbbit.cube_columns cc, bare
    LEFT JOIN LATERAL (
        SELECT n_distinct FROM pg_stats
        WHERE schemaname = 'cubes' AND tablename = bare.nm AND attname = cc.column_name
    ) st ON true
    CROSS JOIN LATERAL (
        SELECT CASE
            -- explicit hints from the column's data type
            WHEN cc.data_type IN ('boolean') THEN 'dimension'
            WHEN cc.data_type IN ('date', 'timestamp without time zone', 'timestamp with time zone', 'time without time zone') THEN 'time'
            -- a *_id / id column is a key (groupable as a segment, but not a measure)
            WHEN cc.column_name ~* '(^|_)id$' THEN 'key'
            -- numeric + high cardinality (or money/measure wording) = a measure
            WHEN cc.data_type IN ('integer', 'bigint', 'smallint', 'numeric', 'real', 'double precision')
                 AND (coalesce(st.n_distinct, -1) < 0 OR coalesce(st.n_distinct, 100) > 50
                      OR cc.semantics ~* '\y(measure|amount|currency|revenue|sum|count|total|price|qty|quantity)\y')
                THEN 'measure'
            -- textual / low-cardinality numeric = a dimension
            WHEN cc.data_type IN ('text', 'character varying', 'character', '"char"') THEN 'dimension'
            WHEN coalesce(st.n_distinct, 0) BETWEEN 1 AND 50 THEN 'dimension'
            ELSE 'measure'
        END AS kind
    ) k
    WHERE cc.cube_name = bare.nm
    ORDER BY (k.kind = 'measure'), cc.column_name;   -- dimensions first
$fn$;

-- metric_dimensions: the groupable columns for a metric (empty unless it declares a cube_source).
CREATE OR REPLACE FUNCTION rvbbit.metric_dimensions(p_name text)
RETURNS TABLE (column_name text, data_type text, kind text, groupable boolean, distinct_est numeric, semantics text)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE v_cube text;
BEGIN
    SELECT labels->>'cube_source' INTO v_cube
      FROM rvbbit.metric_defs WHERE name = p_name ORDER BY created_at DESC, version DESC LIMIT 1;
    IF v_cube IS NULL OR btrim(v_cube) = '' THEN RETURN; END IF;
    RETURN QUERY SELECT * FROM rvbbit.cube_dimensions(v_cube);
END $fn$;

-- _slice_metric_sql: graft dimensions + GROUP BY onto a scalar metric's resolved SQL.
-- Reuses the metric's measures (its existing select-list) verbatim; only the projection changes.
-- Constraint: the metric must be a single-FROM aggregation with no own GROUP BY (the KPI shape).
CREATE OR REPLACE FUNCTION rvbbit._slice_metric_sql(p_sql text, p_dims text[])
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    v_measures text; v_rest text; v_dims_csv text; m text[];
BEGIN
    IF p_dims IS NULL OR cardinality(p_dims) = 0 THEN RETURN p_sql; END IF;

    -- split "SELECT <measures> FROM <rest>" on the FIRST FROM (non-greedy select-list)
    m := regexp_match(p_sql, '^\s*select\s+(.*?)\s+from\s+(.*)$', 'is');
    IF m IS NULL THEN
        RAISE EXCEPTION 'rvbbit._slice_metric_sql: cannot parse SELECT ... FROM in metric SQL';
    END IF;
    v_measures := m[1];
    v_rest := 'FROM ' || m[2];

    IF v_rest ~* '\ygroup\s+by\y' THEN
        RAISE EXCEPTION 'rvbbit._slice_metric_sql: metric already has GROUP BY; cannot slice';
    END IF;
    -- a trailing ORDER BY / LIMIT would land after our GROUP BY (illegal) — drop it
    v_rest := regexp_replace(v_rest, '\s+(order\s+by|limit)\s+.*$', '', 'is');

    -- quote each dimension (firewall) — callers validate existence against cube_dimensions first
    SELECT string_agg(quote_ident(d), ', ') INTO v_dims_csv FROM unnest(p_dims) d;

    RETURN 'SELECT ' || v_dims_csv || ', ' || v_measures || ' ' || v_rest
           || ' GROUP BY ' || v_dims_csv;
END $fn$;

-- metric_by(): the dimensional sibling of metric(). A separate name (not an overload of metric(),
-- which the extension owns and which would be call-ambiguous) with p_slice as the required 2nd arg.
-- An empty p_slice behaves exactly like metric(). Mirrors metric()'s as_of / floor handling.
CREATE OR REPLACE FUNCTION rvbbit.metric_by(
    p_name       text,
    p_slice      text[],
    p_params     jsonb       DEFAULT '{}'::jsonb,
    p_def_as_of  timestamptz DEFAULT now(),
    p_data_as_of timestamptz DEFAULT NULL
) RETURNS SETOF jsonb LANGUAGE plpgsql AS $fn$
DECLARE
    v_sql   text;
    v_saved text;
    v_cube  text;
    d       text;
BEGIN
    v_sql := rvbbit.metric_sql(p_name, p_params, p_def_as_of);

    -- dimensional slice: validate each dim against the metric's cube, then graft GROUP BY
    IF p_slice IS NOT NULL AND cardinality(p_slice) > 0 THEN
        SELECT labels->>'cube_source' INTO v_cube
          FROM rvbbit.metric_defs WHERE name = p_name ORDER BY created_at DESC, version DESC LIMIT 1;
        IF v_cube IS NULL OR btrim(v_cube) = '' THEN
            RAISE EXCEPTION 'rvbbit.metric_by("%"): not dimensional — declare labels.cube_source to slice', p_name;
        END IF;
        FOREACH d IN ARRAY p_slice LOOP
            IF NOT EXISTS (SELECT 1 FROM rvbbit.cube_dimensions(v_cube) cd WHERE cd.column_name = d) THEN
                RAISE EXCEPTION 'rvbbit.metric_by("%"): unknown dimension "%" (not a column of %)', p_name, d, v_cube;
            END IF;
        END LOOP;
        v_sql := rvbbit._slice_metric_sql(v_sql, p_slice);
    END IF;

    v_sql := rvbbit._resolve_relative_refs(v_sql, v_sql, p_params, p_def_as_of, p_data_as_of);

    -- Pin an EXPLICIT instant for "latest" so the snapshot floor reaches the nested table scan.
    v_saved := current_setting('rvbbit.as_of_timestamp', true);
    PERFORM set_config('rvbbit.as_of_timestamp',
                       coalesce(p_data_as_of::text, now()::text), true);

    BEGIN
        RETURN QUERY EXECUTE 'SELECT to_jsonb(t) FROM (' || v_sql || ') AS t';
    EXCEPTION WHEN OTHERS THEN
        PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
        RAISE EXCEPTION 'rvbbit.metric_by("%"): % | SQL: %', p_name, SQLERRM, v_sql;
    END;

    PERFORM set_config('rvbbit.as_of_timestamp', coalesce(v_saved, ''), true);
    RETURN;
END $fn$;
