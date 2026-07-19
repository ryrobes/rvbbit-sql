-- 0189: block sizing + smarter placement. The assistant had no size
-- vocabulary (every block spawned 720x460, apps 880x580) and the desktop's
-- fallback placement piled every 4th+ block on one fixed spot. The lens now
-- places into free space first and overlaps MINIMALLY when the screen is
-- full; this teaches the matching size latitude so multi-block answers can
-- actually fit.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := '- place is "auto" (the desktop chooses a free spot) or {"near":"<block_name>"}. Never invent coordinates.';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0189: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL THEN
        RAISE NOTICE '0189: system prompt absent; skipping';
        RETURN;
    END IF;
    IF position('"size":{"width"' IN v_system) > 0 THEN
        RAISE NOTICE '0189: block sizing already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0189: placement anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        '- place is "auto" (the desktop finds a free spot, and when the screen is full it overlaps as LITTLE as possible — never a pile) or {"near":"<block_name>"}. Never invent coordinates. You MAY size blocks: "size":{"width":460,"height":320} (pixels, clamped 360-1400 x 260-900; omit for the 720x460 default). SIZE TO CONTENT: a lone metric or short list ~420x300, a chart ~640x420, wide detail grids up to ~1100x500 — six default-sized blocks cannot fit on one screen, six compact ones can. update_block accepts "size" in its patch to resize an existing block.');

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0189: block sizing taught (% chars)', length(v_system);
END
$patch$;
