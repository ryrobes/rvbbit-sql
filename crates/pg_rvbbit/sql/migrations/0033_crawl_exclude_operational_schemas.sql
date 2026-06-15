-- 0033_crawl_exclude_operational_schemas
--
-- The catalog crawl fingerprints EVERY non-system schema, including the live
-- app's high-churn operational schemas (e.g. dagster_operational). Those tables
-- are constantly locked by the running app, so the crawl shards convoy on
-- relation locks (observed: all 6 shards blocked on dagster_operational.snapshots)
-- and the crawl stalls before it ever embeds anything. Their transient run/event
-- tables also have little search value.
--
-- Fix: skip a configurable set of schemas. rvbbit._catalog_excluded_schemas()
-- reads rvbbit.settings key 'catalog_crawl_exclude_schemas' (jsonb array of
-- schema names); unset => ['dagster_operational']. Both the parallel
-- (catalog_crawl_run_parallel) and serial (catalog_crawl) enumerations apply it.
-- (Carried as a migration because this DB's pgrx extension upgrade path is
-- blocked, so catalog_kg.sql changes don't reach it via ALTER EXTENSION.)

-- helper -------------------------------------------------------------
CREATE OR REPLACE FUNCTION rvbbit._catalog_excluded_schemas()
RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN to_regclass('rvbbit.settings') IS NULL THEN ARRAY['dagster_operational']::text[]
        WHEN EXISTS (SELECT 1 FROM rvbbit.settings WHERE key = 'catalog_crawl_exclude_schemas')
            THEN COALESCE(
                (SELECT array_agg(x.v)
                   FROM rvbbit.settings s,
                        LATERAL jsonb_array_elements_text(s.value) AS x(v)
                  WHERE s.key = 'catalog_crawl_exclude_schemas'),
                ARRAY[]::text[])
        ELSE ARRAY['dagster_operational']::text[]
    END;
$$;

-- seed the setting so it's visible/editable (add more schemas as needed) ---
INSERT INTO rvbbit.settings (key, value, updated_at)
VALUES ('catalog_crawl_exclude_schemas', '["dagster_operational"]'::jsonb, now())
ON CONFLICT (key) DO NOTHING;

-- parallel crawl (cron path) -----------------------------------------
CREATE OR REPLACE PROCEDURE rvbbit.catalog_crawl_run_parallel(
    schemas          text[]  DEFAULT NULL,
    graph            text    DEFAULT 'db_catalog',
    sample_rows      int     DEFAULT 50000,
    examples_k       int     DEFAULT 12,
    do_embed         boolean DEFAULT true,
    embed_specialist text    DEFAULT '',
    parallelism      int     DEFAULT 4)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_graph   text := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    v_run     bigint;
    v_total   int;
    v_n       int := greatest(1, least(coalesce(parallelism, 1), 16));
    v_conn    text;
    v_have_dblink boolean := false;
    i         int;
BEGIN
    PERFORM rvbbit.catalog_kg_ensure();

    INSERT INTO rvbbit.catalog_runs (graph_id, status, schemas)
    VALUES (v_graph, 'running', schemas)
    RETURNING run_id INTO v_run;
    COMMIT;

    -- Enqueue every target table as a 'queued' progress row with a global
    -- ordinal (the same enumeration the serial crawler uses).
    WITH ordered AS (
        SELECT c.oid AS reloid, n.nspname AS sch, c.relname AS rel,
               row_number() OVER (ORDER BY n.nspname, c.relname) AS ord
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'm', 'v')
           AND NOT c.relispartition
           AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'rvbbit')
           AND n.nspname NOT LIKE 'pg_toast%'
           AND n.nspname NOT LIKE 'pg_temp_%'
           -- Skip configured operational schemas (e.g. dagster_operational): they
           -- are high-churn and the live app's locks on them convoy the crawl
           -- shards, and their transient run/event tables have little search value.
           AND n.nspname <> ALL (rvbbit._catalog_excluded_schemas())
           AND (schemas IS NULL OR n.nspname = ANY (schemas))
    )
    INSERT INTO rvbbit.catalog_crawl_progress
        (run_id, ordinal, total, reloid, schema_name, rel_name, status, started_at)
    SELECT v_run, ord, (SELECT count(*) FROM ordered), reloid, sch, rel, 'queued', now()
      FROM ordered;
    GET DIAGNOSTICS v_total = ROW_COUNT;
    COMMIT;

    IF v_total = 0 THEN
        UPDATE rvbbit.catalog_runs SET status = 'ok', finished_at = now() WHERE run_id = v_run;
        COMMIT;
        RETURN;
    END IF;

    -- Try to make dblink available; if we can't, fall back to a single in-process shard.
    IF v_n > 1 THEN
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'dblink') THEN
                EXECUTE 'CREATE EXTENSION IF NOT EXISTS dblink';
            END IF;
            v_have_dblink := true;
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'catalog_crawl_run_parallel: dblink unavailable (%), running serially', SQLERRM;
            v_have_dblink := false;
        END;
    END IF;

    IF v_n <= 1 OR NOT v_have_dblink THEN
        -- serial: one shard covering everything, in this backend
        CALL rvbbit._catalog_crawl_shard(v_run, 0, 1, v_graph,
                                         sample_rows, examples_k, do_embed, embed_specialist);
    ELSE
        v_conn := rvbbit._crawl_dblink_conninfo();
        -- fan out N async workers (the parent holds no table/row locks here)
        FOR i IN 0 .. v_n - 1 LOOP
            PERFORM dblink_connect('rvbbit_crawl_' || i, v_conn);
            PERFORM dblink_send_query('rvbbit_crawl_' || i,
                format('CALL rvbbit._catalog_crawl_shard(%s, %s, %s, %L, %s, %s, %L::boolean, %L)',
                       v_run, i, v_n, v_graph, sample_rows, examples_k, do_embed::text, embed_specialist));
        END LOOP;
        -- join: wait for each worker; tolerate one erroring (its committed
        -- per-table progress survives, the others continue).
        FOR i IN 0 .. v_n - 1 LOOP
            BEGIN
                PERFORM * FROM dblink_get_result('rvbbit_crawl_' || i) AS t(x text);
            EXCEPTION WHEN others THEN
                RAISE NOTICE 'catalog_crawl_run_parallel: worker % failed: %', i, SQLERRM;
            END;
            BEGIN PERFORM dblink_disconnect('rvbbit_crawl_' || i); EXCEPTION WHEN others THEN NULL; END;
        END LOOP;
    END IF;

    -- Serial pass for the deferred shared edges (schema nodes + has_table + FK
    -- references), now that all workers are joined: no contention.
    CALL rvbbit._catalog_crawl_shared_edges(v_run, v_graph);
    COMMIT;

    -- Finalize: aggregate counters from the progress log.
    UPDATE rvbbit.catalog_runs SET
        status        = 'ok',
        finished_at   = now(),
        tables_seen   = (SELECT count(*) FROM rvbbit.catalog_crawl_progress WHERE run_id = v_run AND status = 'ok'),
        columns_seen  = (SELECT coalesce(sum(n_columns), 0) FROM rvbbit.catalog_crawl_progress WHERE run_id = v_run AND status = 'ok'),
        docs_embedded = (SELECT coalesce(sum(n_embedded), 0) FROM rvbbit.catalog_crawl_progress WHERE run_id = v_run AND status = 'ok')
     WHERE run_id = v_run;
    COMMIT;
