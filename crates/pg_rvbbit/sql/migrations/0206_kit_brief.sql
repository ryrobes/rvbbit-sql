-- 0206: kit_brief — the skill card (docs/KIT_PLATES_PLAN.md, zero-UI round)
--
-- The deterministic compilation of a kit into narrative markdown: what it
-- is, the nouns, how things arrive, what must hold (with LIVE status),
-- what comes out, where humans look. Every line is quoted or mechanically
-- derived from the kit's own metadata — no model involvement, compiled at
-- read time, so it can never hallucinate a rule or drift from the kit.
--
-- CRUCIALLY this is the same document agents receive when directed at the
-- kit (public.kit_brief over run_sql): the human reading the Briefing tab
-- reads exactly what the agent reads. Confidence by construction, not
-- emulation. The LLM "rehearsal" (warehouse-mcp kit_rehearsal) sits on
-- top as a lint — never as the source.

CREATE OR REPLACE FUNCTION rvbbit.kit_brief(p_kit text)
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE
    k       record;
    r       record;
    a       record;
    md      text := '';
    sect    text;
    v_args  text;
    n       int;
BEGIN
    SELECT * INTO k FROM rvbbit.kits WHERE kit = p_kit;
    IF NOT FOUND THEN
        RETURN '# ' || p_kit || E'\n\n(no such kit)';
    END IF;

    md := '# Kit briefing: ' || coalesce(k.title, p_kit) || ' (`' || p_kit || '`)' || E'\n\n';
    IF coalesce(k.description, '') <> '' THEN
        md := md || k.description || E'\n\n';
    END IF;
    md := md || '_Compiled live from the kit''s own metadata — this is the same briefing agents receive._'
             || E'\n\n';

    -- ── the nouns ────────────────────────────────────────────────────
    sect := '';
    FOR r IN
        SELECT DISTINCT g.dst AS tbl
        FROM rvbbit.kit_graph(p_kit) g
        WHERE g.dst_kind = 'table'
        ORDER BY 1
    LOOP
        sect := sect || '- `' || r.tbl || '`'
            || coalesce(' — ' || obj_description(rvbbit._safe_regclass(r.tbl), 'pg_class'), '')
            || E'\n';
    END LOOP;
    IF sect <> '' THEN
        md := md || E'## The nouns (tables this kit lives in)\n\n' || sect || E'\n';
    END IF;

    -- ── how things arrive (the write API) ────────────────────────────
    sect := '';
    FOR r IN
        SELECT p.plate_id, act.key AS aname,
               act.value ->> 'description' AS descr,
               act.value -> 'args' AS args
        FROM rvbbit.plates p,
             jsonb_each(coalesce(p.actions, '{}'::jsonb)) act
        WHERE p.kit = p_kit
        ORDER BY p.plate_id, act.key
    LOOP
        SELECT string_agg((arg ->> 'name') ||
                   CASE WHEN coalesce((arg ->> 'required')::boolean, true) THEN '' ELSE '?' END,
                   ', ' ORDER BY ord)
        INTO v_args
        FROM jsonb_array_elements(coalesce(r.args, '[]'::jsonb)) WITH ORDINALITY AS t(arg, ord);
        sect := sect || '- **' || r.aname || '(' || coalesce(v_args, '') || ')**'
            || coalesce(' — ' || nullif(r.descr, ''), '')
            || '  _(action on `' || r.plate_id || '`; audited; `?` = optional)_'
            || E'\n';
    END LOOP;
    IF sect <> '' THEN
        md := md || E'## How things arrive (the write API)\n\nAll writes go through these named, audited actions — never raw SQL:\n\n'
                 || sect || E'\n';
    END IF;

    -- ── what must hold (rules + live status) ─────────────────────────
    -- logic-plate explanations verbatim: the human-written WHY is the
    -- highest-value text in the kit; agents read it as their instructions.
    FOR r IN
        SELECT p.plate_id, p.title, p.template
        FROM rvbbit.plates p
        WHERE p.kit = p_kit AND coalesce(to_jsonb(p) ->> 'surface', 'ui') = 'logic'
        ORDER BY p.plate_id
    LOOP
        md := md || '## What must hold — ' || r.title || E'\n\n'
                 || btrim(r.template) || E'\n\n';
    END LOOP;

    sect := '';
    n := 0;
    FOR r IN SELECT * FROM rvbbit.kit_pulse(p_kit) LOOP
        n := n + 1;
        sect := sect || '- `' || r.check_id || '` (' || r.source || '): **'
            || CASE r.status WHEN 'green' THEN 'green'
                             WHEN 'error' THEN 'check error'
                             ELSE r.violations || ' violation' || CASE WHEN r.violations = 1 THEN '' ELSE 's' END END
            || '**'
            || CASE WHEN r.status = 'red' AND r.sample <> '[]'::jsonb
                    THEN ' — e.g. ' || left((r.sample -> 0)::text, 140)
                    ELSE '' END
            || E'\n';
    END LOOP;
    IF n > 0 THEN
        md := md || E'### Checks, right now\n\n' || sect || E'\n';
    END IF;

    -- ── what comes out ───────────────────────────────────────────────
    sect := '';
    FOR r IN
        SELECT DISTINCT g.src, g.dst
        FROM rvbbit.kit_graph(p_kit) g
        WHERE g.edge = 'writes'
        ORDER BY 1, 2
    LOOP
        sect := sect || '- `' || r.src || '` writes `' || r.dst || '`' || E'\n';
    END LOOP;
    IF sect <> '' THEN
        md := md || E'## What comes out\n\n' || sect || E'\n';
    END IF;

    -- ── where humans look ────────────────────────────────────────────
    sect := '';
    FOR r IN
        SELECT l.layout_id AS id, l.title, l.description, 'layout' AS kind
        FROM rvbbit.plate_layouts l WHERE l.kit = p_kit
        UNION ALL
        SELECT p.plate_id, p.title, p.description, 'plate'
        FROM rvbbit.plates p
        WHERE p.kit = p_kit AND coalesce(to_jsonb(p) ->> 'surface', 'ui') = 'ui'
        ORDER BY 4, 1
    LOOP
        sect := sect || '- ' || CASE r.kind WHEN 'layout' THEN 'Layout' ELSE 'Plate' END
            || ' `' || r.id || '` — ' || coalesce(nullif(r.description, ''), r.title) || E'\n';
    END LOOP;
    IF sect <> '' THEN
        md := md || E'## Where humans look\n\nWhen you finish something, point people here:\n\n'
                 || sect || E'\n';
    END IF;

    md := md || E'---\n_Every statement above is compiled from live kit metadata ('
             || to_char(now(), 'YYYY-MM-DD HH24:MI') || E' — checks were evaluated at read time). '
             || E'Read current gaps any time: `SELECT * FROM kit_pulse('
             || quote_literal(p_kit) || E')`._\n';
    RETURN md;
END $fn$;

COMMENT ON FUNCTION rvbbit.kit_brief(text) IS
    'The skill card: a kit compiled into narrative markdown (nouns, write API, rules verbatim + live check status, outputs, human surfaces). Deterministic — the same briefing agents receive. docs/KIT_PLATES_PLAN.md';

CREATE OR REPLACE FUNCTION public.kit_brief(p_kit text)
RETURNS text LANGUAGE sql AS $fn$ SELECT rvbbit.kit_brief(p_kit) $fn$;
COMMENT ON FUNCTION public.kit_brief(text) IS
    'READ THIS FIRST when directed at a kit: the kit''s full briefing (what it is, the write API, the rules and their live status, where humans look). Pass-through to rvbbit.kit_brief.';
