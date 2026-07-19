-- 0181: rv-chart islands are now responsive (island-owned ResizeObserver
-- feeding Vega an explicit width + autosize:fit). Teach it so the
-- assistant never again tries width hacks — a real session burned many
-- turns attempting width:"container" (which measures 0 in a portal) and
-- ended by REMOVING the charts from sales/reports.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := 'mark="bar|line|area" rv-emit="col"></rv-chart>';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0181: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0181: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('follow window resizes automatically' IN v_system) > 0 THEN
        RAISE NOTICE '0181: chart autosize already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0181: rv-chart anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        v_anchor || ' (charts fill their container width and follow window resizes automatically — NEVER set width, autosize, or container sizing yourself, and never remove a chart because of sizing)');

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0181: chart autosize taught (% chars)', length(v_system);
END
$patch$;
