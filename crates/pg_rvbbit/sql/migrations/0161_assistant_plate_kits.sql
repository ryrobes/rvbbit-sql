-- 0161: teach the assistant that `kit` is the reactivity scope.
--
-- Field report: the assistant authored a leaderboard over demo_kit.field_notes
-- but left kit NULL, so it never refreshed when the intake plate (kit 'demo')
-- added a note — plates only auto-refresh SAME-KIT siblings after actions.
-- One anchored insert into the PLATES section of desktop_assistant_turn.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := E'\n- WORKFLOW: validate every plate query';
    v_rule text;
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0161: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0161: PLATES section absent (0160 not applied?); skipping';
        RETURN;
    END IF;
    IF position('REACTIVITY: kit is the sharing scope' IN v_system) > 0 THEN
        RAISE NOTICE '0161: reactivity rule already present; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0161: WORKFLOW anchor not found — prompt drifted, re-author this patch';
    END IF;

    v_rule := E'\n- REACTIVITY: kit is the sharing scope — after any plate ACTION writes data, every open plate with the SAME kit re-renders automatically; kit-less plates never hear siblings. When your new plate reads tables that another plate''s actions write, adopt that plate''s kit (SELECT plate_id, kit FROM rvbbit.plates to check). Only invent a new kit for a genuinely new family of surfaces.';

    v_system := replace(v_system, v_anchor, v_rule || v_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';

    RAISE NOTICE '0161: assistant now knows kit = reactivity scope (% chars)', length(v_system);
END
$patch$;
