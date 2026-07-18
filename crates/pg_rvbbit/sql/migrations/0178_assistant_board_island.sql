-- 0178: teach the assistant the rv-board island (tier 3, island 1).
-- Kanban whose columns come from rows; dragging a card to another
-- column fires a named plate action with args {id, to} — writes stay
-- behind the named-action wall. Anchored prompt patch; skip-if-present.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := ' — islands must NOT sit inside rv-each';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0178: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0178: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('rv-board' IN v_system) > 0 THEN
        RAISE NOTICE '0178: rv-board already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0178: islands anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        ', <rv-board query="q" group-by="col" group-label="col" id="col" title="col" value="col" note="col" tone="col" action="name"></rv-board> (kanban: one column per distinct group-by value in SQL order; LEFT-JOIN rows with NULL id are empty-column placeholders so idle groups stay drop targets; dragging a card to another column fires the named action with args {id, to} — declare that action taking EXACTLY id and to, nullif-cast to when it is a date; omit action = read-only board)'
        || v_anchor);

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0178: rv-board taught (% chars)', length(v_system);
END
$patch$;
