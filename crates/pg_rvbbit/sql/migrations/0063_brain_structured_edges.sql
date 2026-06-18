-- 0063_brain_structured_edges — Phase 2: deterministic KG edges from query-source structured fields.
--
-- Phase 1 made MCP artifacts (Linear/JIRA issues) first-class docs (embed + LLM/NER enrichment). But a
-- ticket carries a GRAPH the body text doesn't: assignee, project, team, parent, labels, cycle. Relying
-- on the LLM triple-extractor to recover that is wasteful and lossy. Instead: a provider declares an
-- EDGE MAP — a list of {predicate, kind, path} where `path` is a JSONPath into the artifact's structured
-- fields. brain_enrich_doc walks it over the doc's stored `props` and asserts deterministic, high-
-- confidence edges:
--   issue --in_project--> project        issue --assigned_to--> person
--   issue --in_team--> team              issue --has_label--> label (one per array elem)
-- Each also gets a parallel `mentions` edge, so two issues in the same project / with the same assignee
-- become related via the existing shared-entity machinery (brain_related) — the org graph emerges, and
-- it cross-links with the file corpus too (a ticket and an SOP sharing "Florida" already relate).
--
-- `props` (the raw artifact JSON) is captured at sync time and stored on the doc, so edges re-derive on
-- re-enrich without re-fetching. ACL is unchanged (these docs are global per Phase 1).

-- ── schema ────────────────────────────────────────────────────────────────────
ALTER TABLE rvbbit.brain_documents     ADD COLUMN IF NOT EXISTS props jsonb;   -- structured fields (query sources)
ALTER TABLE rvbbit.brain_sync_manifest ADD COLUMN IF NOT EXISTS props jsonb;   -- carry props through sync
ALTER TABLE rvbbit.brain_doc_providers ADD COLUMN IF NOT EXISTS edge_map jsonb NOT NULL DEFAULT '[]'::jsonb;

-- ── provider definition now carries the edge map ──────────────────────────────
-- Drop the 6-arg form first (adding a 7th defaulted arg would make 3-arg calls ambiguous).
DROP FUNCTION IF EXISTS rvbbit.brain_define_provider(text, text, text, text, text, text);
CREATE OR REPLACE FUNCTION rvbbit.brain_define_provider(
    p_provider text, p_label text, p_list_sql text,
    p_item_sql text DEFAULT NULL, p_icon text DEFAULT NULL, p_description text DEFAULT NULL,
    p_edge_map jsonb DEFAULT '[]'::jsonb
) RETURNS text LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.brain_doc_providers (provider, label, list_sql, item_sql, icon, description, edge_map)
    VALUES (p_provider, p_label, p_list_sql, nullif(btrim(p_item_sql),''), p_icon, p_description,
            coalesce(p_edge_map, '[]'::jsonb))
    ON CONFLICT (provider) DO UPDATE SET
        label = excluded.label, list_sql = excluded.list_sql, item_sql = excluded.item_sql,
        icon = excluded.icon, description = excluded.description, edge_map = excluded.edge_map,
        updated_at = now()
    RETURNING provider;
$fn$;

-- ── sync: capture props (to_jsonb wrapper handles any/optional provider columns) ──
CREATE OR REPLACE FUNCTION rvbbit.brain_sync_query_source(p_source_id bigint, p_trigger text DEFAULT 'manual')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_provider text; v_list text; v_item text; v_two boolean;
    v_config jsonb; v_role text;
    rec record; j jsonb; ij jsonb; v_uri text;
    v_fetched int := 0; v_apply jsonb;
