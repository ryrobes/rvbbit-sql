-- 0209: teach decks — the narrative artifact (docs/DECKS_PLAN.md).
-- One new desktop command, publish_deck: the assistant authors a deck SPEC
-- (never HTML/JSX); the desktop pins every slide's data.sql at publish time
-- and opens the published deck in a window. Rendered by the versioned
-- deck-runtime bundle (vendored bolt-slides); rows land in rvbbit.dashboards
-- with app_kind='deck' — the same rails as live apps.

DO $patch$
DECLARE
    v_steps jsonb;
    v_system text;
    v_cmd_anchor text := '{"op":"register_kit","kit":"crime"';
BEGIN
    SELECT steps INTO v_steps FROM rvbbit.operators WHERE name = 'desktop_assistant_turn';
    IF v_steps IS NULL THEN
        RAISE NOTICE '0209: desktop_assistant_turn not installed; skipping';
        RETURN;
    END IF;
    v_system := v_steps->0->>'system';
    IF v_system IS NULL THEN
        RAISE NOTICE '0209: assistant system prompt absent; skipping';
        RETURN;
    END IF;
    IF position('publish_deck' IN v_system) > 0 THEN
        RAISE NOTICE '0209: decks already taught; skipping';
        RETURN;
    END IF;
    IF position(v_cmd_anchor IN v_system) = 0 THEN
        RAISE EXCEPTION '0209: register_kit command anchor not found — prompt drifted, re-author';
    END IF;

    -- 1. Command example, before register_kit in the command list.
    v_system := replace(v_system, v_cmd_anchor,
        '{"op":"publish_deck","name":"Q3 billing story","description":"optional","spec":{"deck":{"title":"Q3 billing, in five slides","theme":{"accent":"#5eead4"},"slides":[{"component":"Cover","props":{"kicker":"Support · Q3","title":"Billing anger is down 41%.","subtitle":"one line"},"notes":"presenter notes"},{"component":"ChartSlide","props":{"kind":"bar","title":"A claim, not a label."},"data":{"sql":"SELECT class AS label, count(*)::int AS value FROM t GROUP BY 1","bind":"data","shape":"rows","mode":"pinned"}}]}}},'
        || E'\n    ' || v_cmd_anchor);

    -- 2. The doctrine section, appended to the end of the prompt.
    v_system := v_system || E'\n\n' ||
'DECKS (publish_deck — paged slide narratives, docs/DECKS_PLAN.md)
- A deck is a JSON SPEC rendered by a pre-built engine. You never write HTML or JSX for it. It publishes like a live app (rvbbit.dashboards, app_kind=deck), opens on the desktop, and serves at /apps/<slug>.
- Slide shape: {"component":Name,"props":{...},"notes":"what to say over this slide","data":{"sql":"...","bind":"<prop>","shape":"rows|points|cell","column":"<col>","mode":"pinned|live"}}.
- Components: Cover(kicker,title,subtitle,foot) · Text(kicker,headline,accent,subhead,body[]) · BigNumber(kicker,value,label,detail) · ChartSlide(kind:"bar"|"line"|"donut",title,subtitle) · Quote(quote,attribution) · Agenda(kicker,title,items[]) · StatGrid(kicker,title,stats:[{value,label}]) · Section(kicker,title) · Timeline(items) · Table(columns,rows).
- THE DESKTOP PINS THE DATA. Put SQL in data.sql — it runs at publish time and the rows are embedded. NEVER hand-type numeric results into props; never invent data. Draw the SQL from the desktop''s blocks — their queries are your source of record (adapt freely: aggregate, alias, LIMIT).
- Binding contracts: ChartSlide bar → bind:"data", shape:"rows", SQL aliases label/value. ChartSlide line → bind:"points", shape:"points", column:<measure column>. BigNumber → bind:"value", shape:"cell". Table → bind:"rows".
- mode:"pinned" (default) = deck-as-evidence, numbers frozen at publish. mode:"live" = re-queries every view (standing-meeting decks). Use live only when asked.
- Taste (non-negotiable): one idea per slide; no bullet walls; open with a Cover and close with a so-what Text; chart titles are CLAIMS ("The 2001 spike is real."), never labels ("Sightings per year"); 6-10 slides unless asked otherwise; presenter notes on every slide.
- Build the outline from the desktop: the blocks'' spatial reading order (top-left → bottom-right) is the default narrative order; the focused block is the likely centerpiece. After publishing, the apply report carries the slug, version, and pin failures — repair failed slides with a follow-up publish_deck (same name = new version).';

    UPDATE rvbbit.operators
    SET steps = jsonb_set(v_steps, '{0,system}', to_jsonb(v_system))
    WHERE name = 'desktop_assistant_turn';
    RAISE NOTICE '0209: decks taught (% chars)', length(v_system);
END $patch$;
