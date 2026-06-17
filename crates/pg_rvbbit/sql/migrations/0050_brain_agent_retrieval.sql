-- 0050_brain_agent_retrieval — turn the brain from a search box into a navigable surface.
--
-- Philosophy: semantic search is the ENTRY POINT (embeddings = relevance); the knowledge graph is the
-- MAP (edges = recall + navigation). Give the agent grounded chunks + breadcrumbs (entities, related
-- docs) so it can decide what to pull next — two axes of expansion, one tool each, all ACL-gated:
--
--   brain_search(email,q,k)               — ranked chunks + each chunk's doc entities (the enriched
--                                            envelope the ask_brain MCP tool returns).
--   brain_context(email,doc,idx,window)   — VERTICAL: the chunks AROUND a hit (cheaper than the full doc).
--   brain_related(email,doc)              — LATERAL: a doc's graph neighborhood (entities, typed
--                                            relations, related docs) — the threads to pull.
--   brain_entity(email,name)              — LATERAL: entity-centric — its relations + the visible docs
--                                            that mention it ("what do we know about X?").

-- ── enriched semantic search: chunks + breadcrumbs (chunk_idx, score, doc entities) ──
CREATE OR REPLACE FUNCTION rvbbit.brain_search(p_email text, p_query text, p_k int DEFAULT 8)
RETURNS TABLE(doc_id bigint, chunk_id bigint, chunk_idx int, title text, folder_path text,
              source text, occurred_at timestamptz, chunk text, score double precision, entities text[])
LANGUAGE sql STABLE AS $fn$
    WITH q AS (SELECT rvbbit.embed(p_query) AS v),
         vis AS (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
    SELECT d.doc_id, c.chunk_id, c.idx, d.title, d.folder_path, s.label, d.occurred_at,
           c.text, rvbbit.cosine_vec(c.embedding, (SELECT v FROM q)) AS score,
           coalesce((SELECT array_agg(lbl) FROM (
                SELECT ob.label AS lbl
                  FROM rvbbit.kg_nodes dn
                  JOIN rvbbit.kg_edges me ON me.subject_node_id = dn.node_id AND me.predicate_norm = 'mentions'
                  JOIN rvbbit.kg_nodes ob ON ob.node_id = me.object_node_id
                 WHERE dn.graph_id = 'brain' AND dn.kind = 'document'
                   AND dn.label_norm = lower(rvbbit.brain_doc_label(d.doc_id))
                 ORDER BY me.confidence DESC NULLS LAST LIMIT 10) z), '{}') AS entities
    FROM rvbbit.brain_chunks c
    JOIN vis ON vis.doc_id = c.doc_id
    JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    WHERE c.embedding IS NOT NULL AND nullif(btrim(coalesce(p_query, '')), '') IS NOT NULL
    ORDER BY score DESC
    LIMIT greatest(1, least(coalesce(p_k, 8), 50));
$fn$;

-- ── VERTICAL: the chunks surrounding a hit (window on either side), ACL-gated ──
CREATE OR REPLACE FUNCTION rvbbit.brain_context(p_email text, p_doc_id bigint, p_chunk_idx int, p_window int DEFAULT 2)
RETURNS TABLE(idx int, chunk text) LANGUAGE sql STABLE AS $fn$
    SELECT c.idx, c.text FROM rvbbit.brain_chunks c
     WHERE c.doc_id = p_doc_id
       AND c.idx BETWEEN p_chunk_idx - greatest(0, p_window) AND p_chunk_idx + greatest(0, p_window)
       AND EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)
     ORDER BY c.idx;
$fn$;

-- ── LATERAL: a doc's graph neighborhood (entities + typed relations + related docs) ──
CREATE OR REPLACE FUNCTION rvbbit.brain_related(p_email text, p_doc_id bigint, p_max int DEFAULT 15)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT CASE
        WHEN NOT EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)
        THEN jsonb_build_object('doc_id', p_doc_id, 'visible', false)
        ELSE jsonb_build_object(
            'doc_id', p_doc_id, 'visible', true,
            'entities', coalesce((SELECT jsonb_agg(jsonb_build_object('kind', kind, 'label', label))
                FROM (SELECT kind, label FROM rvbbit.brain_doc_graph(p_email, p_doc_id) WHERE rel_type = 'entity' LIMIT p_max) e), '[]'::jsonb),
            'relations', coalesce((SELECT jsonb_agg(jsonb_build_object('subject', subject, 'predicate', predicate, 'object', object))
                FROM (SELECT subject, predicate, object FROM rvbbit.brain_doc_relations(p_email, p_doc_id, p_max)) r), '[]'::jsonb),
            'related', coalesce((SELECT jsonb_agg(jsonb_build_object('doc_id', doc_id, 'title', label, 'shared', weight))
                FROM (SELECT doc_id, label, weight FROM rvbbit.brain_doc_graph(p_email, p_doc_id) WHERE rel_type = 'related_doc' LIMIT p_max) rd), '[]'::jsonb)
        ) END;
$fn$;

-- ── LATERAL: entity-centric — its relations + the visible docs that mention it ──
CREATE OR REPLACE FUNCTION rvbbit.brain_entity(p_email text, p_entity text, p_max int DEFAULT 20)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    WITH n AS (   -- resolve the entity: exact label first, then a contains-match fallback
        SELECT node_id, kind, label FROM rvbbit.kg_nodes
         WHERE graph_id = 'brain' AND kind <> 'document'
           AND (label_norm = lower(btrim(p_entity)) OR label ILIKE '%' || btrim(p_entity) || '%')
         ORDER BY (label_norm = lower(btrim(p_entity))) DESC, length(label) ASC
         LIMIT 1
    ),
    rels AS (
        SELECT sn.label AS subj, e.predicate, ob.label AS obj
          FROM rvbbit.kg_edges e JOIN n ON (e.subject_node_id = n.node_id OR e.object_node_id = n.node_id)
          JOIN rvbbit.kg_nodes sn ON sn.node_id = e.subject_node_id
          JOIN rvbbit.kg_nodes ob ON ob.node_id = e.object_node_id
         WHERE e.graph_id = 'brain' AND e.predicate_norm NOT IN ('mentions', 'links_to')
         ORDER BY e.confidence DESC NULLS LAST LIMIT p_max
    ),
    docs AS (   -- visible docs that mention the entity
        SELECT DISTINCT (dn.properties->>'doc_id')::bigint AS did
          FROM rvbbit.kg_edges me JOIN n ON me.object_node_id = n.node_id
          JOIN rvbbit.kg_nodes dn ON dn.node_id = me.subject_node_id AND dn.kind = 'document'
         WHERE me.graph_id = 'brain' AND me.predicate_norm = 'mentions'
           AND (dn.properties->>'doc_id') IS NOT NULL
           AND (dn.properties->>'doc_id')::bigint IN (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
         LIMIT p_max
    )
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM n) THEN jsonb_build_object('entity', p_entity, 'found', false)
        ELSE jsonb_build_object(
            'entity', (SELECT label FROM n), 'kind', (SELECT kind FROM n), 'found', true,
            'relations', coalesce((SELECT jsonb_agg(jsonb_build_object('subject', subj, 'predicate', predicate, 'object', obj)) FROM rels), '[]'::jsonb),
            'docs', coalesce((SELECT jsonb_agg(jsonb_build_object('doc_id', d.did,
                        'title', (SELECT title FROM rvbbit.brain_documents WHERE doc_id = d.did))) FROM docs d), '[]'::jsonb)
        ) END;
$fn$;