BEGIN
    SELECT nullif(s.config->>'provider',''), coalesce(s.config,'{}'::jsonb)
      INTO v_provider, v_config
      FROM rvbbit.brain_sources s WHERE s.source_id = p_source_id;
    IF v_provider IS NULL THEN
        RAISE EXCEPTION 'brain_sync_query_source: source % has no provider (not a query source)', p_source_id;
    END IF;
    SELECT list_sql, item_sql INTO v_list, v_item FROM rvbbit.brain_doc_providers WHERE provider = v_provider;
    IF v_list IS NULL THEN
        RAISE EXCEPTION 'brain_sync_query_source: provider "%" not defined (source %)', v_provider, p_source_id;
    END IF;
    v_two := nullif(btrim(coalesce(v_item,'')),'') IS NOT NULL;

    -- (1) Replace the manifest from the provider's list query. to_jsonb(q) captures whatever columns the
    -- query returns (uri/title/content_hash/occurred_at, body in single-phase, optional props) so the
    -- contract stays flexible. $1 (= source config) is always bound for the inner SQL to use if it wants.
    DELETE FROM rvbbit.brain_sync_manifest WHERE source_id = p_source_id;
    FOR rec IN EXECUTE
        format('SELECT to_jsonb(q) AS j FROM (%s) q WHERE ($1 IS NOT NULL OR $1 IS NULL)', v_list)
        USING v_config
    LOOP
        j := rec.j;
        v_uri := j->>'uri';
        CONTINUE WHEN nullif(btrim(coalesce(v_uri,'')),'') IS NULL;
        INSERT INTO rvbbit.brain_sync_manifest
            (source_id, uri, title, rel_path, folder_id, mime, modified_at, content_hash, permissions, staged_path, body, props)
        VALUES (p_source_id, v_uri, j->>'title', '/', NULL, 'text/markdown',
                nullif(j->>'occurred_at','')::timestamptz,
                CASE WHEN v_two THEN j->>'content_hash'
                     ELSE coalesce(j->>'content_hash', md5(coalesce(j->>'body',''))) END,
                '{}', NULL,
                CASE WHEN v_two THEN NULL ELSE j->>'body' END,
                j->'props')
        ON CONFLICT (source_id, uri) DO UPDATE SET
            title = excluded.title, modified_at = excluded.modified_at,
            content_hash = excluded.content_hash, body = excluded.body, props = excluded.props;
    END LOOP;

    -- (2) Two-phase: fetch body/props for NEW/CHANGED uris only ($1 = uri).
    IF v_two THEN
        FOR rec IN
            SELECT m.uri FROM rvbbit.brain_sync_manifest m
             WHERE m.source_id = p_source_id
               AND ( m.content_hash IS NULL
                  OR NOT EXISTS (SELECT 1 FROM rvbbit.brain_documents d
                                 WHERE d.source_id = p_source_id AND d.uri = m.uri AND d.deleted_at IS NULL
                                   AND d.content_hash IS NOT DISTINCT FROM m.content_hash) )
        LOOP
            BEGIN
                EXECUTE format('SELECT to_jsonb(q) AS j FROM (%s) q', v_item) INTO ij USING rec.uri;
            EXCEPTION WHEN OTHERS THEN ij := NULL; END;
            UPDATE rvbbit.brain_sync_manifest m
               SET body = ij->>'body',
                   title = coalesce(nullif(btrim(ij->>'title'),''), m.title),
                   modified_at = coalesce(nullif(ij->>'occurred_at','')::timestamptz, m.modified_at),
                   props = coalesce(ij->'props', m.props)
             WHERE m.source_id = p_source_id AND m.uri = rec.uri;
            IF (ij->>'body') IS NOT NULL THEN v_fetched := v_fetched + 1; END IF;
        END LOOP;
    END IF;

    -- (3) Global visibility role.
    v_role := rvbbit.brain_folder_role(p_source_id, NULL);
    INSERT INTO rvbbit.brain_roles (role, origin, is_public, label)
    VALUES (v_role, 'sync', true, 'auto: query source (global)')
    ON CONFLICT (role) DO UPDATE SET is_public = true, origin = 'sync';

    -- (4) Reconcile, then copy props onto the docs (apply_manifest is shared file-source code).
    v_apply := rvbbit.brain_sync_apply_manifest(p_source_id, coalesce(p_trigger, 'manual'));
    UPDATE rvbbit.brain_roles SET is_public = true WHERE role = v_role;
    UPDATE rvbbit.brain_documents d
       SET props = m.props
      FROM rvbbit.brain_sync_manifest m
     WHERE m.source_id = p_source_id AND d.source_id = p_source_id AND d.uri = m.uri
       AND d.props IS DISTINCT FROM m.props;
    UPDATE rvbbit.brain_sources SET kind = 'query', last_synced_at = now() WHERE source_id = p_source_id;

    RETURN coalesce(v_apply, '{}'::jsonb)
           || jsonb_build_object('provider', v_provider, 'two_phase', v_two, 'fetched', v_fetched);
END $fn$;

