-- 0005_cubes_enrich — the cube SEMANTIC LAYER (V2).
--
-- V1 made cubes fast (materialized rvbbit tables) and findable (a kind='cube' catalog node
-- with a best-effort embedding of name+grain+raw column types). V2 makes them UNDERSTOOD:
-- a decoupled enrich pass drafts per-column docs + a grain + a description with an LLM, stores
-- them in rvbbit.cube_columns (human-editable), folds them back into a far richer embedding,
-- and surfaces them (plus lineage + a row sample) through describe_cube. The column semantics
-- are the moat — what stops the agent inventing acct_xref_v2_amt. See docs/CUBES_PLAN.md §7-8.
--
-- Everything here is additive + idempotent (CREATE OR REPLACE / IF NOT EXISTS / ALTER ADD IF
-- NOT EXISTS); it reuses existing infra only — create_operator + _exec_op_jsonb (the same LLM
-- path as rvbbit.triples), rvbbit.embed, the catalog_docs vector space — so it needs no Rust
-- rebuild.

-- ── per-column semantic layer (LLM-drafted, human-editable) ─────────────────
CREATE TABLE IF NOT EXISTS rvbbit.cube_columns (
    cube_name   text        NOT NULL,
    column_name text        NOT NULL,
    data_type   text,
    doc         text,                 -- what the column IS
    semantics   text,                 -- what it MEANS / how to use it / caveats
    source_ref  text,                 -- where it comes from (raw table.column or expr), best-effort
    confidence  real,                 -- LLM self-rated [0,1]; low = flag for review
    edited_by   text,                 -- NULL = LLM-drafted; set when a human corrects it (Inspector)
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cube_name, column_name)
);

-- cube-level auto enrichment lives in the MUTABLE control row (never touches the immutable,
-- versioned cube_defs — enrichment is not a redefinition of the SQL).
ALTER TABLE rvbbit.cube_control ADD COLUMN IF NOT EXISTS auto_description text;
ALTER TABLE rvbbit.cube_control ADD COLUMN IF NOT EXISTS auto_grain       text;
ALTER TABLE rvbbit.cube_control ADD COLUMN IF NOT EXISTS enriched_at      timestamptz;

-- ── lineage: the real base relations a cube reads (raw + rvbbit) ────────────
-- route_explain only reports rvbbit-accelerated tables; a cube's sources are usually RAW
-- tables, so we read them straight off the planner via EXPLAIN (FORMAT JSON) — Postgres did
-- the parsing, and it catches raw tables, joins and CTEs' base scans. Best-effort: any plan
-- failure yields {}.
-- VOLATILE (not STABLE): EXPLAIN is disallowed inside a non-volatile function.
CREATE OR REPLACE FUNCTION rvbbit._cube_source_tables(p_sql text)
RETURNS text[] LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_json json; v_plan jsonb; v_tables text[];
BEGIN
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN RETURN '{}'::text[]; END IF;
    -- VERBOSE so scan nodes carry "Schema" (schema-qualified lineage); FORMAT JSON so we can
    -- walk the plan tree for every base relation it scans.
    BEGIN
        EXECUTE 'EXPLAIN (FORMAT JSON, VERBOSE) ' || rtrim(btrim(p_sql), ';') INTO v_json;
    EXCEPTION WHEN OTHERS THEN
        RETURN '{}'::text[];
    END;
    v_plan := v_json::jsonb;
    SELECT array_agg(DISTINCT t ORDER BY t) INTO v_tables FROM (
        SELECT CASE WHEN nullif(node->>'Schema','') IS NOT NULL
                    THEN (node->>'Schema') || '.' || (node->>'Relation Name')
                    ELSE (node->>'Relation Name') END AS t
        FROM jsonb_path_query(v_plan, '$.** ? (exists(@."Relation Name"))') AS node
        WHERE nullif(node->>'Relation Name','') IS NOT NULL
    ) s WHERE t IS NOT NULL;
    RETURN coalesce(v_tables, '{}'::text[]);
END $fn$;

