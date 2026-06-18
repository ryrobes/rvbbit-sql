-- 0060_brain_shared_idf_floor — clean the SHARED set itself, not just the score/ranking.
--
-- Agent feedback after 0059: tf-idf `score` ranks correctly, but the raw `shared` count and the
-- `shared_entities` list re-inflated (B2B 16→25) because the fuller NER entity set fed the overlap with
-- generic section-header nouns (website, marketing, forms, responses, documentation, decision, …). The
-- score down-weights those (low idf) so it stayed sane, but the COUNT/LIST counted them equally — count
-- and score told different stories, and an agent thresholding on the raw count gets misled.
--
-- Two reasons idf-floor ALONE can't fix it at this corpus size (6 docs): the generic nouns and the
-- genuinely-discriminating ones (AirSlate) BOTH sit at df=2 → idf=1.03, so no threshold separates them.
-- The lexical denylist is what actually separates generic English from proper nouns here; it regressed
-- because it matched RAW surface forms, so plurals/gerunds (forms, marketing, documentation) slipped
-- past entries like form/market/document. Fix is two-pronged:
--   1. STEM-AWARE denylist applied to shared-membership: match on the Snowball stem so one entry
--      (form, market, document, …) catches every morphological variant. Filters `mine` (the doc's own
--      entity set), which feeds entities/shared/related — so the count, the list, and the doc entities
--      all clean up together.
--   2. idf FLOOR on shared-membership (GUC rvbbit.brain_shared_idf_floor, default 0.3): the scalable
--      backstop. Does little at 6 docs, but as the corpus grows it auto-drops corpus-ubiquitous words
--      (≈70%+ of docs) without manual denylisting, keeping `shared` aligned with `score`.
-- Also: brain_search now dedups chunk entities by normalized key (student/students collapse).

-- Single source of truth for the stopword word list (human-readable; raw + stemmed matching both read it).
CREATE OR REPLACE FUNCTION rvbbit._brain_stopwords()
RETURNS text[] LANGUAGE sql STABLE AS $fn$
    SELECT string_to_array(
        coalesce(nullif(current_setting('rvbbit.brain_entity_stopwords', true), ''),
            'email,date,time,form,account,document,documents,system,application,market,spreadsheet,'
            'rules,file,page,number,url,phone,address,google,data,report,reports,field,fields,status,'
            'contact,update,it,i,force,lightning,request,process,team,user,users,link,note,notes,'
            'information,section,column,row,record,records,item,items,type,name,'
            'you,your,we,our,they,true,false,yes,no,none,n/a,na,all,any,other,this,that,here,there,'
            'website,websites,activation,marketing,response,responses,documentation,decision,decisions'), ',');
$fn$;

-- Structural junk + raw-surface stopword match (used by brain_search per-chunk surface).
CREATE OR REPLACE FUNCTION rvbbit._brain_is_junk_entity(p_label text)
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT p_label IS NULL
        OR length(btrim(p_label)) < 2                                  -- single char
        OR btrim(p_label) !~ '[A-Za-z]'                                -- no letters: symbols / pure numbers
        OR btrim(p_label) ~ '^[\$€£]?\s*[\d.,]+\s*%?$'                 -- currency / numeric amounts
        OR lower(btrim(p_label)) = ANY (rvbbit._brain_stopwords());
$fn$;

-- brain_related — stem-aware denylist + idf floor on shared-membership (count now matches score).
CREATE OR REPLACE FUNCTION rvbbit.brain_related(p_email text, p_doc_id bigint, p_max int DEFAULT 15)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    vis AS MATERIALIZED (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    ndocs AS (SELECT greatest(count(*), 1)::float AS n FROM rvbbit.kg_nodes WHERE graph_id='brain' AND kind='document'),
    floor AS (SELECT coalesce(nullif(current_setting('rvbbit.brain_shared_idf_floor', true), '')::float, 0.3) AS v),
    -- stopword stems, computed once: a generic noun and its plural/gerund collapse to one key here
    stops AS MATERIALIZED (SELECT DISTINCT rvbbit._brain_norm_key(w) AS s FROM unnest(rvbbit._brain_stopwords()) w),
    -- this doc's entities, collapsed to normalized keys, junk + stopword-stems removed
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
                   AND bn.nk NOT IN (SELECT s FROM stops)
                 GROUP BY bn.nk, n.label, n.kind) z
         GROUP BY z.nk),
    docfreq AS (SELECT bn.nk, count(DISTINCT e.subject_node_id) AS df
                  FROM rvbbit.kg_edges e
                  JOIN rvbbit.brain_node_norm bn ON bn.node_id = e.object_node_id AND bn.nk IN (SELECT nk FROM mine)
                  JOIN rvbbit.kg_nodes dd ON dd.node_id = e.subject_node_id AND dd.kind='document'
                 WHERE e.graph_id='brain' AND e.predicate_norm='mentions' GROUP BY bn.nk),
    other AS (SELECT (n2.properties->>'doc_id')::bigint AS rdoc, bn.nk
                FROM rvbbit.kg_edges e2
                JOIN rvbbit.brain_node_norm bn ON bn.node_id = e2.object_node_id AND bn.nk IN (SELECT nk FROM mine)
                JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
               WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
                 AND (n2.properties->>'doc_id') IS NOT NULL
                 AND (n2.properties->>'doc_id')::bigint <> p_doc_id
                 AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM vis)),
    -- one row per (other doc, shared key); idf floor applied to MEMBERSHIP so count = score's story
    sharedk AS (SELECT o.rdoc, my.nk, max(my.label) AS label, max(ln((nd.n + 1.0) / (df.df + 0.5))) AS idf
                  FROM mine my JOIN other o ON o.nk = my.nk JOIN docfreq df ON df.nk = my.nk CROSS JOIN ndocs nd
                 GROUP BY o.rdoc, my.nk
                HAVING max(ln((nd.n + 1.0) / (df.df + 0.5))) >= (SELECT v FROM floor)),
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

-- brain_search — dedup chunk entities by normalized key so student/students (etc.) collapse.
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
                 GROUP BY rvbbit._brain_norm_key(ob.label)
                 ORDER BY min(CASE WHEN ob.kind IN ('location','state','place','organization','person',
                                                    'metric','event','product','program') THEN 0 ELSE 1 END),
                          max(lower(ob.label))
                 LIMIT 12) z), '{}') AS entities
    FROM rvbbit.brain_chunks c
    JOIN vis ON vis.doc_id = c.doc_id
    JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    WHERE c.embedding IS NOT NULL AND nullif(btrim(coalesce(p_query, '')), '') IS NOT NULL
    ORDER BY score DESC
    LIMIT greatest(1, least(coalesce(p_k, 8), 50));
$fn$;
