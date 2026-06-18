-- 0057_brain_related_tfidf — make `shared` a rankable relevance SCORE, not a raw count.
--
-- After denoise, `shared` is a clean count — but every shared entity still counts equally, so two SOPs
-- that both mention generic-but-not-junk terms (Location, state, compliance, due date) score the same
-- as a pair sharing rare, discriminating ones (AirSlate, Illinois, Keith). tf-idf fixes the weighting:
-- each shared entity contributes its inverse-document-frequency idf = ln((N+1)/(df+0.5)), where df is
-- how many docs mention it across the corpus. Ubiquitous entities → idf ≈ 0; rare ones → high idf.
--
-- The related block now carries `score` (sum of idf over the shared entities — what you rank/threshold
-- on), keeps `shared` (the raw count, for transparency), and orders `shared_entities` by idf so the
-- DISCRIMINATING entities lead. Related docs are ranked by score.

-- Extend the junk denylist with pronouns/booleans/generic determiners that slipped through.
CREATE OR REPLACE FUNCTION rvbbit._brain_is_junk_entity(p_label text)
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT p_label IS NULL
        OR length(btrim(p_label)) < 2
        OR btrim(p_label) !~ '[A-Za-z]'
        OR btrim(p_label) ~ '^[\$€£]?\s*[\d.,]+\s*%?$'
        OR lower(btrim(p_label)) = ANY (string_to_array(
              coalesce(nullif(current_setting('rvbbit.brain_entity_stopwords', true), ''),
                'email,date,time,form,account,document,documents,system,application,market,spreadsheet,'
                'rules,file,page,number,url,phone,address,google,data,report,reports,field,fields,status,'
                'contact,update,it,i,force,lightning,request,process,team,user,users,link,note,notes,'
                'information,section,column,row,record,records,item,items,type,name,'
                'you,your,we,our,they,true,false,yes,no,none,n/a,na,all,any,other,this,that,here,there'), ','));
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_related(p_email text, p_doc_id bigint, p_max int DEFAULT 15)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    ents AS (SELECT DISTINCT e.object_node_id AS ent
               FROM rvbbit.kg_edges e JOIN dn ON e.subject_node_id = dn.node_id
              WHERE e.graph_id='brain' AND e.predicate_norm='mentions'),
    ndocs AS (SELECT greatest(count(*), 1)::float AS n FROM rvbbit.kg_nodes WHERE graph_id='brain' AND kind='document'),
    docfreq AS (   -- df: how many docs mention each entity (corpus-wide), for idf
        SELECT e.object_node_id AS ent, count(DISTINCT e.subject_node_id) AS df
          FROM rvbbit.kg_edges e JOIN rvbbit.kg_nodes n ON n.node_id = e.subject_node_id AND n.kind='document'
         WHERE e.graph_id='brain' AND e.predicate_norm='mentions'
         GROUP BY e.object_node_id),
    shared_ents AS (   -- one row per (other visible doc, shared non-junk entity), with its idf weight
        SELECT (n2.properties->>'doc_id')::bigint AS rdoc, ob.label AS lbl,
               ln((nd.n + 1.0) / (dfreq.df + 0.5)) AS idf
          FROM rvbbit.kg_edges e2
          JOIN ents ON e2.object_node_id = ents.ent
          JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
          JOIN rvbbit.kg_nodes ob ON ob.node_id = ents.ent
          JOIN docfreq dfreq ON dfreq.ent = ents.ent
          CROSS JOIN ndocs nd
         WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
           AND (n2.properties->>'doc_id') IS NOT NULL
           AND (n2.properties->>'doc_id')::bigint <> p_doc_id
           AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
           AND NOT rvbbit._brain_is_junk_entity(ob.label)),
    shared_dedup AS (   -- collapse same-label/different-kind nodes so the count + list reconcile
        SELECT rdoc, max(lbl) AS lbl, max(idf) AS idf
          FROM shared_ents GROUP BY rdoc, lower(lbl)),
    related AS (
        SELECT rdoc,
               count(*)::int AS shared,
               round(sum(idf)::numeric, 2) AS score,
               (array_agg(lbl ORDER BY idf DESC))[1:30] AS shared_labels
          FROM shared_dedup GROUP BY rdoc
         ORDER BY sum(idf) DESC LIMIT p_max)
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM guard)
        THEN jsonb_build_object('doc_id', p_doc_id, 'visible', false)
        ELSE jsonb_build_object(
            'doc_id', p_doc_id, 'visible', true,
            'entities', coalesce((SELECT jsonb_agg(jsonb_build_object('kind', kind, 'label', label)) FROM (
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
                    'score', score, 'shared', shared, 'shared_entities', to_jsonb(shared_labels)))
                FROM related), '[]'::jsonb)
        ) END;
$fn$;
