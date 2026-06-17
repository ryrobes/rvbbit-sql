-- 0048_brain_kg_enrichment — Phase 2: understand documents, not just embed them.
--
-- The brain already chunks + embeds + ACL-filters (0031/0046/0047). This wires it into rvbbit's
-- existing knowledge-graph engine (kg_nodes/edges/evidence + kg_assert_*/kg_context) so a document
-- becomes a first-class node connected to the entities it mentions and the documents it relates to:
--
--   • doc node           kind='document' in graph 'brain' (kept separate from the catalog/data KGs)
--   • entity extraction   rvbbit.triples (LLM) over each chunk → typed entity nodes (person/org/…)
--                         + entity—relation—entity edges, with the source chunk as evidence
--   • doc → mentions →    edges from the document to every entity it names (the bipartite layer that
--                         makes "which other docs touch this entity" a graph walk, not a re-embed)
--   • [[wikilinks]]       parsed from the body → document → links_to → document edges (Obsidian style);
--                         unresolved targets become stub nodes (the KG tolerates dangling)
--   • cross-doc relations fall out for free: two docs that mention the same entity are 2 hops apart
--
-- Enrichment is LLM-heavy, so it's a SEPARATE, budgeted pass (brain_enrich_pending) — NOT inlined into
-- ingest. Re-enriches only when a doc's content_hash changes. All graph queries that surface other
-- documents route through brain_visible_docs(email) so a restricted doc's mentions never leak.

-- enrichment state (re-enrich on content change)
ALTER TABLE rvbbit.brain_documents ADD COLUMN IF NOT EXISTS enriched_at timestamptz;
ALTER TABLE rvbbit.brain_documents ADD COLUMN IF NOT EXISTS enrich_hash text;

-- deterministic, unique-per-doc node label (title is not unique; the doc_id disambiguates)
CREATE OR REPLACE FUNCTION rvbbit.brain_doc_label(p_doc_id bigint)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT format('%s #%s', coalesce(nullif(btrim(d.title), ''), d.uri, 'doc'), d.doc_id)
    FROM rvbbit.brain_documents d WHERE d.doc_id = p_doc_id;
$fn$;

-- ensure the document's KG node exists (graph 'brain'); exact-match only (no fuzzy doc merge)
CREATE OR REPLACE FUNCTION rvbbit.brain_doc_node(p_doc_id bigint)
RETURNS bigint LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_label text; v_src text; v_folder text;
BEGIN
    SELECT rvbbit.brain_doc_label(p_doc_id), s.label, d.folder_path
      INTO v_label, v_src, v_folder
      FROM rvbbit.brain_documents d JOIN rvbbit.brain_sources s ON s.source_id = d.source_id
     WHERE d.doc_id = p_doc_id;
    IF v_label IS NULL THEN RETURN NULL; END IF;
    RETURN rvbbit.kg_assert_node('document', v_label,
              jsonb_build_object('doc_id', p_doc_id, 'source', v_src, 'folder', v_folder),
              1.0, '', 0.0, 'brain');   -- match_threshold 0 → exact label, no embedding merge
END $fn$;

-- ── enrich ONE document into the brain KG ─────────────────────────────────────
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
        -- triples is an LLM call; isolate per-chunk failures so one bad chunk can't abort the doc.
        BEGIN v_tj := rvbbit.triples(ch.text, 'all'); EXCEPTION WHEN OTHERS THEN v_tj := '[]'::jsonb; END;
        IF jsonb_typeof(v_tj) <> 'array' THEN CONTINUE; END IF;

        FOR tr IN SELECT * FROM jsonb_to_recordset(v_tj)
                    AS x(subject text, predicate text, object text, evidence text,
                         confidence double precision, subject_kind text, object_kind text) LOOP
            CONTINUE WHEN nullif(btrim(tr.subject),'') IS NULL
                       OR nullif(btrim(tr.object),'') IS NULL
                       OR nullif(btrim(tr.predicate),'') IS NULL;
            -- Reserve the 'document' kind for real brain docs; an LLM sometimes types a
            -- referenced thing (e.g. "Bravo Memo") as 'document' — remap so extracted entities
            -- never masquerade as doc nodes (keeps brain_doc_graph's doc logic clean).
            v_subj_kind := coalesce(nullif(btrim(tr.subject_kind),''), 'entity');
            v_obj_kind  := coalesce(nullif(btrim(tr.object_kind),''),  'entity');
            IF lower(v_subj_kind) = 'document' THEN v_subj_kind := 'reference'; END IF;
            IF lower(v_obj_kind)  = 'document' THEN v_obj_kind  := 'reference'; END IF;

            -- entity —relation→ entity (auto-creates both entity nodes)
            PERFORM rvbbit.kg_assert_edge(v_subj_kind, tr.subject, tr.predicate, v_obj_kind, tr.object,
                                          coalesce(tr.confidence, 0.9), '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
            n_rel := n_rel + 1;

            -- document —mentions→ each entity, with the source chunk as evidence
            v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_subj_kind, tr.subject,
                                                0.9, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
            PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_chunks'::regclass,
                        ch.chunk_id::text, 'text', coalesce(nullif(tr.evidence,''), left(ch.text, 240)),
                        coalesce(tr.confidence, 0.9), '{}'::jsonb, NULL, g);
            PERFORM rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_obj_kind, tr.object,
                                          0.9, '{}'::jsonb, '{}'::jsonb, '', 0.0, g);
            n_men := n_men + 2;
        END LOOP;
    END LOOP;

    -- Obsidian-style [[wikilinks]] → document links_to document (resolve within the same source).
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

