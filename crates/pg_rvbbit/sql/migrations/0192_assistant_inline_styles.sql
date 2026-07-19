-- 0192: inline styles unlocked — design latitude within the guardrails.
-- Every plate looked the same because the style attribute was stripped and
-- all looks routed through pre-baked class vocabularies. The guardrail was
-- never "no styles" — it's containment: the lens now allowlists VISUAL
-- properties (colors, gradients, exact grid templates, shadows, typography,
-- spacing) and strips the escape hatches (position/z-index/transform/
-- pointer-events) plus url() at the value level — including url() smuggled
-- in through DATA, since the scrub runs post-interpolation.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := '- STYLING: native classes only (style attributes are stripped):';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0192: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL THEN
        RAISE NOTICE '0192: system prompt absent; skipping';
        RETURN;
    END IF;
    IF position('CUSTOM LOOKS' IN v_system) > 0 THEN
        RAISE NOTICE '0192: inline styles already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0192: styling anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        '- CUSTOM LOOKS: the style attribute IS allowed, restricted to VISUAL properties — any color (oklch/hex/var()), background gradients (background-image: linear-gradient(...)), borders/radius/shadows, typography (font-size/weight/letter-spacing), spacing, display/flex/grid including exact grid-template-columns, opacity/filter/mix-blend-mode. STRIPPED (never try): position/inset/z-index, transform, pointer-events, transitions/animations, and url() anywhere (even via data). Styles are DATA-DRIVABLE — style="width: {{ row.pct }}%" for exact bars, background: {{ row.heat }} for heatmaps, colors as SQL columns. Prefer theme vars (var(--main), var(--foreground), var(--chrome-border), color-mix(in oklch, var(--main) 20%, transparent)) so designs retheme; hardcode color only when the design demands it. Use custom style to make surfaces DISTINCT — not every plate should look like the default cards.'
        || E'\n' || '- STYLING (native classes, still the fast path):');

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0192: inline styles taught (% chars)', length(v_system);
END
$patch$;
