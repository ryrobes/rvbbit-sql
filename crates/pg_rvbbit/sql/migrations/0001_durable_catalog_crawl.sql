-- 0001_durable_catalog_crawl
--
-- Durable catalog crawler: a per-table COMMIT procedure (catalog_crawl_run) that
-- survives interruption with partial results + a live progress log, plus a
-- per-table worker and a lazy-ensure for its stateful table. Replaces the old
-- all-or-nothing single-transaction crawl. Idempotent (CREATE OR REPLACE /
-- IF NOT EXISTS), applied once and tracked in rvbbit.schema_migrations by
-- rvbbit.migrate(). Depends on the base catalog surfaces in sql/catalog_kg.sql
-- (catalog_runs, catalog_fingerprint_table, kg_assert_*, embed, …).

-- ---------------------------------------------------------------------
-- The crawler: enumerate user tables -> fingerprint -> KG + docs
--
-- Three layers:
--   * rvbbit._catalog_crawl_one(run, reloid, …)  — does ALL the work for ONE
--     table (fingerprint -> KG nodes/edges/evidence/docs/embedding/snapshots)
--     and returns per-table counters. No transaction control: the caller owns
--     the commit boundary. Single source of truth for per-table work.
--   * rvbbit.catalog_crawl(…) FUNCTION — the original all-or-nothing form: one
--     transaction over every table. Fine for small schemas, but if it is
--     cancelled or errors the whole run rolls back (you get nothing). Unchanged
--     signature + return shape, so existing callers keep working.
--   * rvbbit.catalog_crawl_run(…) PROCEDURE — the durable form: COMMITs after
--     each table and logs live progress to rvbbit.catalog_crawl_progress, so a
--     long crawl survives cancellation/restart with partial results intact and
--     can be watched table-by-table while it runs. Prefer this for big schemas
--     and from pg_cron:  CALL rvbbit.catalog_crawl_run();
-- ---------------------------------------------------------------------

-- Per-table progress log. One row per (run, table); the catalog_crawl_run
-- procedure writes + COMMITs the 'running' row BEFORE the heavy work, then
-- updates it to ok/error after — so
--   SELECT * FROM rvbbit.catalog_crawl_progress WHERE run_id = :r ORDER BY ordinal
-- shows exactly which table the crawl is on (i of N) and which are done/failed,
-- live, mid-run.
-- Idempotent bootstrap for the durable crawler's stateful object. The worker /
-- function / procedure below are all CREATE OR REPLACE, so (re)loading this file
-- always refreshes them — but the progress *table* can only be CREATEd, so an
-- install that predates it would be missing it. catalog_kg_ensure() guarantees
-- the table (and its extension membership) exists, and the crawl entrypoints
-- call it on first use, so a stale install self-heals with no migration file.
-- A session-local flag makes it a no-op after the first call per backend, so the
-- entrypoints can call it unconditionally for free.
CREATE OR REPLACE FUNCTION rvbbit.catalog_kg_ensure()
RETURNS void
LANGUAGE plpgsql
SET client_min_messages = warning   -- silence "relation already exists, skipping"
AS $ensure$
BEGIN
    IF current_setting('rvbbit.catalog_kg_ensured', true) = 'on' THEN
        RETURN;                                   -- already ensured in this backend
    END IF;
    PERFORM set_config('rvbbit.catalog_kg_ensured', 'on', false);

    CREATE TABLE IF NOT EXISTS rvbbit.catalog_crawl_progress (
        run_id       bigint  NOT NULL,
        ordinal      int     NOT NULL,        -- 1-based position within this run
        total        int     NOT NULL,        -- total tables in the run (for "i of N")
        reloid       oid,
        schema_name  text    NOT NULL,
        rel_name     text    NOT NULL,
        status       text    NOT NULL DEFAULT 'running',  -- running | ok | error
        n_columns    int,
        n_embedded   int,
        n_rows       bigint,
        error        text,
        started_at   timestamptz NOT NULL DEFAULT now(),
        finished_at  timestamptz,
        PRIMARY KEY (run_id, ordinal)
    );
    CREATE INDEX IF NOT EXISTS catalog_crawl_progress_run_idx
        ON rvbbit.catalog_crawl_progress (run_id, status);

    -- Attach the table to the extension if it's free-standing (created at runtime
    -- on an old install, it would otherwise be an orphan non-member that breaks a
    -- future ALTER EXTENSION UPDATE). Guarded + best-effort: a no-op when already
    -- a member, and harmless if ALTER EXTENSION isn't permitted in this context.
    BEGIN
        IF NOT EXISTS (
            SELECT 1
              FROM pg_depend d
              JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'pg_rvbbit'
             WHERE d.classid = 'pg_class'::regclass
               AND d.objid   = 'rvbbit.catalog_crawl_progress'::regclass
               AND d.deptype = 'e'
        ) THEN
            EXECUTE 'ALTER EXTENSION pg_rvbbit ADD TABLE rvbbit.catalog_crawl_progress';
        END IF;
    EXCEPTION WHEN others THEN NULL;
    END;
