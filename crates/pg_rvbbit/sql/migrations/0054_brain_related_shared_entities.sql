-- 0054_brain_related_shared_entities — make `shared` self-explaining.
--
-- Agent ask: a `shared: 17` should be reconcilable from the payload, not require diffing two
-- 500-entity lists. So each related doc now carries `shared_entities` — the ACTUAL overlapping entity
-- labels (len == shared, capped at 30) — drawn from the same mentions store as the count. The graph
-- explains itself: you can read exactly which concepts make two docs related.

CREATE OR REPLACE FUNCTION rvbbit.brain_related(p_email text, p_doc_id bigint, p_max int DEFAULT 15)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    ents AS (SELECT DISTINCT e.object_node_id AS ent
               FROM rvbbit.kg_edges e JOIN dn ON e.subject_node_id = dn.node_id
              WHERE e.graph_id='brain' AND e.predicate_norm='mentions'),
    related AS (
        SELECT (n2.properties->>'doc_id')::bigint AS rdoc,
               count(DISTINCT ob.node_id)::int AS shared,
               (array_agg(DISTINCT ob.label))[1:30] AS shared_labels
          FROM rvbbit.kg_edges e2
          JOIN ents ON e2.object_node_id = ents.ent
          JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
          JOIN rvbbit.kg_nodes ob ON ob.node_id = ents.ent
         WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
           AND (n2.properties->>'doc_id') IS NOT NULL
           AND (n2.properties->>'doc_id')::bigint <> p_doc_id
           AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
         GROUP BY rdoc
         ORDER BY count(DISTINCT ob.node_id) DESC LIMIT p_max
    )
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM guard)
        THEN jsonb_build_object('doc_id', p_doc_id, 'visible', false)
        ELSE jsonb_build_object(
            'doc_id', p_doc_id, 'visible', true,
            'entities', coalesce((SELECT jsonb_agg(jsonb_build_object('kind', ne.kind, 'label', ne.label))
                FROM (SELECT n.kind, n.label FROM rvbbit.kg_nodes n JOIN ents ON n.node_id = ents.ent
                      ORDER BY n.label LIMIT p_max) ne), '[]'::jsonb),
            'relations', coalesce((SELECT jsonb_agg(jsonb_build_object('subject', subject, 'predicate', predicate, 'object', object))
                FROM (SELECT subject, predicate, object FROM rvbbit.brain_doc_relations(p_email, p_doc_id, p_max)) r), '[]'::jsonb),
            'related', coalesce((SELECT jsonb_agg(jsonb_build_object(
                    'doc_id', rdoc,
                    'title', (SELECT title FROM rvbbit.brain_documents WHERE doc_id = rdoc),
                    'shared', shared,
                    'shared_entities', to_jsonb(shared_labels)))
                FROM related), '[]'::jsonb)
        ) END;
$fn$;
