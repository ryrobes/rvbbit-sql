-- 0160: teach the Desktop Assistant to author PLATES (KIT_PLATES_PLAN §2
-- doctrine: no visual builder ever — authoring is agent-composed iteration.
-- This is that editor.)
--
-- Surgical patch of desktop_assistant_turn's system prompt (0146): two
-- anchored replacements — new command examples in the COMMANDS block and a
-- PLATES section before SPEND & CONSENT. Fails LOUDLY if either anchor has
-- drifted so a re-authored prompt can't silently lose the plate vocabulary.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_examples_anchor text := '{"op":"close_block","target":"handle"}';
    v_section_anchor text := E'\n\nSPEND & CONSENT';
    v_examples text;
    v_section text;
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0160: desktop_assistant_turn not installed; skipping prompt patch';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL THEN
        RAISE EXCEPTION '0160: desktop_assistant_turn steps[0].system missing';
    END IF;
    IF position('PLATES (durable SQL surfaces' IN v_system) > 0 THEN
        RAISE NOTICE '0160: prompt already carries the PLATES section; skipping';
        RETURN;
    END IF;
    IF position(v_examples_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0160: COMMANDS example anchor not found — prompt drifted, re-author this patch';
    END IF;
    IF position(v_section_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0160: SPEND & CONSENT anchor not found — prompt drifted, re-author this patch';
    END IF;

    v_examples := v_examples_anchor || E',\n    ' ||
        '{"op":"upsert_plate","plate_id":"team/my_surface","title":"My Surface","template":"<div class=\"plate-cards\">...</div>","queries":{"kpis":{"sql":"SELECT ..."}},"params":[],"actions":{},"kit":null,"description":"..."}' ||
        E',\n    ' ||
        '{"op":"open_plate","plate_id":"team/my_surface"}';

    v_section := E'\n\nPLATES (durable SQL surfaces — rvbbit.plates rows)\n'
        || E'- A plate is a REUSABLE surface stored as a database row and rendered natively: HTML template + named read-only queries + declared params (+ named actions for writes). Prefer a plate over an app block when the user wants something durable that travels with the database ("make me a tool/surface/app for X I can reopen"); blocks stay the tool for ad-hoc analysis. upsert_plate installs or replaces (same plate_id = iterate); it does NOT auto-open — follow with open_plate.\n'
        || E'- TEMPLATE VOCABULARY (strict allowlist; anything else is stripped): {{ row.col }} / {{ params.x }} interpolation (always escaped) | rv-each="query" repeats the element per row | rv-if="row.flag" or "!row.flag" INSIDE rv-each; rv-if="query.column" OUTSIDE it (first-row truthiness — this is how tabs work: a tab param + a query computing show_* booleans) | islands: <rv-grid query="q"></rv-grid>, <rv-chart query="q" x="col" y="col" mark="bar|line|area" rv-emit="col"></rv-chart>, <rv-metric query="q" value="col" title="Label"></rv-metric> — islands must NOT sit inside rv-each | param emitters: rv-emit="param" + rv-value="..." on <button> (click; re-click unselects), or rv-emit on <select> / <input type="search|range|date|number|checkbox|radio"> (emits on change; radios auto-group; <select rv-emit="x" query="opts" value="valcol" label="labelcol" placeholder="All"></select> builds its options from a query and marks the current value selected) | rv-live on a search input = debounced emit-while-typing | rv-open-sql="{{ row.script }}" opens SQL BUILT-NOT-RUN (never auto-executed) | rv-open="plate:<id>" opens another plate | <form rv-action="name"> with inputs named after the action''s args is the ONLY write path.\n'
        || E'- LOGIC LIVES IN SQL, never in the template (no expressions exist): tones/flags/selection are COLUMNS. ''ok''/''warn''/''bad'' AS tone drives class="plate-card {{ row.tone }}"; CASE WHEN v = {{ params.v }} THEN ''active'' ELSE '''' END AS sel drives class="{{ row.sel }}". Query SQL references params as {{ params.x }} (bound as escaped literals): WHERE (nullif({{ params.q }}, '''') IS NULL OR title ILIKE ''%'' || {{ params.q }} || ''%''). Give numeric params "type":"number" (pagination: LIMIT 20 OFFSET {{ params.page }} * 20, with prev/next/pageno/has_next computed as COLUMNS of a pager query). "from_bus": true params sync with the desktop filter bus (any window emitting that field re-scopes the plate).\n'
        || E'- STYLING: native classes only (style attributes are stripped): plate-section, plate-cards, plate-card (+ ok/warn/bad; children plate-card-title/-value/-note), plate-table, plate-form, plate-field(-inline), plate-toolbar, plate-tabs, plate-pager, plate-split, plate-rail, plate-columns, plate-kv, plate-feed(-item/-meta), plate-banner(-big/-note), plate-metric.\n'
        || E'- ACTIONS (the write path): "actions":{"add_note":{"sql":"INSERT INTO t (a) VALUES ({{a}})","args":[{"name":"a","type":"text"}],"confirm":false,"description":"..."}} — parameterized, validated, audited; set confirm true for destructive ones. Plate QUERIES stay read-only SELECT/WITH; only ACTIONS may write, and only via {{arg}} parameters.\n'
        || E'- WORKFLOW: validate every plate query with the query tool FIRST (bind {{ params.x }} manually with a sample value). The installer rejects unsafe templates and non-SELECT queries; when apply_report shows upsert_plate skipped, read the reason, fix, and upsert again with the same plate_id.';

    v_system := replace(v_system, v_examples_anchor, v_examples);
    v_system := replace(v_system, v_section_anchor, v_section || v_section_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';

    RAISE NOTICE '0160: desktop_assistant_turn prompt now speaks plates (% chars)', length(v_system);
END
$patch$;
