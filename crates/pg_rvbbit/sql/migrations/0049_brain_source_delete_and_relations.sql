-- 0049_brain_source_delete_and_relations — source lifecycle + richer KG surfacing.
--
--   • brain_delete_source(id, purge_docs)  — remove a configured source. purge_docs=true wipes its
--       documents (cascade) + their brain-KG document nodes/edges + the synthetic folder roles it
--       created; purge_docs=false keeps the docs by reassigning them to a "<label> (archived)" manual
--       source (ACL preserved) and drops only the remote source + its sync artifacts.
--   • brain_doc_relations(email, doc_id)    — the TYPED relationships (not just "mentions") among the
--       entities a doc names — i.e. the edges, e.g. (Acme)-[acquired]->(Beta). ACL-gated like the rest.

CREATE OR REPLACE FUNCTION rvbbit.brain_delete_source(p_source_id bigint, p_purge_docs boolean DEFAULT true)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_label text; v_docs bigint; v_archived bigint;
BEGIN
    SELECT label INTO v_label FROM rvbbit.brain_sources WHERE source_id = p_source_id;
    IF v_label IS NULL THEN RAISE EXCEPTION 'brain_delete_source: source % not found', p_source_id; END IF;
    SELECT count(*) INTO v_docs FROM rvbbit.brain_documents WHERE source_id = p_source_id;

    IF p_purge_docs THEN
        -- remove this source's documents from the brain KG (doc nodes + their edges cascade)
        DELETE FROM rvbbit.kg_nodes
         WHERE graph_id = 'brain' AND kind = 'document' AND (properties->>'doc_id') IS NOT NULL
           AND (properties->>'doc_id')::bigint IN
               (SELECT doc_id FROM rvbbit.brain_documents WHERE source_id = p_source_id);
        -- drop the synthetic folder roles this source created (named sync/<source_id>/…)
        DELETE FROM rvbbit.brain_role_members WHERE role LIKE 'sync/' || p_source_id || '/%';
        DELETE FROM rvbbit.brain_roles WHERE origin = 'sync' AND role LIKE 'sync/' || p_source_id || '/%';
        -- delete the source → cascades documents/chunks/doc_roles/manifest/pending_grants
        DELETE FROM rvbbit.brain_sources WHERE source_id = p_source_id;
        RETURN jsonb_build_object('deleted_source', v_label, 'purged_docs', v_docs);
    ELSE
        -- keep the documents: reassign to a manual archive source (synthetic roles stay valid)
        v_archived := rvbbit.brain_define_source(v_label || ' (archived)', 'manual');
        UPDATE rvbbit.brain_documents SET source_id = v_archived WHERE source_id = p_source_id;
        DELETE FROM rvbbit.brain_sources WHERE source_id = p_source_id;  -- manifest/pending cascade
        RETURN jsonb_build_object('deleted_source', v_label, 'kept_docs', v_docs,
                                  'archived_as', v_label || ' (archived)');
    END IF;
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.brain_doc_relations(p_email text, p_doc_id bigint, p_max int DEFAULT 40)
RETURNS TABLE(subject_kind text, subject text, predicate text, object_kind text, object text, confidence double precision)
LANGUAGE sql STABLE AS $fn$
    WITH guard AS (SELECT 1 WHERE EXISTS (SELECT 1 FROM rvbbit.brain_visible_docs(p_email) v WHERE v.doc_id = p_doc_id)),
    dn AS (SELECT node_id FROM rvbbit.kg_nodes
            WHERE graph_id='brain' AND kind='document' AND label_norm = lower(rvbbit.brain_doc_label(p_doc_id))),
    ents AS (SELECT e.object_node_id AS ent FROM rvbbit.kg_edges e JOIN dn ON e.subject_node_id = dn.node_id
              WHERE e.graph_id='brain' AND e.predicate_norm = 'mentions')
    SELECT sn.kind, sn.label, e.predicate, ob.kind, ob.label, e.confidence
      FROM rvbbit.kg_edges e
      JOIN rvbbit.kg_nodes sn ON sn.node_id = e.subject_node_id
      JOIN rvbbit.kg_nodes ob ON ob.node_id = e.object_node_id, guard
     WHERE e.graph_id='brain' AND e.predicate_norm NOT IN ('mentions', 'links_to')
       AND (e.subject_node_id IN (SELECT ent FROM ents) OR e.object_node_id IN (SELECT ent FROM ents))
     ORDER BY e.confidence DESC NULLS LAST
     LIMIT greatest(1, p_max);
$fn$;
