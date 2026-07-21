-- 0205: the kit surface agents and apps may actually reach
--
-- The warehouse read path (run_sql / the live-app bridge) deliberately
-- refuses SQL naming rvbbit.* — internals stay hidden from MCP clients.
-- But kit_pulse / kit_graph / the kit catalog are CONSUMER surfaces: the
-- whole zero-UI loop is an agent asking "what's still missing?" over MCP,
-- and the Kit Flow app drawing the switchboard over the bridge. Expose
-- them as thin public pass-throughs — the same pattern as cubes living in
-- an app-visible schema. The bodies still run the governed rvbbit code.

CREATE OR REPLACE VIEW public.kit_catalog AS
    SELECT kit, title, description, version FROM rvbbit.kits;
COMMENT ON VIEW public.kit_catalog IS
    'Installed kits — the app/agent-visible catalog (pass-through to rvbbit.kits).';

CREATE OR REPLACE FUNCTION public.kit_pulse(p_kit text)
RETURNS TABLE(source text, check_id text, explanation text,
              violations bigint, sample jsonb, status text)
LANGUAGE sql AS $fn$ SELECT * FROM rvbbit.kit_pulse(p_kit) $fn$;
COMMENT ON FUNCTION public.kit_pulse(text) IS
    'Every check in a kit (contracts + logic plates) with explanation, violation count, sample, status. THE agent call for "what is still missing?" — red rows are the sentences to say. Pass-through to rvbbit.kit_pulse.';

CREATE OR REPLACE FUNCTION public.kit_flow(p_kit text)
RETURNS TABLE(src_kind text, src text, edge text, dst_kind text, dst text, detail text)
LANGUAGE sql STABLE AS $fn$ SELECT * FROM rvbbit.kit_graph(p_kit) $fn$;
COMMENT ON FUNCTION public.kit_flow(text) IS
    'A kit''s derived app flow (layouts/plates/params/tables/contracts + edges). Pass-through to rvbbit.kit_graph — what the Kit Flow app draws.';

-- the catalog is meant to be seen; check EXECUTION stays invoker-side so
-- logic-plate checks run under the viewer's grants (the whole point).
GRANT SELECT ON public.kit_catalog TO PUBLIC;


-- A viewer without USAGE on a kit's schema must get a SMALLER graph, not
-- an error: to_regclass raises insufficient_privilege mid-scan. Guarded
-- resolver + re-issue of _kit_graph_raw with it.
CREATE OR REPLACE FUNCTION rvbbit._safe_regclass(p text)
RETURNS regclass LANGUAGE plpgsql STABLE AS $fn$
BEGIN
    RETURN to_regclass(p);
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit._kit_graph_raw(p_kit text)
RETURNS TABLE(src_kind text, src text, edge text, dst_kind text, dst text, detail text)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    r  record;
    m  text;
    t  text;
BEGIN
    -- layouts contain plates
    FOR r IN SELECT l.layout_id, pane ->> 'plate' AS plate
             FROM rvbbit.plate_layouts l, jsonb_array_elements(l.panes) pane
             WHERE l.kit = p_kit AND pane ->> 'plate' IS NOT NULL LOOP
        RETURN QUERY SELECT 'layout', r.layout_id, 'contains', 'plate', r.plate, '';
    END LOOP;

    FOR r IN SELECT p.plate_id, p.surface, p.module, p.template, p.queries, p.actions, p.params
             FROM rvbbit.plates p WHERE p.kit = p_kit LOOP
        -- template rv-open targets: plate -> plate
        FOR m IN SELECT (regexp_matches(r.template, 'rv-open(?:-dbl)?="plate:([^"@]+)', 'g'))[1] LOOP
            RETURN QUERY SELECT 'plate', r.plate_id, 'opens', 'plate', m, '';
        END LOOP;
        -- param bus: emits / listens
        FOR m IN SELECT DISTINCT (regexp_matches(r.template, 'rv-emit="([a-zA-Z_][\w]*)"', 'g'))[1] LOOP
            RETURN QUERY SELECT 'plate', r.plate_id, 'emits', 'param', m, '';
        END LOOP;
        FOR m IN SELECT prm ->> 'name' FROM jsonb_array_elements(coalesce(r.params, '[]'::jsonb)) prm
                 WHERE (prm ->> 'from_bus')::boolean IS TRUE LOOP
            RETURN QUERY SELECT 'param', m, 'drives', 'plate', r.plate_id, '';
        END LOOP;
        -- queries read tables
        FOR m, t IN SELECT q.key, (regexp_matches(q.value ->> 'sql',
                        '(?:FROM|JOIN)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*|[a-zA-Z_][\w]*)', 'gi'))[1]
                    FROM jsonb_each(coalesce(r.queries, '{}'::jsonb)) q LOOP
            CONTINUE WHEN rvbbit._safe_regclass(t) IS NULL;
            RETURN QUERY SELECT 'plate', r.plate_id,
                CASE WHEN r.surface = 'logic' THEN 'checks' ELSE 'reads' END,
                'table', rvbbit._safe_regclass(t)::text, m;
        END LOOP;
        -- actions write tables
        FOR m, t IN SELECT a.key, (regexp_matches(a.value ->> 'sql',
                        '(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*|[a-zA-Z_][\w]*)', 'gi'))[1]
                    FROM jsonb_each(coalesce(r.actions, '{}'::jsonb)) a LOOP
            CONTINUE WHEN rvbbit._safe_regclass(t) IS NULL;
            RETURN QUERY SELECT 'plate', r.plate_id, 'writes', 'table', rvbbit._safe_regclass(t)::text, m;
        END LOOP;
        -- module membership (contracts gate modules)
        IF r.module IS NOT NULL AND r.module <> '' THEN
            RETURN QUERY SELECT 'module', r.module, 'contains', 'plate', r.plate_id, '';
        END IF;
    END LOOP;

    -- contracts gate modules + check tables
    FOR r IN SELECT c.module, c.contract_id, c.violations_sql
             FROM rvbbit.kit_contracts c WHERE c.kit = p_kit LOOP
        RETURN QUERY SELECT 'contract', r.contract_id::text, 'gates', 'module',
            coalesce(r.module, '(switchboard)'), '';
        FOR t IN SELECT (regexp_matches(r.violations_sql,
                     '(?:FROM|JOIN)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*|[a-zA-Z_][\w]*)', 'gi'))[1] LOOP
            CONTINUE WHEN rvbbit._safe_regclass(t) IS NULL;
            RETURN QUERY SELECT 'contract', r.contract_id::text, 'checks', 'table', rvbbit._safe_regclass(t)::text, '';
        END LOOP;
    END LOOP;
END $fn$;

