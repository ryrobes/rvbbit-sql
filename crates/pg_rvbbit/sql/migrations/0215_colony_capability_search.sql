-- 0215_colony_capability_search
--
-- Colony (docs/PEER_CAPABILITIES_PLAN.md §6 item 3): "extend capability_search()
-- to include peer-shared entries so 'what can answer an embedding question
-- right now' is answerable the same way any other capability question
-- already is." rvbbit.capability_search() is a thin wrapper over
-- rvbbit.data_search() over the rvbbit_capabilities KG (0147) — every kind
-- (cap_operator, cap_metric, cap_cube, cap_pack, cap_brain, cap_alert,
-- cap_mcp_tool) was added the same way: a new numbered block appended
-- inside capability_crawl()'s body, never a change to capability_search()
-- itself. This migration follows that exact precedent — block 9,
-- kind='cap_peer_backend' — plus wires register_peer_backend/
-- deregister_peer_backend/set_peer_backend_enabled to best-effort trigger a
-- re-crawl (same pattern as generate_mcp_operators, 0198), so a newly
-- shared or detached backend becomes searchable immediately.
--
-- Deliberately does NOT filter to enabled=true: a paused backend stays
-- discoverable (with its doc saying so) rather than silently vanishing,
-- since "this exists but isn't answering right now" is real information —
-- matching how cap_brain already marks a disabled source rather than
-- omitting it.

CREATE OR REPLACE FUNCTION rvbbit.capability_crawl(do_embed boolean DEFAULT true, embed_specialist text DEFAULT ''::text)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $crawl_fn$
DECLARE
    v_graph text := 'rvbbit_capabilities';
    v_embed_ok boolean := do_embed;
    v_node bigint;
    v_doc text;
    v_vec real[];
    v_sig text;
    v_cost text;
    v_n_seed int := 0; v_n_ops int := 0; v_n_metrics int := 0;
    v_n_cubes int := 0; v_emb int := 0;
    rec record;
