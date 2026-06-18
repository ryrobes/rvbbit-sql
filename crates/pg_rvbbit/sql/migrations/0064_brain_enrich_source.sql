-- 0064_brain_enrich_source — one-click bulk (re-)enrich of a whole source ("set"), triples-optional.
--
-- After syncing a query source (e.g. 100 Linear issues), you want to enrich the WHOLE set in one go.
-- brain_enrich_pending is global + capped; this is source-scoped and uncapped. Two additions:
--   • brain_enrich_doc gains a triples toggle via GUC rvbbit.brain_skip_triples — the LLM relation pass
--     is the expensive part and adds little to short, structured artifacts (tickets); NER + the
--     deterministic structured-edge pass carry the value. (GUC, not a new arg, so no call-site churn.)
--   • brain_enrich_source(source_id, force, max_chunks, skip_triples): enrich every live doc in the
--     source (force) or only the new/changed ones (default). skip_triples defaults to AUTO — on for
--     query/MCP sources (provider set), off for file sources — overridable. Refreshes the norm cache.

-- ── brain_enrich_doc: skip the LLM triples pass when rvbbit.brain_skip_triples is on ──
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_doc(p_doc_id bigint, p_max_chunks integer DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE
    g           constant text := 'brain';
    v_doclabel  text;
    v_docnode   bigint;
    v_source_id bigint;
    v_body      text;
    v_hash      text;
    n_rel int := 0; n_men int := 0; n_link int := 0; n_ner int := 0; n_struct int := 0;
    ch  record; tr record; v_tj jsonb; v_men_edge bigint;
    wl  text; v_target bigint; v_subj_kind text; v_obj_kind text;
    v_ner_on boolean;
    v_labels text;
    v_ner jsonb; ent record; v_ekind text;
    v_ci int := 0;
    v_ner_cap int;
    v_edge_map jsonb; v_props jsonb; es record; v_obj jsonb; v_lbl text;
    v_skip_triples boolean;
BEGIN
    SELECT source_id, body, content_hash INTO v_source_id, v_body, v_hash
      FROM rvbbit.brain_documents WHERE doc_id = p_doc_id AND deleted_at IS NULL;
    IF NOT FOUND THEN RETURN jsonb_build_object('skipped', 'not found or deleted'); END IF;

    v_skip_triples := coalesce(nullif(current_setting('rvbbit.brain_skip_triples', true), '')::boolean, false);

    SELECT bd.props, bdp.edge_map INTO v_props, v_edge_map
      FROM rvbbit.brain_documents bd
      LEFT JOIN rvbbit.brain_sources bs ON bs.source_id = bd.source_id
      LEFT JOIN rvbbit.brain_doc_providers bdp ON bdp.provider = bs.config->>'provider'
     WHERE bd.doc_id = p_doc_id;

    v_docnode := rvbbit.brain_doc_node(p_doc_id);
    v_doclabel := rvbbit.brain_doc_label(p_doc_id);
    DELETE FROM rvbbit.kg_edges
     WHERE graph_id = g AND subject_node_id = v_docnode
       AND (predicate_norm IN ('mentions', 'links_to') OR (properties->>'via') = 'structured');

    v_ner_on := EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
                        WHERE ns.nspname = 'rvbbit' AND p.proname = 'extract_entities');
    v_labels := coalesce(nullif(current_setting('rvbbit.brain_ner_labels', true), ''),
        'person, organization, location, place, product, service, event, date, money, amount, '
        'metric, policy, program, department, role, phone number, email, account, deadline, '
        'requirement, system, document');
    v_ner_cap := greatest(p_max_chunks,
        coalesce(nullif(current_setting('rvbbit.brain_ner_max_chunks', true), '')::int, 400));

    FOR ch IN SELECT chunk_id, text FROM rvbbit.brain_chunks
               WHERE doc_id = p_doc_id ORDER BY idx LIMIT greatest(1, v_ner_cap) LOOP
        IF nullif(btrim(ch.text), '') IS NULL THEN v_ci := v_ci + 1; CONTINUE; END IF;

        -- (1) Relation triples → first p_max_chunks chunks, unless skipped (GUC).
        IF v_ci < p_max_chunks AND NOT v_skip_triples THEN
            BEGIN v_tj := rvbbit.triples(ch.text, 'all'); EXCEPTION WHEN OTHERS THEN v_tj := '[]'::jsonb; END;
            IF jsonb_typeof(v_tj) = 'array' THEN
                FOR tr IN SELECT * FROM jsonb_to_recordset(v_tj)
                            AS x(subject text, predicate text, object text, evidence text,
                                 confidence double precision, subject_kind text, object_kind text) LOOP
                    CONTINUE WHEN nullif(btrim(tr.subject),'') IS NULL
                               OR nullif(btrim(tr.object),'') IS NULL
                               OR nullif(btrim(tr.predicate),'') IS NULL
                               OR btrim(tr.subject) ~ '(,[^,]+){3,}' OR btrim(tr.object) ~ '(,[^,]+){3,}'
                               OR rvbbit._brain_is_clause(tr.subject) OR rvbbit._brain_is_clause(tr.object);
                    v_subj_kind := coalesce(nullif(btrim(tr.subject_kind),''), 'entity');
                    v_obj_kind  := coalesce(nullif(btrim(tr.object_kind),''),  'entity');
                    IF lower(v_subj_kind) = 'document' THEN v_subj_kind := 'reference'; END IF;
                    IF lower(v_obj_kind)  = 'document' THEN v_obj_kind  := 'reference'; END IF;

                    PERFORM rvbbit.kg_assert_edge(v_subj_kind, tr.subject, tr.predicate, v_obj_kind, tr.object,
                                                  coalesce(tr.confidence, 0.9), '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
                    n_rel := n_rel + 1;
                    v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_subj_kind, tr.subject,
                                                        0.9, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
                    PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_chunks'::regclass,
                                ch.chunk_id::text, 'text', coalesce(nullif(tr.evidence,''), left(ch.text, 240)),
                                coalesce(tr.confidence, 0.9), '{}'::jsonb, NULL, g);
                    v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_obj_kind, tr.object,
                                                        0.9, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
                    PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_chunks'::regclass,
                                ch.chunk_id::text, 'text', coalesce(nullif(tr.evidence,''), left(ch.text, 240)),
                                coalesce(tr.confidence, 0.9), '{}'::jsonb, NULL, g);
                    n_men := n_men + 2;
                END LOOP;
            END IF;
        END IF;

        -- (2) NER → EVERY chunk (cheap) for comprehensive entity coverage.
        IF v_ner_on THEN
            BEGIN v_ner := rvbbit.extract_entities(ch.text, v_labels); EXCEPTION WHEN OTHERS THEN v_ner := '[]'::jsonb; END;
            IF jsonb_typeof(v_ner) = 'array' THEN
                FOR ent IN SELECT * FROM jsonb_to_recordset(v_ner) AS y(text text, label text) LOOP
                    CONTINUE WHEN nullif(btrim(ent.text),'') IS NULL
                               OR btrim(ent.text) ~ '(,[^,]+){3,}'
                               OR rvbbit._brain_is_clause(ent.text);
                    v_ekind := coalesce(nullif(btrim(ent.label),''), 'entity');
                    IF lower(v_ekind) = 'document' THEN v_ekind := 'reference'; END IF;
                    v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_ekind, ent.text,
                                                        0.85, '{}'::jsonb, jsonb_build_object('via', 'ner'), '', 0.0, g);
                    PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_chunks'::regclass,
                                ch.chunk_id::text, 'text', left(ch.text, 240), 0.85, '{}'::jsonb, NULL, g);
                    n_ner := n_ner + 1;
                END LOOP;
            END IF;
        END IF;
        v_ci := v_ci + 1;
    END LOOP;

    -- (3) STRUCTURED edges: provider edge_map × the doc's props (deterministic, high-confidence).
    IF v_props IS NOT NULL AND jsonb_typeof(v_edge_map) = 'array' AND jsonb_array_length(v_edge_map) > 0 THEN
        FOR es IN SELECT * FROM jsonb_to_recordset(v_edge_map) AS x(predicate text, kind text, path text) LOOP
            CONTINUE WHEN nullif(btrim(es.predicate),'') IS NULL OR nullif(btrim(es.path),'') IS NULL;
            BEGIN
                FOR v_obj IN SELECT jsonb_path_query(v_props, es.path::jsonpath) LOOP
                    v_lbl := btrim(v_obj #>> '{}');
                    CONTINUE WHEN nullif(v_lbl,'') IS NULL OR rvbbit._brain_is_junk_entity(v_lbl);
                    v_ekind := coalesce(nullif(btrim(es.kind),''), 'entity');
                    IF lower(v_ekind) = 'document' THEN v_ekind := 'reference'; END IF;
                    PERFORM rvbbit.kg_assert_edge('document', v_doclabel, es.predicate, v_ekind, v_lbl,
                                1.0, '{}'::jsonb, jsonb_build_object('via','structured'), '', 0.0, g);
                    v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_ekind, v_lbl,
                                1.0, '{}'::jsonb, jsonb_build_object('via','structured'), '', 0.0, g);
                    PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_documents'::regclass,
                                p_doc_id::text, 'props', es.predicate || ': ' || v_lbl, 1.0, '{}'::jsonb, NULL, g);
                    n_struct := n_struct + 1;
                END LOOP;
            EXCEPTION WHEN OTHERS THEN NULL;
            END;
        END LOOP;
    END IF;

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
    RETURN jsonb_build_object('doc_id', p_doc_id, 'relations', n_rel, 'mentions', n_men,
                              'ner_entities', n_ner, 'structured', n_struct, 'links', n_link,
                              'ner', v_ner_on, 'triples', NOT v_skip_triples);
