-- 0188: teach layouts — the plate compose layer (docs/PLATE_COMPOSE_PLAN.md).
-- upsert_layout / patch_layout / open_layout commands + the doctrine:
-- layouts own arrangement, never behavior.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_cmd_anchor text := '{"op":"register_kit","kit":"crime"';
    v_doc_anchor text := '- LARGE PLATES ARE BUILT INCREMENTALLY';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0188: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0188: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('upsert_layout' IN v_system) > 0 THEN
        RAISE NOTICE '0188: layouts already taught; skipping';
        RETURN;
    END IF;
    IF position(v_cmd_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0188: register_kit command anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_doc_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0188: large-plates anchor not found — prompt drifted, re-author';
    END IF;

    -- 1. Command examples, before register_kit in the list.
    v_system := replace(v_system, v_cmd_anchor,
        '{"op":"upsert_layout","layout_id":"crm/home","title":"CRM","kit":"crm","default":true,"design":{"width":1600,"height":900},"panes":[{"id":"directory","plate":"crm/customers","x":0.01,"y":0.02,"w":0.54,"h":0.95},{"id":"inspector","plate":"crm/customer-card","slot":true,"x":0.56,"y":0.02,"w":0.43,"h":0.55,"z":1,"title":"Customer"}]},'
        || E'\n    ' || '{"op":"patch_layout","layout_id":"crm/home","panes":{"inspector":{"w":0.42},"old_pane":null}},'
        || E'\n    ' || '{"op":"open_layout","layout_id":"crm/home"},'
        || E'\n    ' || v_cmd_anchor);

    -- 2. The doctrine bullet, before the incremental-authoring bullet.
    v_system := replace(v_system, v_doc_anchor,
        '- LAYOUTS (the compose layer): a layout arranges EXISTING plates on a free-floating full-screen canvas — panes carry {id, plate, x,y,w,h (FRACTIONS of design.width/height, in [0,1]), z, params (pinned defaults), slot, title}. A layout owns ARRANGEMENT, NEVER BEHAVIOR: no buttons, actions, or logic on the layout — a header/nav bar is a thin toolbar PLATE placed as a pane. Cross-pane reactivity is the ordinary param bus (rv-emit in one pane, from_bus in another — nothing extra to wire). "slot": true panes render empty until a plate targets them: rv-open="plate:crm/customer-card@inspector" renders INTO pane "inspector" instead of opening a window; plain rv-open="plate:x" from inside a layout opens a centered modal over it (edit/create plates are NEVER dead panes). "default": true on upsert_layout makes it the kit''s front door. Responsive design = SIBLING LAYOUTS, not squishing: a phone variant is its own layout row (maybe fewer panes). patch_layout for edits: fields replace, panes merge per id (null removes). Panes overlap intentionally via z when layering serves the design; keep 2-6 panes typical.'
        || E'\n' || v_doc_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0188: layouts taught (% chars)', length(v_system);
END
$patch$;
