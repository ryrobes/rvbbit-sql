-- 0166: teach the assistant the switchboard logic tier — decision tables and
-- operator tie-ins (KIT_PLATES_PLAN §6). Anchored prompt insert, same pattern
-- as 0160/0161.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := E'\n- WORKFLOW: validate every plate query';
    v_rule text;
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0166: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0166: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('kit_rules DECISION TABLES' IN v_system) > 0 THEN
        RAISE NOTICE '0166: logic-tier rule already present; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0166: WORKFLOW anchor not found — prompt drifted, re-author this patch';
    END IF;

    v_rule := E'\n- COMPLEX LOGIC (plates trigger, they never think): (1) verdicts are COLUMNS when plain SQL can decide. (2) rvbbit.kit_rules DECISION TABLES when business rules should be rows a human can iterate on: SELECT rvbbit.upsert_kit_rule(kit, rule_set, rule_id, when_sql, verdict_jsonb, priority, description) — when_sql is one boolean EXPRESSION over `subject` (jsonb of the row); consume set-based with CROSS JOIN LATERAL rvbbit.rule_verdict(''<kit>'', ''<rule_set>'', to_jsonb(row_alias)) v — first match by priority wins, v.verdict carries your columns (label/tone/…), v.rule_id is provenance (surface it, e.g. title="rule: {{ row.rule_id }}"), a broken rule wins LOUDLY with {"rule_error":true}. Always add a priority-999 default rule with when_sql ''true''. (3) Multi-step or semantic logic belongs in OPERATORS; a plate action''s sql may call one (SELECT my_op({{arg}})) — project cost with explain_semantic first when models are involved. Keep kit helper operators out of global discovery with SELECT rvbbit.set_operator_kit(''<op>'', ''<kit>'', ''kit'') — they still execute; they just leave pickers and capability_search.';

    v_system := replace(v_system, v_anchor, v_rule || v_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';

    RAISE NOTICE '0166: assistant now knows the kit logic tier (% chars)', length(v_system);
END
$patch$;
