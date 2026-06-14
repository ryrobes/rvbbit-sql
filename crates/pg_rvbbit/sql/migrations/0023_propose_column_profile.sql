-- 0023_propose_column_profile — feed real column profiles (enums/null%/distinct) to the drafters.
--
-- propose_metric guessed status='Completed' when the real label is 'Successful' because the drafter
-- only saw column NAME+TYPE — never the actual values. The data already exists in pg_stats
-- (most_common_vals = the enum dictionary, null_frac, n_distinct); we just weren't putting it in the
-- propose context. _column_profile packages it, both drafters now include it, and the prompts tell
-- the model to use the EXACT literals from top_values. Kills the wrong-literal class at the source.
-- Reuses pg_stats (no scans). Additive + idempotent.

-- per-column profile: type + null% + distinct + the actual top values (the enum dictionary).
CREATE OR REPLACE FUNCTION rvbbit._column_profile(p_schema text, p_table text)
RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_agg(jsonb_build_object(
               'name', c.column_name, 'type', c.data_type,
               'null_pct', s.null_pct, 'distinct', s.n_distinct, 'top_values', s.top_vals)
               ORDER BY c.ordinal_position)
    FROM information_schema.columns c
    LEFT JOIN LATERAL (
        SELECT round((null_frac * 100)::numeric, 1) AS null_pct,
               n_distinct,
               (most_common_vals::text::text[])[1:8] AS top_vals
        FROM pg_stats
        WHERE schemaname = c.table_schema AND tablename = c.table_name AND attname = c.column_name
    ) s ON true
    WHERE c.table_name = p_table AND (p_schema IS NULL OR c.table_schema = p_schema);
$$;

-- ── propose_cube: column context now carries enums/null%/distinct ───────────
CREATE OR REPLACE FUNCTION rvbbit.propose_cube(
    p_subject     text,
    p_seed_tables text[] DEFAULT NULL,
    p_schema      text   DEFAULT NULL,
    p_max_tables  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_cands text[]; v_oids oid[]; v_fk jsonb;
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

    SELECT array_agg(o) INTO v_oids FROM (
        SELECT to_regclass(t)::oid AS o FROM unnest(v_cands) t
    ) z WHERE o IS NOT NULL;

    SELECT jsonb_agg(jsonb_build_object(
             'from_table', pfn.nspname || '.' || pf.relname, 'from_column', a.attname,
             'to_table',   pcn.nspname || '.' || pc.relname, 'to_column',   a2.attname))
      INTO v_fk
      FROM pg_constraint con
      JOIN pg_class pf      ON pf.oid  = con.conrelid
      JOIN pg_namespace pfn ON pfn.oid = pf.relnamespace
      JOIN pg_class pc      ON pc.oid  = con.confrelid
      JOIN pg_namespace pcn ON pcn.oid = pc.relnamespace
      JOIN pg_attribute a   ON a.attrelid  = con.conrelid  AND a.attnum  = con.conkey[1]
      JOIN pg_attribute a2  ON a2.attrelid = con.confrelid AND a2.attnum = con.confkey[1]
     WHERE con.contype = 'f' AND pf.oid = ANY(v_oids) AND pc.oid = ANY(v_oids);

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

-- ── propose_metric: column context now carries enums/null%/distinct ────────
CREATE OR REPLACE FUNCTION rvbbit.propose_metric(
    p_subject      text,
    p_seed_sources text[] DEFAULT NULL,
    p_schema       text   DEFAULT NULL,
    p_max_sources  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_srcs text[]; v_cols jsonb := '{}'::jsonb; v_docs jsonb := '{}'::jsonb; v_metrics jsonb;
    v_ctx text; v_out jsonb; s text; sch text; tbl text;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: subject is required';
    END IF;

    IF p_seed_sources IS NOT NULL AND cardinality(p_seed_sources) > 0 THEN
        v_srcs := p_seed_sources;
    ELSE
        SELECT array_agg(DISTINCT schema_name || '.' || rel_name)
          INTO v_srcs
          FROM rvbbit.data_search(p_subject, p_max_sources, ARRAY['cube', 'db_table'], 'db_catalog')
         WHERE rel_name IS NOT NULL
           AND (p_schema IS NULL OR schema_name = p_schema OR schema_name = 'cubes');
        IF v_srcs IS NULL OR cardinality(v_srcs) = 0 THEN
            SELECT array_agg(t) INTO v_srcs FROM (
                SELECT 'cubes.' || name AS t FROM rvbbit.cubes()
                UNION ALL
                SELECT table_schema || '.' || table_name
                  FROM information_schema.tables
                 WHERE table_type = 'BASE TABLE'
                   AND table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
                   AND (p_schema IS NULL OR table_schema = p_schema)
                LIMIT p_max_sources) z;
        END IF;
    END IF;
    IF v_srcs IS NULL OR cardinality(v_srcs) = 0 THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: no candidate sources for subject % (pass p_seed_sources or build cubes/crawl)', p_subject;
    END IF;

    FOREACH s IN ARRAY v_srcs LOOP
        IF to_regclass(s) IS NULL THEN CONTINUE; END IF;
        sch := split_part(s, '.', 1); tbl := split_part(s, '.', 2);
        IF tbl = '' THEN tbl := sch; sch := NULL; END IF;
        v_cols := v_cols || jsonb_build_object(s, rvbbit._column_profile(sch, tbl));
        v_docs := v_docs || jsonb_build_object(s, (
            SELECT left(doc, 500) FROM rvbbit.catalog_docs
              WHERE rel_name = tbl AND col_name IS NULL AND (sch IS NULL OR schema_name = sch)
              ORDER BY updated_at DESC NULLS LAST LIMIT 1));
    END LOOP;

    SELECT jsonb_agg(jsonb_build_object('name', name, 'grain', grain, 'description', description))
      INTO v_metrics
      FROM (SELECT name, grain, description FROM rvbbit.metric_catalog ORDER BY name LIMIT 20) m;

    v_ctx := jsonb_build_object(
        'subject', p_subject,
        'candidate_sources', to_jsonb(v_srcs),
        'columns', v_cols,
        'source_docs', v_docs,
        'existing_metrics', coalesce(v_metrics, '[]'::jsonb),
        'exemplars', rvbbit.get_proposal_exemplars('metric', p_subject, 4))::text;

    v_out := rvbbit.propose_metric_draft(v_ctx);
    IF v_out IS NULL
       OR nullif(btrim(v_out->>'name'), '') IS NULL
       OR nullif(btrim(v_out->>'sql'), '')  IS NULL THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: draft generation failed for subject %', p_subject;
    END IF;

    RETURN v_out || jsonb_build_object('candidate_sources', to_jsonb(v_srcs));
END $fn$;

-- tell the drafters the column profile now carries real values (idempotent append).
UPDATE rvbbit.operators
   SET system_prompt = system_prompt ||
       E'\nEach column entry carries "top_values" (the actual most-common literal values), "null_pct" ' ||
       'and "distinct". Use the EXACT literals from top_values in WHERE/filter predicates — never ' ||
       'guess a status/type/category string. Skip columns that are ~100% null.'
 WHERE name IN ('propose_cube_draft', 'propose_metric_draft')
   AND position('top_values' IN system_prompt) = 0;