END $fn$;

-- serial crawl (lens "Crawl" button path) ----------------------------
CREATE OR REPLACE FUNCTION rvbbit.catalog_crawl(
    schemas          text[]  DEFAULT NULL,
    graph            text    DEFAULT 'db_catalog',
    sample_rows      int     DEFAULT 50000,
    examples_k       int     DEFAULT 12,
    do_embed         boolean DEFAULT true,
    embed_specialist text    DEFAULT '')
RETURNS jsonb
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_graph    text := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    v_run      bigint;
    rec        record;
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
    v_embed_ok boolean := do_embed;
    v_tables   bigint := 0;
    v_cols     bigint := 0;
    v_edges    bigint := 0;
    v_emb      bigint := 0;
BEGIN
    INSERT INTO rvbbit.catalog_runs (graph_id, status, schemas)
    VALUES (v_graph, 'running', schemas)
    RETURNING run_id INTO v_run;

    FOR rec IN
        SELECT c.oid AS reloid, n.nspname AS schema_name, c.relname AS rel_name
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'm', 'v')
           AND NOT c.relispartition
           AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'rvbbit')
           AND n.nspname NOT LIKE 'pg_toast%'
           AND n.nspname NOT LIKE 'pg_temp_%'
           -- Skip configured operational schemas (rvbbit._catalog_excluded_schemas);
           -- high-churn app tables (e.g. dagster_operational) convoy the crawl on locks.
           AND n.nspname <> ALL (rvbbit._catalog_excluded_schemas())
           AND (schemas IS NULL OR n.nspname = ANY (schemas))
         ORDER BY n.nspname, c.relname
    LOOP
        BEGIN
            fp := rvbbit.catalog_fingerprint_table(rec.reloid::regclass, sample_rows, examples_k);
        EXCEPTION WHEN others THEN
            CONTINUE;   -- skip un-fingerprintable relation
        END;

        v_schema  := fp->>'schema';
        v_table   := fp->>'table';
        v_comment := fp->>'comment';
        v_tlabel  := v_schema || '.' || v_table;

        -- Batch-warm the embedding cache for this table's docs (table + every
        -- column) in ONE embedder call so the per-doc rvbbit.embed() below are
        -- cache hits — decisive for a remote embedder (OpenAI/OpenRouter). One
        -- batched request instead of N+1 round-trips. Best-effort.
        IF v_embed_ok THEN
            BEGIN
                PERFORM rvbbit.embed_batch(
                    ARRAY[rvbbit.catalog_table_doc(fp)]
                    || COALESCE((SELECT array_agg(
                           rvbbit.catalog_column_doc(v_schema, v_table, v_comment, e))
                         FROM jsonb_array_elements(fp->'columns') AS e), ARRAY[]::text[]),
                    embed_specialist, 'document');
            EXCEPTION WHEN others THEN NULL;  -- fall back to per-doc embed
            END;
        END IF;

        -- schema node
        PERFORM rvbbit.kg_assert_node('db_schema', v_schema,
                    jsonb_build_object('name', v_schema), 1.0, '', 0.0, v_graph);

        -- table node (full fingerprint summary + search_doc)
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
                    1.0, '', 0.0, v_graph);
        v_tables := v_tables + 1;

        PERFORM rvbbit.kg_assert_edge('db_schema', v_schema, 'has_table',
                    'db_table', v_tlabel, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, v_graph);
        v_edges := v_edges + 1;

        PERFORM rvbbit.kg_link_evidence(
                    target_node_id => v_tnode,
                    source_table   => rec.reloid::regclass,
                    source_pk      => fp->>'oid',
                    evidence_text  => left(v_doc, 2000),
                    confidence     => 1.0,
                    graph          => v_graph);

        v_vec := NULL;
        IF v_embed_ok THEN
            BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
            EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
        END IF;
        INSERT INTO rvbbit.catalog_docs
            (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
        VALUES (v_tnode, v_graph, 'db_table', v_schema, v_table, NULL, v_doc, v_vec,
                CASE WHEN v_vec IS NOT NULL THEN now() END, now())
        ON CONFLICT (graph_id, node_id) DO UPDATE SET
            kind = EXCLUDED.kind, schema_name = EXCLUDED.schema_name,
            rel_name = EXCLUDED.rel_name, col_name = EXCLUDED.col_name,
            doc = EXCLUDED.doc, embedding = EXCLUDED.embedding,
            embedded_at = EXCLUDED.embedded_at, updated_at = now();
        IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;

        -- table drift snapshot (table-level summary, columns array stripped)
        INSERT INTO rvbbit.catalog_snapshots
            (run_id, graph_id, node_id, kind, schema_name, rel_name, col_name, obj_key, fingerprint, embedding)
        VALUES (v_run, v_graph, v_tnode, 'db_table', v_schema, v_table, NULL, v_tlabel,
                fp - 'columns', v_vec);

        -- columns
        FOR v_col IN SELECT e FROM jsonb_array_elements(fp->'columns') AS e
        LOOP
            v_clabel := v_tlabel || '.' || (v_col->>'name');
            v_doc := rvbbit.catalog_column_doc(v_schema, v_table, v_comment, v_col);
            -- Node properties stay lean: the bulky value_dist / quantiles live
            -- only in the drift snapshot, not in kg_nodes.
            v_cnode := rvbbit.kg_assert_node('db_column', v_clabel,
                        jsonb_strip_nulls((v_col - 'value_dist' - 'quantiles') || jsonb_build_object(
                            'schema',     v_schema,
                            'table',      v_table,
                            'table_oid',  fp->>'oid',
                            'search_doc', v_doc)),
                        1.0, '', 0.0, v_graph);
            v_cols := v_cols + 1;

            PERFORM rvbbit.kg_assert_edge('db_table', v_tlabel, 'has_column',
                        'db_column', v_clabel, 1.0, '{}'::jsonb,
                        jsonb_build_object('ordinal', v_col->>'ordinal'), '', 0.0, v_graph);
            v_edges := v_edges + 1;

            IF (v_col->>'is_fk') = 'true' AND (v_col->>'fk_target') IS NOT NULL THEN
                PERFORM rvbbit.kg_assert_edge('db_column', v_clabel, 'references',
                            'db_column', v_col->>'fk_target', 1.0, '{}'::jsonb, '{}'::jsonb,
                            '', 0.0, v_graph);
                v_edges := v_edges + 1;
            END IF;

            PERFORM rvbbit.kg_link_evidence(
                        target_node_id => v_cnode,
                        source_table   => rec.reloid::regclass,
                        source_column  => v_col->>'name',
                        source_pk      => fp->>'oid',
                        evidence_text  => left(v_doc, 2000),
                        confidence     => 1.0,
                        graph          => v_graph);

            v_vec := NULL;
            IF v_embed_ok THEN
                BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist, 'document');
                EXCEPTION WHEN others THEN v_vec := NULL; v_embed_ok := false; END;
            END IF;
            INSERT INTO rvbbit.catalog_docs
                (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
            VALUES (v_cnode, v_graph, 'db_column', v_schema, v_table, v_col->>'name', v_doc, v_vec,
                    CASE WHEN v_vec IS NOT NULL THEN now() END, now())
            ON CONFLICT (graph_id, node_id) DO UPDATE SET
                kind = EXCLUDED.kind, schema_name = EXCLUDED.schema_name,
                rel_name = EXCLUDED.rel_name, col_name = EXCLUDED.col_name,
                doc = EXCLUDED.doc, embedding = EXCLUDED.embedding,
                embedded_at = EXCLUDED.embedded_at, updated_at = now();
            IF v_vec IS NOT NULL THEN v_emb := v_emb + 1; END IF;

            -- column drift snapshot (full fingerprint incl. value_dist/quantiles)
            INSERT INTO rvbbit.catalog_snapshots
                (run_id, graph_id, node_id, kind, schema_name, rel_name, col_name, obj_key, fingerprint, embedding)
            VALUES (v_run, v_graph, v_cnode, 'db_column', v_schema, v_table, v_col->>'name', v_clabel,
                    v_col, v_vec);
        END LOOP;
    END LOOP;

    UPDATE rvbbit.catalog_runs
       SET status = 'ok', tables_seen = v_tables, columns_seen = v_cols,
           edges_made = v_edges, docs_embedded = v_emb, finished_at = now()
     WHERE run_id = v_run;

    RETURN jsonb_build_object('run_id', v_run, 'graph', v_graph,
        'tables', v_tables, 'columns', v_cols, 'edges', v_edges, 'docs_embedded', v_emb);
EXCEPTION WHEN others THEN
    UPDATE rvbbit.catalog_runs
       SET status = 'failed', error = SQLERRM, finished_at = now()
     WHERE run_id = v_run;
    RAISE;
END $fn$;
