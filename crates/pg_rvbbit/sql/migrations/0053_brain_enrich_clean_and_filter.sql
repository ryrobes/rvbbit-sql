-- 0053_brain_enrich_clean_and_filter — idempotent enrichment + span-length filter.
--
-- Agent feedback: (a) re-ingesting a doc creates new chunk_ids, orphaning the old kg_evidence, so a
-- doc that's re-ingested but not yet re-enriched shows empty chunk-scoped entities while its doc-level
-- mentions (and thus `shared` counts) persist — a confusing split. (b) GLiNER/triples occasionally
-- emit whole clauses as "entities" ("Accelerated Academy has received a signed contract…"), which
-- inflate `shared` and waste tokens. Fixes:
--   • brain_enrich_doc now CLEAN-REBUILDS: it first drops the doc's prior mentions/links edges (+ their
--     evidence, via cascade) so re-enrichment is idempotent — no duplicate evidence, no stale chunk refs.
--   • a span-length filter drops entities longer than 8 words / 64 chars (clauses, not entities) from
--     both the triples and NER paths, on top of the existing comma-mash filter.

-- A span that's a clause, not an entity: > 8 words or > 64 chars.
CREATE OR REPLACE FUNCTION rvbbit._brain_is_clause(p_text text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
    SELECT coalesce(
        array_length(regexp_split_to_array(btrim(p_text), '\s+'), 1) > 8
        OR length(btrim(p_text)) > 64, false);
$fn$;

-- Robust pending-detection: also re-enrich docs re-ingested since last enrich. Drag-dropped docs
-- have content_hash NULL, so the hash comparison never fires for them — enriched_at < ingested_at does.
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_pending(p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0;
BEGIN
    FOR rec IN SELECT doc_id FROM rvbbit.brain_documents
                WHERE deleted_at IS NULL AND body IS NOT NULL
                  AND (enriched_at IS NULL
                       OR enrich_hash IS DISTINCT FROM content_hash
                       OR enriched_at < ingested_at)
                ORDER BY ingested_at DESC LIMIT greatest(1, p_max_docs) LOOP
        BEGIN
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN n_err := n_err + 1;
        END;
    END LOOP;
    RETURN jsonb_build_object('enriched_docs', n_docs, 'errors', n_err);
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_doc(p_doc_id bigint, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    g           constant text := 'brain';
    v_doclabel  text;
    v_docnode   bigint;
    v_source_id bigint;
    v_body      text;
    v_hash      text;
    n_rel int := 0; n_men int := 0; n_link int := 0; n_ner int := 0;
    ch  record; tr record; v_tj jsonb; v_men_edge bigint;
    wl  text; v_target bigint; v_subj_kind text; v_obj_kind text;
    v_ner_on boolean;
    v_labels text;
    v_ner jsonb; ent record; v_ekind text;
BEGIN
    SELECT source_id, body, content_hash INTO v_source_id, v_body, v_hash
      FROM rvbbit.brain_documents WHERE doc_id = p_doc_id AND deleted_at IS NULL;
    IF NOT FOUND THEN RETURN jsonb_build_object('skipped', 'not found or deleted'); END IF;

    v_docnode := rvbbit.brain_doc_node(p_doc_id);
    v_doclabel := rvbbit.brain_doc_label(p_doc_id);

    -- Idempotent rebuild: drop this doc's prior mentions/links edges (evidence cascades) so a
    -- re-enrich starts clean — no piled-up evidence, no references to deleted chunk_ids.
    DELETE FROM rvbbit.kg_edges
     WHERE graph_id = g AND predicate_norm IN ('mentions', 'links_to') AND subject_node_id = v_docnode;

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

        -- (2) NER pass → comprehensive entity mentions (GLiNER specialist; modular, may be remote GPU).
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
    RETURN jsonb_build_object('doc_id', p_doc_id, 'relations', n_rel, 'mentions', n_men,
                              'ner_entities', n_ner, 'links', n_link, 'ner', v_ner_on);
END $fn$;
