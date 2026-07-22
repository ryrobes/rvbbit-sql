-- 0207: data_crawl_run — the facts sweep (Scry's [data] layer, fed)
--
-- rvbbit.data_crawl() has always existed (LLM triples from row samples →
-- the data_kg graph) but NOTHING ever called it — the facts layer of Scry
-- was empty on every install unless someone hand-crawled tables one by
-- one. This adds the sweep: a durable procedure over a POLICY of tables,
-- deliberately MANUAL (or cron'd explicitly) — never ridden along with
-- the catalog heartbeat, because triples cost LLM calls and fact-mining
-- is selective by nature (CRM notes yes, texel tables no).
--
-- Policy: rvbbit.settings key 'data_crawl_tables' — a jsonb array whose
-- entries are either "schema.table" strings or objects:
--   {"table": "crm.interactions", "sample_size": 50, "focus": "all",
--    "where_sql": "at > now() - interval '90 days'", "pk_expr": "interaction_id"}
-- (pk_expr matters: real row keys preserve the cross-row frequency signal;
--  the default content-hash works but names nothing.)
--
-- Run it:      CALL rvbbit.data_crawl_run();                  -- policy sweep
--              CALL rvbbit.data_crawl_run(ARRAY['crm.interactions']);
-- Cron (opt-in, explicitly): cron.schedule_in_database('rvbbit_data_crawl',
--   '0 5 * * 0', 'CALL rvbbit.data_crawl_run()', '<your db>');
--
-- Durability: progress commits PER TABLE (catalog_crawl_run pattern) and
-- the run is visible in rvbbit.catalog_runs under graph_id 'data_kg'.
-- GOTCHA the wrapper exists to absorb: data_crawl(reset=>true) clears the
-- WHOLE graph — the sweep resets once up front, then per-table calls run
-- with reset=>false, so tables accumulate instead of erasing each other.

INSERT INTO rvbbit.settings (key, value, updated_at)
VALUES ('data_crawl_tables', '[]'::jsonb, now())
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE PROCEDURE rvbbit.data_crawl_run(
    p_tables      text[]  DEFAULT NULL,
    p_sample_size int     DEFAULT NULL,
    p_graph       text    DEFAULT 'data_kg',
    p_reset       boolean DEFAULT true)
LANGUAGE plpgsql AS $proc$
DECLARE
    v_graph   text := rvbbit.kg_normalize_graph(coalesce(nullif(btrim(p_graph), ''), 'data_kg'));
    v_entries jsonb;
    v_entry   jsonb;
    v_tbl     text;
    v_rel     regclass;
    v_run     bigint;
    v_res     jsonb;
    v_tables  int := 0;
    v_errors  int := 0;
    v_triples bigint := 0;
    v_nodes   bigint := 0;
BEGIN
    IF p_tables IS NOT NULL THEN
        SELECT jsonb_agg(to_jsonb(t)) INTO v_entries FROM unnest(p_tables) AS t;
    ELSE
        SELECT value INTO v_entries FROM rvbbit.settings WHERE key = 'data_crawl_tables';
    END IF;
    IF v_entries IS NULL OR jsonb_typeof(v_entries) <> 'array' OR jsonb_array_length(v_entries) = 0 THEN
        RAISE NOTICE 'data_crawl_run: nothing to sweep — set rvbbit.settings ''data_crawl_tables'' (jsonb array of "schema.table" or {"table":..., "sample_size":..., "focus":..., "where_sql":..., "pk_expr":...}) or pass p_tables.';
        RETURN;
    END IF;

    INSERT INTO rvbbit.catalog_runs (graph_id, status, schemas)
    VALUES (v_graph, 'running',
            (SELECT array_agg(coalesce(e ->> 'table', trim(both '"' from e::text)))
             FROM jsonb_array_elements(v_entries) e))
    RETURNING run_id INTO v_run;
    COMMIT;

    -- reset ONCE (mirrors data_crawl's own reset statements), then accumulate
    IF p_reset THEN
        DELETE FROM rvbbit.kg_edges    WHERE graph_id = v_graph;
        DELETE FROM rvbbit.kg_nodes    WHERE graph_id = v_graph;
        DELETE FROM rvbbit.catalog_docs WHERE graph_id = v_graph;
        COMMIT;
    END IF;

    FOR v_entry IN SELECT e FROM jsonb_array_elements(v_entries) e LOOP
        IF jsonb_typeof(v_entry) = 'string' THEN
            v_entry := jsonb_build_object('table', trim(both '"' from v_entry::text));
        END IF;
        v_tbl := v_entry ->> 'table';
        v_rel := rvbbit._safe_regclass(v_tbl);
        IF v_rel IS NULL THEN
            RAISE NOTICE 'data_crawl_run: skipping % (not found / not visible)', v_tbl;
            v_errors := v_errors + 1;
            CONTINUE;
        END IF;
        BEGIN
            v_res := rvbbit.data_crawl(
                rel             => v_rel,
                sample_size     => coalesce((v_entry ->> 'sample_size')::int, p_sample_size, 50),
                focus           => coalesce(nullif(v_entry ->> 'focus', ''), 'all'),
                graph           => v_graph,
                match_threshold => coalesce((v_entry ->> 'match_threshold')::float8, 0.92),
                specialist      => coalesce(v_entry ->> 'specialist', ''),
                where_sql       => nullif(v_entry ->> 'where_sql', ''),
                reset           => false,          -- the sweep already reset once
                pk_expr         => nullif(v_entry ->> 'pk_expr', ''));
            v_tables  := v_tables + 1;
            v_triples := v_triples + coalesce((v_res ->> 'triples')::bigint, 0);
            v_nodes   := v_nodes + coalesce((v_res ->> 'nodes')::bigint, 0);
            RAISE NOTICE 'data_crawl_run: % → % rows, % triples', v_tbl, v_res ->> 'rows', v_res ->> 'triples';
        EXCEPTION WHEN OTHERS THEN
            v_errors := v_errors + 1;
            RAISE NOTICE 'data_crawl_run: % FAILED — %', v_tbl, SQLERRM;
        END;
        COMMIT;   -- durable per table: a failure later never loses this table's facts
    END LOOP;

    UPDATE rvbbit.catalog_runs
       SET status = CASE WHEN v_tables > 0 OR v_errors = 0 THEN 'ok' ELSE 'failed' END,
           tables_seen = v_tables, edges_made = v_triples, docs_embedded = v_nodes,
           error = CASE WHEN v_errors > 0 THEN v_errors || ' table(s) skipped/failed (see NOTICEs)' END,
           finished_at = now()
     WHERE run_id = v_run;
    COMMIT;
END $proc$;

COMMENT ON PROCEDURE rvbbit.data_crawl_run(text[], int, text, boolean) IS
    'The facts sweep: data_crawl() over the data_crawl_tables policy (or p_tables), durable per table, logged in catalog_runs under data_kg. Feeds Scry''s [data] layer + data_search. MANUAL by design — triples cost LLM calls.';