END $ensure$;

-- Materialize the table at install time (idempotent; safe to re-run on reload).
SELECT rvbbit.catalog_kg_ensure();

-- The unit of work: fingerprint + materialize ONE table (reloid) into the
-- KG/doc/snapshot layers under run p_run. Returns counters. Raises if the table
-- cannot be fingerprinted — the caller decides whether to skip (function) or
-- record the failure and continue (procedure). Embedding is best-effort per
-- table: a failed embed leaves embedding NULL and the crawl continues.
CREATE OR REPLACE FUNCTION rvbbit._catalog_crawl_one(
    p_run              bigint,
    p_reloid           oid,
    p_graph            text,
    p_sample_rows      int     DEFAULT 50000,
    p_examples_k       int     DEFAULT 12,
    p_do_embed         boolean DEFAULT true,
    p_embed_specialist text    DEFAULT '')
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

    -- schema node
    PERFORM rvbbit.kg_assert_node('db_schema', v_schema,
                jsonb_build_object('name', v_schema), 1.0, '', 0.0, p_graph);

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
                1.0, '', 0.0, p_graph);

    PERFORM rvbbit.kg_assert_edge('db_schema', v_schema, 'has_table',
                'db_table', v_tlabel, 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, p_graph);
    v_edges := v_edges + 1;

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

    -- table drift snapshot (table-level summary, columns array stripped)
    INSERT INTO rvbbit.catalog_snapshots
        (run_id, graph_id, node_id, kind, schema_name, rel_name, col_name, obj_key, fingerprint, embedding)
    VALUES (p_run, p_graph, v_tnode, 'db_table', v_schema, v_table, NULL, v_tlabel,
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
                    1.0, '', 0.0, p_graph);
        v_cols := v_cols + 1;

        PERFORM rvbbit.kg_assert_edge('db_table', v_tlabel, 'has_column',
                    'db_column', v_clabel, 1.0, '{}'::jsonb,
                    jsonb_build_object('ordinal', v_col->>'ordinal'), '', 0.0, p_graph);
        v_edges := v_edges + 1;

        IF (v_col->>'is_fk') = 'true' AND (v_col->>'fk_target') IS NOT NULL THEN
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

        -- column drift snapshot (full fingerprint incl. value_dist/quantiles)
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

-- All-or-nothing crawler (single transaction). Convenient for small schemas and
-- backward-compatible: same signature + return shape as before.
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
    r          jsonb;
    v_tables   bigint := 0;
    v_cols     bigint := 0;
    v_edges    bigint := 0;
    v_emb      bigint := 0;
BEGIN
    PERFORM rvbbit.catalog_kg_ensure();   -- self-heal the progress table on first use

    INSERT INTO rvbbit.catalog_runs (graph_id, status, schemas)
    VALUES (v_graph, 'running', schemas)
    RETURNING run_id INTO v_run;

    FOR rec IN
        SELECT c.oid AS reloid
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'm', 'v')
           AND NOT c.relispartition
           AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'rvbbit')
           AND n.nspname NOT LIKE 'pg_toast%'
           AND n.nspname NOT LIKE 'pg_temp_%'
           AND (schemas IS NULL OR n.nspname = ANY (schemas))
         ORDER BY n.nspname, c.relname
    LOOP
        BEGIN
            r := rvbbit._catalog_crawl_one(v_run, rec.reloid, v_graph,
                                           sample_rows, examples_k, do_embed, embed_specialist);
        EXCEPTION WHEN others THEN
            CONTINUE;   -- skip un-fingerprintable relation
        END;
        v_tables := v_tables + 1;
        v_cols   := v_cols  + (r->>'columns')::bigint;
        v_edges  := v_edges + (r->>'edges')::bigint;
        v_emb    := v_emb   + (r->>'docs_embedded')::bigint;
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

