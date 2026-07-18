-- 0170: plates are an EXPLICIT pathway for the assistant (Ryan's call).
--
-- "Build me an X" defaults to blocks/app artifacts; the assistant installs
-- a plate only when the user says plate/kit or extends an existing kit.
-- Anchored replace of 0160's routing sentence, same fail-loud pattern.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_old text := 'Prefer a plate over an app block when the user wants something durable that travels with the database ("make me a tool/surface/app for X I can reopen"); blocks stay the tool for ad-hoc analysis.';
    v_new text := 'PLATES ARE AN EXPLICIT PATHWAY: install one ONLY when the user asks for it by name — they say "plate" or "kit", or ask to add to / change an existing kit''s surfaces. A generic "build me an app / dashboard / tool for X" ALWAYS defaults to blocks and app artifacts. When a request smells plate-shaped (durable, reusable, should travel with the database), you may OFFER the plate route in one short sentence — but never install a plate uninvited.';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0170: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0170: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('PLATES ARE AN EXPLICIT PATHWAY' IN v_system) > 0 THEN
        RAISE NOTICE '0170: opt-in routing already present; skipping';
        RETURN;
    END IF;
    IF position(v_old IN v_system) = 0 THEN
        RAISE EXCEPTION '0170: routing-sentence anchor not found — prompt drifted, re-author this patch';
    END IF;
    v_system := replace(v_system, v_old, v_new);
    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0170: plates are now opt-in for the assistant (% chars)', length(v_system);
END
$patch$;
