-- 0173: value contracts for enum-ish target columns (crime-kit experiment
-- finding #2). Shape checks passed while MEANING diverged: Boston filled
-- is_violent_flag with '1', NYC with 'F', and the plate matched neither.
--
-- Fixes, in layers:
--   spec  — a target column may declare "values": ["violent", ""] (its
--           closed vocabulary; empty string covers "none").
--   check — fitting_check probes a 1000-row sample of the mapping and
--           FAILS any values-declared column emitting out-of-vocabulary
--           values (NULL always allowed).
--   draft — fitting_draft teaches clover: when a column declares values,
--           DERIVE with a CASE over source columns — never pass a raw
--           city-local column through.

-- fitting_check v2: adds the value probe.
CREATE OR REPLACE FUNCTION rvbbit.fitting_check(
    p_kit text,
    p_target text,
    p_select_sql text
) RETURNS TABLE (check_name text, ok boolean, detail text)
LANGUAGE plpgsql
AS $fchk$
DECLARE
    t rvbbit.kit_targets%ROWTYPE;
    c jsonb;
    v_cols text[] := '{}';
    v_types text[] := '{}';
    v_idx int;
    v_allowed text[];
    v_bad text;
BEGIN
    SELECT * INTO t FROM rvbbit.kit_targets kt WHERE kt.kit = p_kit AND kt.target = p_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'fitting_check: no target %/%', p_kit, p_target;
    END IF;
    IF p_select_sql !~* '^[[:space:]]*(SELECT|WITH)\y' OR p_select_sql ~ ';' THEN
        check_name := 'shape'; ok := false; detail := 'mapping must be a single SELECT (no semicolons)';
        RETURN NEXT; RETURN;
    END IF;

    -- Column discovery via a zero-row probe into a temp view.
    BEGIN
        EXECUTE 'CREATE OR REPLACE TEMP VIEW _fitting_probe AS ' || p_select_sql;
    EXCEPTION WHEN others THEN
        check_name := 'runs'; ok := false; detail := SQLERRM;
        RETURN NEXT; RETURN;
    END;
    check_name := 'runs'; ok := true; detail := 'SELECT is valid';
    RETURN NEXT;

    SELECT array_agg(a.attname::text ORDER BY a.attnum),
           array_agg(format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum)
    INTO v_cols, v_types
    FROM pg_attribute a
    JOIN pg_class cl ON cl.oid = a.attrelid
    WHERE cl.relname = '_fitting_probe'
      AND cl.relnamespace = pg_my_temp_schema()
      AND a.attnum > 0 AND NOT a.attisdropped;

    FOR c IN SELECT * FROM jsonb_array_elements(t.columns) LOOP
        check_name := 'column ' || (c->>'name');
        v_idx := array_position(v_cols, c->>'name');
        IF v_idx IS NULL THEN
            ok := NOT coalesce((c->>'required')::boolean, true);
            detail := CASE WHEN ok THEN 'optional column absent' ELSE 'REQUIRED column missing from mapping' END;
        ELSE
            ok := true;
            detail := 'present as ' || v_types[v_idx] ||
                      CASE WHEN c->>'type' IS NOT NULL AND position(lower(c->>'type') IN lower(v_types[v_idx])) = 0
                           THEN ' (target expects ' || (c->>'type') || ' — verify)' ELSE '' END;
        END IF;
        RETURN NEXT;

        -- Value contract: sample the mapping; out-of-vocabulary values fail.
        IF v_idx IS NOT NULL AND jsonb_typeof(c->'values') = 'array' THEN
            SELECT array_agg(x) INTO v_allowed FROM jsonb_array_elements_text(c->'values') x;
            check_name := 'values ' || (c->>'name');
            BEGIN
                EXECUTE format(
                    'SELECT string_agg(DISTINCT left(v, 30), '', '') FROM (
                       SELECT %I::text AS v FROM _fitting_probe LIMIT 1000
                     ) s WHERE v IS NOT NULL AND NOT (v = ANY($1))',
                    c->>'name')
                INTO v_bad USING v_allowed;
                IF v_bad IS NULL THEN
                    ok := true;
                    detail := 'sampled values all within {' || array_to_string(v_allowed, ', ') || '}';
                ELSE
                    ok := false;
                    detail := 'out-of-vocabulary values: ' || left(v_bad, 120)
                           || ' — expected only {' || array_to_string(v_allowed, ', ')
                           || '}; derive with a CASE expression';
                END IF;
            EXCEPTION WHEN others THEN
                ok := false;
                detail := 'value probe failed: ' || SQLERRM;
            END;
            RETURN NEXT;
        END IF;
    END LOOP;
    EXECUTE 'DROP VIEW IF EXISTS _fitting_probe';
END
$fchk$;

-- fitting_draft v2: the prompt teaches value derivation.
CREATE OR REPLACE FUNCTION rvbbit.fitting_draft(
    p_kit text,
    p_target text,
    p_schema text,
    p_rel text,
    p_use_llm boolean DEFAULT true
) RETURNS TABLE (draft text, drafted_by text, note text)
LANGUAGE plpgsql
AS $fd$
DECLARE
    t rvbbit.kit_targets%ROWTYPE;
    v_src regclass;
    v_source_cols text;
    v_samples text := '';
    v_prompt text;
    v_reply text;
    r record;
    v_line text;
