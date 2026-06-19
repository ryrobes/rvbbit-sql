-- 0067_brain_search_filtered — pre-filter the corpus (dimensions) + ANN tier, brute fallback.
--
-- Two upgrades to brain_search (and ask_brain, which now delegates to it):
--   1. PRE-FILTER on dimensions before ranking — p_filter jsonb: {source|[sources], folder (prefix),
--      since, until}. ACL (brain_visible_docs) is always intersected. So "only Linear" or "this folder,
--      last 90d" narrows the candidate set BEFORE the vector search.
--   2. TIER + FALLBACK — if the pgvector mirror (0066) is ready, over-fetch top-(k×8) by cosine from the
--      HNSW index, then apply ACL+dimension filters, take top-k. If that under-returns (a SELECTIVE
--      filter whose matches fall outside the over-fetch window) OR the tier isn't ready, fall back to a
--      brute cosine scan OVER THE FILTERED CANDIDATES — which is cheap precisely because the filter is
--      selective. Net: exact when selective, fast (ANN) when broad. Raw cosine throughout (center=false),
--      so relevance is identical to the prior brute-only behavior; only latency changes.

-- Idempotent + dependency-safe: drop ask_brain (it calls brain_search) BEFORE brain_search, and drop
-- BOTH the old 3-arg and any existing 4-arg form — so re-running this migration over an already-upgraded
-- DB (e.g. 0068 applied) can't trip "cannot change return type of existing function".
DROP FUNCTION IF EXISTS rvbbit.ask_brain(text, text, integer);
DROP FUNCTION IF EXISTS rvbbit.ask_brain(text, text, integer, jsonb);
DROP FUNCTION IF EXISTS rvbbit.brain_search(text, text, integer);
DROP FUNCTION IF EXISTS rvbbit.brain_search(text, text, integer, jsonb);

