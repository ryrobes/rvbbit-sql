-- 0179: the edit loop + two select truths (dispatch double-click round).
--   1. rv-board gains rv-emit / rv-open: double-click a card -> emit its
--      id to the bus, open the edit plate (from_bus param = selector).
--   2. FORM selects may be query-driven with a boolean `selected` column
--      — and boolean attributes must NEVER be templated: the sanitizer
--      turns selected="" into a BARE selected, which reads as ON.
-- Anchored prompt patch; skip-if-present; fail-loud anchors.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_board_anchor text := 'omit action = read-only board)';
    v_select_anchor text := 'builds its options from a query and marks the current value selected)';
    v_section_anchor text := E'\n- REACTIVITY: kit is the sharing scope';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0179: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0179: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('EDIT LOOP' IN v_system) > 0 THEN
        RAISE NOTICE '0179: edit loop already taught; skipping';
        RETURN;
    END IF;
    IF position(v_board_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0179: rv-board anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_select_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0179: select anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_section_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0179: REACTIVITY anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_board_anchor,
        'omit action = read-only board; add rv-emit="field" + rv-open="plate:<id>" and DOUBLE-CLICKING a card emits its id to the bus then opens that plate — the edit-loop gesture)');

    v_system := replace(v_system, v_select_anchor,
        'builds its options from a query and marks the current value selected; FORM selects (name=, no rv-emit) may be query-driven the same way — selection comes from a boolean selected COLUMN in the options query; NEVER template a boolean attribute (selected/checked): the sanitizer turns attr="" into a BARE attr, which reads as ON)');

    v_system := replace(v_system, v_section_anchor,
        E'\n- EDIT LOOP: pair a board or list with an EDIT plate. The edit plate declares its record-id param {"from_bus": true}; the board''s rv-emit publishes the id on double-click and rv-open opens the plate. Prefill: a single-row query keyed on the param; text inputs take value="{{ row.x }}" via SIBLING rv-each blocks per field group (rv-each NEVER nests — an outer loop eats the inner loop''s tokens); selects are query-driven with a selected column; ship the id back in a hidden input; the UPDATE action re-derives computed fields (e.g. ends_at from the job type''s minutes) and nullif-casts date/time args.'
        || v_section_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0179: edit loop taught (% chars)', length(v_system);
END
$patch$;
