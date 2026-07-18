-- 0177: teach the assistant the tier-1 layout palette + rv-group.
-- Completes the 0176 arc: side-by-side columns per entity ARE now
-- expressible (rv-group + plate-columns / plate-cal), and layout itself
-- became placement-as-data (c1..c7 day cells, w0..w100 bar widths —
-- classes computed by SQL, exactly like tones). Anchored prompt patch;
-- skip-if-present; fail-loud anchors.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_vocab_anchor text := 'repeats the element per row | rv-if=';
    v_gap_anchor text := 'True side-by-side columns per entity are not expressible yet — prefer the grouped feed over hardcoding names.';
    v_section_anchor text := E'\n- REACTIVITY: kit is the sharing scope';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0177: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0177: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('LAYOUT PALETTE' IN v_system) > 0 THEN
        RAISE NOTICE '0177: layout palette already taught; skipping';
        RETURN;
    END IF;
    IF position(v_vocab_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0177: vocabulary anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_section_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0177: REACTIVITY anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_vocab_anchor,
        'repeats the element per row | rv-group="query:column" on a wrapper repeats it once per distinct value of column (SQL ORDER BY = layout order; {{ group.key }} / {{ group.count }} interpolate; inside it rv-each="group" iterates that group''s rows; may not nest) | rv-if=');

    -- 0176 taught this as a gap; if that exact sentence is present, upgrade it.
    IF position(v_gap_anchor IN v_system) > 0 THEN
        v_system := replace(v_system, v_gap_anchor,
            'For side-by-side columns per entity, wrap rv-group with plate-columns (or plate-cal for calendars); the grouped feed remains the inline-header alternative.');
    END IF;

    v_system := replace(v_system, v_section_anchor,
        E'\n- LAYOUT PALETTE (placement-as-data): layout classes are COLUMNS computed by SQL, exactly like tones. plate-cal = 7-column calendar grid — each child takes c1..c7 (its day column; r1..r8 pin month rows), plate-cal-head cells pin to the top row, chips stack under their day automatically (plate-cal-chip = compact card). plate-bar with an inner <div class="w45 ok"> = capacity/progress (w0..w100 in 5% steps — SQL rounds the percentage; ok/warn/bad tones the fill; closed/over states are your CASE expressions). plate-avatar shows SQL-computed initials; plate-dot ok/warn/bad = status dots; plate-empty = honest empty state (pair with an rv-if flag column); hue-1..hue-8 = stable category accents on cards/chips/dots (map categories in SQL). Never invent class names outside the palette — unknown classes style as nothing.'
        || v_section_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0177: layout palette + rv-group taught (% chars)', length(v_system);
END
$patch$;