-- ── the enrich LLM operator (one call per cube -> strict JSON) ──────────────
-- Same machinery as rvbbit.triples: a scalar jsonb operator (parser=json) with a retry
-- validator. Editable in rvbbit.operators like any other operator.
DO $do$
BEGIN
    PERFORM rvbbit.create_operator(
        op_name        => 'cube_enrich',
        op_shape       => 'scalar',
        op_arg_names   => ARRAY['context'],
        op_arg_types   => ARRAY['text'],
        op_return_type => 'jsonb',
        op_model       => 'openai/gpt-5.4-mini',
        op_parser      => 'json',
        op_max_tokens  => 6000,
        op_temperature => 0.0,
        op_description =>
            'Draft a cube''s semantic layer (description + grain + per-column docs) as strict ' ||
            'JSON from its SQL, a row sample, and its source tables'' catalog docs. Used by ' ||
            'rvbbit.enrich_cube.',
        op_system =>
            'You document a data "cube" (a wide, curated analytical table) so an analyst or AI ' ||
            'agent can use it without guessing. You are given a JSON context: the cube name, any ' ||
            'human-written description/grain, the cube SQL, the column list (name + type), a small ' ||
            'sample of rows, and the source tables'' existing docs. ' ||
            'Return ONLY a JSON object with EXACTLY these keys: ' ||
            '"description" (1-3 sentences: what this cube is and what questions it answers), ' ||
            '"grain" (one sentence: what one row represents, e.g. "one row per opportunity"), ' ||
            'and "columns" (a JSON array). Each columns item MUST have: "name" (copied verbatim ' ||
            'from the provided column list), "doc" (what the column is, plain language), ' ||
            '"semantics" (what it means / how to use it / units / gotchas; "" if nothing to add), ' ||
            '"source_ref" (the raw source table.column or a short note like "derived: sum(amount)"; ' ||
            '"" if unknown), and "confidence" (0.0-1.0, your certainty about this column). ' ||
            'Document EVERY provided column exactly once; do not invent columns not in the list. ' ||
            'Prefer the human description/grain when present, refining not contradicting it. ' ||
            'No markdown, no code fence, no commentary — JSON object only.',
        op_user =>
            E'CUBE CONTEXT (JSON):\n{{ context }}\n\nReturn the enrichment JSON object only.',
        op_tests => NULL
    );

    PERFORM rvbbit.set_operator_retry(
        'cube_enrich',
        $cfg${
          "until": {"function": "rvbbit.cube_enrich_valid"},
          "max_attempts": 3,
          "instructions": "Your previous output was invalid. Return ONLY a JSON object with keys description, grain, and columns (a non-empty array). Each columns item must have a non-empty name copied from the provided column list, plus doc, semantics, source_ref and a numeric confidence. No markdown or extra keys."
        }$cfg$::jsonb
    );
END $do$;

-- retry validator: the raw LLM text must parse to an object carrying a non-empty columns
-- array of named items. Signature mirrors rvbbit.triples_valid (output text, inputs jsonb) —
-- the retry harness calls it with the raw output BEFORE json parsing.
CREATE OR REPLACE FUNCTION rvbbit.cube_enrich_valid(output text, inputs jsonb DEFAULT '{}'::jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE doc jsonb;
BEGIN
    IF output IS NULL OR btrim(output) = '' THEN RETURN false; END IF;
    BEGIN
        doc := output::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN false;
    END;
    RETURN jsonb_typeof(doc) = 'object'
       AND jsonb_typeof(doc->'columns') = 'array'
       AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(doc->'columns') c
             WHERE nullif(btrim(c->>'name'), '') IS NOT NULL);
END $$;

-- ── enrich_cube: draft + persist the semantic layer, then re-embed richer ───
CREATE OR REPLACE FUNCTION rvbbit.enrich_cube(
    p_name             text,
    p_sample_rows      int     DEFAULT 12,
    p_overwrite_edited boolean DEFAULT false
) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE
    v_qual    text := 'cubes.' || quote_ident(p_name);
    v_sql text; v_desc text; v_grain text;
    v_cols jsonb; v_sample jsonb; v_sources jsonb := '[]'::jsonb;
    v_src text[]; s text; sch text; tbl text; v_src_doc text;
    v_ctx text; v_out jsonb; v_col jsonb;
    v_n int := 0; v_auto_desc text; v_auto_grain text;