BEGIN
    -- Warm the embedding cache in one batch call (per-doc embeds below become
    -- cache hits; decisive for remote embedders). Best-effort.
    IF v_embed_ok THEN
        BEGIN
            PERFORM rvbbit.embed_batch(docs, embed_specialist, 'document')
            FROM (
                SELECT array_agg(d) AS docs FROM (
                    SELECT rvbbit.capability_doc(kind, name, signature, description, example, cost_class, tags) AS d
                      FROM rvbbit.capability_seed
                    UNION ALL
                    SELECT rvbbit.capability_doc('cap_operator', o.name,
                        o.name || '(' || coalesce((SELECT string_agg(an || ' ' || at, ', ')
                                                     FROM unnest(o.arg_names, o.arg_types) AS z(an, at)), '')
                               || ') → ' || coalesce(o.return_type, 'text'),
                        coalesce(o.description, 'Installed semantic operator.'),
                        '', CASE WHEN coalesce(o.model, '') = '' THEN 'cheap' ELSE 'metered_llm' END,
                        ARRAY['operator', o.shape])
                      FROM rvbbit.operators o WHERE coalesce(o.visibility, 'public') <> 'kit'
                ) all_docs
            ) batched
            WHERE docs IS NOT NULL;
        EXCEPTION WHEN others THEN NULL;
        END;
    END IF;

    -- 1) Curated seed → syntax/doctrine nodes.
    FOR rec IN SELECT * FROM rvbbit.capability_seed LOOP
        v_doc := rvbbit.capability_doc(rec.kind, rec.name, rec.signature,
                                       rec.description, rec.example, rec.cost_class, rec.tags);
        v_node := rvbbit.kg_assert_node(rec.kind, rec.name,
                    jsonb_strip_nulls(jsonb_build_object(
                        'signature',  rec.signature,
                        'cost_class', rec.cost_class,
                        'tags',       to_jsonb(rec.tags),
                        'example',    rec.example,
                        'search_doc', v_doc)),
                    1.0, '', 0.0, v_graph);
        v_n_seed := v_n_seed + 1;
        v_vec := NULL;
        IF v_embed_ok THEN
            BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
            EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
        END IF;
        INSERT INTO rvbbit.catalog_docs
            (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
        VALUES (v_node, v_graph, rec.kind, NULL, rec.name, NULL, v_doc, v_vec,
                CASE WHEN v_vec IS NOT NULL THEN now() END, now())
        ON CONFLICT (graph_id, node_id) DO UPDATE SET
            kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
            embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
        IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
    END LOOP;

    -- 2) Installed operators (system AND user-created) → cap_operator nodes.
    FOR rec IN SELECT o.* FROM rvbbit.operators o WHERE coalesce(o.visibility, 'public') <> 'kit' LOOP
        v_sig := rec.name || '('
              || coalesce((SELECT string_agg(an || ' ' || at, ', ')
                             FROM unnest(rec.arg_names, rec.arg_types) AS z(an, at)), '')
              || ') → ' || coalesce(rec.return_type, 'text')
              || CASE WHEN coalesce(rec.infix_word, '') <> ''
                      THEN '  |  infix: <text> ' || rec.infix_word || ' <arg>' ELSE '' END;
        v_cost := CASE WHEN coalesce(rec.model, '') = '' THEN 'cheap' ELSE 'metered_llm' END;
        -- P2: OBSERVED costs from receipt history (30d, error-free) — the tool
        -- list audits itself. Two-sig-fig bucketing keeps nightly re-crawls
        -- from churning embeddings over noise.
        DECLARE
            v_calls bigint; v_avg_cost numeric; v_p50 numeric; v_observed text := '';
        BEGIN
            SELECT count(*),
                   avg(r.cost_usd),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms)
              INTO v_calls, v_avg_cost, v_p50
              FROM rvbbit.receipts r
             WHERE r.operator = rec.name
               AND r.invocation_at > now() - interval '30 days'
               AND r.error IS NULL;
            IF v_calls > 0 THEN
                v_observed := ' Observed (30d): ~' || rvbbit._cap_sig2(v_calls) || ' calls'
                    || coalesce(', ~$' || rvbbit._cap_sig2(v_avg_cost) || '/call', '')
                    || coalesce(', p50 ' || rvbbit._cap_sig2(v_p50) || 'ms', '') || '.';
            END IF;
            -- Infix operators get a synthesized usage example — retrieval works
            -- in the user's vocabulary, not just the signature's.
            v_doc := rvbbit.capability_doc('cap_operator', rec.name, v_sig,
                        coalesce(rec.description, 'Installed semantic operator.') || v_observed,
                        CASE WHEN coalesce(rec.infix_word, '') <> ''
                             THEN 'SELECT * FROM t WHERE text_column ' || rec.infix_word
                                  || ' ''<concept>''  -- rows whose text matches the concept by meaning'
                             ELSE '' END,
                        v_cost, ARRAY['operator', rec.shape]);
            v_node := rvbbit.kg_assert_node('cap_operator', rec.name,
                        jsonb_strip_nulls(jsonb_build_object(
                            'signature',  v_sig,
                            'model',      nullif(rec.model, ''),
                            'shape',      rec.shape,
                            'cost_class', v_cost,
                            'observed',   CASE WHEN v_calls > 0 THEN jsonb_build_object(
                                              'calls_30d', v_calls,
                                              'avg_cost_usd', v_avg_cost,
                                              'p50_ms', v_p50) END,
                            'search_doc', v_doc)),
                        1.0, '', 0.0, v_graph);
        END;
        v_n_ops := v_n_ops + 1;
        IF coalesce(rec.model, '') <> '' THEN
            PERFORM rvbbit.kg_assert_node('model', rec.model,
                        jsonb_build_object('name', rec.model), 1.0, '', 0.0, v_graph);
            PERFORM rvbbit.kg_assert_edge('cap_operator', rec.name, 'runs_on',
                        'model', rec.model, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, v_graph);
            -- provider/model convention: 'openai/gpt-…' → provider 'openai'.
            IF rec.model LIKE '%/%' THEN
                PERFORM rvbbit.kg_assert_node('provider', split_part(rec.model, '/', 1),
                            jsonb_build_object('name', split_part(rec.model, '/', 1)),
                            1.0, '', 0.0, v_graph);
                PERFORM rvbbit.kg_assert_edge('model', rec.model, 'served_by',
                            'provider', split_part(rec.model, '/', 1),
                            1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, v_graph);
            END IF;
        END IF;
        v_vec := NULL;
        IF v_embed_ok THEN
            BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
            EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
        END IF;
        INSERT INTO rvbbit.catalog_docs
            (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
        VALUES (v_node, v_graph, 'cap_operator', NULL, rec.name, NULL, v_doc, v_vec,
                CASE WHEN v_vec IS NOT NULL THEN now() END, now())
        ON CONFLICT (graph_id, node_id) DO UPDATE SET
            kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
            embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
        IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
    END LOOP;

    -- 3) Blessed metrics (latest version per name), with best-effort lineage:
    -- FROM/JOIN identifiers in the metric SQL become db_table_ref nodes so the
    -- explorer can walk metric → source table. (True cross-graph unification
    -- with db_catalog nodes is deferred — see plan open questions.)
    FOR rec IN SELECT DISTINCT ON (name) name, description, grain, params, sql
                 FROM rvbbit.metric_defs ORDER BY name, version DESC LOOP
        v_sig := 'SELECT rvbbit.metric(' || quote_literal(rec.name) || ')';
        v_doc := rvbbit.capability_doc('cap_metric', rec.name, v_sig,
                    coalesce(rec.description, 'Blessed metric.')
                    || CASE WHEN rec.grain IS NOT NULL THEN ' Grain: ' || rec.grain || '.' ELSE '' END
                    || ' Governed definition — prefer over hand-rolled aggregates.',
                    v_sig, 'free', ARRAY['metric','blessed','kpi']);
        v_node := rvbbit.kg_assert_node('cap_metric', rec.name,
                    jsonb_strip_nulls(jsonb_build_object(
                        'grain', rec.grain, 'params', rec.params, 'search_doc', v_doc)),
                    1.0, '', 0.0, v_graph);
        v_n_metrics := v_n_metrics + 1;
        DECLARE v_tbl text;
        BEGIN
            FOR v_tbl IN
                SELECT DISTINCT lower(m[1])
                  FROM regexp_matches(coalesce(rec.sql, ''),
                                      '(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_.]*)', 'gi') AS m
                 WHERE lower(m[1]) NOT IN ('select', 'lateral', 'unnest', 'generate_series')
            LOOP
                PERFORM rvbbit.kg_assert_node('db_table_ref', v_tbl,
                            jsonb_build_object('name', v_tbl), 1.0, '', 0.0, v_graph);
                PERFORM rvbbit.kg_assert_edge('cap_metric', rec.name, 'derived_from',
                            'db_table_ref', v_tbl, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, v_graph);
            END LOOP;
        END;
        v_vec := NULL;
        IF v_embed_ok THEN
            BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
            EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
        END IF;
        INSERT INTO rvbbit.catalog_docs
            (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
        VALUES (v_node, v_graph, 'cap_metric', NULL, rec.name, NULL, v_doc, v_vec,
                CASE WHEN v_vec IS NOT NULL THEN now() END, now())
        ON CONFLICT (graph_id, node_id) DO UPDATE SET
            kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
            embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
        IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
    END LOOP;

    -- 4) Cubes (latest version per name).
    FOR rec IN SELECT DISTINCT ON (name) name, description, grain
                 FROM rvbbit.cube_defs ORDER BY name, version DESC LOOP
        v_doc := rvbbit.capability_doc('cap_cube', rec.name,
                    'curated mart — query its materialized table',
                    coalesce(rec.description, 'Curated cube.')
                    || CASE WHEN rec.grain IS NOT NULL THEN ' Grain: ' || rec.grain || '.' ELSE '' END
                    || ' Prefer over re-deriving the same joins from raw tables.',
                    'SELECT * FROM ' || quote_ident(rec.name) || ' LIMIT 50',
                    'free', ARRAY['cube','mart','joined']);
        v_node := rvbbit.kg_assert_node('cap_cube', rec.name,
                    jsonb_strip_nulls(jsonb_build_object(
                        'grain', rec.grain, 'search_doc', v_doc)),
                    1.0, '', 0.0, v_graph);
        v_n_cubes := v_n_cubes + 1;
        v_vec := NULL;
        IF v_embed_ok THEN
            BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
            EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
        END IF;
        INSERT INTO rvbbit.catalog_docs
            (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
        VALUES (v_node, v_graph, 'cap_cube', NULL, rec.name, NULL, v_doc, v_vec,
                CASE WHEN v_vec IS NOT NULL THEN now() END, now())
        ON CONFLICT (graph_id, node_id) DO UPDATE SET
            kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
            embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
        IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
    END LOOP;

    -- 5) Capability packs — the ADJACENT POSSIBLE. install_state is honest:
    -- 'installed' when the pack's backend is registered or any of its
    -- operators exist here; 'available' means "you could do this if you
    -- install it" (agents may SUGGEST installs, never perform them).
    DECLARE
        v_state text;
        v_opname text;
        v_n_packs int := 0;
    BEGIN
        FOR rec IN SELECT c.name, c.title, c.description, c.kind AS pack_kind,
                          c.tags, c.operators, c.gpu_required, c.backend_name
                     FROM rvbbit.capability_catalog c
                    WHERE c.active LOOP
            v_state := CASE
                WHEN (rec.backend_name IS NOT NULL
                      AND EXISTS (SELECT 1 FROM rvbbit.backends b WHERE b.name = rec.backend_name))
                  OR EXISTS (SELECT 1 FROM rvbbit.operators o
                              WHERE o.name = ANY (coalesce(rec.operators, '{}')))
                THEN 'installed' ELSE 'available' END;
            v_doc := rvbbit.capability_doc('cap_pack', rec.name,
                        'capability pack (' || rec.pack_kind || ') — status: ' || v_state,
                        coalesce(rec.title, rec.name) || '. ' || coalesce(rec.description, '')
                        || CASE WHEN cardinality(coalesce(rec.operators, '{}')) > 0
                                THEN ' Provides operators: ' || array_to_string(rec.operators, ', ') || '.'
                                ELSE '' END
                        || CASE WHEN v_state = 'available'
                                THEN ' NOT currently installed — suggest installation to the user; never install autonomously.'
                                ELSE '' END,
                        '', CASE WHEN rec.gpu_required THEN 'gpu' ELSE 'varies' END,
                        coalesce(rec.tags, '{}') || ARRAY['pack', v_state]);
            v_node := rvbbit.kg_assert_node('cap_pack', rec.name,
                        jsonb_strip_nulls(jsonb_build_object(
                            'title', rec.title, 'pack_kind', rec.pack_kind,
                            'install_state', v_state, 'gpu_required', rec.gpu_required,
                            'search_doc', v_doc)),
                        1.0, '', 0.0, v_graph);
            v_n_packs := v_n_packs + 1;
            FOREACH v_opname IN ARRAY coalesce(rec.operators, '{}') LOOP
                IF EXISTS (SELECT 1 FROM rvbbit.operators o WHERE o.name = v_opname) THEN
                    PERFORM rvbbit.kg_assert_edge('cap_pack', rec.name, 'contains',
                                'cap_operator', v_opname, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, v_graph);
                END IF;
            END LOOP;
            v_vec := NULL;
            IF v_embed_ok THEN
                BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
                EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
            END IF;
            INSERT INTO rvbbit.catalog_docs
                (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
            VALUES (v_node, v_graph, 'cap_pack', NULL, rec.name, NULL, v_doc, v_vec,
                    CASE WHEN v_vec IS NOT NULL THEN now() END, now())
            ON CONFLICT (graph_id, node_id) DO UPDATE SET
                kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
                embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
            IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
        END LOOP;

        -- 6) Document brains (sources searchable via ask_brain).
        FOR rec IN SELECT s.source_id, s.label, s.kind AS src_kind, s.enabled,
                          (SELECT count(*) FROM rvbbit.brain_documents d
                            WHERE d.source_id = s.source_id) AS n_docs
                     FROM rvbbit.brain_sources s LOOP
            v_doc := rvbbit.capability_doc('cap_brain', rec.label,
                        'rvbbit.ask_brain(<identity email>, <question>) — corpus: ' || rec.label,
                        'Document brain source (' || rec.src_kind || ', '
                        || rec.n_docs || ' documents' || CASE WHEN rec.enabled THEN '' ELSE ', DISABLED' END
                        || '). Role-gated document Q&A — answers ground in documents the identity may see.',
                        '', 'metered_llm', ARRAY['brain','documents','rag']);
            v_node := rvbbit.kg_assert_node('cap_brain', rec.label,
                        jsonb_strip_nulls(jsonb_build_object(
                            'source_kind', rec.src_kind, 'n_docs', rec.n_docs,
                            'enabled', rec.enabled, 'search_doc', v_doc)),
                        1.0, '', 0.0, v_graph);
            v_vec := NULL;
            IF v_embed_ok THEN
                BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
                EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
            END IF;
            INSERT INTO rvbbit.catalog_docs
                (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
            VALUES (v_node, v_graph, 'cap_brain', NULL, rec.label, NULL, v_doc, v_vec,
                    CASE WHEN v_vec IS NOT NULL THEN now() END, now())
            ON CONFLICT (graph_id, node_id) DO UPDATE SET
                kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
                embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
            IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
        END LOOP;

        -- 7) Active alert rules (latest version per name).
        FOR rec IN SELECT DISTINCT ON (name) name, description,
                          condition_spec->>'kind' AS cond_kind, cardinality
                     FROM rvbbit.alert_rules ORDER BY name, version DESC LOOP
            v_doc := rvbbit.capability_doc('cap_alert', rec.name,
                        'alert rule — condition kind: ' || coalesce(rec.cond_kind, 'sql'),
                        coalesce(rec.description, 'Active alert rule.')
                        || ' Durable watch (edge-triggered, ' || rec.cardinality || ').',
                        '', 'cheap', ARRAY['alert','watch']);
            v_node := rvbbit.kg_assert_node('cap_alert', rec.name,
                        jsonb_strip_nulls(jsonb_build_object(
                            'condition_kind', rec.cond_kind, 'search_doc', v_doc)),
                        1.0, '', 0.0, v_graph);
            v_vec := NULL;
            IF v_embed_ok THEN
                BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
                EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
            END IF;
            INSERT INTO rvbbit.catalog_docs
                (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
            VALUES (v_node, v_graph, 'cap_alert', NULL, rec.name, NULL, v_doc, v_vec,
                    CASE WHEN v_vec IS NOT NULL THEN now() END, now())
            ON CONFLICT (graph_id, node_id) DO UPDATE SET
                kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
                embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
            IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
        END LOOP;

        -- 8) Warehouse MCP connector tools — the EXTERNAL-agent surface
        -- (Claude Desktop/Cowork/Code via the DW MCP connector). In the corpus
        -- even when the connector isn't used locally: they document what the
        -- warehouse can do, and most mirror rvbbit SQL reachable right here.
        DECLARE v_n_mcp int := 0;
        BEGIN
            FOR rec IN SELECT t.name, t.summary FROM rvbbit.capability_mcp_tools t LOOP
                v_doc := rvbbit.capability_doc('cap_mcp_tool', rec.name,
                            'Warehouse MCP connector tool (external agents)',
                            coalesce(nullif(rec.summary, ''), 'Warehouse MCP tool.')
                            || ' NOT a local SQL function — exposed to external agents via the Warehouse MCP connector. The same ability is usually reachable locally through the rvbbit SQL surface (search this graph for the SQL-side sibling).',
                            '', 'varies', ARRAY['mcp','external','warehouse-connector']);
                v_node := rvbbit.kg_assert_node('cap_mcp_tool', rec.name,
                            jsonb_strip_nulls(jsonb_build_object('search_doc', v_doc)),
                            1.0, '', 0.0, v_graph);
                v_n_mcp := v_n_mcp + 1;
                v_vec := NULL;
                IF v_embed_ok THEN
                    BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
                    EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
                END IF;
                INSERT INTO rvbbit.catalog_docs
                    (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
                VALUES (v_node, v_graph, 'cap_mcp_tool', NULL, rec.name, NULL, v_doc, v_vec,
                        CASE WHEN v_vec IS NOT NULL THEN now() END, now())
                ON CONFLICT (graph_id, node_id) DO UPDATE SET
                    kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
                    embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
                IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
            END LOOP;

            -- 9) Colony peer-shared backends (docs/PEER_CAPABILITIES_PLAN.md) — a
            -- DataRabbit client sharing a local LLM/ML model/MCP server, reachable
            -- via enqueue_peer_request/poll_peer_response while that client stays
            -- up. NOT filtered to enabled — a paused backend stays discoverable
            -- with its doc saying so, same doctrine as cap_brain's DISABLED marker.
            DECLARE v_n_peer int := 0;
            BEGIN
                FOR rec IN SELECT b.backend_name, b.kind AS peer_kind, b.template,
                                  b.scope_role, b.shared_by, b.description, b.enabled,
                                  coalesce(l.instance_count, 0) AS instance_count
                             FROM rvbbit.peer_backends b
                             LEFT JOIN rvbbit.peer_backends_live l USING (backend_name)
                LOOP
                    v_doc := rvbbit.capability_doc('cap_peer_backend', rec.backend_name,
                                'rvbbit.enqueue_peer_request(' || quote_literal(rec.backend_name)
                                || ', payload) + rvbbit.poll_peer_response(request_id)',
                                coalesce(rec.description, 'Colony peer-shared backend.')
                                || ' Kind: ' || rec.peer_kind
                                || CASE WHEN rec.template IS NOT NULL THEN ' (' || rec.template || ')' ELSE '' END
                                || '. Scope: ' || rec.scope_role || ', shared by ' || rec.shared_by || '.'
                                || CASE WHEN NOT rec.enabled THEN ' PAUSED by its sharer — not answering right now.'
                                        WHEN rec.instance_count > 0 THEN ' LIVE — ' || rec.instance_count || ' instance(s) online.'
                                        ELSE ' Currently OFFLINE — no live instance right now.' END,
                                '', 'varies', ARRAY['peer', 'colony', 'shared', rec.peer_kind]);
                    v_node := rvbbit.kg_assert_node('cap_peer_backend', rec.backend_name,
                                jsonb_strip_nulls(jsonb_build_object(
                                    'peer_kind', rec.peer_kind, 'template', rec.template,
                                    'scope_role', rec.scope_role, 'shared_by', rec.shared_by,
                                    'enabled', rec.enabled, 'live', rec.instance_count > 0,
                                    'search_doc', v_doc)),
                                1.0, '', 0.0, v_graph);
                    v_n_peer := v_n_peer + 1;
                    v_vec := NULL;
                    IF v_embed_ok THEN
                        BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
                        EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
                    END IF;
                    INSERT INTO rvbbit.catalog_docs
                        (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
                    VALUES (v_node, v_graph, 'cap_peer_backend', NULL, rec.backend_name, NULL, v_doc, v_vec,
                            CASE WHEN v_vec IS NOT NULL THEN now() END, now())
                    ON CONFLICT (graph_id, node_id) DO UPDATE SET
                        kind = EXCLUDED.kind, rel_name = EXCLUDED.rel_name, doc = EXCLUDED.doc,
                        embedding = EXCLUDED.embedding, embedded_at = EXCLUDED.embedded_at, updated_at = now();
                    IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;
                END LOOP;

                RETURN jsonb_build_object(
                    'graph', v_graph, 'seed', v_n_seed, 'operators', v_n_ops,
                    'metrics', v_n_metrics, 'cubes', v_n_cubes, 'packs', v_n_packs,
                    'mcp_tools', v_n_mcp, 'peer_backends', v_n_peer,
                    'docs_embedded', v_emb, 'embedder_ok', v_embed_ok);
            END;
        END;
    END;
END
$crawl_fn$;

-- Best-effort re-crawl on every lifecycle transition (register/deregister/
-- pause/resume), same pattern as generate_mcp_operators (0198) — a shared
-- or detached backend becomes capability_search-able immediately rather
-- than waiting on the next full re-crawl.

CREATE OR REPLACE FUNCTION rvbbit.register_peer_backend(p_backend_name text, p_kind text, p_scope_role text, p_template text DEFAULT NULL::text, p_description text DEFAULT NULL::text, p_model_digest text DEFAULT NULL::text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $register_fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_scope_role) THEN
        RAISE EXCEPTION 'register_peer_backend: role % does not exist', p_scope_role;
    END IF;
    INSERT INTO rvbbit.peer_backends (backend_name, kind, template, model_digest, scope_role, shared_by, description)
    VALUES (p_backend_name, p_kind, p_template, p_model_digest, p_scope_role, session_user, p_description)
    ON CONFLICT (backend_name) DO UPDATE
       SET kind = EXCLUDED.kind, template = EXCLUDED.template, model_digest = EXCLUDED.model_digest,
           scope_role = EXCLUDED.scope_role, description = EXCLUDED.description;
    BEGIN
        PERFORM rvbbit.capability_crawl();
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'register_peer_backend: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
    END;
END;
$register_fn$;

CREATE OR REPLACE FUNCTION rvbbit.deregister_peer_backend(p_backend_name text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $deregister_fn$
DECLARE
    v_ids uuid[];
BEGIN
    WITH updated AS (
        UPDATE rvbbit.peer_capability_requests
           SET status = 'failed'
         WHERE backend_name = p_backend_name AND status IN ('pending', 'claimed')
         RETURNING request_id
    )
    SELECT array_agg(request_id) INTO v_ids FROM updated;

    IF v_ids IS NOT NULL THEN
        INSERT INTO rvbbit.peer_capability_responses (request_id, response, error)
        SELECT unnest(v_ids), NULL, 'peer backend was detached by its sharer'
        ON CONFLICT (request_id) DO UPDATE SET error = EXCLUDED.error, completed_at = now();
    END IF;

    DELETE FROM rvbbit.peer_backend_presence WHERE backend_name = p_backend_name;
    DELETE FROM rvbbit.peer_backends WHERE backend_name = p_backend_name;
    BEGIN
        PERFORM rvbbit.capability_crawl();
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'deregister_peer_backend: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
    END;
END;
$deregister_fn$;

CREATE OR REPLACE FUNCTION rvbbit.set_peer_backend_enabled(p_backend_name text, p_enabled boolean)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $enable_fn$
BEGIN
    UPDATE rvbbit.peer_backends SET enabled = p_enabled WHERE backend_name = p_backend_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'set_peer_backend_enabled: no such peer backend %', p_backend_name;
    END IF;
    BEGIN
        PERFORM rvbbit.capability_crawl();
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'set_peer_backend_enabled: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
    END;
END;
$enable_fn$;
