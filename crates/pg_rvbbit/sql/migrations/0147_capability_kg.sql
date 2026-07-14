-- 0147_capability_kg.sql
-- Capability KG: the system that knows what it can do. A named graph
-- (rvbbit_capabilities) over the EXISTING KG + catalog_docs + data_search
-- machinery, pointed at VERBS instead of NOUNS: semantic SQL syntax, installed
-- operators (system AND user-created), blessed metrics, cubes. One agent-facing
-- entry point — rvbbit.capability_search('how do I ...') — turns
-- if-you-know-you-know features into JIT, problem-shaped lookup with no prompt
-- bloat. Design: docs/CAPABILITY_KG_PLAN.md.

-- ── The curated seed: language features no table can derive ────────────────
-- Descriptions are documentation-as-data: written once, inherited by every
-- agent surface. Every signature here is verified against the live surface.

CREATE TABLE IF NOT EXISTS rvbbit.capability_seed (
    name        text PRIMARY KEY,
    kind        text NOT NULL DEFAULT 'cap_syntax',
    signature   text NOT NULL,
    description text NOT NULL,
    example     text NOT NULL,
    cost_class  text NOT NULL DEFAULT 'free',
    tags        text[] NOT NULL DEFAULT '{}'
);

INSERT INTO rvbbit.capability_seed (name, kind, signature, description, example, cost_class, tags) VALUES
('semantic_sql_operators', 'cap_syntax',
 '<text_expr> <operator> ''<concept>''  |  rvbbit.<operator>(text, args...)',
 'Semantic SQL: LLM-backed operators usable inline in any query — filter, classify, extract, or score text BY MEANING instead of pattern. Use when asked to find or label rows by how their text READS: sounds angry, looks like spam, seems like a hoax, mentions a competitor, reads sarcastic, describes an injury. Installed operators include means (semantic boolean filter), about (topical relevance), classify (label from a comma-separated label list), extract, sentiment, entities, condense, contradicts, entails and more — see cap_operator entries for each installed instance and its exact signature. They call a model PER ROW: filter or LIMIT the row set first, and project cost with explain_semantic before broad application.',
 'SELECT title FROM reports WHERE observed means ''describes a probable hoax'' LIMIT 200',
 'metered_llm', ARRAY['semantic','llm','text','filter','classify']),

('explain_semantic', 'cap_syntax',
 'rvbbit.explain_semantic(query text) → setof lines  |  rvbbit.explain_semantic_analyze(query text)',
 'Project a query''s semantic execution graph WITHOUT running it: which operators fire, how many external model calls, and the projected dollar cost sketched from receipt history and the model-rates table. The _analyze variant executes the query once and reports measured calls, tokens, latency, and actual cost. Use before any broad semantic-operator query; when receipt history is cold (estimate $0 on obviously-nonzero work), measure a small LIMIT sample instead and extrapolate.',
 'SELECT * FROM rvbbit.explain_semantic($q$ SELECT count(*) FROM reviews WHERE body means ''angry customer'' $q$)',
 'free', ARRAY['cost','budget','explain','estimate']),

('time_travel_as_of', 'cap_syntax',
 'SELECT ... FROM <table> AS OF TIMESTAMP ''<ts>''  |  AS OF GENERATION <n>  |  SET rvbbit.as_of_generation',
 'Time travel on rvbbit-accelerated tables: query any table as it existed at a past timestamp or generation. Compare present vs past by joining a table to itself AS OF an earlier point. The rvbbit.as_of_generation GUC pins a whole session/transaction. Generations advance on compaction; rvbbit.generations lists them per table.',
 'SELECT now.state, now.n - past.n AS delta FROM (SELECT state, count(*) n FROM sightings GROUP BY 1) now JOIN (SELECT state, count(*) n FROM sightings AS OF TIMESTAMP ''2026-07-01'' GROUP BY 1) past USING (state)',
 'free', ARRAY['history','time travel','snapshot','versioning','yesterday']),

