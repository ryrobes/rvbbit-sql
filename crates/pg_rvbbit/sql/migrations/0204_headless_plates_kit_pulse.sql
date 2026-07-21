-- 0204: Headless plates + the kit pulse + the derived kit graph
-- (the zero-UI workflow round — BURROW/HUB conversations, docs/KIT_PLATES_PLAN.md)
--
-- Three ideas, one doctrine (completeness-as-data):
--
--   * LOGIC PLATES — plates with surface='logic' are containers for checks
--     + prose, not screens: every declared query is a CHECK (rows returned
--     = violations, empty = green) and the template is the EXPLANATION a
--     human wrote for the agent. Same authoring surface as UI plates
--     (upsert_plate, revisions, assistant edits) — set surface via UPDATE,
--     like module (no signature churn).
--   * rvbbit.kit_pulse(kit) — the agentic rendering of the ruleset: one
--     call returns every check (kit contracts + logic-plate queries) with
--     its explanation, violation count, and a small sample. In chat, red
--     rows become SENTENCES ("still need the backplate photo"); on a
--     plate they'd be red modules. Same rows, two renderings.
--   * rvbbit.kit_graph(kit) — the app flow DERIVED from what's already
--     declared, no new nouns: layouts contain plates, templates rv-open
--     each other, params flow via emit/listen, actions write tables,
--     queries read tables, contracts gate modules. kit_graph_crawl()
--     asserts it into a named KG graph so the existing graph explorers
--     can draw the switchboard.

