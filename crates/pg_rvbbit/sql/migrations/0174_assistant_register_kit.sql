-- 0174: the assistant can register/re-register kits (crime-kit experiment
-- finding #1 — kit metadata was the one step it couldn't do itself).
-- Anchored prompt patch: register_kit joins the command examples and the
-- PLATES section explains when to use it.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_examples_anchor text := '{"op":"open_plate","plate_id":"team/my_surface"}';
    v_section_anchor text := E'\n- REACTIVITY: kit is the sharing scope';
    v_examples text;
    v_rule text;
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0174: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0174: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('"op":"register_kit"' IN v_system) > 0 THEN
        RAISE NOTICE '0174: register_kit already taught; skipping';
        RETURN;
    END IF;
    IF position(v_examples_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0174: open_plate example anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_section_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0174: REACTIVITY anchor not found — prompt drifted, re-author';
    END IF;

    v_examples := v_examples_anchor || E',\n    ' ||
        '{"op":"register_kit","kit":"crime","title":"Crime Analytics","description":"...","version":"0.1.0"}';

    v_rule := E'\n- KIT REGISTRY: when you create the FIRST plates of a new kit (or the user asks you to name/version one), also emit register_kit — it records title/version/description in rvbbit.kits so the shelf groups the kit properly and it can be exported. Re-register to bump version or reword; version DOWNGRADES are refused (the error will appear in apply_report). Contracts, targets, and setup DDL remain operator/Fitting-Room work.';

    v_system := replace(v_system, v_examples_anchor, v_examples);
    v_system := replace(v_system, v_section_anchor, v_rule || v_section_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0174: assistant can now register kits (% chars)', length(v_system);
END
$patch$;
