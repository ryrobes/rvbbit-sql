-- 0056_brain_entity_denoise — denoise, dedup, and rank-before-truncate the entity surfaces.
--
-- Agent feedback on the full-NER run: the extractor now reaches every chunk, but the visible entity
-- fields are noisy: (a) generic stopwords ("email","date","Form","account","google") and tokenizer
-- fragments ("force","lightning" ← Salesforce) inflate `shared`; (b) pure amounts ("$","$100") and
-- single chars ("I","IT") leak in; (c) the doc-level `entities` list was alpha-sorted then truncated,
-- so a doc returned 15 dollar-signs and dropped every real entity; (d) the per-chunk cap (8) evicted
-- `Florida` in favor of person names on a geographic query.
--
-- These are READ-surface fixes — they clean what brain_search / brain_related return without a
-- re-enrich (the junk still exists as nodes, just filtered on read). Changes:
--   • _brain_is_junk_entity(): single chars, letterless tokens (amounts/symbols), and a generic
--     stopword/​fragment denylist (tunable via rvbbit.brain_entity_stopwords).
--   • brain_search: chunk entities deduped case-insensitively, junk-filtered, geo/org/person kinds
--     prioritized into the (raised to 12) cap so query-relevant entities survive.
--   • brain_related: doc `entities` ranked by mention-frequency (salience) then truncated; `shared`
--     count + `shared_entities` exclude junk so the number means something and still reconciles.

CREATE OR REPLACE FUNCTION rvbbit._brain_is_junk_entity(p_label text)
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT p_label IS NULL
        OR length(btrim(p_label)) < 2                                  -- single char
        OR btrim(p_label) !~ '[A-Za-z]'                                -- no letters: symbols / pure numbers
        OR btrim(p_label) ~ '^[\$€£]?\s*[\d.,]+\s*%?$'                 -- currency / numeric amounts
        OR lower(btrim(p_label)) = ANY (string_to_array(
              coalesce(nullif(current_setting('rvbbit.brain_entity_stopwords', true), ''),
                'email,date,time,form,account,document,documents,system,application,market,spreadsheet,'
                'rules,file,page,number,url,phone,address,google,data,report,reports,field,fields,status,'
                'contact,update,it,i,force,lightning,request,process,team,user,users,link,note,notes,'
                'information,section,column,row,record,records,item,items,type,name'), ','));
$fn$;

-- ── enriched search: chunk entities deduped, junk-filtered, geo/org/person-prioritized, cap 12 ──
CREATE OR REPLACE FUNCTION rvbbit.brain_search(p_email text, p_query text, p_k int DEFAULT 8)
RETURNS TABLE(doc_id bigint, chunk_id bigint, chunk_idx int, title text, folder_path text,
              source text, occurred_at timestamptz, chunk text, score double precision, entities text[])
LANGUAGE sql STABLE AS $fn$
    WITH q AS (SELECT rvbbit.embed(p_query) AS v),
         vis AS (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
    SELECT d.doc_id, c.chunk_id, c.idx, d.title, d.folder_path, s.label, d.occurred_at,
           c.text, rvbbit.cosine_vec(c.embedding, (SELECT v FROM q)) AS score,
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
                 GROUP BY lower(ob.label)
                 ORDER BY min(CASE WHEN ob.kind IN ('location','state','place','organization','person',
                                                    'metric','event','product','program') THEN 0 ELSE 1 END),
                          lower(ob.label)
                 LIMIT 12) z), '{}') AS entities
    FROM rvbbit.brain_chunks c
    JOIN vis ON vis.doc_id = c.doc_id
    JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    WHERE c.embedding IS NOT NULL AND nullif(btrim(coalesce(p_query, '')), '') IS NOT NULL
    ORDER BY score DESC
    LIMIT greatest(1, least(coalesce(p_k, 8), 50));
$fn$;

-- ── related: salience-ranked doc entities + junk-free shared count/list ────────
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
               count(DISTINCT lower(ob.label))::int AS shared,
               (array_agg(DISTINCT ob.label))[1:30] AS shared_labels
          FROM rvbbit.kg_edges e2
          JOIN ents ON e2.object_node_id = ents.ent
          JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
          JOIN rvbbit.kg_nodes ob ON ob.node_id = ents.ent
         WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
           AND (n2.properties->>'doc_id') IS NOT NULL
           AND (n2.properties->>'doc_id')::bigint <> p_doc_id
           AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
           AND NOT rvbbit._brain_is_junk_entity(ob.label)
         GROUP BY rdoc
         ORDER BY count(DISTINCT lower(ob.label)) DESC LIMIT p_max
    )
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM guard)
        THEN jsonb_build_object('doc_id', p_doc_id, 'visible', false)
        ELSE jsonb_build_object(
            'doc_id', p_doc_id, 'visible', true,
            'entities', coalesce((SELECT jsonb_agg(jsonb_build_object('kind', kind, 'label', label)) FROM (
                -- dedup case-insensitively (keep the highest-frequency kind/casing), then rank by salience
                SELECT kind, label FROM (
                    SELECT DISTINCT ON (lower(n.label)) n.kind, n.label,
                           (SELECT count(*) FROM rvbbit.kg_evidence ev JOIN rvbbit.kg_edges me ON me.edge_id = ev.edge_id
                            WHERE me.object_node_id = n.node_id AND me.subject_node_id = (SELECT node_id FROM dn)
                              AND me.predicate_norm='mentions') AS freq
                      FROM rvbbit.kg_nodes n JOIN ents ON n.node_id = ents.ent
                     WHERE NOT rvbbit._brain_is_junk_entity(n.label)
                     ORDER BY lower(n.label),
                              (SELECT count(*) FROM rvbbit.kg_evidence ev JOIN rvbbit.kg_edges me ON me.edge_id = ev.edge_id
                               WHERE me.object_node_id = n.node_id AND me.subject_node_id = (SELECT node_id FROM dn)
                                 AND me.predicate_norm='mentions') DESC) d
                 ORDER BY freq DESC, label LIMIT p_max) e), '[]'::jsonb),
            'relations', coalesce((SELECT jsonb_agg(jsonb_build_object('subject', subject, 'predicate', predicate, 'object', object))
                FROM (SELECT subject, predicate, object FROM rvbbit.brain_doc_relations(p_email, p_doc_id, p_max)) r), '[]'::jsonb),
            'related', coalesce((SELECT jsonb_agg(jsonb_build_object(
                    'doc_id', rdoc, 'title', (SELECT title FROM rvbbit.brain_documents WHERE doc_id = rdoc),
                    'shared', shared, 'shared_entities', to_jsonb(shared_labels)))
                FROM related), '[]'::jsonb)
        ) END;
$fn$;