('metrics_layer', 'cap_syntax',
 'rvbbit.metric(p_name, p_params jsonb, p_def_as_of, p_data_as_of) → scalar  |  rvbbit.metric_defs catalog',
 'The blessed metrics layer: versioned, governed metric definitions in rvbbit.metric_defs (name, sql, params, grain, description). rvbbit.metric(''name'') evaluates the CANONICAL definition — bitemporal: def_as_of picks the definition version, data_as_of time-travels the data. DOCTRINE: when a blessed metric covers the ask, use it instead of hand-rolling an aggregate, and say so — your number then agrees with the company''s number.',
 'SELECT rvbbit.metric(''revenue'', ''{"region":"EU"}''::jsonb)',
 'free', ARRAY['metrics','kpi','governed','revenue','blessed']),

('define_metric', 'cap_syntax',
 'rvbbit.define_metric(p_name, p_sql, p_params jsonb, p_grain, p_description, p_owner, p_labels, p_check)',
 'Bless a query as a governed, versioned metric. The SQL becomes the canonical definition (new version per redefinition — history preserved, bitemporally queryable). Use when the user says a number is "the" number: promote the ad-hoc aggregate into the metric catalog.',
 'SELECT rvbbit.define_metric(''wa_sightings'', ''SELECT count(*) FROM sightings WHERE state=''''Washington'''''', ''{}''::jsonb, NULL, ''Blessed count of WA sightings'')',
 'free', ARRAY['metrics','governance','bless','promote']),

('cubes_layer', 'cap_syntax',
 'rvbbit.cube_defs catalog — curated wide-join marts materialized as accelerated tables',
 'Cubes: curated multi-table joins materialized as rvbbit-accelerated tables, refreshed on policy. When a cube already covers the entities the question needs, query the cube instead of re-deriving the joins from raw tables — faster and canonical. rvbbit.cube_defs lists name, sql, grain, description.',
 'SELECT name, grain, description FROM rvbbit.cube_defs ORDER BY created_at DESC',
 'free', ARRAY['cubes','marts','joins','curated']),

('data_search', 'cap_syntax',
 'rvbbit.data_search(query text, k int, kinds text[], graph text) → ranked tables/columns',
 'Semantic search over the database catalog itself: find tables and columns BY MEANING, not name — each hit carries a fingerprint doc with types, example values, row counts, and freshness. Use before assuming a table/column name or grepping information_schema. Default graph db_catalog covers the connected database.',
 'SELECT * FROM rvbbit.data_search(''customer contact info'', 10)',
 'cheap', ARRAY['discovery','schema','catalog','find table']),

('ask_brain', 'cap_syntax',
 'rvbbit.ask_brain(p_email text, p_query text, p_k int, p_filter jsonb) → grounded answer over documents',
 'Role-gated question answering over ingested document corpora (brains): contracts, wikis, PDFs, docs. Use when the question is about DOCUMENTS or institutional knowledge rather than tables. Access control is enforced by role — answers only draw on documents the identity may see.',
 'SELECT rvbbit.ask_brain(''user@example.com'', ''what does our refund policy say about digital goods?'')',
 'metered_llm', ARRAY['documents','knowledge','rag','policy','brain']),

('knn_embed', 'cap_syntax',
 'rvbbit.embed(text[, specialist, mode]) → real[]  |  rvbbit.knn(reloid, query real[], k)',
 'Vector similarity primitives: embed text (cached), then k-nearest-neighbor search over a table''s embedding column. Backs semantic dedupe, similar-item lookup, and custom retrieval when the higher-level surfaces (data_search, ask_brain) don''t fit.',
 'SELECT * FROM rvbbit.knn(''products''::regclass::oid, rvbbit.embed(''cozy wool blanket''), 10)',
 'cheap', ARRAY['vectors','similarity','knn','embedding']),

('flow_then_pipelines', 'cap_syntax',
 '<query> THEN <op> ...  |  rvbbit.flow(spec text) → setof jsonb',
 'Pipelines in SQL: chain steps where each stage''s rowset feeds the next (semantic ops over results, fan-out work, multi-step transforms) without temp tables or app glue. THEN splits stages token-aware; rvbbit.flow runs a declared spec.',
 'SELECT title FROM reports LIMIT 50 THEN condense',
 'metered_llm', ARRAY['pipeline','chain','multi-step','flow']),

('train_predict', 'cap_syntax',
 'rvbbit.train_model(model_name, source_sql, target_column, task, feature_schema, training_opts, description)',
 'Train a model from a query (classification/regression) — on success a predict_<model_name>() operator is auto-registered and usable inline in SQL like any other operator; evaluate_model reports quality. Turns "could we predict X" into a column.',
 'SELECT rvbbit.train_model(''churn'', ''SELECT * FROM customer_features'', ''churned'')',
 'gpu', ARRAY['ml','train','predict','model']),

('synth_sql', 'cap_syntax',
 'rvbbit.synth_sql(intent text, operator text, opts jsonb) → SQL text',
 'Natural-language-to-SQL codegen against the live schema (cached by intent shape). Useful as a drafting aid for gnarly queries; validate before trusting.',
 'SELECT rvbbit.synth_sql(''top 10 states by sightings since 2015'')',
 'metered_llm', ARRAY['nl2sql','codegen','draft']),

('alerts_watch', 'cap_syntax',
 'rvbbit.define_alert(p_name, p_condition jsonb, p_action jsonb, p_fire_policy, p_cardinality, p_fan_out_cap, p_cadence, p_description, p_owner, p_labels)',
 'Durable, edge-triggered watches: a SQL or semantic condition swept on cadence, firing an action (operator, MCP tool, flow) on transition. The "tell me if X happens" verb — survives sessions, deduplicates firings per entity.',
 'SELECT rvbbit.define_alert(''wa_spike'', ''{"kind":"sql","sql":"SELECT count(*) > 700 FROM sightings WHERE state=''''Washington''''"}''::jsonb, ''{"kind":"notify"}''::jsonb)',
 'cheap', ARRAY['alerts','watch','monitor','notify']),

('capability_search', 'cap_syntax',
 'rvbbit.capability_search(q text, k int DEFAULT 8, kinds text[] DEFAULT NULL) → ranked capabilities',
 'Ask the system what it can do: free-text search over this capability graph — semantic operators, blessed metrics, cubes, brains, syntax features — each hit with signature, example, and cost class. Re-crawl with rvbbit.capability_crawl() after installing or creating capabilities.',
 'SELECT * FROM rvbbit.capability_search(''label text rows with categories'')',
 'cheap', ARRAY['discovery','capabilities','help','what can you do'])
ON CONFLICT (name) DO UPDATE SET
    kind = EXCLUDED.kind, signature = EXCLUDED.signature,
    description = EXCLUDED.description, example = EXCLUDED.example,
    cost_class = EXCLUDED.cost_class, tags = EXCLUDED.tags;

-- ── The crawler ─────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION rvbbit.capability_doc(
    p_kind text, p_name text, p_signature text, p_description text,
    p_example text, p_cost text, p_tags text[])
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'capability ' || p_name
        || E'\nkind: ' || p_kind
        || E'\nsignature: ' || coalesce(p_signature, '')
        || E'\ncost: ' || coalesce(p_cost, 'unknown')
        || CASE WHEN cardinality(p_tags) > 0
                THEN E'\ntags: ' || array_to_string(p_tags, ', ') ELSE '' END
        || E'\n' || coalesce(p_description, '')
        || CASE WHEN coalesce(p_example, '') <> ''
                THEN E'\nexample: ' || p_example ELSE '' END;
$fn$;

-- Two significant figures, for embed-stable observed-cost lines (nightly
-- recrawls must not churn embeddings over noise-level changes).
CREATE OR REPLACE FUNCTION rvbbit._cap_sig2(v numeric)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE
        WHEN v IS NULL OR v <= 0 THEN NULL
        ELSE trim(trailing '.' FROM trim(trailing '0' FROM
             round(v, (1 - floor(log(v)))::int)::text))
    END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.capability_crawl(
    do_embed         boolean DEFAULT true,
    embed_specialist text    DEFAULT '')
RETURNS jsonb LANGUAGE plpgsql AS $fn$
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
                      FROM rvbbit.operators o
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
    FOR rec IN SELECT o.* FROM rvbbit.operators o LOOP
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

            RETURN jsonb_build_object(
                'graph', v_graph, 'seed', v_n_seed, 'operators', v_n_ops,
                'metrics', v_n_metrics, 'cubes', v_n_cubes, 'packs', v_n_packs,
                'mcp_tools', v_n_mcp,
                'docs_embedded', v_emb, 'embedder_ok', v_embed_ok);
        END;
    END;
END
$fn$;

-- ── The agent-facing entry point ────────────────────────────────────────────

CREATE OR REPLACE FUNCTION rvbbit.capability_search(
    q     text,
    k     int    DEFAULT 8,
    kinds text[] DEFAULT NULL)
RETURNS TABLE (kind text, name text, score float8, doc text)
LANGUAGE sql STABLE AS $fn$
    SELECT ds.kind, ds.rel_name AS name, ds.score, ds.doc
      FROM rvbbit.data_search(q, k, kinds, 'rvbbit_capabilities') ds;
$fn$;

-- ── Warehouse MCP connector tools (external-agent surface, in-corpus) ─────
-- Regenerate with scripts/gen_capability_mcp_tools.py after tool changes.

-- 79 tools
-- generated by scripts/gen_capability_mcp_tools.py — do not hand-edit rows
CREATE TABLE IF NOT EXISTS rvbbit.capability_mcp_tools (
    name    text PRIMARY KEY,
    summary text NOT NULL DEFAULT ''
);
INSERT INTO rvbbit.capability_mcp_tools (name, summary) VALUES
('alert_events', 'The firing audit log (newest first): which rule+entity fired, the transition, fired/failed status, the action output or error, and when. Pass name to scope to one rule.'),
('alert_state', 'Per-entity reconciler state for a rule: entity_key, last_status (pass|fail), score, consecutive fail count, and when it last changed/fired — the breakdown behind a rule''s breach count.'),
('alert_sweep_runs', 'The sweep heartbeat (newest first): per tick — tier, start/finish, rules evaluated, transitions, enqueued, errors. Use it to confirm the reconciler is alive and see its rate.'),
('ask_brain', 'Ask the document brain — semantic search over the docs YOU are permitted to see, returned as grounded, citeable context (NOT a synthesized answer — compose the answer from these chunks and cite title/folder). Access is enforced from your authenticated identity: docs you lack a role for, or that exclude you, never appear. The ABSENCE of a doc means you''re not cleared for it — never speculate about what you can''t see. PRE-FILTER to avoid mixing object classes: pass `filters` to narrow BEFORE the search — {"type": "ticket"} · {"type": ["document","meeting"]} · {"source": "Linear · all"} · {"folde'),
('ask_system_learning', 'Ask what RVBBIT has learned about this database. This is the agent-safe shortcut over ask_brain(filters={"type":["system_learning"]}) so callers do not need to remember the doc_type name. Results include workload/layout/routing/acceleration/operator breadcrumbs, not a synthesized answer. Compose an answer from the returned chunks and cite titles.'),
('brain_browse', 'The document brain as a file tree — every folder + doc YOU may see (ACL-enforced). Powers a file-explorer view and lets you navigate before asking. Returns folders + docs with folder_path, title, source, mime, occurred_at, chunk count.'),
('brain_context', 'VERTICAL expand: the chunks immediately AROUND a search hit (window on each side) — cheaper than pulling a whole long document when you just need a hit''s local context. Pass the doc_id + chunk_idx from an ask_brain hit. ACL-gated (empty if you''re not cleared for the doc).'),
('brain_crawl_folder', 'Crawl a SERVER-LOCAL folder and ingest its text documents into the brain — the on-disk folder structure becomes the brain''s folder tree (e.g. <root>/HR/policy.md → folder /<source>/HR). `path` must be readable by the MCP process (mount it into the container). roles = access roles applied to EVERY ingested doc (omit → the source''s defaults → DEFAULT-DENY: nobody sees them). Handles .md/.markdown/.mdx/.txt/.text/.rst/.org/.log; skips binaries + files over max_bytes. Re-crawl is idempotent (keyed on each file''s path), so it doubles as a sync.'),
('brain_entity', 'LATERAL expand, entity-centric: given a concept/person/org/metric (e.g. ''NPS'', ''refund policy''), return its typed relations and the visible documents that mention it — ''what do we know about X?''. Resolves by exact then fuzzy name match. ACL-gated (docs list is filtered to what you can see).'),
('brain_exclude', 'The subject-exclusion belt: hide a specific doc from a specific person even if their role would allow it (the meeting that''s ABOUT them). Returns the exclusion.'),
('brain_facets', 'Discover what you can FILTER by: the document TYPES (document, ticket, meeting, …) and SOURCES you are cleared to see, each with a doc count. Call this before ask_brain when you want to narrow — then pass filters={"type": "ticket"} or {"source": "..."} to ask_brain. ACL-enforced: only your visible corpus is counted.'),
('brain_get_doc', 'Open one document''s full body + metadata — only if you''re cleared for it (else NOT_VISIBLE).'),
('brain_grant', 'Grant (on=true) or revoke (on=false) a brain ROLE to a principal (email). Roles→emails are just rows — this IS the access model; who holds what determines what each person''s brain can see. Revocation takes effect on the next query (no re-index).'),
('brain_ingest', 'Ingest a document into the brain (operator action): chunks + embeds it and assigns access role(s). roles = the roles allowed to see it (omit → the source''s default roles → if none, DEFAULT-DENY: nobody can see it until granted a role). folder = its file-explorer path. Returns the doc_id.'),
('brain_related', 'LATERAL expand: a document''s knowledge-graph neighborhood — the entities it names, the typed relations among them (e.g. X -acquired-> Y), and OTHER docs you can see that share its entities. Follow a thread from a doc instead of re-searching. ACL-gated.'),
('brain_set_doc_roles', 'Set the access role(s) on a document — the docs a role grants are visible to anyone holding it. Pass [] to make it private again (default-deny). A freshly-ingested doc with no roles is invisible to everyone (incl. the explorer''s own listing); this is how you make it visible.'),
('breaching_alerts', ''),
('breaching_kpis', ''),
('capture_live_app', 'Semantic search over the catalog KG + data-KG, each table hit grounded with live samples, cheap per-column stats, and freshness/drift. Internal (rvbbit/pg_*) schemas are hidden, so users only ever see the data they''re meant to.'),
('compare', 'Period-over-period / variance for a metric: its value at period_a vs period_b with Δ and %Δ. Pass `by` (a cube dimension) to break it down per segment (a variance table). Periods are data-time instants (e.g. ''2026-03-31'' vs ''2026-06-30'') — each side is the metric AS OF that instant via the bitemporal engine. Render the breakdown as a table sorted by |Δ|.'),
('create_live_app', ''),
('dashboard_crawl', ''),
('dashboard_dependents', ''),
('dashboard_template', ''),
('debug_live_app', ''),
('describe_cube', 'A cube''s grain, columns, freshness + definition SQL (the agent''s grounding to query it).'),
('describe_table', 'Full profile of one table: columns, live samples, AND per-column stats — null %, distinct count, and the actual most-common values (the enum/value dictionary, so you never guess a status/type literal) — plus freshness. Pass lean=true for a compact view (columns + null%/distinct + freshness, no samples or top-values) on wide tables to stay under the token budget.'),
('edit_cube', 'Edit an existing cube''s DEFINITION in place — appends a new version (revert via the prior version) that goes LIVE immediately. Shape-aware: a column change rebuilds the cube table, a filter-only change preserves its AS-OF history. sql is required (the full new SELECT).'),
('edit_metric', 'Edit an existing metric IN PLACE — appends a new version (old versions are kept, so it''s reversible) that goes LIVE immediately. Only the fields you pass change. check_sql: omit to keep the current check, pass "" to remove it. Use this to fix/improve a metric you (or someone) defined.'),
('get_alert', 'One alert rule in full: latest-version condition_spec/action_spec/fire_policy, cardinality, fan-out cap, control state (enabled/muted/cadence), category, version history, a live state summary (breaching/entities/pending, last fired), and its most recent firing events.'),
('get_dashboard', ''),
('get_live_app', ''),
('get_metric', 'One metric''s definition (with category/subcategory) + version history.'),
('get_proposal', 'Full detail of one proposal — sql, grain, source_tables, params, check_sql, join_rationale, confidence, status, result_name. Use after list_proposals to inspect a specific draft.'),
('get_tool_help', ''),
('list_alerts', 'Alert rules with live vitals: each rule''s condition/action shape, on/off + mute + cadence tier, and current breach/entity/pending counts + last-fired. The agent''s entry point for "what''s firing and why". Filters: category, enabled (bool), muted (bool), tier (fast|normal|slow), search (name/description). Also returns the global alerts_enabled kill-switch state.'),
('list_cubes', 'Curated subject-area tables (cubes) — wide, documented, accelerated, with their category. The agent''s entry point: look here (and at metrics) before raw tables. Optional `category` filter.'),
('list_dashboards', ''),
('list_live_apps', ''),
('list_metrics', 'The blessed, governed metric catalog (latest version per metric), with its category. Read from metric_catalog (not metric_defs) so the shared category/subcategory taxonomy is included — use the optional `category` filter to scope to one subject area.'),
('list_proposals', 'See the proposal queue — drafts (yours or others'') and their fate. Filter by status (pending/accepted/rejected/withdrawn), kind (cube/metric), or proposed_by. ACCEPTED proposals carry result_name (the object created); REJECTED/WITHDRAWN carry notes (the reason). Use this to LEARN from feedback before proposing again — don''t re-propose something already rejected, and refine_proposal a pending draft instead of submitting a duplicate.'),
('live_app_logs', ''),
('live_app_status', ''),
('live_app_template', ''),
('materialize_metric', 'Snapshot a metric NOW into the durable observation log (value + KPI verdict at this instant) — the basis for trend history and breach monitoring. Returns the observation id.'),
('metric', 'A blessed, governed scalar number — bitemporal (as_of = data-time, def_as_of = def-time). Pass group_by (a list of cube dimension columns) to slice a DIMENSIONAL metric — one defined over a cube (labels.cube_source) — into a breakdown row per group (e.g. group_by=[''stage_name'']). The metric''s measures are reused verbatim; dimensions are validated against the cube''s real columns. Call metric_dimensions(name) to discover which columns are sliceable.'),
('metric_dimensions', 'The cube columns a DIMENSIONAL metric can be sliced by (empty unless it declares labels.cube_source). Each entry: column, type, kind (dimension/time/key/measure), groupable. Feed groupable columns to metric(name, group_by=[...]) for a breakdown.'),
('metric_history', 'The durable observation series for a metric (newest first): value, KPI verdict/status, the data-time it was taken at, and how it was triggered. Turns a definition into a trend.'),
('metric_lineage', 'The base tables a metric reads (for impact analysis) — resolved from its SQL via the planner. The metric-side mirror of dashboard_dependents.'),
('mute_alert', 'Temporarily silence a rule''s ACTIONS without stopping evaluation. minutes=None mutes indefinitely (until unmuted); otherwise for that many minutes. Returns the muted_until.'),
('pivot', 'A governed crosstab of a DIMENSIONAL metric: rows (a cube dimension) × cols (a cube dimension) × one measure, with row/column/grand totals. Reshapes metric_by into a matrix — values are the blessed metric''s, dimensions are validated against the cube — so it''s a repeatable pivot table, not hand-rolled SQL. measure defaults to the metric''s first numeric measure. Call metric_dimensions(metric) to see the sliceable columns. Render as a matrix.'),
('preview_alert_condition', 'Dry-run an alert CONDITION read-only — the observable feedback for authoring a rule before any rule exists. Runs the query (LIMIT 500) and returns its (entity_key, score, status) rows + counts. If expr is given, wraps the query in the same CASE the sweep uses (status=''fail'' when expr true) so a bad boolean expr surfaces as an error here. A condition query should return an entity_key column plus EITHER a status (''pass''/''fail'') OR a numeric score. Read-only: writes are blocked.'),
('preview_metric_observation', 'The latest materialized observation for a metric — exactly what a metric-kind condition reads (status pass/fail, value, verdict, data-time). Check this before wiring an alert onto a metric.'),
('profile_schema', 'A fast overview of every (allowed) table: estimated row count + column count — to see which tables are populated WITHOUT running count(*) probes. Optionally scope to one schema. Row counts are planner estimates (pg_class.reltuples, ~0 if never analyzed); use describe_table for a full per-column profile of one table.'),
('propose_cube', 'Draft a candidate cube for a subject — a documented join over your tables. Returns a DRAFT only (name, sql, grain, description, source_tables, join_rationale, confidence + the FK edges it reasoned from); NOTHING is created. The draft is LOGGED to a review queue (returns its proposal_id) so a human can bless it in the lens Cube Proposals inbox (or define_cube on the primary). Propose freely — good ideas are captured for review, not lost. Pass seed_tables (schema.table list) to pin the join, or a schema to scope discovery.'),
('propose_metric', 'Draft a candidate metric for a subject — a small, governed aggregation, PREFERRING a cube as its source. Returns a DRAFT only (name, sql, grain, description, params, optional KPI check_sql, source, confidence) plus validated/sample from a dry-run; NOTHING is created. The draft is LOGGED to the review queue (returns its proposal_id) so a human can bless it in the lens Proposals inbox (→ define_metric). Propose freely. Pass seed_sources (cubes.x / schema.table list) or a schema.'),
('publish_dashboard', ''),
('refine_proposal', 'Edit a PENDING proposal in place after seeing feedback — instead of submitting a duplicate. Only the fields you pass change. (Cube SQL is plain; metric SQL may use {param} tokens.) Pass category/subcategory to (re)file it under a folder before review.'),
('run_alert_sweep', 'Run one reconciler sweep NOW for a tier (fast|normal|slow) instead of waiting for cron — evaluates conditions, diffs state, enqueues transitions. Returns the sweep summary. Pair with run_alert_worker to actually dispatch what it enqueues.'),
('run_alert_worker', 'Drain up to max_items from the action queue NOW (dispatch pending alert actions) instead of waiting for cron. Fire-and-forget; results land in alert_events. Returns the drain summary.'),
('run_sql', 'Governed read-only execute: validate -> safe_select gate -> read-only run + LIMIT.'),
('run_sql_multi', 'Governed read-only BATCH: many named FLAT queries, one round trip. This exists so dashboards/apps never glue multi-concern payloads together inside SQL (top-level json_build_object) just to save bridge calls — each concern stays a flat rowset the router can accelerate, the catalog can mine, and Promote can later lift into a metric/cube. Per-query errors are isolated under their name; one bad query doesn''t sink the batch. result_mode=''summary'' returns per-query row_count/columns/truncated/ elapsed/error plus the first preview_rows rows — use it to VALIDATE a dashboard''s query set without haulin'),
('scoreboard', 'The executive KPI matrix: every blessed metric laid out as category › subcategory (left axis) × time periods (top axis), each cell = the metric''s value that period, with its latest target verdict and a trend. Reads the materialized observation log (the governed history — no recompute). Filter by category; grain = day|week|month|quarter|year; periods = how many columns back. The ''how are we doing?'' view — render it as one opinionated matrix, not a flat list.'),
('search_data', 'Semantic search over the catalog KG + data-KG, each table hit grounded with live samples, cheap per-column stats, and freshness/drift. Internal (rvbbit/pg_*) schemas are hidden, so users only ever see the data they''re meant to.'),
('search_tools', ''),
('set_alert_cadence', 'Move a rule to a sweep tier: ''fast'' (~1m), ''normal'' (~15m), or ''slow'' (~hourly).'),
('set_alert_enabled', 'Enable or disable a rule (control flag; survives re-definition). Disabled rules are skipped by the sweep. The non-destructive on/off — use this (or mute) to silence a noisy alert, not delete.'),
('set_alerts_enabled', 'The GLOBAL alerts kill-switch. on=false pauses ALL sweeps + actions at once (the circuit breaker); on=true resumes. Returns the new state. Pairs with the alerts_enabled flag in list_alerts.'),
('set_category', 'Categorize a cube or metric (kind = ''cube'' | ''metric'') in the shared taxonomy — lightweight and mutable (no new version). Pass category=null to clear it. Use this to organize the catalog; read it back via list_cubes / list_metrics.'),
('start_live_app', ''),
('stop_live_app', ''),
('sync_system_learning', ''),
('system_learning_status', ''),
('unmute_alert', 'Clear a rule''s mute (resume its actions).'),
('update_dashboard', ''),
('update_live_app', ''),
('upload_artifact', ''),
('validate_sql', 'Plan, don''t execute — route_explain dry-run so Claude can self-correct cheaply.'),
('withdraw_proposal', 'Retract a PENDING proposal you no longer want reviewed (status -> withdrawn).')
ON CONFLICT (name) DO UPDATE SET summary = EXCLUDED.summary;
