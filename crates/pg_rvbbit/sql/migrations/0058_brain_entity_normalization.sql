-- 0058_brain_entity_normalization — normalize before dedup/overlap so concepts match across forms.
--
-- Agent feedback: `student`/`students`, `Texas`/`TX`, `Edvera`/`EDvera` each count as separate
-- entities, inflating counts and — worse — WEAKENING relatedness recall (a doc saying "TX" never
-- matches one saying "Texas"). Fix: a normalized matching key = lowercase + Snowball stemmer (plurals)
-- + a US-state abbrev↔name alias map. brain_related now matches/dedups/idf-weights on this key, and
-- surfaces a representative display label. (Display stays human; the KEY drives the math.)
--
-- Note: normalization is computed at READ time (per entity node). Fine at this corpus size; if it
-- ever gets slow, materialize the key on kg_nodes.

-- Canonical US state name for an abbrev OR a full name (lowercased), else NULL.
CREATE OR REPLACE FUNCTION rvbbit._brain_state_full(p text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT m.stname FROM (VALUES
        ('al','alabama'),('ak','alaska'),('az','arizona'),('ar','arkansas'),('ca','california'),
        ('co','colorado'),('ct','connecticut'),('de','delaware'),('fl','florida'),('ga','georgia'),
        ('hi','hawaii'),('id','idaho'),('il','illinois'),('in','indiana'),('ia','iowa'),
        ('ks','kansas'),('ky','kentucky'),('la','louisiana'),('me','maine'),('md','maryland'),
        ('ma','massachusetts'),('mi','michigan'),('mn','minnesota'),('ms','mississippi'),('mo','missouri'),
        ('mt','montana'),('ne','nebraska'),('nv','nevada'),('nh','new hampshire'),('nj','new jersey'),
        ('nm','new mexico'),('ny','new york'),('nc','north carolina'),('nd','north dakota'),('oh','ohio'),
        ('ok','oklahoma'),('or','oregon'),('pa','pennsylvania'),('ri','rhode island'),('sc','south carolina'),
        ('sd','south dakota'),('tn','tennessee'),('tx','texas'),('ut','utah'),('vt','vermont'),
        ('va','virginia'),('wa','washington'),('wv','west virginia'),('wi','wisconsin'),('wy','wyoming'),
        ('dc','district of columbia')
    ) AS m(abbr, stname)
    WHERE lower(btrim(p)) = m.abbr OR lower(btrim(p)) = m.stname
    LIMIT 1;
$fn$;

-- Normalized matching key: state canon → else per-word Snowball stem → else lowercased label.
CREATE OR REPLACE FUNCTION rvbbit._brain_norm_key(p_label text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT coalesce(
        rvbbit._brain_state_full(p_label),
        nullif((SELECT string_agg(coalesce((ts_lexize('english_stem', wrd))[1], wrd), ' ' ORDER BY ord)
                FROM unnest(regexp_split_to_array(lower(btrim(p_label)), '\s+')) WITH ORDINALITY AS t(wrd, ord)), ''),
        lower(btrim(p_label)));
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_related(p_email text, p_doc_id bigint, p_max int DEFAULT 15)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    -- normalized key per (non-junk) entity node, computed once
    nodekey AS (SELECT n.node_id, n.kind, n.label, rvbbit._brain_norm_key(n.label) AS nk
                  FROM rvbbit.kg_nodes n
                 WHERE n.graph_id='brain' AND n.kind <> 'document' AND NOT rvbbit._brain_is_junk_entity(n.label)),
    ndocs AS (SELECT greatest(count(*), 1)::float AS n FROM rvbbit.kg_nodes WHERE graph_id='brain' AND kind='document'),
    docfreq AS (SELECT nk.nk, count(DISTINCT e.subject_node_id) AS df
                  FROM rvbbit.kg_edges e JOIN nodekey nk ON nk.node_id = e.object_node_id
                  JOIN rvbbit.kg_nodes dd ON dd.node_id = e.subject_node_id AND dd.kind='document'
                 WHERE e.graph_id='brain' AND e.predicate_norm='mentions' GROUP BY nk.nk),
    -- this doc's entity keys + a representative label + within-doc frequency
    mine AS (SELECT nk.nk,
                    (array_agg(nk.label ORDER BY freq DESC))[1] AS label,
                    (array_agg(nk.kind  ORDER BY freq DESC))[1] AS kind,
                    sum(freq)::int AS freq
               FROM (SELECT nk.node_id, nk.nk, nk.label, nk.kind,
                            count(ev.evidence_id) AS freq
                       FROM rvbbit.kg_edges e JOIN dn ON e.subject_node_id = dn.node_id
                       JOIN nodekey nk ON nk.node_id = e.object_node_id
                       LEFT JOIN rvbbit.kg_evidence ev ON ev.edge_id = e.edge_id
                      WHERE e.graph_id='brain' AND e.predicate_norm='mentions'
                      GROUP BY nk.node_id, nk.nk, nk.label, nk.kind) nk
              GROUP BY nk.nk),
    other AS (SELECT (n2.properties->>'doc_id')::bigint AS rdoc, nk.nk
                FROM rvbbit.kg_edges e2 JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
                JOIN nodekey nk ON nk.node_id = e2.object_node_id
               WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
                 AND (n2.properties->>'doc_id') IS NOT NULL
                 AND (n2.properties->>'doc_id')::bigint <> p_doc_id
                 AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))),
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