BEGIN
    IF to_regclass(v_qual) IS NULL THEN
        RAISE EXCEPTION 'rvbbit.enrich_cube: cube % does not exist (define it first)', p_name;
    END IF;
    SELECT sql, description, grain INTO v_sql, v_desc, v_grain
      FROM rvbbit.cube_catalog WHERE name = p_name;
    IF v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.enrich_cube: no definition for cube %', p_name;
    END IF;

    -- columns (name + type)
    SELECT jsonb_agg(jsonb_build_object('name', column_name, 'type', data_type)
                     ORDER BY ordinal_position)
      INTO v_cols
      FROM information_schema.columns
     WHERE table_schema = 'cubes' AND table_name = p_name;

    -- clamped row sample (every cell truncated to 200 chars to bound prompt size)
    BEGIN
        EXECUTE format(
            'SELECT jsonb_agg(r) FROM (SELECT (SELECT jsonb_object_agg(k, left(v, 200)) '
            'FROM jsonb_each_text(to_jsonb(t)) e(k, v)) AS r FROM %s t LIMIT %s) z',
            v_qual, greatest(coalesce(p_sample_rows, 12), 1))
        INTO v_sample;
    EXCEPTION WHEN OTHERS THEN
        v_sample := NULL;
    END;

    -- source tables + their existing catalog docs (lineage-grounded context)
    v_src := rvbbit._cube_source_tables(v_sql);
    FOREACH s IN ARRAY coalesce(v_src, '{}'::text[]) LOOP
        sch := split_part(s, '.', 1); tbl := split_part(s, '.', 2);
        IF tbl = '' THEN tbl := sch; sch := NULL; END IF;
        v_src_doc := NULL;
        BEGIN
            SELECT doc INTO v_src_doc
              FROM rvbbit.catalog_docs
             WHERE rel_name = tbl AND col_name IS NULL
               AND (sch IS NULL OR schema_name = sch)
             ORDER BY updated_at DESC NULLS LAST LIMIT 1;
        EXCEPTION WHEN OTHERS THEN
            v_src_doc := NULL;
        END;
        v_sources := v_sources || jsonb_build_object('table', s, 'doc', v_src_doc);
    END LOOP;

    v_ctx := jsonb_build_object(
        'cube', p_name,
        'description', v_desc,
        'grain', v_grain,
        'sql', v_sql,
        'columns', coalesce(v_cols, '[]'::jsonb),
        'sample_rows', coalesce(v_sample, '[]'::jsonb),
        'source_tables', v_sources
    )::text;

    v_out := rvbbit.cube_enrich(v_ctx);   -- the LLM call (validated + retried)
    IF v_out IS NULL OR jsonb_typeof(v_out->'columns') <> 'array' THEN
        RAISE EXCEPTION 'rvbbit.enrich_cube: enrichment returned no columns for %', p_name;
    END IF;

    v_auto_desc  := nullif(btrim(v_out->>'description'), '');
    v_auto_grain := nullif(btrim(v_out->>'grain'), '');

    -- upsert per-column docs (only for columns that actually exist on the cube;
    -- preserve human edits unless p_overwrite_edited)
    FOR v_col IN SELECT value FROM jsonb_array_elements(v_out->'columns') LOOP
        CONTINUE WHEN nullif(btrim(v_col->>'name'), '') IS NULL;
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'cubes' AND table_name = p_name
               AND column_name = v_col->>'name');
        INSERT INTO rvbbit.cube_columns
            (cube_name, column_name, data_type, doc, semantics, source_ref, confidence, edited_by, updated_at)
        SELECT p_name, v_col->>'name',
               (SELECT data_type FROM information_schema.columns
                 WHERE table_schema = 'cubes' AND table_name = p_name
                   AND column_name = v_col->>'name'),
               nullif(btrim(v_col->>'doc'), ''),
               nullif(btrim(v_col->>'semantics'), ''),
               nullif(btrim(v_col->>'source_ref'), ''),
               least(greatest(coalesce((v_col->>'confidence')::real, 0.5), 0), 1),
               NULL, now()
        ON CONFLICT (cube_name, column_name) DO UPDATE SET
            data_type  = EXCLUDED.data_type,
            doc        = CASE WHEN cube_columns.edited_by IS NOT NULL AND NOT p_overwrite_edited
                              THEN cube_columns.doc ELSE EXCLUDED.doc END,
            semantics  = CASE WHEN cube_columns.edited_by IS NOT NULL AND NOT p_overwrite_edited
                              THEN cube_columns.semantics ELSE EXCLUDED.semantics END,
            source_ref = CASE WHEN cube_columns.edited_by IS NOT NULL AND NOT p_overwrite_edited
                              THEN cube_columns.source_ref ELSE EXCLUDED.source_ref END,
            confidence = CASE WHEN cube_columns.edited_by IS NOT NULL AND NOT p_overwrite_edited
                              THEN cube_columns.confidence ELSE EXCLUDED.confidence END,
            updated_at = now();
        v_n := v_n + 1;
    END LOOP;

    UPDATE rvbbit.cube_control
       SET auto_description = v_auto_desc, auto_grain = v_auto_grain,
           enriched_at = now(), updated_at = now()
     WHERE cube_name = p_name;

    -- fold the new column docs into a far richer embedding
    BEGIN
        PERFORM rvbbit.register_cube_node(p_name);
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'rvbbit.enrich_cube: re-register % failed: %', p_name, SQLERRM;
    END;

    RETURN jsonb_build_object(
        'cube', p_name, 'columns_enriched', v_n,
        'description', v_auto_desc, 'grain', v_auto_grain,
        'source_tables', to_jsonb(coalesce(v_src, '{}'::text[])));