-- Durable crawler: COMMIT per table + live progress log. Survives cancellation
-- or a crash with partial results intact, and can be watched table-by-table via
-- rvbbit.catalog_crawl_progress while it runs. No return value (it's a
-- procedure) — read the rvbbit.catalog_runs row (or the progress rows) for
-- results. pg_cron-safe (cron does not wrap jobs in a transaction).
CREATE OR REPLACE PROCEDURE rvbbit.catalog_crawl_run(
    schemas          text[]  DEFAULT NULL,
    graph            text    DEFAULT 'db_catalog',
    sample_rows      int     DEFAULT 50000,
    examples_k       int     DEFAULT 12,
    do_embed         boolean DEFAULT true,
    embed_specialist text    DEFAULT '')
LANGUAGE plpgsql AS $fn$
DECLARE
    v_graph    text := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    v_run      bigint;
    v_oids     oid[];
    v_total    int;
    v_oid      oid;
    v_ord      int := 0;
    v_schema   text;
    v_table    text;
    r          jsonb;
    v_tables   bigint := 0;
    v_cols     bigint := 0;
    v_edges    bigint := 0;
    v_emb      bigint := 0;
BEGIN
    PERFORM rvbbit.catalog_kg_ensure();   -- self-heal the progress table on first use

    INSERT INTO rvbbit.catalog_runs (graph_id, status, schemas)
    VALUES (v_graph, 'running', schemas)
    RETURNING run_id INTO v_run;
    COMMIT;   -- the run row is visible immediately, before any heavy work

    -- Snapshot the target list up front: lets the loop COMMIT freely (a COMMIT
    -- inside a live FOR-over-query cursor is fragile) and gives us "i of N".
    SELECT array_agg(c.oid ORDER BY n.nspname, c.relname)
      INTO v_oids
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'm', 'v')
       AND NOT c.relispartition
       AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'rvbbit')
       AND n.nspname NOT LIKE 'pg_toast%'
       AND n.nspname NOT LIKE 'pg_temp_%'
       AND (schemas IS NULL OR n.nspname = ANY (schemas));
    v_total := COALESCE(array_length(v_oids, 1), 0);

    IF v_total = 0 THEN
        UPDATE rvbbit.catalog_runs SET status = 'ok', finished_at = now() WHERE run_id = v_run;
        COMMIT;
        RETURN;
    END IF;

    FOREACH v_oid IN ARRAY v_oids LOOP
        v_ord := v_ord + 1;

        -- the table may have been dropped since we snapshotted the list
        SELECT n.nspname, c.relname INTO v_schema, v_table
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.oid = v_oid;
        IF v_schema IS NULL THEN CONTINUE; END IF;

        -- record 'running' and COMMIT so the current table is visible while the
        -- (potentially slow) fingerprint + embed runs
        INSERT INTO rvbbit.catalog_crawl_progress
            (run_id, ordinal, total, reloid, schema_name, rel_name, status, started_at)
        VALUES (v_run, v_ord, v_total, v_oid, v_schema, v_table, 'running', now());
        COMMIT;

        BEGIN
            r := rvbbit._catalog_crawl_one(v_run, v_oid, v_graph,
                                           sample_rows, examples_k, do_embed, embed_specialist);

            v_tables := v_tables + 1;
            v_cols   := v_cols  + (r->>'columns')::bigint;
            v_edges  := v_edges + (r->>'edges')::bigint;
            v_emb    := v_emb   + (r->>'docs_embedded')::bigint;

            UPDATE rvbbit.catalog_crawl_progress
               SET status = 'ok', n_columns = (r->>'columns')::int,
                   n_embedded = (r->>'docs_embedded')::int,
                   n_rows = (r->>'n_rows')::bigint, finished_at = now()
             WHERE run_id = v_run AND ordinal = v_ord;

            UPDATE rvbbit.catalog_runs
               SET tables_seen = v_tables, columns_seen = v_cols,
                   edges_made = v_edges, docs_embedded = v_emb
             WHERE run_id = v_run;
        EXCEPTION WHEN others THEN
            -- this table's partial work was rolled back to the block's implicit
            -- savepoint; log the failure and keep going with the next table
            UPDATE rvbbit.catalog_crawl_progress
               SET status = 'error', error = SQLERRM, finished_at = now()
             WHERE run_id = v_run AND ordinal = v_ord;
        END;
        COMMIT;   -- persist this table's work (or its 'error' row) before the next
    END LOOP;

    UPDATE rvbbit.catalog_runs
       SET status = 'ok', tables_seen = v_tables, columns_seen = v_cols,
           edges_made = v_edges, docs_embedded = v_emb, finished_at = now()
     WHERE run_id = v_run;
    COMMIT;
END $fn$;