ALTER TABLE rvbbit.plates ADD COLUMN IF NOT EXISTS surface text NOT NULL DEFAULT 'ui';
COMMENT ON COLUMN rvbbit.plates.surface IS
    '''ui'' renders as an app surface; ''logic'' is a headless container: queries are checks (rows = violations), template is the human-written explanation agents read. Set via UPDATE (like module).';

-- ── the pulse ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.kit_pulse(p_kit text)
RETURNS TABLE(source text, check_id text, explanation text,
              violations bigint, sample jsonb, status text)
LANGUAGE plpgsql AS $fn$
DECLARE
    r      record;
    q      record;
    v_n    bigint;
    v_rows jsonb;
BEGIN
    -- kit contracts (the module gates)
    FOR r IN SELECT c.module, c.contract_id, c.description, c.violations_sql
             FROM rvbbit.kit_contracts c WHERE c.kit = p_kit
             ORDER BY c.module, c.contract_id LOOP
        BEGIN
            EXECUTE format('SELECT count(*), jsonb_agg(t) FROM (SELECT * FROM (%s) __v LIMIT 3) t',
                           rtrim(btrim(r.violations_sql), ';'))
            INTO v_n, v_rows;
            RETURN QUERY SELECT 'contract:' || coalesce(r.module, ''), r.contract_id::text,
                coalesce(r.description, ''), coalesce(v_n, 0), coalesce(v_rows, '[]'::jsonb),
                CASE WHEN coalesce(v_n, 0) = 0 THEN 'green' ELSE 'red' END;
        EXCEPTION WHEN OTHERS THEN
            RETURN QUERY SELECT 'contract:' || coalesce(r.module, ''), r.contract_id::text,
                coalesce(r.description, ''), -1::bigint,
                jsonb_build_array(jsonb_build_object('error', SQLERRM)), 'error';
        END;
    END LOOP;

    -- logic plates: every query is a check; the template is the explanation
    FOR r IN SELECT p.plate_id, p.description, p.template, p.queries
             FROM rvbbit.plates p
             WHERE p.kit = p_kit AND p.surface = 'logic'
             ORDER BY p.plate_id LOOP
        FOR q IN SELECT key AS qname, value ->> 'sql' AS sql
                 FROM jsonb_each(coalesce(r.queries, '{}'::jsonb)) ORDER BY key LOOP
            CONTINUE WHEN q.sql IS NULL OR btrim(q.sql) = ''
                     OR q.sql ~ '\{\{';   -- param-bound checks need a caller; skip here
            BEGIN
                EXECUTE format('SELECT count(*), jsonb_agg(t) FROM (SELECT * FROM (%s) __v LIMIT 3) t',
                               rtrim(btrim(q.sql), ';'))
                INTO v_n, v_rows;
                RETURN QUERY SELECT 'logic:' || r.plate_id, q.qname::text,
                    coalesce(nullif(btrim(r.template), ''), coalesce(r.description, '')),
                    coalesce(v_n, 0), coalesce(v_rows, '[]'::jsonb),
                    CASE WHEN coalesce(v_n, 0) = 0 THEN 'green' ELSE 'red' END;
            EXCEPTION WHEN OTHERS THEN
                RETURN QUERY SELECT 'logic:' || r.plate_id, q.qname::text,
                    coalesce(nullif(btrim(r.template), ''), coalesce(r.description, '')),
                    -1::bigint, jsonb_build_array(jsonb_build_object('error', SQLERRM)), 'error';
            END;
        END LOOP;
    END LOOP;
END $fn$;

COMMENT ON FUNCTION rvbbit.kit_pulse(text) IS
    'The agentic rendering of a kit''s ruleset: every check (contracts + logic-plate queries) with explanation, violation count, and sample rows. Red rows become sentences in chat, red modules on plates. docs/KIT_PLATES_PLAN.md';

-- ── the derived graph ────────────────────────────────────────────────────
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
            CONTINUE WHEN to_regclass(t) IS NULL;
            RETURN QUERY SELECT 'plate', r.plate_id,
                CASE WHEN r.surface = 'logic' THEN 'checks' ELSE 'reads' END,
                'table', to_regclass(t)::text, m;
        END LOOP;
        -- actions write tables
        FOR m, t IN SELECT a.key, (regexp_matches(a.value ->> 'sql',
                        '(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*|[a-zA-Z_][\w]*)', 'gi'))[1]
                    FROM jsonb_each(coalesce(r.actions, '{}'::jsonb)) a LOOP
            CONTINUE WHEN to_regclass(t) IS NULL;
            RETURN QUERY SELECT 'plate', r.plate_id, 'writes', 'table', to_regclass(t)::text, m;
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
            CONTINUE WHEN to_regclass(t) IS NULL;
            RETURN QUERY SELECT 'contract', r.contract_id::text, 'checks', 'table', to_regclass(t)::text, '';
        END LOOP;
    END LOOP;
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.kit_graph(p_kit text)
RETURNS TABLE(src_kind text, src text, edge text, dst_kind text, dst text, detail text)
LANGUAGE sql STABLE AS $fn$
    SELECT DISTINCT ON (src_kind, src, edge, dst_kind, dst)
           src_kind, src, edge, dst_kind, dst, detail
    FROM rvbbit._kit_graph_raw(p_kit)
    ORDER BY src_kind, src, edge, dst_kind, dst;
$fn$;

COMMENT ON FUNCTION rvbbit.kit_graph(text) IS
    'The kit''s app flow DERIVED from declared metadata (layouts/rv-open/param bus/reads/writes/contracts) — no new declarations. kit_graph_crawl() mirrors it into the KG for the graph explorers.';

-- mirror into a named KG graph so existing graph surfaces can draw it
CREATE OR REPLACE FUNCTION rvbbit.kit_graph_crawl(p_kit text)
RETURNS int LANGUAGE plpgsql AS $fn$
DECLARE
    g   text := 'kit_flow_' || p_kit;
    e   record;
    n   int := 0;
BEGIN
    FOR e IN SELECT * FROM rvbbit.kit_graph(p_kit) LOOP
        PERFORM rvbbit.kg_assert_node(e.src_kind, e.src, '{}'::jsonb, 1.0, '', 0.92, g);
        PERFORM rvbbit.kg_assert_node(e.dst_kind, e.dst, '{}'::jsonb, 1.0, '', 0.92, g);
        PERFORM rvbbit.kg_assert_edge(e.src_kind, e.src, e.edge, e.dst_kind, e.dst,
                                      1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
        n := n + 1;
    END LOOP;
    RETURN n;
END $fn$;