BEGIN
    SELECT * INTO t FROM rvbbit.kit_targets kt WHERE kt.kit = p_kit AND kt.target = p_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'fitting_draft: no target %/%', p_kit, p_target;
    END IF;
    v_src := format('%I.%I', p_schema, p_rel)::regclass;

    SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
    INTO v_source_cols
    FROM pg_attribute a
    WHERE a.attrelid = v_src AND a.attnum > 0 AND NOT a.attisdropped;

    IF p_use_llm THEN
        BEGIN
            FOR r IN EXECUTE format('SELECT to_jsonb(s) AS j FROM %s s LIMIT 3', v_src) LOOP
                SELECT string_agg(key || '=' || left(coalesce(value, ''), 60), ', ')
                INTO v_line FROM jsonb_each_text(r.j);
                v_samples := v_samples || '  ' || coalesce(v_line, '') || E'\n';
            END LOOP;

            v_prompt :=
                E'You write a single PostgreSQL SELECT that maps a source table onto a target view spec.\n'
                || E'Rules: output ONLY the SQL (no fences, no prose, no trailing semicolon). One SELECT, FROM exactly '
                || format('%I.%I', p_schema, p_rel) || E'.\n'
                || E'Every target column must appear, aliased with AS to its exact target name, in spec order. '
                || E'Use casts/expressions when a source column plausibly matches semantically even under a different name. '
                || E'IMPORTANT — columns listing "allowed values" have a CLOSED vocabulary: you must DERIVE those values with a '
                || E'CASE expression over whatever source columns carry the signal (codes, categories, flags, descriptions). '
                || E'Never pass a raw source column through to a values-constrained target — its city/domain-local codes will not conform. '
                || E'Prefer '''' (empty string) as the CASE fallback when the spec allows it.\n'
                || E'If nothing in the source plausibly maps: required columns get /* TODO map this */ NULL::<type> AS <name>, '
                || E'optional columns get NULL::<type> AS <name>. Never invent joins or subqueries.\n\n'
                || 'TARGET ' || t.target || ' — ' || coalesce(t.description, '') || E'\ncolumns:\n'
                || coalesce((SELECT string_agg('  ' || (c->>'name') || ' ' || coalesce(c->>'type', 'text')
                        || CASE WHEN coalesce((c->>'required')::boolean, true) THEN ' (required)' ELSE ' (optional)' END
                        || ' — ' || coalesce(c->>'description', '')
                        || CASE WHEN jsonb_typeof(c->'values') = 'array'
                                THEN ' [allowed values: ' || (SELECT string_agg('''' || x || '''', ', ') FROM jsonb_array_elements_text(c->'values') x) || ']'
                                ELSE '' END, E'\n' ORDER BY ord)
                    FROM jsonb_array_elements(t.columns) WITH ORDINALITY e(c, ord)), '')
                || E'\n\nSOURCE ' || p_schema || '.' || p_rel || E'\ncolumns: ' || coalesce(v_source_cols, '')
                || E'\nsample rows:\n' || v_samples;

            v_reply := rvbbit.clover_llm_ask(v_prompt, '{}'::jsonb);
            v_reply := btrim(regexp_replace(v_reply, '^\s*```[a-zA-Z]*\s*|\s*```\s*$', '', 'g'));
            v_reply := rtrim(btrim(v_reply), ';');
            IF v_reply ~* '^[[:space:]]*(SELECT|WITH)\y' AND v_reply !~ ';' THEN
                draft := v_reply;
                drafted_by := 'clover_llm';
                note := 'review the mapping — fitting_check is the judge';
                RETURN NEXT;
                RETURN;
            END IF;
            note := 'clover reply was not a plain SELECT — fell back to name-match';
        EXCEPTION WHEN others THEN
            note := 'clover unavailable (' || left(SQLERRM, 80) || ') — fell back to name-match';
        END;
    END IF;

    SELECT 'SELECT ' || string_agg(
             CASE WHEN s.attname IS NOT NULL THEN quote_ident(s.attname) || ' AS ' || quote_ident(e.c->>'name')
                  WHEN NOT coalesce((e.c->>'required')::boolean, true)
                       THEN 'NULL::' || coalesce(e.c->>'type', 'text') || ' AS ' || quote_ident(e.c->>'name')
                  ELSE '/* TODO map this */ NULL::' || coalesce(e.c->>'type', 'text') || ' AS ' || quote_ident(e.c->>'name') END,
             E',\n       ' ORDER BY e.ord)
           || E'\nFROM ' || format('%I.%I', p_schema, p_rel)
    INTO draft
    FROM jsonb_array_elements(t.columns) WITH ORDINALITY e(c, ord)
    LEFT JOIN (
        SELECT a.attname::text FROM pg_attribute a
        WHERE a.attrelid = v_src AND a.attnum > 0 AND NOT a.attisdropped
    ) s ON s.attname = e.c->>'name';
    drafted_by := 'name-match';
    note := coalesce(note, 'deterministic draft (LLM skipped)');
    RETURN NEXT;
END
$fd$;
