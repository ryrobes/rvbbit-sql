-- 0032_batch_embed_crawl_prewarm
--
-- The parallel catalog crawl's per-table worker (_catalog_crawl_one, from 0003)
-- embedded one doc per rvbbit.embed() call. Fine for a ~9ms in-process embedder,
-- but ~25x too slow once the default embedder is a REMOTE API (OpenAI/OpenRouter,
-- ~200ms/call) — a 30-min crawl ballooned to 7-8h.
--
-- Fix: before processing a table, batch-warm the embedding cache for ALL of that
-- table's docs (table doc + every column doc) in ONE rvbbit.embed_batch() call
-- (sends up to spec.batch_size inputs per request via predict_batch). The per-doc
-- rvbbit.embed() calls below then resolve from cache — N+1 sequential remote
-- round-trips per table collapse into one batched request. Best-effort: wrapped
-- so a build lacking rvbbit.embed_batch falls back to the per-doc path.
--
-- (rvbbit.embed_batch and the serial catalog_crawl's matching prewarm ship via
-- the extension SQL; this migration carries the change to the runtime-migrated
-- _catalog_crawl_one on already-installed databases.)

CREATE OR REPLACE FUNCTION rvbbit._catalog_crawl_one(
    p_run              bigint,
    p_reloid           oid,
    p_graph            text,
    p_sample_rows      int     DEFAULT 50000,
    p_examples_k       int     DEFAULT 12,
    p_do_embed         boolean DEFAULT true,
    p_embed_specialist text    DEFAULT '',
    p_defer_shared     boolean DEFAULT false)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    fp         jsonb;
    v_col      jsonb;
    v_schema   text;
    v_table    text;
    v_comment  text;
    v_tlabel   text;
    v_clabel   text;
    v_tnode    bigint;
    v_cnode    bigint;
    v_doc      text;
    v_vec      real[];
    v_embed_ok boolean := p_do_embed;
    v_cols     bigint := 0;
    v_edges    bigint := 0;
    v_emb      bigint := 0;
