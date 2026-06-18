-- 0051_brain_chunk_entities — make search-hit entities chunk-scoped + drop junk "list" entities.
--
-- Agent feedback on 0050: brain_search stamped the SAME doc-level entity set on every hit (8× the
-- tokens, zero per-chunk signal), and the LLM occasionally mashes a list into one node
-- ("AL, AR, ID, KS, …"). Fixes:
--   • brain_search entities are now CHUNK-scoped — derived from kg_evidence (each chunk's own mentions),
--     so different hits carry different concepts; doc-level aggregation still happens in the MCP rollup.
--   • a junk filter drops comma-mashed pseudo-entities (3+ comma segments) everywhere they'd surface.
--   • brain_enrich_doc skips those mashed entities at the source AND links chunk evidence on BOTH the
--     subject and object mentions edges (so chunk-scoping has full coverage on freshly-enriched docs).

-- ── enriched search with CHUNK-scoped entities (via evidence) + junk filter ────
CREATE OR REPLACE FUNCTION rvbbit.brain_search(p_email text, p_query text, p_k int DEFAULT 8)
RETURNS TABLE(doc_id bigint, chunk_id bigint, chunk_idx int, title text, folder_path text,
              source text, occurred_at timestamptz, chunk text, score double precision, entities text[])
LANGUAGE sql STABLE AS $fn$
    WITH q AS (SELECT rvbbit.embed(p_query) AS v),
         vis AS (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
    SELECT d.doc_id, c.chunk_id, c.idx, d.title, d.folder_path, s.label, d.occurred_at,
           c.text, rvbbit.cosine_vec(c.embedding, (SELECT v FROM q)) AS score,
           coalesce((SELECT array_agg(lbl) FROM (
                SELECT DISTINCT ob.label AS lbl
                  FROM rvbbit.kg_evidence ev
                  JOIN rvbbit.kg_edges me ON me.edge_id = ev.edge_id AND me.predicate_norm = 'mentions'
                  JOIN rvbbit.kg_nodes ob ON ob.node_id = me.object_node_id
                 WHERE ev.graph_id = 'brain'
                   AND ev.source_table = 'rvbbit.brain_chunks'::regclass
                   AND ev.source_pk = c.chunk_id::text
                   AND ob.label !~ '(,[^,]+){3,}'   -- drop comma-mashed "list" entities
                 LIMIT 8) z), '{}') AS entities
    FROM rvbbit.brain_chunks c
    JOIN vis ON vis.doc_id = c.doc_id
    JOIN rvbbit.brain_documents d ON d.doc_id = c.doc_id
    JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
    WHERE c.embedding IS NOT NULL AND nullif(btrim(coalesce(p_query, '')), '') IS NOT NULL
    ORDER BY score DESC
    LIMIT greatest(1, least(coalesce(p_k, 8), 50));
$fn$;

-- ── enrichment: skip mashed-list entities + evidence on BOTH mentions edges ────
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_doc(p_doc_id bigint, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    g           constant text := 'brain';
    v_doclabel  text;
    v_source_id bigint;
    v_body      text;
    v_hash      text;
    n_rel int := 0; n_men int := 0; n_link int := 0;
    ch  record; tr record; v_tj jsonb; v_men_edge bigint;
    wl  text; v_target bigint; v_subj_kind text; v_obj_kind text;
BEGIN
    SELECT source_id, body, content_hash INTO v_source_id, v_body, v_hash
      FROM rvbbit.brain_documents WHERE doc_id = p_doc_id AND deleted_at IS NULL;
    IF NOT FOUND THEN RETURN jsonb_build_object('skipped', 'not found or deleted'); END IF;

    PERFORM rvbbit.brain_doc_node(p_doc_id);
    v_doclabel := rvbbit.brain_doc_label(p_doc_id);

    FOR ch IN SELECT chunk_id, text FROM rvbbit.brain_chunks
               WHERE doc_id = p_doc_id ORDER BY idx LIMIT greatest(1, p_max_chunks) LOOP
        IF nullif(btrim(ch.text), '') IS NULL THEN CONTINUE; END IF;
        BEGIN v_tj := rvbbit.triples(ch.text, 'all'); EXCEPTION WHEN OTHERS THEN v_tj := '[]'::jsonb; END;
        IF jsonb_typeof(v_tj) <> 'array' THEN CONTINUE; END IF;

        FOR tr IN SELECT * FROM jsonb_to_recordset(v_tj)
                    AS x(subject text, predicate text, object text, evidence text,
                         confidence double precision, subject_kind text, object_kind text) LOOP
            CONTINUE WHEN nullif(btrim(tr.subject),'') IS NULL
                       OR nullif(btrim(tr.object),'') IS NULL
                       OR nullif(btrim(tr.predicate),'') IS NULL
                       -- drop entities the LLM mashed from a list ("AL, AR, ID, KS, …")
                       OR btrim(tr.subject) ~ '(,[^,]+){3,}'
                       OR btrim(tr.object)  ~ '(,[^,]+){3,}';
            v_subj_kind := coalesce(nullif(btrim(tr.subject_kind),''), 'entity');
            v_obj_kind  := coalesce(nullif(btrim(tr.object_kind),''),  'entity');
            IF lower(v_subj_kind) = 'document' THEN v_subj_kind := 'reference'; END IF;
            IF lower(v_obj_kind)  = 'document' THEN v_obj_kind  := 'reference'; END IF;

            PERFORM rvbbit.kg_assert_edge(v_subj_kind, tr.subject, tr.predicate, v_obj_kind, tr.object,
                                          coalesce(tr.confidence, 0.9), '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
            n_rel := n_rel + 1;

            -- doc —mentions→ subject (+ chunk evidence)
            v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_subj_kind, tr.subject,
                                                0.9, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
            PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_chunks'::regclass,
                        ch.chunk_id::text, 'text', coalesce(nullif(tr.evidence,''), left(ch.text, 240)),
                        coalesce(tr.confidence, 0.9), '{}'::jsonb, NULL, g);
            -- doc —mentions→ object (+ chunk evidence, so chunk-scoped entities cover both ends)
            v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_obj_kind, tr.object,
                                                0.9, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
            PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_chunks'::regclass,
                        ch.chunk_id::text, 'text', coalesce(nullif(tr.evidence,''), left(ch.text, 240)),
                        coalesce(tr.confidence, 0.9), '{}'::jsonb, NULL, g);
            n_men := n_men + 2;
        END LOOP;
    END LOOP;

    FOR wl IN SELECT DISTINCT btrim((m)[1]) FROM regexp_matches(coalesce(v_body,''), '\[\[([^\]]+)\]\]', 'g') m LOOP
        CONTINUE WHEN wl = '';
        SELECT doc_id INTO v_target FROM rvbbit.brain_documents
         WHERE source_id = v_source_id AND deleted_at IS NULL AND doc_id <> p_doc_id
           AND (lower(title) = lower(wl) OR uri = wl) LIMIT 1;
        IF v_target IS NOT NULL THEN
            PERFORM rvbbit.brain_doc_node(v_target);
            PERFORM rvbbit.kg_assert_edge('document', v_doclabel, 'links_to', 'document',
                                          rvbbit.brain_doc_label(v_target), 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
        ELSE
            PERFORM rvbbit.kg_assert_edge('document', v_doclabel, 'links_to', 'document',
                                          wl || ' (unresolved)', 0.4,
                                          '{}'::jsonb, jsonb_build_object('unresolved', true), '', 0.0, g);
        END IF;
        n_link := n_link + 1;
    END LOOP;

    UPDATE rvbbit.brain_documents SET enriched_at = now(), enrich_hash = v_hash WHERE doc_id = p_doc_id;
    RETURN jsonb_build_object('doc_id', p_doc_id, 'relations', n_rel, 'mentions', n_men, 'links', n_link);
END $fn$;