END $fn$;

-- a human (Cube Studio Inspector) corrects a drafted column doc; marks it edited so
-- subsequent enrich passes won't clobber it (unless forced).
CREATE OR REPLACE FUNCTION rvbbit.set_cube_column_doc(
    p_cube text, p_column text, p_doc text DEFAULT NULL,
    p_semantics text DEFAULT NULL, p_source_ref text DEFAULT NULL,
    p_editor text DEFAULT 'human'
) RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    INSERT INTO rvbbit.cube_columns
        (cube_name, column_name, data_type, doc, semantics, source_ref, confidence, edited_by, updated_at)
    VALUES (p_cube, p_column,
            (SELECT data_type FROM information_schema.columns
              WHERE table_schema = 'cubes' AND table_name = p_cube AND column_name = p_column),
            p_doc, p_semantics, p_source_ref, 1.0, coalesce(nullif(btrim(p_editor), ''), 'human'), now())
    ON CONFLICT (cube_name, column_name) DO UPDATE SET
        doc        = coalesce(EXCLUDED.doc, cube_columns.doc),
        semantics  = coalesce(EXCLUDED.semantics, cube_columns.semantics),
        source_ref = coalesce(EXCLUDED.source_ref, cube_columns.source_ref),
        confidence = 1.0,
        edited_by  = EXCLUDED.edited_by,
        updated_at = now();
    BEGIN
        PERFORM rvbbit.register_cube_node(p_cube);   -- re-embed with the corrected doc
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
END $fn$;

