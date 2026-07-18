-- 0176: two plate-authoring teachings from the scheduling-kit
-- assistant-builder round (foundation kits, round 1):
--   1. It hardcoded the five crew names as five template columns + ten
--      queries — config-as-rows violated at the surface (a new hire
--      needs plate re-authoring). Teach the grouped-feed pattern.
--   2. Its booking action cast args bare ({{d}}::date), which works live
--      but fails validate_kit's empty-dummy EXPLAIN. Teach nullif casts.
-- Anchored prompt patch; skip-if-present; fail-loud anchors.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := E'\n- REACTIVITY: kit is the sharing scope';
    v_rules text;
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0176: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0176: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('GROUPED FEED' IN v_system) > 0 THEN
        RAISE NOTICE '0176: plate patterns already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0176: REACTIVITY anchor not found — prompt drifted, re-author';
    END IF;

    v_rules :=
        E'\n- CONFIG-DRIVEN SURFACES: never hardcode entity names from data rows (crew, categories, day sections) into a template — a new row must show up without re-authoring the plate. The vocabulary has no nested rv-each, so per-entity sections use the GROUPED FEED pattern: ONE query ORDER BY entity, then time, with a header-flag column (CASE WHEN row_number() OVER (PARTITION BY entity ORDER BY starts_at) = 1 THEN 1 END AS first_of_group); inside rv-each an <h3 rv-if="row.first_of_group">{{ row.entity }}</h3> renders each section header from data. Same idea replaces N copy-pasted per-day queries: one query with day labels and flags. True side-by-side columns per entity are not expressible yet — prefer the grouped feed over hardcoding names.'
        || E'\n- ACTION ARG CASTS: validate_kit EXPLAIN-checks every action with empty-string dummies, so a bare {{arg}}::date (or ::time/::timestamptz/::int) fails at parse. Always wrap casts: nullif({{arg}},'''')::date. Declare truly numeric args as type number so the dummy binds 0 instead.';

    v_system := replace(v_system, v_anchor, v_rules || v_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0176: plate patterns taught (% chars)', length(v_system);
END
$patch$;