CREATE OR REPLACE FUNCTION rvbbit.brain_search(
    p_email text, p_query text, p_k int DEFAULT 8, p_filter jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE(doc_id bigint, chunk_id bigint, chunk_idx int, title text, folder_path text,
                source text, occurred_at timestamptz, chunk text, score double precision, entities text[])
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_q       real[];
    v_k       int;
    v_of      int;
    v_sources text[];
    v_folder  text;
    v_since   timestamptz;
    v_until   timestamptz;
    v_ids     bigint[];
    v_scores  float8[];
BEGIN
    IF nullif(btrim(coalesce(p_query, '')), '') IS NULL THEN RETURN; END IF;
    v_q := rvbbit.embed(p_query);
    v_k := greatest(1, least(coalesce(p_k, 8), 50));
    v_of := greatest(v_k * 8, 100);   -- HNSW over-fetch window

    -- dimension filters (ACL is always applied separately)
    v_sources := CASE WHEN p_filter ? 'source' THEN
        CASE jsonb_typeof(p_filter->'source')
            WHEN 'array' THEN ARRAY(SELECT jsonb_array_elements_text(p_filter->'source'))
            ELSE ARRAY[p_filter->>'source'] END
        ELSE NULL END;
    v_folder := nullif(p_filter->>'folder', '');
    v_since  := nullif(p_filter->>'since', '')::timestamptz;
    v_until  := nullif(p_filter->>'until', '')::timestamptz;

    -- (1) ANN tier: over-fetch by cosine, then apply ACL + dimension filters, take top-k.
    IF rvbbit.vector_ready('brain_chunks', array_length(v_q, 1)) THEN
        SELECT array_agg(t.id ORDER BY t.score DESC, t.id),
               array_agg(t.score ORDER BY t.score DESC, t.id)
          INTO v_ids, v_scores
          FROM (
            SELECT a.id, a.score
              FROM rvbbit.vector_ann('brain_chunks', v_q, v_of) a
              JOIN rvbbit.brain_chunks c    ON c.chunk_id = a.id
              JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
              JOIN rvbbit.brain_sources s   ON s.source_id = d.source_id
             WHERE c.doc_id IN (SELECT bv.doc_id FROM rvbbit.brain_visible_docs(p_email) bv)
               AND (v_sources IS NULL OR s.label = ANY(v_sources))
               AND (v_folder  IS NULL OR d.folder_path = v_folder OR d.folder_path LIKE v_folder || '%')
               AND (v_since   IS NULL OR d.occurred_at >= v_since)
               AND (v_until   IS NULL OR d.occurred_at <= v_until)
             ORDER BY a.score DESC
             LIMIT v_k
          ) t;
    END IF;

    -- (2) Brute fallback: tier not ready, or ANN under-returned (selective filter → small candidate set).
    IF v_ids IS NULL OR array_length(v_ids, 1) < v_k THEN
        SELECT array_agg(t.chunk_id ORDER BY t.sc DESC, t.chunk_id),
               array_agg(t.sc ORDER BY t.sc DESC, t.chunk_id)
          INTO v_ids, v_scores
          FROM (
            SELECT c.chunk_id, rvbbit.cosine_vec(c.embedding, v_q) AS sc
              FROM rvbbit.brain_chunks c
              JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
              JOIN rvbbit.brain_sources s   ON s.source_id = d.source_id
             WHERE c.embedding IS NOT NULL
               AND c.doc_id IN (SELECT bv.doc_id FROM rvbbit.brain_visible_docs(p_email) bv)
               AND (v_sources IS NULL OR s.label = ANY(v_sources))
               AND (v_folder  IS NULL OR d.folder_path = v_folder OR d.folder_path LIKE v_folder || '%')
               AND (v_since   IS NULL OR d.occurred_at >= v_since)
               AND (v_until   IS NULL OR d.occurred_at <= v_until)
             ORDER BY sc DESC
             LIMIT v_k
          ) t;
    END IF;

    IF v_ids IS NULL THEN RETURN; END IF;

    -- build result rows (+ per-chunk entity rollup) for just the ranked chunks
    RETURN QUERY
        SELECT d.doc_id, c.chunk_id, c.idx, d.title, d.folder_path, s.label, d.occurred_at, c.text,
               v_scores[arr.ord]::double precision AS score,
               coalesce((SELECT array_agg(lbl ORDER BY prio, lbl) FROM (
                    SELECT max(ob.label) AS lbl,
                           min(CASE WHEN ob.kind IN ('location','state','place','organization','person',
                                                     'metric','event','product','program') THEN 0 ELSE 1 END) AS prio
                      FROM rvbbit.kg_evidence ev
                      JOIN rvbbit.kg_edges me ON me.edge_id = ev.edge_id AND me.predicate_norm = 'mentions'
                      JOIN rvbbit.kg_nodes ob ON ob.node_id = me.object_node_id
                     WHERE ev.graph_id = 'brain' AND ev.source_table = 'rvbbit.brain_chunks'::regclass
                       AND ev.source_pk = c.chunk_id::text
                       AND NOT rvbbit._brain_is_junk_entity(ob.label)
                     GROUP BY rvbbit._brain_norm_key(ob.label)
                     ORDER BY min(CASE WHEN ob.kind IN ('location','state','place','organization','person',
                                                        'metric','event','product','program') THEN 0 ELSE 1 END),
                              max(lower(ob.label))
                     LIMIT 12) z), '{}') AS entities
          FROM unnest(v_ids) WITH ORDINALITY AS arr(cid, ord)
          JOIN rvbbit.brain_chunks c    ON c.chunk_id = arr.cid
          JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
          JOIN rvbbit.brain_sources s   ON s.source_id = d.source_id
         ORDER BY arr.ord;
END $fn$;

-- Keep the ANN mirror current: refresh it in the nightly AFTER sync+enrich (a doc not yet in the mirror
-- is invisible to the ANN path until then; the brute fallback only triggers on under-return, so a stale
-- mirror that returns ≥k would miss new docs). Full rebuild is fine at this scale; revisit if it grows.
CREATE OR REPLACE FUNCTION rvbbit.brain_nightly(p_max_docs int DEFAULT 100, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_sync jsonb; v_qsync jsonb; v_enrich jsonb; v_vec jsonb;
BEGIN
    v_sync   := rvbbit.brain_sync_sources('auto');
    v_qsync  := rvbbit.brain_sync_query_sources('auto');
    v_enrich := rvbbit.brain_enrich_pending(p_max_docs, p_max_chunks);
    BEGIN v_vec := rvbbit.vector_refresh('brain_chunks');
    EXCEPTION WHEN OTHERS THEN v_vec := jsonb_build_object('ok', false, 'reason', SQLERRM); END;
    RETURN jsonb_build_object('sync', v_sync, 'query_sync', v_qsync, 'enrich', v_enrich, 'vector', v_vec);
END $fn$;

-- ask_brain delegates to brain_search (shares the tier + filters); thinner column set for the MCP/lens.
CREATE OR REPLACE FUNCTION rvbbit.ask_brain(
    p_email text, p_query text, p_k int DEFAULT 8, p_filter jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE(doc_id bigint, title text, folder_path text, source text,
                occurred_at timestamptz, chunk text, score double precision)
LANGUAGE sql STABLE AS $fn$
    SELECT doc_id, title, folder_path, source, occurred_at, chunk, score
      FROM rvbbit.brain_search(p_email, p_query, p_k, p_filter);
$fn$;
