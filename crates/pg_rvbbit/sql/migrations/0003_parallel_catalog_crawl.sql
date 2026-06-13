-- 0003_parallel_catalog_crawl
--
-- A PARALLEL sibling of catalog_crawl_run that fans tables out across N
-- background backends via dblink. The serial catalog_crawl_run (0001) is left
-- as-is — this is opt-in:  CALL rvbbit.catalog_crawl_run_parallel(parallelism => 4);
--
-- Why a "defer shared edges" mode: kg_assert_edge asserts BOTH endpoint nodes
-- (locking their rows), so two per-table writes are NOT disjoint across shards
-- and deadlock under naive parallelism:
--   (1) the shared per-schema node — every table re-asserts it via its has_table
--       edge; and
--   (2) FK `references` edges — a table in one shard references a column owned by
--       another shard's table, so workers lock each other's nodes in opposite
--       orders → a cycle.
-- Everything else (fingerprint, embed, table/column nodes, has_column edges,
-- evidence, docs, snapshots) is disjoint per table and parallel-safe. So the
-- workers run with p_defer_shared = true (skipping those two), and the parent
-- asserts the schema nodes + has_table + FK references edges in ONE serial pass
-- after the workers finish — no contention, and it's cheap (catalog-only).
--
-- Deadlock hygiene also requires the parent to hold no table/row locks while
-- waiting on dblink_get_result: it COMMITs the run + the enqueue before fanning
-- out, and the shared-edge pass happens only after all workers are joined.
--
-- Note: the embedder can still be the ceiling — N workers issue N concurrent
-- rvbbit.embed() calls, which only helps if the embedder serves concurrency.

-- Per-table worker, now with p_defer_shared: when true, skip the cross-shard
-- contending writes (schema node, has_table edge, FK references edges). Default
-- false keeps the serial crawler (0001) byte-for-byte unchanged.
DROP FUNCTION IF EXISTS rvbbit._catalog_crawl_one(bigint, oid, text, int, int, boolean, text);
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

