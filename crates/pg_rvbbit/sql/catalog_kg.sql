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
        || COALESCE(' Examples: ' || NULLIF((
                SELECT string_agg(e->>'value', ', ')
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

        BEGIN
            EXECUTE format(
                $q$SELECT jsonb_agg(jsonb_build_object('value', t.v, 'n', t.c) ORDER BY t.c DESC, t.v)
                     FROM (SELECT %I::text AS v, count(*) AS c
                             FROM %s WHERE %I IS NOT NULL
                            GROUP BY 1 ORDER BY c DESC, 1 LIMIT %s) t$q$,
                rc.attname, v_src, rc.attname, examples_k) INTO v_examples;
        EXCEPTION WHEN others THEN v_examples := NULL; END;

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
            'example_values', COALESCE(v_examples, '[]'::jsonb));
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
-- ---------------------------------------------------------------------

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
            BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist);
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

        -- columns
        FOR v_col IN SELECT e FROM jsonb_array_elements(fp->'columns') AS e
        LOOP
            v_clabel := v_tlabel || '.' || (v_col->>'name');
            v_doc := rvbbit.catalog_column_doc(v_schema, v_table, v_comment, v_col);
            v_cnode := rvbbit.kg_assert_node('db_column', v_clabel,
                        jsonb_strip_nulls(v_col || jsonb_build_object(
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
                BEGIN v_vec := rvbbit.embed(v_doc, embed_specialist);
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

-- ---------------------------------------------------------------------
-- Free-text data search (brute-force cosine; ILIKE fallback)
-- ---------------------------------------------------------------------

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
    v_graph text := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    v_q     real[];
BEGIN
    BEGIN v_q := rvbbit.embed(query, '');
    EXCEPTION WHEN others THEN v_q := NULL; END;

    IF v_q IS NULL OR array_length(v_q, 1) IS NULL THEN
        -- No embedder available: degrade to substring ranking over the doc.
        RETURN QUERY
            SELECT d.node_id, d.kind, d.schema_name, d.rel_name, d.col_name,
                   NULL::float8 AS score, d.doc
              FROM rvbbit.catalog_docs d
             WHERE d.graph_id = v_graph
               AND (kinds IS NULL OR d.kind = ANY (kinds))
               AND d.doc ILIKE '%' || query || '%'
             ORDER BY length(d.doc)
             LIMIT k;
        RETURN;
    END IF;

    RETURN QUERY
        WITH scored AS (
            SELECT d.node_id, d.kind, d.schema_name, d.rel_name, d.col_name, d.doc,
                   CASE WHEN d.embedding IS NULL THEN NULL::float8
                        ELSE (SELECT sum(u.de * u.qe)
                                     / NULLIF(sqrt(sum(u.de * u.de)) * sqrt(sum(u.qe * u.qe)), 0)
                                FROM unnest(d.embedding, v_q) AS u(de, qe))
                   END AS score
              FROM rvbbit.catalog_docs d
             WHERE d.graph_id = v_graph
               AND (kinds IS NULL OR d.kind = ANY (kinds))
        )
        SELECT s.node_id, s.kind, s.schema_name, s.rel_name, s.col_name, s.score, s.doc
          FROM scored s
         WHERE s.score IS NOT NULL
         ORDER BY s.score DESC
         LIMIT k;
END $fn$;
