-- 0183: rv-chart grows past x/y — quick attrs (color/stack/x-format/
-- y-format/height) plus full Vega-Lite passthrough via spec='{json}'.
-- Limiting charts to a single bare mark lost most of the point of using
-- Vega-Lite; the island still owns data, width, autosize, and theme, so
-- the spec is mark+encoding vocabulary only, never sizing or plumbing.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := 'and never remove a chart because of sizing)';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0183: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0183: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('full Vega-Lite fragment in spec=' IN v_system) > 0 THEN
        RAISE NOTICE '0183: chart spec already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0183: chart-sizing anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        'and never remove a chart because of sizing; color="col" adds a series+legend, stack="true|normalize" stacks bars/areas, x-format/y-format take d3 format strings (y-format="$,.0f"), height="260" overrides the 220 default; for anything richer — multi-series lines, layered marks, temporal axes, custom tooltips, explicit sort — put a full Vega-Lite fragment in spec=''{"mark":...,"encoding":...}'' (single-quoted attribute holding JSON; mark/encoding/transform/layer only): the island force-injects data, width, autosize, background, and theme config, so never include data, width, or config in spec — a malformed spec renders an inline error, not a blank)');

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0183: chart spec passthrough taught (% chars)', length(v_system);
END
$patch$;