-- Serial pass: assert the shared graph edges the parallel workers deferred —
-- schema nodes + has_table edges (from the run's crawled tables) and FK
-- references edges (re-derived from pg_constraint). One backend, no contention.
CREATE OR REPLACE PROCEDURE rvbbit._catalog_crawl_shared_edges(p_run bigint, p_graph text)
LANGUAGE plpgsql AS $fn$
DECLARE rec record;
BEGIN
    -- schema nodes + has_table edges
    FOR rec IN
        SELECT DISTINCT schema_name, rel_name
          FROM rvbbit.catalog_crawl_progress
         WHERE run_id = p_run AND status = 'ok'
    LOOP
        PERFORM rvbbit.kg_assert_node('db_schema', rec.schema_name,
                    jsonb_build_object('name', rec.schema_name), 1.0, '', 0.0, p_graph);
        PERFORM rvbbit.kg_assert_edge('db_schema', rec.schema_name, 'has_table',
                    'db_table', rec.schema_name || '.' || rec.rel_name,
                    1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, p_graph);
    END LOOP;

    -- FK references edges, derived from the catalog for the run's tables
    FOR rec IN
        SELECT sn.nspname || '.' || sc.relname || '.' || sa.attname AS src_label,
               tn.nspname || '.' || tc.relname || '.' || ta.attname AS tgt_label
          FROM pg_constraint con
          JOIN pg_class sc      ON sc.oid = con.conrelid
          JOIN pg_namespace sn  ON sn.oid = sc.relnamespace
          JOIN pg_class tc      ON tc.oid = con.confrelid
          JOIN pg_namespace tn  ON tn.oid = tc.relnamespace
          CROSS JOIN LATERAL unnest(con.conkey, con.confkey) AS k(src_attnum, tgt_attnum)
          JOIN pg_attribute sa  ON sa.attrelid = con.conrelid AND sa.attnum = k.src_attnum
          JOIN pg_attribute ta  ON ta.attrelid = con.confrelid AND ta.attnum = k.tgt_attnum
         WHERE con.contype = 'f'
           AND con.conrelid IN (
               SELECT reloid FROM rvbbit.catalog_crawl_progress
                WHERE run_id = p_run AND status = 'ok' AND reloid IS NOT NULL)
    LOOP
        PERFORM rvbbit.kg_assert_edge('db_column', rec.src_label, 'references',
                    'db_column', rec.tgt_label, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, p_graph);
    END LOOP;
END $fn$;

-- Self-connection string for the dblink workers. Defaults to the current
-- database over the local socket as the current user; override for a
-- password/host setup via:  SET rvbbit.crawl_dblink_conninfo = 'host=… dbname=… user=… password=…'
CREATE OR REPLACE FUNCTION rvbbit._crawl_dblink_conninfo()
RETURNS text
LANGUAGE sql STABLE AS $fn$
    SELECT COALESCE(
        NULLIF(current_setting('rvbbit.crawl_dblink_conninfo', true), ''),
        format('dbname=%s', quote_ident(current_database()))
    )
$fn$;

-- One worker: crawl this shard's slice (ordinal % p_nshards = p_shard) of the
-- run's queued tables, COMMITting after each. Runs with p_defer_shared = true so
-- the cross-shard contending edges are left for the parent's serial pass.
CREATE OR REPLACE PROCEDURE rvbbit._catalog_crawl_shard(
    p_run             bigint,
    p_shard           int,
    p_nshards         int,
    p_graph           text,
    p_sample_rows     int,
    p_examples_k      int,
    p_do_embed        boolean,
    p_embed_specialist text)
LANGUAGE plpgsql AS $fn$
DECLARE
    rec record;
    r   jsonb;
BEGIN
    -- NB: do NOT call catalog_kg_ensure() here. Each worker is a fresh backend,
    -- so its session guard is unset and it would re-run the ensure DDL (CREATE
    -- TABLE/INDEX IF NOT EXISTS, ALTER EXTENSION) — which takes AccessExclusiveLock
    -- on catalog_crawl_progress and deadlocks against the other workers' row
    -- UPDATEs to it. The parent runs catalog_kg_ensure() before fanning out.
    FOR rec IN
        SELECT ordinal, reloid
          FROM rvbbit.catalog_crawl_progress
         WHERE run_id = p_run
           AND status = 'queued'
           AND (p_nshards <= 1 OR (ordinal % p_nshards) = p_shard)
         ORDER BY ordinal
    LOOP
        IF rec.reloid IS NULL THEN CONTINUE; END IF;

        UPDATE rvbbit.catalog_crawl_progress
           SET status = 'running', started_at = now()
         WHERE run_id = p_run AND ordinal = rec.ordinal;
        COMMIT;

        BEGIN
            r := rvbbit._catalog_crawl_one(p_run, rec.reloid, p_graph,
                                           p_sample_rows, p_examples_k, p_do_embed, p_embed_specialist,
                                           true);   -- defer shared edges
            UPDATE rvbbit.catalog_crawl_progress
               SET status = 'ok', n_columns = (r->>'columns')::int,
                   n_embedded = (r->>'docs_embedded')::int,
                   n_rows = (r->>'n_rows')::bigint, finished_at = now()
             WHERE run_id = p_run AND ordinal = rec.ordinal;
        EXCEPTION WHEN others THEN
            UPDATE rvbbit.catalog_crawl_progress
               SET status = 'error', error = SQLERRM, finished_at = now()
             WHERE run_id = p_run AND ordinal = rec.ordinal;
        END;
        COMMIT;
    END LOOP;
END $fn$;

-- Parallel crawler. Same surface as catalog_crawl_run plus `parallelism`
-- (default 4; 1 = serial, no dblink). Records the run in rvbbit.catalog_runs and
-- live progress in rvbbit.catalog_crawl_progress, exactly like the serial form.
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