END $fn$;

-- ── bulk-enrich every doc in a source (the "set") — triples-optional, auto-off for query sources ──
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_source(
    p_source_id bigint, p_force boolean DEFAULT false,
    p_max_chunks int DEFAULT 20, p_skip_triples boolean DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0; v_skip boolean;
BEGIN
    -- AUTO: skip the LLM triples for query/MCP sources (structured + NER are the value there).
    v_skip := coalesce(p_skip_triples,
        EXISTS (SELECT 1 FROM rvbbit.brain_sources
                WHERE source_id = p_source_id AND nullif(config->>'provider','') IS NOT NULL));
    PERFORM set_config('rvbbit.brain_skip_triples', v_skip::text, true);  -- local to this txn

    FOR rec IN SELECT doc_id FROM rvbbit.brain_documents
                WHERE source_id = p_source_id AND deleted_at IS NULL AND body IS NOT NULL
                  AND (p_force OR enriched_at IS NULL
                       OR enrich_hash IS DISTINCT FROM content_hash OR enriched_at < ingested_at)
                ORDER BY ingested_at DESC LOOP
        BEGIN
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN n_err := n_err + 1;
        END;
    END LOOP;

    PERFORM rvbbit.brain_refresh_node_norm();
    RETURN jsonb_build_object('source_id', p_source_id, 'enriched_docs', n_docs, 'errors', n_err,
                              'skip_triples', v_skip, 'forced', p_force);
END $fn$;
