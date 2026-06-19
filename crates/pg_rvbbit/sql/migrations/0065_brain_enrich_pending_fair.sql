-- 0065_brain_enrich_pending_fair — stop the nightly enrich backlog from starving freshly-crawled docs.
--
-- Symptom: a doc crawled last night stayed un-enriched, because brain_enrich_pending is LIMIT 25 ordered
-- by ingested_at DESC. When a big query source (the 458 Linear issues) sits in the backlog, file docs
-- get pushed behind it and never reached; a re-crawl that bumps content_hash re-queues a doc behind the
-- pile again. Two fixes:
--   • Enrich FILE docs (non-query sources) FIRST — they're the costly, human-authored ones you most want
--     in the graph; query/MCP docs are cheap and plentiful and can drain after.
--   • Per-doc triples toggle: skip the LLM triples pass for query/MCP-source docs in the nightly too
--     (same rationale as the bulk Enrich button) — so a ticket backlog is cheap and the cap goes further.
-- brain_nightly raises its enrich cap accordingly (query docs are now cheap).

CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_pending(p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0;
BEGIN
    FOR rec IN
        SELECT bd.doc_id, (nullif(bs.config->>'provider','') IS NOT NULL) AS is_query
          FROM rvbbit.brain_documents bd
          JOIN rvbbit.brain_sources bs ON bs.source_id = bd.source_id
         WHERE bd.deleted_at IS NULL AND bd.body IS NOT NULL
           AND (bd.enriched_at IS NULL OR bd.enrich_hash IS DISTINCT FROM bd.content_hash
                OR bd.enriched_at < bd.ingested_at)
         ORDER BY (nullif(bs.config->>'provider','') IS NOT NULL),  -- file docs (false) before query docs (true)
                  bd.ingested_at DESC
         LIMIT greatest(1, p_max_docs)
    LOOP
        BEGIN
            -- query/MCP docs: skip the costly LLM triples (NER + structured edges carry their value)
            PERFORM set_config('rvbbit.brain_skip_triples', rec.is_query::text, true);
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN n_err := n_err + 1;
        END;
    END LOOP;
    PERFORM rvbbit.brain_refresh_node_norm();
    RETURN jsonb_build_object('enriched_docs', n_docs, 'errors', n_err);
END $fn$;

-- nightly: connector + query sync, then drain a bigger enrich backlog (query docs are cheap now)
CREATE OR REPLACE FUNCTION rvbbit.brain_nightly(p_max_docs int DEFAULT 100, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_sync jsonb; v_qsync jsonb; v_enrich jsonb;
BEGIN
    v_sync   := rvbbit.brain_sync_sources('auto');
    v_qsync  := rvbbit.brain_sync_query_sources('auto');
    v_enrich := rvbbit.brain_enrich_pending(p_max_docs, p_max_chunks);
    RETURN jsonb_build_object('sync', v_sync, 'query_sync', v_qsync, 'enrich', v_enrich);
END $fn$;
