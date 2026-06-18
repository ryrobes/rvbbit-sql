-- 0052_brain_ner_pass — comprehensive per-chunk entity coverage via a GLiNER NER specialist.
--
-- Relation-triples (0048/0051) only surface entities that participate in a relationship, so bare
-- mentions ("Florida") and triple-less chunks get no entities. This adds a dedicated NER pass that
-- calls rvbbit.extract_entities(text, labels) — a FIRST-CLASS rvbbit operator backed by the GLiNER
-- warren specialist (capability extract/gliner-medium-v2.1). It's location-transparent: the operator
-- dispatches to the `extract_gliner` backend wherever it lives — a local CPU sidecar OR a remote GPU
-- warren node — so installing the capability anywhere just makes this call work. No hardcoded endpoint.
--
-- Guarded: if the GLiNER capability isn't installed, the pass is skipped and enrichment is triples-only
-- (today's behavior). Labels are GUC-tunable: SET rvbbit.brain_ner_labels = 'person, org, …'.
-- The extracted entities become `mentions` edges WITH chunk evidence, so brain_search's chunk-scoped
-- entities become comprehensive (GLiNER coverage) on top of the typed relations (triples).

CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_doc(p_doc_id bigint, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    g           constant text := 'brain';
    v_doclabel  text;
    v_source_id bigint;
    v_body      text;
    v_hash      text;
    n_rel int := 0; n_men int := 0; n_link int := 0; n_ner int := 0;
    ch  record; tr record; v_tj jsonb; v_men_edge bigint;
    wl  text; v_target bigint; v_subj_kind text; v_obj_kind text;
    -- NER pass
    v_ner_on boolean;
    v_labels text;
    v_ner jsonb; ent record; v_ekind text;
BEGIN
    SELECT source_id, body, content_hash INTO v_source_id, v_body, v_hash
      FROM rvbbit.brain_documents WHERE doc_id = p_doc_id AND deleted_at IS NULL;
    IF NOT FOUND THEN RETURN jsonb_build_object('skipped', 'not found or deleted'); END IF;

    PERFORM rvbbit.brain_doc_node(p_doc_id);
    v_doclabel := rvbbit.brain_doc_label(p_doc_id);

    -- Is the GLiNER NER operator available (capability installed, local or remote)? Checked once.
    v_ner_on := EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
                        WHERE ns.nspname = 'rvbbit' AND p.proname = 'extract_entities');
    v_labels := coalesce(nullif(current_setting('rvbbit.brain_ner_labels', true), ''),
        'person, organization, location, place, product, service, event, date, money, amount, '
        'metric, policy, program, department, role, phone number, email, account, deadline, '
        'requirement, system, document');

    FOR ch IN SELECT chunk_id, text FROM rvbbit.brain_chunks
               WHERE doc_id = p_doc_id ORDER BY idx LIMIT greatest(1, p_max_chunks) LOOP
        IF nullif(btrim(ch.text), '') IS NULL THEN CONTINUE; END IF;

        -- (1) Relation triples → typed entity—relation—entity edges + doc mentions (LLM).
        BEGIN v_tj := rvbbit.triples(ch.text, 'all'); EXCEPTION WHEN OTHERS THEN v_tj := '[]'::jsonb; END;
        IF jsonb_typeof(v_tj) = 'array' THEN
            FOR tr IN SELECT * FROM jsonb_to_recordset(v_tj)
                        AS x(subject text, predicate text, object text, evidence text,
                             confidence double precision, subject_kind text, object_kind text) LOOP
                CONTINUE WHEN nullif(btrim(tr.subject),'') IS NULL
                           OR nullif(btrim(tr.object),'') IS NULL
                           OR nullif(btrim(tr.predicate),'') IS NULL
                           OR btrim(tr.subject) ~ '(,[^,]+){3,}'
                           OR btrim(tr.object)  ~ '(,[^,]+){3,}';
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

        -- (2) NER pass → comprehensive entity mentions (GLiNER specialist; modular, may be remote GPU).
        IF v_ner_on THEN
            BEGIN v_ner := rvbbit.extract_entities(ch.text, v_labels); EXCEPTION WHEN OTHERS THEN v_ner := '[]'::jsonb; END;
            IF jsonb_typeof(v_ner) = 'array' THEN
                FOR ent IN SELECT * FROM jsonb_to_recordset(v_ner) AS y(text text, label text) LOOP
                    CONTINUE WHEN nullif(btrim(ent.text),'') IS NULL OR btrim(ent.text) ~ '(,[^,]+){3,}';
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
    END LOOP;

    -- (3) Obsidian-style [[wikilinks]] → document links_to document.
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
                              'ner_entities', n_ner, 'links', n_link, 'ner', v_ner_on);
END $fn$;
