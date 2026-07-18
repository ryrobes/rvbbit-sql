-- 0180: the utility palette — plates get a curated, scoped shadcn-style
-- Tailwind subset (the model already speaks it; this line just says it
-- exists and where its edges are). Anchored prompt patch.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := E'\n- REACTIVITY: kit is the sharing scope';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0180: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0180: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('UTILITY PALETTE' IN v_system) > 0 THEN
        RAISE NOTICE '0180: utility palette already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0180: REACTIVITY anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        E'\n- UTILITY PALETTE: plates also speak a curated Tailwind subset with shadcn semantic tokens — layout (flex/grid/gap/p/m/space-y, scales 0-8), typography (text-xs..2xl, font-medium..bold, uppercase/tracking/leading, truncate, tabular-nums, line-clamp-1..3), semantic colors ONLY (text-foreground/muted-foreground/primary/success/warning/destructive, bg-background/card/muted/primary + /10 /20 tints, border-border/primary/... + /40), borders/rounded-*, opacity, overflow. NOT available (compiled to nothing AND scrubbed): positioning (fixed/absolute/sticky/relative), z-*, inset/top/left, transforms, screen sizing, pointer-events, and ALL arbitrary-value [bracket] classes — never use raw color scales like bg-blue-500 (they do not exist here; semantic tokens keep plates theme-proof). Doctrine: plate-* classes remain the COMPONENT layer (cards/tables/forms/chips) — use utilities for ARRANGEMENT and EMPHASIS between them, not to rebuild components or decorate for its own sake.'
        || v_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0180: utility palette taught (% chars)', length(v_system);
END
$patch$;