-- ── enrich the backlog: docs never enriched, or changed since last enrich ─────
CREATE OR REPLACE FUNCTION rvbbit.brain_enrich_pending(p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE rec record; n_docs int := 0; n_err int := 0;
BEGIN
    FOR rec IN SELECT doc_id FROM rvbbit.brain_documents
                WHERE deleted_at IS NULL AND body IS NOT NULL
                  AND (enriched_at IS NULL OR enrich_hash IS DISTINCT FROM content_hash)
                ORDER BY ingested_at DESC LIMIT greatest(1, p_max_docs) LOOP
        BEGIN
            PERFORM rvbbit.brain_enrich_doc(rec.doc_id, p_max_chunks);
            n_docs := n_docs + 1;
        EXCEPTION WHEN OTHERS THEN n_err := n_err + 1;
        END;
    END LOOP;
    RETURN jsonb_build_object('enriched_docs', n_docs, 'errors', n_err);
END $fn$;

-- ── ACL-aware "how does this doc relate to others" (for the UI / MCP) ─────────
-- Entities the (visible) doc mentions, plus OTHER documents the caller may see that share those
-- entities or are wikilinked. Restricted docs are filtered out → no leak via the graph.
CREATE OR REPLACE FUNCTION rvbbit.brain_doc_graph(p_email text, p_doc_id bigint, p_max_related int DEFAULT 20)
RETURNS TABLE(rel_type text, kind text, label text, doc_id bigint, weight int)
LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    ents AS (SELECT DISTINCT e.object_node_id AS ent
               FROM rvbbit.kg_edges e JOIN dn ON e.subject_node_id = dn.node_id
              WHERE e.graph_id='brain' AND e.predicate_norm='mentions'),
    related AS (
        SELECT (n2.properties->>'doc_id')::bigint AS rdoc, count(*)::int AS shared
          FROM rvbbit.kg_edges e2 JOIN ents ON e2.object_node_id = ents.ent
          JOIN rvbbit.kg_nodes n2 ON n2.node_id = e2.subject_node_id AND n2.kind='document'
         WHERE e2.graph_id='brain' AND e2.predicate_norm='mentions'
           AND (n2.properties->>'doc_id') IS NOT NULL
           AND (n2.properties->>'doc_id')::bigint <> p_doc_id
           AND (n2.properties->>'doc_id')::bigint IN (SELECT doc_id FROM rvbbit.brain_visible_docs(p_email))
         GROUP BY 1)
    SELECT 'entity'::text, ne.kind, ne.label, NULL::bigint, count(*)::int AS weight
      FROM ents JOIN rvbbit.kg_nodes ne ON ne.node_id = ents.ent, guard
     GROUP BY ne.kind, ne.label
    UNION ALL
    SELECT 'related_doc'::text, 'document', d.title, r.rdoc, r.shared
      FROM related r JOIN rvbbit.brain_documents d ON d.doc_id = r.rdoc, guard
     ORDER BY weight DESC
     LIMIT greatest(1, p_max_related);
$fn$;

-- ── nightly: sync remote sources, then enrich the backlog (one cron command) ──
CREATE OR REPLACE FUNCTION rvbbit.brain_nightly(p_max_docs int DEFAULT 25, p_max_chunks int DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_sync jsonb; v_enrich jsonb;
BEGIN
    v_sync   := rvbbit.brain_sync_sources('auto');
    v_enrich := rvbbit.brain_enrich_pending(p_max_docs, p_max_chunks);
    RETURN jsonb_build_object('sync', v_sync, 'enrich', v_enrich);
END $fn$;
