-- =====================================================================
-- Self-Introspecting Catalog KG + Data Search  (Phase 1 + 2)
-- See docs/CATALOG_KG_PLAN.md
--
-- Crawls user tables, fingerprints them (structural stats + example
-- distinct values), materializes a `db_catalog` knowledge graph using the
-- existing rvbbit KG primitives, and exposes free-text KNN data search over
-- deterministic fingerprint documents.
--
-- Idempotent: safe to re-run via psql -f, and included in the extension via
-- crates/pg_rvbbit/src/catalog_kg.rs (extension_sql_file!).
--
-- Depends only on already-shipped functions: rvbbit.kg_assert_node /
-- kg_assert_edge / kg_link_evidence / kg_normalize_* and rvbbit.embed.
--
-- IMPORTANT: all KG writes pass match_threshold => 0.0 so kg_resolve_node
-- only runs the exact alias / label_norm tier (deterministic dedup, zero
-- embeddings). A positive threshold would trigger an O(N) per-row embedding
-- scan on every assert. See docs/CATALOG_KG_PLAN.md §3.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Stores
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rvbbit.catalog_runs (
    run_id        bigserial PRIMARY KEY,
    graph_id      text NOT NULL,
    status        text NOT NULL DEFAULT 'running',   -- running | ok | failed
    schemas       text[],
    tables_seen   bigint NOT NULL DEFAULT 0,
    columns_seen  bigint NOT NULL DEFAULT 0,
    edges_made    bigint NOT NULL DEFAULT 0,
    docs_embedded bigint NOT NULL DEFAULT 0,
    error         text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

-- One fingerprint document per catalog node (db_table / db_column), plus its
-- embedding. node_id joins back to rvbbit.kg_nodes.
CREATE TABLE IF NOT EXISTS rvbbit.catalog_docs (
    node_id      bigint NOT NULL,
    graph_id     text NOT NULL,
    kind         text NOT NULL,        -- db_table | db_column
    schema_name  text,
    rel_name     text,
    col_name     text,
    doc          text NOT NULL,
    embedding    real[],
    embedded_at  timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, node_id)
);

CREATE INDEX IF NOT EXISTS catalog_docs_kind_idx
    ON rvbbit.catalog_docs (graph_id, kind);

