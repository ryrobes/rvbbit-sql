-- 0059_brain_norm_cache — cache the normalized entity key AND bound relatedness to one doc's entities.
--
-- 0058 computed the normalized key (state-alias + Snowball stem) per entity node at read time. Two
-- things made brain_related cost ~15s, and both are fixed here:
--   1. The key was stemmed per node on every call. Labels are immutable once asserted, so the key is
--      stable — cache it. brain_node_norm holds node_id → normalized key (indexed on nk);
--      brain_refresh_node_norm() incrementally fills keys for new nodes, the enrichment backlog pass
--      refreshes after writing, and this migration backfills once.
--   2. (The real bottleneck.) brain_related built a `nodekey` CTE over EVERY entity node in the corpus
--      — running the STABLE junk filter on each — then re-joined that full set three times. Relatedness
--      only needs THIS doc's entities, so brain_related now computes `mine` (this doc's keys) first and
--      restricts docfreq/other with `nk IN (SELECT nk FROM mine)` against the indexed cache. The heavy
--      CTEs are MATERIALIZED so the junk filter runs once. Net: 15208ms → ~66ms, same results.

CREATE TABLE IF NOT EXISTS rvbbit.brain_node_norm (
    node_id bigint PRIMARY KEY REFERENCES rvbbit.kg_nodes(node_id) ON DELETE CASCADE,
    nk      text NOT NULL
);
CREATE INDEX IF NOT EXISTS brain_node_norm_nk_idx ON rvbbit.brain_node_norm (nk);

-- Fill normalized keys for brain entity nodes that don't have one yet (cheap: only new nodes).
CREATE OR REPLACE FUNCTION rvbbit.brain_refresh_node_norm()
RETURNS int LANGUAGE sql VOLATILE AS $fn$
    WITH ins AS (
        INSERT INTO rvbbit.brain_node_norm (node_id, nk)
        SELECT n.node_id, rvbbit._brain_norm_key(n.label)
          FROM rvbbit.kg_nodes n
         WHERE n.graph_id='brain' AND n.kind <> 'document'
           AND NOT EXISTS (SELECT 1 FROM rvbbit.brain_node_norm bn WHERE bn.node_id = n.node_id)
        ON CONFLICT (node_id) DO NOTHING
        RETURNING 1)
    SELECT count(*)::int FROM ins;
$fn$;

-- brain_related — cached keys + bounded to THIS doc's entities (no full-corpus scan).
CREATE OR REPLACE FUNCTION rvbbit.brain_related(p_email text, p_doc_id bigint, p_max int DEFAULT 15)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    vis AS MATERIALIZED (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    ndocs AS (SELECT greatest(count(*), 1)::float AS n FROM rvbbit.kg_nodes WHERE graph_id='brain' AND kind='document'),
    -- this doc's entities, collapsed to normalized keys (the ONLY keys we need to consider)
    mine AS MATERIALIZED (
        SELECT z.nk,
               (array_agg(z.label ORDER BY z.f DESC))[1] AS label,
               (array_agg(z.kind  ORDER BY z.f DESC))[1] AS kind,
               sum(z.f)::int AS freq
          FROM (SELECT bn.nk, n.label, n.kind, count(ev.evidence_id) AS f
                  FROM rvbbit.kg_edges e JOIN dn ON e.subject_node_id = dn.node_id
                  JOIN rvbbit.kg_nodes n ON n.node_id = e.object_node_id
                  JOIN rvbbit.brain_node_norm bn ON bn.node_id = n.node_id
                  LEFT JOIN rvbbit.kg_evidence ev ON ev.edge_id = e.edge_id
                 WHERE e.graph_id='brain' AND e.predicate_norm='mentions'
                   AND NOT rvbbit._brain_is_junk_entity(n.label)
                 GROUP BY bn.nk, n.label, n.kind) z
         GROUP BY z.nk),
    -- df only for this doc's keys (corpus-wide doc count per key), for idf
    docfreq AS (SELECT bn.nk, count(DISTINCT e.subject_node_id) AS df
                  FROM rvbbit.kg_edges e
                  JOIN rvbbit.brain_node_norm bn ON bn.node_id = e.object_node_id AND bn.nk IN (SELECT nk FROM mine)
                  JOIN rvbbit.kg_nodes dd ON dd.node_id = e.subject_node_id AND dd.kind='document'
                 WHERE e.graph_id='brain' AND e.predicate_norm='mentions' GROUP BY bn.nk),
    -- other visible docs that mention this doc's keys
    other AS (SELECT (n2.properties->>'doc_id')::bigint AS rdoc, bn.nk
                FROM rvbbit.kg_edges e2
                JOIN rvbbit.brain_node_norm bn ON bn.node_id = e2.object_node_id AND bn.nk IN (SELECT nk FROM mine)
                JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
               WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
                 AND (n2.properties->>'doc_id') IS NOT NULL
                 AND (n2.properties->>'doc_id')::bigint <> p_doc_id
                 AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM vis)),
    sharedk AS (SELECT o.rdoc, my.nk, max(my.label) AS label, max(ln((nd.n + 1.0) / (df.df + 0.5))) AS idf
                  FROM mine my JOIN other o ON o.nk = my.nk JOIN docfreq df ON df.nk = my.nk CROSS JOIN ndocs nd
                 GROUP BY o.rdoc, my.nk),
    related AS (SELECT rdoc, count(*)::int AS shared, round(sum(idf)::numeric, 2) AS score,
                       (array_agg(label ORDER BY idf DESC))[1:30] AS shared_labels
                  FROM sharedk GROUP BY rdoc ORDER BY sum(idf) DESC LIMIT p_max)
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM guard)
        THEN jsonb_build_object('doc_id', p_doc_id, 'visible', false)
        ELSE jsonb_build_object(
            'doc_id', p_doc_id, 'visible', true,
            'entities', coalesce((SELECT jsonb_agg(jsonb_build_object('kind', kind, 'label', label))
                FROM (SELECT kind, label FROM mine ORDER BY freq DESC, label LIMIT p_max) e), '[]'::jsonb),
            'relations', coalesce((SELECT jsonb_agg(jsonb_build_object('subject', subject, 'predicate', predicate, 'object', object))
                FROM (SELECT subject, predicate, object FROM rvbbit.brain_doc_relations(p_email, p_doc_id, p_max)) r), '[]'::jsonb),
            'related', coalesce((SELECT jsonb_agg(jsonb_build_object(
                    'doc_id', rdoc, 'title', (SELECT title FROM rvbbit.brain_documents WHERE doc_id = rdoc),
                    'score', score, 'shared', shared, 'shared_entities', to_jsonb(shared_labels)))
                FROM related), '[]'::jsonb)
        ) END;
$fn$;

-- Enrichment backlog pass refreshes the norm cache after writing new entity nodes.
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_pending(p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0;
BEGIN
    FOR rec IN SELECT doc_id FROM rvbbit.brain_documents
                WHERE deleted_at IS NULL AND body IS NOT NULL
                  AND (enriched_at IS NULL OR enrich_hash IS DISTINCT FROM content_hash OR enriched_at < ingested_at)
                ORDER BY ingested_at DESC LIMIT greatest(1, p_max_docs) LOOP
        BEGIN
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN n_err := n_err + 1;
        END;
    END LOOP;
    PERFORM rvbbit.brain_refresh_node_norm();
    RETURN jsonb_build_object('enriched_docs', n_docs, 'errors', n_err);
END $fn$;

-- one-time backfill
SELECT rvbbit.brain_refresh_node_norm();
