-- 0025_fk_inference — infer join edges by naming when no FKs are declared.
--
-- propose_cube's fk_edges came back empty on Fivetran/Salesforce data because those exports declare
-- NO foreign keys (pg_constraint is empty). _fk_edges returns declared FKs PLUS name-inferred ones
-- (a column <base>_id / <base>id whose <base> matches another candidate table — singular/plural and
-- the Fivetran _c suffix — that has an id column). These are HINTS for the drafter (marked
-- inferred:true), not authority. propose_cube now uses it. Additive + idempotent.

CREATE OR REPLACE FUNCTION rvbbit._fk_edges(p_tables text[])
RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
DECLARE v_oids oid[]; v_real jsonb; v_inferred jsonb;
BEGIN
    SELECT array_agg(o) INTO v_oids FROM (SELECT to_regclass(t)::oid AS o FROM unnest(p_tables) t) z WHERE o IS NOT NULL;
    IF v_oids IS NULL THEN RETURN '[]'::jsonb; END IF;

    -- declared FKs among the candidates (first column of each)
    SELECT jsonb_agg(jsonb_build_object(
             'from_table', pfn.nspname || '.' || pf.relname, 'from_column', a.attname,
             'to_table',   pcn.nspname || '.' || pc.relname, 'to_column',   a2.attname))
      INTO v_real
      FROM pg_constraint con
      JOIN pg_class pf      ON pf.oid  = con.conrelid
      JOIN pg_namespace pfn ON pfn.oid = pf.relnamespace
      JOIN pg_class pc      ON pc.oid  = con.confrelid
      JOIN pg_namespace pcn ON pcn.oid = pc.relnamespace
      JOIN pg_attribute a   ON a.attrelid  = con.conrelid  AND a.attnum  = con.conkey[1]
      JOIN pg_attribute a2  ON a2.attrelid = con.confrelid AND a2.attnum = con.confkey[1]
     WHERE con.contype = 'f' AND pf.oid = ANY(v_oids) AND pc.oid = ANY(v_oids);

    -- name-inferred: a <base>_id / <base>id column pointing at a candidate table named like <base>
    WITH cols AS (
        SELECT (n.nspname || '.' || c.relname) AS tbl, a.attname AS col,
               lower(regexp_replace(a.attname, '_?id$', '', 'i')) AS base
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = ANY(v_oids) AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attname ~* '_?id$' AND lower(a.attname) <> 'id'
    ),
    tbls AS (
        SELECT (n.nspname || '.' || c.relname) AS tbl,
               lower(regexp_replace(c.relname, '_c$', '', 'i')) AS bare,   -- strip Fivetran/SF _c suffix
               EXISTS (SELECT 1 FROM pg_attribute a2 WHERE a2.attrelid = c.oid AND lower(a2.attname) = 'id') AS has_id
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = ANY(v_oids)
    )
    SELECT jsonb_agg(DISTINCT jsonb_build_object(
             'from_table', cols.tbl, 'from_column', cols.col,
             'to_table', t.tbl, 'to_column', 'id', 'inferred', true))
      INTO v_inferred
      FROM cols
      JOIN tbls t ON t.has_id AND cols.tbl <> t.tbl
        AND (t.bare = cols.base OR t.bare = cols.base || 's'
             OR t.bare || 's' = cols.base OR rtrim(t.bare, 's') = rtrim(cols.base, 's'))
      -- don't duplicate an edge already declared
      WHERE NOT (coalesce(v_real, '[]'::jsonb) @> jsonb_build_array(jsonb_build_object(
             'from_table', cols.tbl, 'from_column', cols.col,
             'to_table', t.tbl, 'to_column', 'id')));

    RETURN coalesce(v_real, '[]'::jsonb) || coalesce(v_inferred, '[]'::jsonb);
END $fn$;

-- propose_cube uses _fk_edges (declared + inferred) for join hints.
CREATE OR REPLACE FUNCTION rvbbit.propose_cube(
    p_subject     text,
    p_seed_tables text[] DEFAULT NULL,
    p_schema      text   DEFAULT NULL,
    p_max_tables  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_cands text[]; v_fk jsonb;
    v_cols jsonb := '{}'::jsonb; v_docs jsonb := '{}'::jsonb;
    v_ctx text; v_out jsonb; s text; sch text; tbl text;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: subject is required';
    END IF;

    IF p_seed_tables IS NOT NULL AND cardinality(p_seed_tables) > 0 THEN
        v_cands := p_seed_tables;
    ELSE
        SELECT array_agg(DISTINCT schema_name || '.' || rel_name)
          INTO v_cands
          FROM rvbbit.data_search(p_subject, p_max_tables, ARRAY['db_table'], 'db_catalog')
         WHERE rel_name IS NOT NULL
           AND (p_schema IS NULL OR schema_name = p_schema);
        IF v_cands IS NULL OR cardinality(v_cands) = 0 THEN
            SELECT array_agg(t) INTO v_cands FROM (
                SELECT table_schema || '.' || table_name AS t
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
                  AND (p_schema IS NULL OR table_schema = p_schema)
                ORDER BY table_schema, table_name
                LIMIT p_max_tables) z;
        END IF;
    END IF;
    IF v_cands IS NULL OR cardinality(v_cands) = 0 THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: no candidate tables for subject % (pass p_seed_tables or run catalog_crawl)', p_subject;
    END IF;

    v_fk := rvbbit._fk_edges(v_cands);   -- declared + name-inferred join edges

    FOREACH s IN ARRAY v_cands LOOP
        IF to_regclass(s) IS NULL THEN CONTINUE; END IF;
        sch := split_part(s, '.', 1); tbl := split_part(s, '.', 2);
        IF tbl = '' THEN tbl := sch; sch := NULL; END IF;
        v_cols := v_cols || jsonb_build_object(s, rvbbit._column_profile(sch, tbl));
        v_docs := v_docs || jsonb_build_object(s, (
            SELECT left(doc, 500) FROM rvbbit.catalog_docs
              WHERE rel_name = tbl AND col_name IS NULL AND (sch IS NULL OR schema_name = sch)
              ORDER BY updated_at DESC NULLS LAST LIMIT 1));
    END LOOP;

    v_ctx := jsonb_build_object(
        'subject', p_subject,
        'candidate_tables', to_jsonb(v_cands),
        'fk_edges', coalesce(v_fk, '[]'::jsonb),
        'column_samples', v_cols,
        'source_docs', v_docs,
        'exemplars', rvbbit.get_proposal_exemplars('cube', p_subject, 4))::text;

    v_out := rvbbit.propose_cube_draft(v_ctx);
    IF v_out IS NULL
       OR nullif(btrim(v_out->>'name'), '') IS NULL
       OR nullif(btrim(v_out->>'sql'), '')  IS NULL THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: draft generation failed for subject %', p_subject;
    END IF;

    RETURN v_out || jsonb_build_object(
        'candidate_tables', to_jsonb(v_cands),
        'fk_edges', coalesce(v_fk, '[]'::jsonb));
END $fn$;