-- Append-only fingerprint history: one row per object (table/column) per crawl
-- run. The KG nodes hold current state; these snapshots are the drift history
-- layer. obj_key is the fully-qualified label and is the stable cross-run
-- identity (see docs/CATALOG_KG_PLAN.md §11).
CREATE TABLE IF NOT EXISTS rvbbit.catalog_snapshots (
    snapshot_id  bigserial PRIMARY KEY,
    run_id       bigint NOT NULL,
    graph_id     text NOT NULL,
    node_id      bigint,
    kind         text NOT NULL,        -- db_table | db_column
    schema_name  text,
    rel_name     text,
    col_name     text,
    obj_key      text NOT NULL,        -- schema.rel  or  schema.rel.col
    fingerprint  jsonb NOT NULL,
    embedding    real[],
    captured_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS catalog_snapshots_obj_idx
    ON rvbbit.catalog_snapshots (graph_id, obj_key, run_id);
CREATE INDEX IF NOT EXISTS catalog_snapshots_run_idx
    ON rvbbit.catalog_snapshots (run_id);

-- ---------------------------------------------------------------------
-- Fingerprint document builders (deterministic, no LLM)
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.catalog_table_doc(fp jsonb)
RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'Table ' || (fp->>'schema') || '.' || (fp->>'table')
        || ' — ' || COALESCE(fp->>'n_rows', '?') || ' rows.'
        || COALESCE(' ' || NULLIF(btrim(fp->>'comment'), '') || '.', '')
        || ' Columns: '
        || COALESCE((
              SELECT string_agg((c->>'name') || ' (' || (c->>'data_type') || ')', ', '
                                ORDER BY (c->>'ordinal')::int)
              FROM jsonb_array_elements(fp->'columns') c), '(none)')
        || '.'
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.catalog_column_doc(
    p_schema text, p_rel text, p_table_comment text, col jsonb)
RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'Column ' || p_schema || '.' || p_rel || '.' || (col->>'name')
        || ' (' || (col->>'data_type') || ')'
        || CASE WHEN (col->>'is_pk') = 'true' THEN ', primary key' ELSE '' END
        || CASE WHEN (col->>'is_fk') = 'true'
                THEN ', foreign key -> ' || COALESCE(col->>'fk_target', '?') ELSE '' END
        || '.'
        || CASE WHEN (col->>'ndv') IS NOT NULL
                THEN ' ' || (col->>'ndv') || ' distinct values.' ELSE '' END
        || CASE WHEN (col->>'null_frac') IS NOT NULL
                THEN ' ' || round((col->>'null_frac')::numeric * 100, 1)::text || '% null.'
                ELSE '' END
        -- Cap each example to 200 chars: a jsonb/text column's example_values
        -- can each be a multi-KB (here: multi-MB) blob, and concatenating them
        -- raw produced a 28 MB fingerprint doc that made lexical search crawl.
        -- A 200-char prefix keeps examples useful for embedding/keyword signal
        -- without unbounded growth. (Re-crawl to regenerate existing docs.)
        || COALESCE(' Examples: ' || NULLIF((
                SELECT string_agg(left(e->>'value', 200), ', ')
                FROM jsonb_array_elements(col->'example_values') e), '') || '.', '')
        || COALESCE(' Table comment: ' || NULLIF(btrim(p_table_comment), '') || '.', '')
        || ' In table ' || p_schema || '.' || p_rel || '.'
$fn$;

-- ---------------------------------------------------------------------
-- Per-relation structural fingerprint (heap-first, plain SQL)
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION rvbbit.catalog_fingerprint_table(
    rel regclass,
    sample_rows int DEFAULT 50000,
    examples_k  int DEFAULT 12)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_schema   text;
    v_table    text;
    v_relkind  "char";
    v_comment  text;
    v_size     bigint;
    v_nrows    bigint;
    v_nsampled bigint;
    v_sampled  boolean := false;
    v_src      text;
    v_pct      numeric;
    v_columns  jsonb := '[]'::jsonb;
    rc         record;
    v_seen     bigint;
    v_nonnull  bigint;
    v_nulls    bigint;
    v_nullfrac float8;
    v_ndv      bigint;
    v_min      text;
    v_max      text;
    v_examples jsonb;
    v_dist     jsonb;
    v_quantiles jsonb;
    distinct_cap int := 256;
BEGIN
    SELECT n.nspname, c.relname, c.relkind,
           obj_description(c.oid, 'pg_class'),
           pg_total_relation_size(c.oid)
      INTO v_schema, v_table, v_relkind, v_comment, v_size
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid = rel;

    EXECUTE format('SELECT count(*) FROM %s', rel) INTO v_nrows;

    IF v_nrows > sample_rows THEN
        v_sampled := true;
        IF v_relkind IN ('r', 'm') THEN
            v_pct := greatest(0.000001, least(100.0, 100.0 * sample_rows / NULLIF(v_nrows, 0)));
            v_src := format('(SELECT * FROM %s TABLESAMPLE SYSTEM (%s)) _s', rel, v_pct);
        ELSE
            v_src := format('(SELECT * FROM %s LIMIT %s) _s', rel, sample_rows);
        END IF;
    ELSE
        v_src := rel::text;
    END IF;

    EXECUTE format('SELECT count(*) FROM %s', v_src) INTO v_nsampled;

    FOR rc IN
        SELECT a.attnum,
               a.attname,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               (NOT a.attnotnull) AS nullable,
               pg_get_expr(ad.adbin, ad.adrelid) AS col_default,
               col_description(a.attrelid, a.attnum) AS col_comment,
               EXISTS (SELECT 1 FROM pg_constraint pc
                        WHERE pc.conrelid = a.attrelid AND pc.contype = 'p'
                          AND a.attnum = ANY (pc.conkey)) AS is_pk,
               (SELECT cn.nspname || '.' || cf.relname || '.' || af.attname
                  FROM pg_constraint fc
                  JOIN pg_class cf      ON cf.oid = fc.confrelid
                  JOIN pg_namespace cn  ON cn.oid = cf.relnamespace
                  JOIN pg_attribute af  ON af.attrelid = fc.confrelid
                       AND af.attnum = fc.confkey[array_position(fc.conkey, a.attnum)]
                 WHERE fc.conrelid = a.attrelid AND fc.contype = 'f'
                   AND a.attnum = ANY (fc.conkey)
                 LIMIT 1) AS fk_target
          FROM pg_attribute a
          LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
         WHERE a.attrelid = rel AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
    LOOP
        EXECUTE format('SELECT count(*), count(%I) FROM %s', rc.attname, v_src)
            INTO v_seen, v_nonnull;
        v_nulls := v_seen - v_nonnull;
        v_nullfrac := CASE WHEN v_seen > 0 THEN v_nulls::float8 / v_seen ELSE NULL END;

        BEGIN
            EXECUTE format('SELECT count(DISTINCT %I) FROM %s', rc.attname, v_src) INTO v_ndv;
        EXCEPTION WHEN others THEN v_ndv := NULL; END;

        BEGIN
            EXECUTE format('SELECT min(%I)::text, max(%I)::text FROM %s',
                           rc.attname, rc.attname, v_src) INTO v_min, v_max;
        EXCEPTION WHEN others THEN v_min := NULL; v_max := NULL; END;

        -- Value distribution: value -> count for up to distinct_cap values.
        -- Complete (every distinct value present) when ndv <= cap; powers exact
        -- new/lost value detection + PSI drift. example_values is derived from
        -- this so we scan once.
        v_dist := NULL;
        BEGIN
            EXECUTE format(
                $q$SELECT jsonb_object_agg(t.v, t.c)
                     FROM (SELECT %I::text AS v, count(*) AS c
                             FROM %s WHERE %I IS NOT NULL
                            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT %s) t$q$,
                rc.attname, v_src, rc.attname, distinct_cap) INTO v_dist;
        EXCEPTION WHEN others THEN v_dist := NULL; END;

        SELECT COALESCE(
                 jsonb_agg(jsonb_build_object('value', s.k, 'n', s.n) ORDER BY s.n DESC, s.k),
                 '[]'::jsonb)
          INTO v_examples
          FROM (SELECT d.key AS k, (d.value)::bigint AS n
                  FROM jsonb_each_text(COALESCE(v_dist, '{}'::jsonb)) AS d(key, value)
                 ORDER BY (d.value)::bigint DESC, d.key
                 LIMIT examples_k) s;

        -- Quantiles for numeric columns (non-numeric types raise -> NULL).
        v_quantiles := NULL;
        BEGIN
            EXECUTE format(
                $q$SELECT jsonb_build_object(
                      'p05', percentile_cont(0.05) WITHIN GROUP (ORDER BY %I),
                      'p25', percentile_cont(0.25) WITHIN GROUP (ORDER BY %I),
                      'p50', percentile_cont(0.50) WITHIN GROUP (ORDER BY %I),
                      'p75', percentile_cont(0.75) WITHIN GROUP (ORDER BY %I),
                      'p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY %I))
                     FROM %s WHERE %I IS NOT NULL$q$,
                rc.attname, rc.attname, rc.attname, rc.attname, rc.attname, v_src, rc.attname)
                INTO v_quantiles;
        EXCEPTION WHEN others THEN v_quantiles := NULL; END;

        v_columns := v_columns || jsonb_build_object(
            'name',          rc.attname,
            'ordinal',       rc.attnum,
            'data_type',     rc.data_type,
            'nullable',      rc.nullable,
            'default',       rc.col_default,
            'comment',       rc.col_comment,
            'is_pk',         rc.is_pk,
            'is_fk',         (rc.fk_target IS NOT NULL),
            'fk_target',     rc.fk_target,
            'n_seen',        v_seen,
            'n_nulls',       v_nulls,
            'null_frac',     v_nullfrac,
            'ndv',           v_ndv,
            'ndv_method',    CASE WHEN v_sampled THEN 'sampled' ELSE 'exact' END,
            'min',           v_min,
            'max',           v_max,
            'example_values', COALESCE(v_examples, '[]'::jsonb),
            'value_dist',     v_dist,
            'value_dist_complete', (v_ndv IS NOT NULL AND v_ndv <= distinct_cap),
            'quantiles',      v_quantiles);
    END LOOP;

    RETURN jsonb_build_object(
        'rel',         rel::text,
        'oid',         (rel::oid)::text,
        'schema',      v_schema,
        'table',       v_table,
        'relkind',     v_relkind,
        'comment',     v_comment,
        'size_bytes',  v_size,
        'n_rows',      v_nrows,
        'n_sampled',   v_nsampled,
        'sampled',     v_sampled,
        'n_columns',   jsonb_array_length(v_columns),
        'columns',     v_columns,
        'profiled_at', now());
END $fn$;

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

-- ---------------------------------------------------------------------
-- Free-text data search — HYBRID (dense + lexical, fused by Reciprocal
-- Rank Fusion).  Two ranker "seams" produce scored candidate lists; the
-- fusion is rank-based, so it never has to calibrate the squished cosine
-- band against the lexical scale, and irrelevant queries collapse to
-- nothing instead of returning everything at a flat 50%.
--
-- The seams (rvbbit.catalog_dense_knn / rvbbit.catalog_lexical_knn) are the
-- swap points: today the dense seam is mean-centered brute-force cosine over
-- real[]; later it can dispatch to pgvector HNSW / Lance with no change to
-- the fusion below.
--
-- Tunable via session settings (no rebuild):
--   SET rvbbit.search_query_prefix = 'Represent this sentence for searching relevant passages: ';
--       -- BGE/Nomic-style query instruction; default '' (no prefix)
--   SET rvbbit.search_dense_floor  = '0.10';   -- centered-cosine floor; raise to be stricter
-- ---------------------------------------------------------------------

-- Dense ranker seam: mean-centered cosine over the stored real[] embeddings.
-- Subtracting the corpus mean removes the anisotropy common-mode that pins raw
-- cosines into a narrow high band, so an unrelated query's scores collapse
-- toward 0 (and are dropped by min_score) while real matches stay positive.
-- Returns the top-k candidates ABOVE the floor, scored by centered cosine.
CREATE OR REPLACE FUNCTION rvbbit.catalog_dense_knn(
    q_vec     real[],
    graph     text,
    kinds     text[],
    k         int,
    min_score float8 DEFAULT 0.10)
RETURNS TABLE (node_id bigint, score float8)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_mu  real[];
    v_qc  real[];
    v_cnt bigint;
    -- Brute-force mean+cosine is O(N*D) per call (recomputed every search). Above
    -- this many same-dim embedded docs the dense path degrades to empty so the
    -- lexical ranker still answers fast — until a cached-mean / ANN tier
    -- (pgvector / Lance) lands behind this seam (Track B). Generous for catalogs.
    v_max constant int := 10000;
BEGIN
    IF q_vec IS NULL OR array_length(q_vec, 1) IS NULL THEN RETURN; END IF;

    SELECT count(*) INTO v_cnt
      FROM rvbbit.catalog_docs d
     WHERE d.graph_id = graph
       AND (kinds IS NULL OR d.kind = ANY (kinds))
       AND d.embedding IS NOT NULL
       AND array_length(d.embedding, 1) = array_length(q_vec, 1);
    IF v_cnt = 0 OR v_cnt > v_max THEN RETURN; END IF;

    -- corpus mean embedding over the candidate set (one element per dimension).
    -- Restrict to docs whose embedding dimensionality matches the query, so a
    -- mixed-dimension corpus (model swap / partial re-embed) can't skew the mean
    -- or misalign the parallel unnest below — the dense path simply returns empty
    -- (graceful degrade to lexical-only) if nothing matches the query dim.
    SELECT array_agg(m ORDER BY i) INTO v_mu
      FROM (SELECT t.i, avg(t.e)::real AS m
              FROM rvbbit.catalog_docs d
                   CROSS JOIN LATERAL unnest(d.embedding) WITH ORDINALITY AS t(e, i)
             WHERE d.graph_id = graph
               AND (kinds IS NULL OR d.kind = ANY (kinds))
               AND d.embedding IS NOT NULL
               AND array_length(d.embedding, 1) = array_length(q_vec, 1)
             GROUP BY t.i) z;

    IF v_mu IS NULL THEN RETURN; END IF;

    -- center the query once: q - mu
    SELECT array_agg(u.qe - u.me ORDER BY u.i) INTO v_qc
      FROM unnest(q_vec, v_mu) WITH ORDINALITY AS u(qe, me, i);

    RETURN QUERY
        WITH scored AS (
            SELECT d.node_id,
                   (SELECT sum((x.de - x.me) * x.qc)
                           / NULLIF(sqrt(sum((x.de - x.me) * (x.de - x.me)))
                                    * sqrt(sum(x.qc * x.qc)), 0)
                      FROM unnest(d.embedding, v_mu, v_qc) AS x(de, me, qc)) AS cos
              FROM rvbbit.catalog_docs d
             WHERE d.graph_id = graph
               AND (kinds IS NULL OR d.kind = ANY (kinds))
               AND d.embedding IS NOT NULL
               AND array_length(d.embedding, 1) = array_length(q_vec, 1)
        )
        SELECT s.node_id, s.cos
          FROM scored s
         WHERE s.cos IS NOT NULL AND s.cos > min_score
         ORDER BY s.cos DESC
         LIMIT k;
END $fn$;

-- Dense ranker DISPATCH SEAM (Track B): pick the fastest available dense tier
-- without any caller edit. Tier-1 = pgvector HNSW (when the `vector` type AND
-- the pgvector tier function both exist — added in P4); Tier-3 = the
-- mean-centered brute force above (always present). A Lance tier can slot in
-- the same way. Identical signature to catalog_dense_knn so it is a drop-in.
-- The reference to catalog_dense_knn_pgvector is late-bound (plpgsql plans each
-- statement on first execution) and guarded by to_regprocedure, so this is safe
-- to create and run on a box where that function does not exist yet.
CREATE OR REPLACE FUNCTION rvbbit.dense_knn_tiered(
    q_vec     real[],
    graph     text,
    kinds     text[],
    k         int,
    min_score float8 DEFAULT 0.10)
RETURNS TABLE (node_id bigint, score float8)
LANGUAGE plpgsql STABLE AS $fn$
BEGIN
    IF to_regtype('vector') IS NOT NULL
       AND to_regprocedure(
             'rvbbit.catalog_dense_knn_pgvector(real[],text,text[],integer,double precision)'
           ) IS NOT NULL
    THEN
        RETURN QUERY SELECT * FROM rvbbit.catalog_dense_knn_pgvector(q_vec, graph, kinds, k, min_score);
        RETURN;
    END IF;
    RETURN QUERY SELECT * FROM rvbbit.catalog_dense_knn(q_vec, graph, kinds, k, min_score);
END $fn$;

-- Lexical ranker seam: exact identifier/substring + Postgres FTS. Identifiers
-- (schema/rel/col) get their `_` and `.` flattened to spaces so the text-search
-- parser tokenizes snake_case names; a literal substring hit on the qualified
-- name is weighted highest (catches `npi`, `feet`, codes the dense model misses).
-- Returns only rows with SOME lexical signal.
CREATE OR REPLACE FUNCTION rvbbit.catalog_lexical_knn(
    query text,
    graph text,
    kinds text[],
    k     int)
RETURNS TABLE (node_id bigint, score float8)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_q text := btrim(COALESCE(query, ''));
BEGIN
    IF v_q = '' THEN RETURN; END IF;
    RETURN QUERY
        WITH src AS (
            -- Cap the doc fed to to_tsvector/position. A jsonb/text column can
            -- fingerprint into a multi-MB "Examples:" blob (seen: 28 MB), and
            -- re-tokenizing it per query costs seconds (and can blow the 1 MB
            -- tsvector limit). 32 KB keeps every legitimate fingerprint whole
            -- while making the pathological outlier a non-event. (Root fix is
            -- bounding doc length in the crawler; this is the search-side guard.)
            SELECT d.node_id, d.schema_name, d.rel_name, d.col_name,
                   left(d.doc, 32768) AS doc
              FROM rvbbit.catalog_docs d
             WHERE d.graph_id = graph
               AND (kinds IS NULL OR d.kind = ANY (kinds))
        ),
        lex AS (
            SELECT d.node_id,
                   ts_rank_cd(
                       to_tsvector('english',
                           d.doc || ' ' ||
                           replace(replace(
                               COALESCE(d.schema_name, '') || ' ' || COALESCE(d.rel_name, '') || ' '
                                 || COALESCE(d.col_name, ''),
                               '_', ' '), '.', ' ')),
                       websearch_to_tsquery('english', v_q)) AS fts,
                   -- position() = LITERAL substring (no LIKE wildcard interpretation), so an
                   -- identifier query like 'patient_id' can't have its '_' match any char.
                   (position(lower(v_q) IN
                       lower(COALESCE(d.schema_name, '') || '.' || COALESCE(d.rel_name, '') || '.'
                             || COALESCE(d.col_name, ''))) > 0)::int AS name_hit,
                   (position(lower(v_q) IN lower(d.doc)) > 0)::int    AS doc_hit
              FROM src d
        )
        SELECT lex.node_id,
               (2.0 * lex.name_hit + 1.0 * lex.doc_hit + lex.fts)::float8 AS sc
          FROM lex
         WHERE lex.fts > 0 OR lex.name_hit > 0 OR lex.doc_hit > 0
         ORDER BY sc DESC
         LIMIT k;
END $fn$;

-- Hybrid free-text search: fuse the two seams with Reciprocal Rank Fusion
-- (score = Σ 1/(C + rank)) and return the top-k by fused relevance. The
-- returned `score` is normalized to [0,1] (top hit = 1.0) so the UI shows a
-- relative match strength, not a raw cosine.  A query with no lexical signal
-- AND no above-floor dense match returns nothing.
CREATE OR REPLACE FUNCTION rvbbit.data_search(
    query  text,
    k      int    DEFAULT 20,
    kinds  text[] DEFAULT NULL,
    graph  text   DEFAULT 'db_catalog')
RETURNS TABLE (
    node_id     bigint,
    kind        text,
    schema_name text,
    rel_name    text,
    col_name    text,
    score       float8,
    doc         text)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_graph  text   := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    v_prefix text   := COALESCE(current_setting('rvbbit.search_query_prefix', true), '');
    v_floor  float8 := 0.10;
    v_q      real[];
    v_pool   int    := GREATEST(k * 4, 50);  -- candidate pool per ranker
    v_rrf    float8 := 60;                    -- RRF damping constant
BEGIN
    -- Read the floor GUC defensively: DECLARE initializers run before any
    -- handler is established, so a non-numeric value (e.g. SET ... = 'high')
    -- must be caught HERE or it kills data_search outright.
    BEGIN v_floor := COALESCE(NULLIF(current_setting('rvbbit.search_dense_floor', true), '')::float8, 0.10);
    EXCEPTION WHEN others THEN v_floor := 0.10; END;

    -- mode='query' → retrieval models get their query instruction automatically;
    -- v_prefix (the search_query_prefix GUC) stays as an optional extra override.
    BEGIN v_q := rvbbit.embed(v_prefix || query, '', 'query');
    EXCEPTION WHEN others THEN v_q := NULL; END;

    RETURN QUERY
        WITH d AS (   -- dense ranker via the tier seam (empty when no embedder)
            SELECT dk.node_id, row_number() OVER (ORDER BY dk.score DESC) AS r
              FROM rvbbit.dense_knn_tiered(v_q, v_graph, kinds, v_pool, v_floor) dk
        ),
        l AS (        -- lexical ranker
            SELECT lk.node_id, row_number() OVER (ORDER BY lk.score DESC) AS r
              FROM rvbbit.catalog_lexical_knn(query, v_graph, kinds, v_pool) lk
        ),
        fused AS (    -- Reciprocal Rank Fusion over the union of both rankers
            SELECT COALESCE(d.node_id, l.node_id) AS node_id,
                   COALESCE(1.0 / (v_rrf + d.r), 0) + COALESCE(1.0 / (v_rrf + l.r), 0) AS rrf
              FROM d FULL OUTER JOIN l ON d.node_id = l.node_id
        ),
        ranked AS (
            SELECT f.node_id, f.rrf, max(f.rrf) OVER () AS top
              FROM fused f
        )
        SELECT dd.node_id, dd.kind, dd.schema_name, dd.rel_name, dd.col_name,
               (r.rrf / NULLIF(r.top, 0))::float8 AS score,
               dd.doc
          FROM ranked r
          JOIN rvbbit.catalog_docs dd
            ON dd.graph_id = v_graph AND dd.node_id = r.node_id
         ORDER BY r.rrf DESC
         LIMIT k;
END $fn$;

-- ════════════════════════════════════════════════════════════════════════
-- data_crawl — the DATA-graph crawler (sibling of catalog_crawl)
-- ════════════════════════════════════════════════════════════════════════
-- Where catalog_crawl maps a database's STRUCTURE (schema/fingerprints) into the
-- db_catalog KG, data_crawl mines its CONTENT: it samples rows, extracts
-- entity/relationship triples from each row's text (via rvbbit.triples_row), and
-- asserts them as edges into a separate graph (default 'data_kg') with built-in
-- embedding entity-resolution (kg_assert_edge match_threshold). The resulting
-- entities are mirrored into catalog_docs so data_search()/Scry's "data" layer
-- can semantically search and spider them — a meaning graph over arbitrary tables.
--
-- Noise controls (arbitrary user tables have no curated text): a generic-role
-- stoplist drops pronoun/reporter/filler "entities" (i, we, narrator, witness,…),
-- pure-numeric subjects/objects are skipped, and `where_sql` lets the caller
-- restrict the sampled rows. `reset` clears the prior graph for a clean rebuild.
CREATE OR REPLACE FUNCTION rvbbit.data_crawl(
    rel regclass,
    sample_size integer DEFAULT 50,
    focus text DEFAULT 'all',
    graph text DEFAULT 'data_kg',
    match_threshold double precision DEFAULT 0.92,
    specialist text DEFAULT '',
    where_sql text DEFAULT NULL,
    reset boolean DEFAULT true,
    pk_expr text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_graph text := rvbbit.kg_normalize_graph(COALESCE(NULLIF(btrim(graph),''),'data_kg'));
    v_rel text := rel::text;
    v_where text := COALESCE(NULLIF(btrim(where_sql),''), 'true');
    -- per-row provenance key: a real column (e.g. a report id) when the caller names
    -- one, else a content hash. NEVER ctid — over rvbbit-AM tables ctid is invalid
    -- (InvalidBlockNumber), collapsing all evidence to one sentinel source_pk and
    -- destroying the cross-row "frequency" signal.
    v_pk text := COALESCE(NULLIF(btrim(pk_expr),''), 'md5(to_jsonb(t)::text)');
    -- generic pronouns / reporter-roles / fillers — high-frequency, low-signal "entities"
    v_stop text[] := ARRAY['i','we','you','he','she','it','they','them','us','me','one',
        'narrator','observer','observers','speaker','speakers','witness','witnesses',
        'reporter','reporting person','author','person','people','someone','somebody',
        'true','false','none','n/a','na','unknown','yes','no','this','that','here','there',
        'this area','the area','the witness','the observer','the narrator','the speaker'];
    rec record; trip record; s text; p text; o text; edge_id bigint;
    v_rows int := 0; v_triples int := 0; v_nodes int := 0;
BEGIN
    IF reset THEN
        DELETE FROM rvbbit.kg_nodes WHERE graph_id = v_graph;
        DELETE FROM rvbbit.catalog_docs WHERE graph_id = v_graph;
    END IF;
    FOR rec IN EXECUTE format(
        'SELECT to_jsonb(t) AS rd, (%s)::text AS pk FROM %s t WHERE %s ORDER BY random() LIMIT %s',
        v_pk, v_rel, v_where, GREATEST(1, sample_size))
    LOOP
        v_rows := v_rows + 1;
        FOR trip IN SELECT subject, predicate, object FROM rvbbit.triples_row(rec.rd, focus) LOOP
            s := NULLIF(btrim(trip.subject),''); p := NULLIF(btrim(trip.predicate),''); o := NULLIF(btrim(trip.object),'');
            IF s IS NULL OR p IS NULL OR o IS NULL THEN CONTINUE; END IF;
            IF s ~ '^[-+]?[0-9]+(\.[0-9]+)?$' OR o ~ '^[-+]?[0-9]+(\.[0-9]+)?$' THEN CONTINUE; END IF;
            IF lower(s) = ANY(v_stop) OR lower(o) = ANY(v_stop) THEN CONTINUE; END IF;  -- specificity filter
            edge_id := rvbbit.kg_assert_edge('entity', s, p, 'entity', o, 1.0, '{}'::jsonb, '{}'::jsonb, specialist, match_threshold, v_graph);
            PERFORM rvbbit.kg_link_evidence(target_edge_id => edge_id, source_table => rel, source_pk => rec.pk,
                source_column => NULL, evidence_text => NULL, confidence => 1.0, properties => rec.rd, graph => v_graph);
            v_triples := v_triples + 1;
        END LOOP;
    END LOOP;
    INSERT INTO rvbbit.catalog_docs (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
    SELECT n.node_id, v_graph, n.kind, 'data', n.label, NULL, n.label, rvbbit.embed(n.label,'','document'), now(), now()
    FROM rvbbit.kg_nodes n WHERE n.graph_id = v_graph AND COALESCE(NULLIF(btrim(n.label),''),'') <> ''
    ON CONFLICT (graph_id, node_id) DO UPDATE SET doc=EXCLUDED.doc, embedding=EXCLUDED.embedding, updated_at=now();
    GET DIAGNOSTICS v_nodes = ROW_COUNT;
    RETURN jsonb_build_object('rows', v_rows, 'triples', v_triples, 'nodes', v_nodes, 'graph', v_graph, 'table', v_rel);
END $fn$;
