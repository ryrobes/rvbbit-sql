-- 0191: teach open_panel — the assistant as DataRabbit's help system.
-- One prompt sentence + a queryable registry (0190) instead of a panel
-- syllabus: discovery costs a SELECT only in the turns that need it.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_cmd_anchor text := '{"op":"open_layout","layout_id":"crm/home"},';
    v_doc_anchor text := '- LAYOUTS (the compose layer)';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0191: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL THEN
        RAISE NOTICE '0191: system prompt absent; skipping';
        RETURN;
    END IF;
    IF position('open_panel' IN v_system) > 0 THEN
        RAISE NOTICE '0191: open_panel already taught; skipping';
        RETURN;
    END IF;
    IF position(v_cmd_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0191: open_layout command anchor not found — prompt drifted, re-author';
    END IF;
    IF position(v_doc_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0191: layouts doctrine anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_cmd_anchor,
        v_cmd_anchor || E'\n    ' ||
        '{"op":"open_panel","panel":"system-objects","hint":"indexes"},');

    v_system := replace(v_system, v_doc_anchor,
        '- DATARABBIT HELP / PANELS: open_panel opens any DataRabbit panel (Finder, Monitor, System Objects, settings, ...). The registry lives in rvbbit.desktop_panels (id, label, description, folder, hints jsonb, notes) — synced from the running app, always current. When the user asks where or how to see something in DataRabbit, SELECT from it, then ANSWER FIRST, OPEN SECOND: one sentence on where the thing lives (mention alternatives), then open the single best panel — never spray windows. hint deep-links where hints lists values (e.g. system-objects accepts "indexes"); an unsupported hint still opens the panel and apply_report tells you — then say which tab/section to click (notes describes what is inside).'
        || E'\n' || v_doc_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0191: open_panel taught (% chars)', length(v_system);
END
$patch$;
