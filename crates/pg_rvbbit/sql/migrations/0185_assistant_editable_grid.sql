-- 0185: the grid island learns spreadsheet editing. Double-click a cell,
-- Enter commits — the grid fires the named action with {id, column, value},
-- so the write wall is unchanged: what persists is whatever the action's
-- SQL decides. The taught idiom is one UPDATE with a CASE per editable
-- column, because {{column}} must NEVER be interpolated as an identifier.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_anchor text := '<rv-grid query="q"></rv-grid>';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0185: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL OR position('PLATES (durable SQL surfaces' IN v_system) = 0 THEN
        RAISE NOTICE '0185: PLATES section absent; skipping';
        RETURN;
    END IF;
    IF position('edit-action=' IN v_system) > 0 THEN
        RAISE NOTICE '0185: editable grid already taught; skipping';
        RETURN;
    END IF;
    IF position(v_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0185: rv-grid anchor not found — prompt drifted, re-author';
    END IF;

    v_system := replace(v_system, v_anchor,
        '<rv-grid query="q"></rv-grid> (spreadsheet editing: add edit-action="name" id="key_col" edit="qty,notes" — double-click edits a cell, Enter commits, Esc cancels; the commit fires the action with args {id, column, value}, declare EXACTLY those three text args; edit= lists the editable columns, omit it and every column except id is editable; write the action as ONE UPDATE using a CASE per editable column: SET qty = CASE WHEN {{column}} = ''qty'' THEN nullif({{value}}, '''')::integer ELSE qty END, notes = CASE WHEN {{column}} = ''notes'' THEN {{value}} ELSE notes END WHERE key_col = {{id}} — NEVER interpolate {{column}} as an identifier, and cast per column with nullif for empties)');

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system)),
        updated_at = clock_timestamp()
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0185: editable grid taught (% chars)', length(v_system);
END
$patch$;