BEGIN
    fp := rvbbit.catalog_fingerprint_table(p_reloid::regclass, p_sample_rows, p_examples_k);

    v_schema  := fp->>'schema';
    v_table   := fp->>'table';
    v_comment := fp->>'comment';
    v_tlabel  := v_schema || '.' || v_table;

    -- Batch-warm the embedding cache for this table's docs (the table doc + every
    -- column doc) in ONE embedder call, so the per-doc rvbbit.embed() calls below
    -- resolve from cache. Decisive for a REMOTE embedder (OpenAI/OpenRouter,
    -- ~200ms/call): collapses N+1 sequential round-trips into one batched request.
    -- Best-effort — on any failure (e.g. older build without embed_batch) the
    -- per-doc embed path below still runs unchanged.
    IF v_embed_ok THEN
        BEGIN
            PERFORM rvbbit.embed_batch(
                ARRAY[rvbbit.catalog_table_doc(fp)]
                || COALESCE((SELECT array_agg(
                       rvbbit.catalog_column_doc(v_schema, v_table, v_comment, e))
                     FROM jsonb_array_elements(fp->'columns') AS e), ARRAY[]::text[]),
                p_embed_specialist, 'document');
        EXCEPTION WHEN others THEN NULL;  -- fall back to per-doc embed
        END;
    END IF;

    -- schema node + has_table edge are SHARED across shards (every table touches
    -- the one schema node) → deferred to the parent's serial pass when requested.
    IF NOT p_defer_shared THEN
        PERFORM rvbbit.kg_assert_node('db_schema', v_schema,
                    jsonb_build_object('name', v_schema), 1.0, '', 0.0, p_graph);
    END IF;

    -- table node (full fingerprint summary + search_doc) — disjoint per table
    v_doc := rvbbit.catalog_table_doc(fp);
    v_tnode := rvbbit.kg_assert_node('db_table', v_tlabel,
                jsonb_strip_nulls(jsonb_build_object(
                    'oid',         fp->>'oid',
                    'schema',      v_schema,
                    'table',       v_table,
                    'relkind',     fp->>'relkind',
                    'n_rows',      fp->>'n_rows',
                    'n_sampled',   fp->>'n_sampled',
                    'size_bytes',  fp->>'size_bytes',
                    'comment',     v_comment,
                    'n_columns',   fp->>'n_columns',
                    'profiled_at', fp->>'profiled_at',
                    'search_doc',  v_doc)),
                1.0, '', 0.0, p_graph);

    IF NOT p_defer_shared THEN
        PERFORM rvbbit.kg_assert_edge('db_schema', v_schema, 'has_table',
                    'db_table', v_tlabel, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, p_graph);
        v_edges := v_edges + 1;
    END IF;

    PERFORM rvbbit.kg_link_evidence(
                target_node_id => v_tnode,
                source_table   => p_reloid::regclass,
                source_pk      => fp->>'oid',
                evidence_text  => left(v_doc, 2000),
                confidence     => 1.0,
                graph          => p_graph);

    v_vec := NULL;
    IF v_embed_ok THEN
        BEGIN v_vec := rvbbit.embed(v_doc, p_embed_specialist, 'document');
        EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
    END IF;
    INSERT INTO rvbbit.catalog_docs
        (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
    VALUES (v_tnode, p_graph, 'db_table', v_schema, v_table, NULL, v_doc, v_vec,
            CASE WHEN v_vec IS NOT NULL THEN now() END, now())
    ON CONFLICT (graph_id, node_id) DO UPDATE SET
        kind = EXCLUDED.kind, schema_name = EXCLUDED.schema_name,
        rel_name = EXCLUDED.rel_name, col_name = EXCLUDED.col_name,
        doc = EXCLUDED.doc, embedding = EXCLUDED.embedding,
        embedded_at = EXCLUDED.embedded_at, updated_at = now();
    IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;

    INSERT INTO rvbbit.catalog_snapshots
        (run_id, graph_id, node_id, kind, schema_name, rel_name, col_name, obj_key, fingerprint, embedding)
    VALUES (p_run, p_graph, v_tnode, 'db_table', v_schema, v_table, NULL, v_tlabel,
            fp - 'columns', v_vec);

    FOR v_col IN SELECT e FROM jsonb_array_elements(fp->'columns') AS e
    LOOP
        v_clabel := v_tlabel || '.' || (v_col->>'name');
        v_doc := rvbbit.catalog_column_doc(v_schema, v_table, v_comment, v_col);
        v_cnode := rvbbit.kg_assert_node('db_column', v_clabel,
                    jsonb_strip_nulls((v_col - 'value_dist' - 'quantiles') || jsonb_build_object(
                        'schema',     v_schema,
                        'table',      v_table,
                        'table_oid',  fp->>'oid',
                        'search_doc', v_doc)),
                    1.0, '', 0.0, p_graph);
        v_cols := v_cols + 1;

        -- has_column: subject + object are both THIS table's nodes → disjoint
        PERFORM rvbbit.kg_assert_edge('db_table', v_tlabel, 'has_column',
                    'db_column', v_clabel, 1.0, '{}'::jsonb,
                    jsonb_build_object('ordinal', v_col->>'ordinal'), '', 0.0, p_graph);
        v_edges := v_edges + 1;

        -- references (FK): object is a column owned by ANOTHER table (possibly
        -- another shard) → deferred to the parent's serial pass when requested.
        IF NOT p_defer_shared
           AND (v_col->>'is_fk') = 'true' AND (v_col->>'fk_target') IS NOT NULL THEN
            PERFORM rvbbit.kg_assert_edge('db_column', v_clabel, 'references',
                        'db_column', v_col->>'fk_target', 1.0, '{}'::jsonb, '{}'::jsonb,
                        '', 0.0, p_graph);
            v_edges := v_edges + 1;
        END IF;

        PERFORM rvbbit.kg_link_evidence(
                    target_node_id => v_cnode,
                    source_table   => p_reloid::regclass,
                    source_column  => v_col->>'name',
                    source_pk      => fp->>'oid',
                    evidence_text  => left(v_doc, 2000),
                    confidence     => 1.0,
                    graph          => p_graph);

        v_vec := NULL;
        IF v_embed_ok THEN
            BEGIN v_vec := rvbbit.embed(v_doc, p_embed_specialist, 'document');
            EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
        END IF;
        INSERT INTO rvbbit.catalog_docs
            (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
        VALUES (v_cnode, p_graph, 'db_column', v_schema, v_table, v_col->>'name', v_doc, v_vec,
                CASE WHEN v_vec IS NOT NULL THEN now() END, now())
        ON CONFLICT (graph_id, node_id) DO UPDATE SET
            kind = EXCLUDED.kind, schema_name = EXCLUDED.schema_name,
            rel_name = EXCLUDED.rel_name, col_name = EXCLUDED.col_name,
            doc = EXCLUDED.doc, embedding = EXCLUDED.embedding,
            embedded_at = EXCLUDED.embedded_at, updated_at = now();
        IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;

        INSERT INTO rvbbit.catalog_snapshots
            (run_id, graph_id, node_id, kind, schema_name, rel_name, col_name, obj_key, fingerprint, embedding)
        VALUES (p_run, p_graph, v_cnode, 'db_column', v_schema, v_table, v_col->>'name', v_clabel,
                v_col, v_vec);
    END LOOP;

    RETURN jsonb_build_object(
        'tables',        1,
        'columns',       v_cols,
        'edges',         v_edges,
        'docs_embedded', v_emb,
        'schema',        v_schema,
        'table',         v_table,
        'n_rows',        (fp->>'n_rows'),
        'n_columns',     (fp->>'n_columns'));
END $fn$;