-- ── enrich: triples + NER + wikilinks + STRUCTURED edges from props × edge_map ──
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
BEGIN
    SELECT source_id, body, content_hash INTO v_source_id, v_body, v_hash
      FROM rvbbit.brain_documents WHERE doc_id = p_doc_id AND deleted_at IS NULL;
    IF NOT FOUND THEN RETURN jsonb_build_object('skipped', 'not found or deleted'); END IF;

    -- structured-edge inputs: the doc's props + its provider's edge map (NULL for non-query sources)
    SELECT bd.props, bdp.edge_map INTO v_props, v_edge_map
      FROM rvbbit.brain_documents bd
      LEFT JOIN rvbbit.brain_sources bs ON bs.source_id = bd.source_id
      LEFT JOIN rvbbit.brain_doc_providers bdp ON bdp.provider = bs.config->>'provider'
     WHERE bd.doc_id = p_doc_id;

    v_docnode := rvbbit.brain_doc_node(p_doc_id);
    v_doclabel := rvbbit.brain_doc_label(p_doc_id);
    -- clean re-derivable edges from this doc: mentions, links_to, and structured (typed) edges
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

        IF v_ci < p_max_chunks THEN
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
                    -- typed semantic edge (issue --assigned_to--> person, …)
                    PERFORM rvbbit.kg_assert_edge('document', v_doclabel, es.predicate, v_ekind, v_lbl,
                                1.0, '{}'::jsonb, jsonb_build_object('via','structured'), '', 0.0, g);
                    -- parallel mentions edge so the structured entity drives relatedness/overlap
                    v_men_edge := rvbbit.kg_assert_edge('document', v_doclabel, 'mentions', v_ekind, v_lbl,
                                1.0, '{}'::jsonb, jsonb_build_object('via','structured'), '', 0.0, g);
                    PERFORM rvbbit.kg_link_evidence(v_men_edge, NULL, 'rvbbit.brain_documents'::regclass,
                                p_doc_id::text, 'props', es.predicate || ': ' || v_lbl, 1.0, '{}'::jsonb, NULL, g);
                    n_struct := n_struct + 1;
                END LOOP;
            EXCEPTION WHEN OTHERS THEN NULL;  -- a bad jsonpath in one spec must not abort the doc
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
                              'ner_entities', n_ner, 'structured', n_struct, 'links', n_link, 'ner', v_ner_on);
END $fn$;

-- ── seed a Linear provider (template; inert until a source binds it + the linear MCP is registered) ──
-- linear_getIssues only returns the most-recent N (no pagination, crashes at high limits), so this
-- fans out across all projects via getProjectIssues for COMPLETE coverage. getProjectIssues omits the
-- `project` field (you queried by it), so the project is injected back into `props` from the outer row
-- for the in_project edge. (Caveat: issues with no project are not captured by a by-project fan-out.)
SELECT rvbbit.brain_define_provider(
    'linear-issues', 'Linear Issues',
    $list$
    SELECT 'linear:' || (r->>'id')                                  AS uri,
           concat_ws(' · ', r->>'identifier', r->>'title')          AS title,
           (r->>'updatedAt')                                        AS content_hash,
           (r->>'updatedAt')::timestamptz                           AS occurred_at,
           concat_ws(E'\n\n', r->>'title', r->>'description',
                     nullif('Status: '   || (r#>>'{state,name}'),    'Status: '),
                     nullif('Project: '  || (p->>'name'),            'Project: '),
                     nullif('Team: '      || (r#>>'{team,name}'),     'Team: '),
                     nullif('Assignee: '  || (r#>>'{assignee,name}'), 'Assignee: ')) AS body,
           r || jsonb_build_object('project', jsonb_build_object('id', p->>'id', 'name', p->>'name')) AS props
      FROM rvbbit.mcp_rows('linear', 'linear_getProjects', '{}'::jsonb) p
      CROSS JOIN LATERAL rvbbit.mcp_rows('linear', 'linear_getProjectIssues',
          jsonb_build_object('projectId', p->>'id', 'limit', 250)) r
    $list$,
    NULL, 'ticket', 'Linear issues, fanned out across all projects (complete coverage).',
    '[{"predicate":"in_team","kind":"team","path":"$.team.name"},
      {"predicate":"in_project","kind":"project","path":"$.project.name"},
      {"predicate":"assigned_to","kind":"person","path":"$.assignee.name"},
      {"predicate":"has_label","kind":"label","path":"$.labels[*].name"},
      {"predicate":"child_of","kind":"reference","path":"$.parent.title"},
      {"predicate":"in_cycle","kind":"cycle","path":"$.cycle.name"}]'::jsonb
);
