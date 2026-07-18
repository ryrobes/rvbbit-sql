-- 0172: LLM-assisted fitting drafts (KIT_PLATES_PLAN §21 follow-through).
--
-- fitting_draft() is the one drafting entry point: it asks clover_llm to
-- map the source table onto the target spec (handles renames like
-- created_at → noted_at, casts, simple expressions), sanity-checks the
-- reply, and FALLS BACK to the deterministic name-match draft whenever
-- Clover is unavailable, slow, or returns something that isn't a plain
-- SELECT. The human always reviews; fitting_check remains the judge.
-- A few sample values (truncated) ride in the prompt so the model can see
-- shapes — the same trust boundary as every other clover_* operator.

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
                || E'If nothing in the source plausibly maps: required columns get /* TODO map this */ NULL::<type> AS <name>, '
                || E'optional columns get NULL::<type> AS <name>. Never invent joins or subqueries.\n\n'
                || 'TARGET ' || t.target || ' — ' || coalesce(t.description, '') || E'\ncolumns:\n'
                || coalesce((SELECT string_agg('  ' || (c->>'name') || ' ' || coalesce(c->>'type', 'text')
                        || CASE WHEN coalesce((c->>'required')::boolean, true) THEN ' (required)' ELSE ' (optional)' END
                        || ' — ' || coalesce(c->>'description', ''), E'\n' ORDER BY ord)
                    FROM jsonb_array_elements(t.columns) WITH ORDINALITY e(c, ord)), '')
                || E'\n\nSOURCE ' || p_schema || '.' || p_rel || E'\ncolumns: ' || coalesce(v_source_cols, '')
                || E'\nsample rows:\n' || v_samples;

            v_reply := rvbbit.clover_llm_ask(v_prompt, '{}'::jsonb);
            -- strip accidental fences / trailing semicolons
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

    -- Deterministic fallback: exact-name matches map through; missing
    -- required columns arrive as TODO placeholders.
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