-- ── richer catalog node: embed the curated docs, not just raw types ─────────
CREATE OR REPLACE FUNCTION rvbbit.register_cube_node(p_name text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
    v_node bigint; v_doc text; v_desc text; v_grain text; v_cols text;
    v_auto_desc text; v_auto_grain text;
    v_vec real[]; v_graph text := 'db_catalog';
BEGIN
    SELECT description, grain INTO v_desc, v_grain FROM rvbbit.cube_catalog WHERE name = p_name;
    SELECT auto_description, auto_grain INTO v_auto_desc, v_auto_grain
      FROM rvbbit.cube_control WHERE cube_name = p_name;
    v_desc  := coalesce(nullif(btrim(v_desc), ''),  nullif(btrim(v_auto_desc), ''));
    v_grain := coalesce(nullif(btrim(v_grain), ''), nullif(btrim(v_auto_grain), ''));

    -- prefer the enriched per-column docs; fall back to raw name (type)
    SELECT string_agg(
             cc.column_name
             || coalesce(' — ' || cc.doc, '')
             || coalesce(' [' || cc.semantics || ']', ''),
             E'\n' ORDER BY cc.column_name)
      INTO v_cols
      FROM rvbbit.cube_columns cc WHERE cc.cube_name = p_name;
    IF v_cols IS NULL THEN
        SELECT string_agg(column_name || ' (' || data_type || ')', ', ' ORDER BY ordinal_position)
          INTO v_cols
          FROM information_schema.columns
         WHERE table_schema = 'cubes' AND table_name = p_name;
    END IF;

    v_doc := format('Cube cubes.%s — %s. Grain: %s.%sColumns:%s%s',
                    p_name, coalesce(v_desc, '(no description)'),
                    coalesce(v_grain, 'unspecified'),
                    E'\n', E'\n', coalesce(v_cols, ''));

    v_node := rvbbit.kg_assert_node('cube', 'cubes.' || p_name,
                jsonb_build_object('schema', 'cubes', 'cube_name', p_name,
                                   'grain', v_grain, 'description', v_desc),
                1.0, '', 0.0, v_graph);
    BEGIN
        v_vec := rvbbit.embed(v_doc, '', 'document');
    EXCEPTION WHEN OTHERS THEN
        v_vec := NULL;            -- no embedder -> register lexically only
    END;
    INSERT INTO rvbbit.catalog_docs
        (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
    VALUES (v_node, v_graph, 'cube', 'cubes', p_name, NULL, v_doc, v_vec,
            CASE WHEN v_vec IS NOT NULL THEN now() END, now())
    ON CONFLICT (graph_id, node_id) DO UPDATE SET
        kind = EXCLUDED.kind, doc = EXCLUDED.doc, embedding = EXCLUDED.embedding,
        embedded_at = EXCLUDED.embedded_at, updated_at = now();
END $fn$;

-- drop_cube gains cube_columns cleanup (the table didn't exist in V1's drop_cube).
CREATE OR REPLACE FUNCTION rvbbit.drop_cube(p_name text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE v_qual text := 'cubes.' || quote_ident(p_name);
BEGIN
    IF to_regclass(v_qual) IS NOT NULL THEN
        EXECUTE format('DROP TABLE %s', v_qual);
    END IF;
    DELETE FROM rvbbit.cube_defs    WHERE name = p_name;
    DELETE FROM rvbbit.cube_control WHERE cube_name = p_name;
    DELETE FROM rvbbit.cube_columns WHERE cube_name = p_name;
    BEGIN
        DELETE FROM rvbbit.catalog_docs WHERE kind = 'cube' AND schema_name = 'cubes' AND rel_name = p_name;
        DELETE FROM rvbbit.kg_nodes     WHERE kind = 'cube' AND label = 'cubes.' || p_name;
    EXCEPTION WHEN OTHERS THEN NULL;   -- catalog may not be present
    END;
END $fn$;

-- ── describe_cube: now carries column docs + lineage + freshness + a sample ─
-- VOLATILE: transitively runs EXPLAIN (lineage) which is disallowed in a non-volatile function.
CREATE OR REPLACE FUNCTION rvbbit.describe_cube(p_name text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_qual text := 'cubes.' || quote_ident(p_name);
    v_sql text; v_sample jsonb; v_out jsonb;
BEGIN
    SELECT sql INTO v_sql FROM rvbbit.cube_catalog WHERE name = p_name;
    IF v_sql IS NULL THEN RETURN NULL; END IF;

    IF to_regclass(v_qual) IS NOT NULL THEN
        BEGIN
            EXECUTE format(
                'SELECT jsonb_agg(r) FROM (SELECT (SELECT jsonb_object_agg(k, left(v, 200)) '
                'FROM jsonb_each_text(to_jsonb(t)) e(k, v)) AS r FROM %s t LIMIT 5) z',
                v_qual) INTO v_sample;
        EXCEPTION WHEN OTHERS THEN
            v_sample := NULL;
        END;
    END IF;

    SELECT jsonb_build_object(
        'name', c.name,
        'description', coalesce(c.description, ctl.auto_description),
        'grain', coalesce(c.grain, ctl.auto_grain),
        'human_description', c.description, 'auto_description', ctl.auto_description,
        'category', c.category, 'version', c.version, 'sql', c.sql,
        'refresh_cron', c.refresh_cron, 'refreshed_at', ctl.refreshed_at,
        'rows', ctl.last_rows, 'enriched_at', ctl.enriched_at,
        'source_tables', to_jsonb(rvbbit._cube_source_tables(c.sql)),
        'columns', (
            SELECT jsonb_agg(jsonb_build_object(
                       'name', ic.column_name, 'type', ic.data_type,
                       'doc', cc.doc, 'semantics', cc.semantics,
                       'source_ref', cc.source_ref, 'confidence', cc.confidence,
                       'edited_by', cc.edited_by)
                   ORDER BY ic.ordinal_position)
            FROM information_schema.columns ic
            LEFT JOIN rvbbit.cube_columns cc
                   ON cc.cube_name = c.name AND cc.column_name = ic.column_name
            WHERE ic.table_schema = 'cubes' AND ic.table_name = c.name),
        'sample', coalesce(v_sample, '[]'::jsonb))
    INTO v_out
    FROM rvbbit.cube_catalog c
    LEFT JOIN rvbbit.cube_control ctl ON ctl.cube_name = c.name
    WHERE c.name = p_name;

    RETURN v_out;
END $fn$;
