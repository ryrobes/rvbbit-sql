-- 0210: teach __heat / __heatrow — alias-driven conditional formatting in
-- the Data Rabbit result grid. Pure SQL aliases, no new command surface:
-- x__heat heat-maps that column's cells (numeric = magnitude scale, auto-
-- diverging around zero; text = stable categorical bands), x__heatrow also
-- tints its whole row. Headers display the stripped name. Outside Data
-- Rabbit the alias is just a slightly odd column name — degrades to nothing.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0210: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL THEN
        RAISE NOTICE '0210: assistant system prompt absent; skipping';
        RETURN;
    END IF;
    IF position('__heatrow' IN v_system) > 0 THEN
        RAISE NOTICE '0210: heat aliases already taught; skipping';
        RETURN;
    END IF;

    v_system := v_system || E'\n\n' ||
'CONDITIONAL FORMATTING (__heat / __heatrow column aliases)
- Alias any result column x__heat and the grid heat-maps its cells: numeric columns get a magnitude scale (auto-diverging red/green around zero when the column spans it — sentiment scores, deltas), text columns get one stable color band per distinct value.
- Alias x__heatrow and the whole row is subtly tinted by that column''s value (categorical row banding); __heat cells on other columns layer on top of the row tint.
- Headers show the alias with the suffix stripped — score__heat displays as "score". Colors derive from the active theme automatically; no palette to pick.
- Reach for it when a table is the right view but one column carries the signal: clover_* scores, deltas, status/category dimensions. They are plain SQL aliases — they work in blocks, plates, cubes, metrics, and saved views, and mean nothing outside Data Rabbit.
- Exactly these two suffixes exist and they take no arguments. Thresholds, custom scales, and the like are saved-view concerns, never alias grammar.';

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system))
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0210: heat aliases taught (% chars)', length(v_system);
END $patch$;
