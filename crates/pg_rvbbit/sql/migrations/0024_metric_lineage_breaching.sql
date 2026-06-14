-- 0024_metric_lineage_breaching — turn the metric definition store into a monitoring surface.
--
-- The snapshot machinery exists (materialize_metric, metric_history, metric_observations, check_metric)
-- but two read helpers were missing for the MCP to expose: which KPIs are breaching right now, and a
-- metric's table lineage (impact analysis). Both are thin reads over what's already there. Additive.

-- metric_lineage: the schema-qualified tables/cubes a metric reads (for impact analysis).
-- Uses a regex over the resolved SQL rather than EXPLAIN, because metrics commonly read cubes
-- (rvbbit CustomScan tables), which EXPLAIN does NOT expose as a "Relation Name".
CREATE OR REPLACE FUNCTION rvbbit.metric_lineage(p_name text)
RETURNS text[] LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_params jsonb; v_sql text; v_tables text[];
BEGIN
    SELECT coalesce(params, '{}'::jsonb) INTO v_params
      FROM rvbbit.metric_defs WHERE name = p_name ORDER BY created_at DESC, version DESC LIMIT 1;
    IF NOT FOUND THEN RETURN '{}'::text[]; END IF;
    BEGIN
        v_sql := rvbbit.metric_sql(p_name, v_params, now());   -- resolve {param} with the metric's defaults
    EXCEPTION WHEN OTHERS THEN
        RETURN '{}'::text[];
    END;
    -- schema-qualified relation after FROM/JOIN (catches raw tables AND cubes.<name>)
    SELECT array_agg(DISTINCT m[1]) INTO v_tables
    FROM regexp_matches(coalesce(v_sql, ''),
                        '(?:from|join)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)', 'gi') m;
    RETURN coalesce(v_tables, '{}'::text[]);
END $fn$;

-- breaching_kpis: the latest observation per metric whose KPI check FAILED (ok = false).
CREATE OR REPLACE FUNCTION rvbbit.breaching_kpis()
RETURNS TABLE (metric_name text, status text, value jsonb, verdict jsonb, observed_at timestamptz)
LANGUAGE sql STABLE AS $fn$
    SELECT metric_name, status, value, verdict, observed_at
    FROM (
        SELECT DISTINCT ON (metric_name)
               metric_name, status, value, verdict, observed_at
        FROM rvbbit.metric_observations
        WHERE verdict IS NOT NULL
        ORDER BY metric_name, observed_at DESC
    ) latest
    WHERE (verdict->>'ok') = 'false'      -- ok=false is a breach; ok=null (error/unknown) is not
    ORDER BY observed_at DESC;
$fn$;
