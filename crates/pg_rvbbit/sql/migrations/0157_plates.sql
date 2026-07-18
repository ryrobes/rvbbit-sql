-- 0157: Plates — the second app species (docs/KIT_PLATES_PLAN.md).
--
-- Server-rendered, sanitized, SQL-driven surfaces shipped AS ROWS so a kit's
-- UI travels with the database (back up the DB, the surfaces come along).
-- The lens is the renderer; this is the storage contract. Logic lives in
-- SQL: templates carry no expression language, writes go only through the
-- named, parameterized actions declared beside the template.

CREATE TABLE IF NOT EXISTS rvbbit.plates (
    plate_id          text PRIMARY KEY,
    kit               text,
    title             text NOT NULL,
    description       text,
    template_version  integer NOT NULL DEFAULT 1,
    template          text NOT NULL,
    queries           jsonb NOT NULL DEFAULT '{}'::jsonb,
    actions           jsonb NOT NULL DEFAULT '{}'::jsonb,
    params            jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT plates_queries_is_object CHECK (jsonb_typeof(queries) = 'object'),
    CONSTRAINT plates_actions_is_object CHECK (jsonb_typeof(actions) = 'object'),
    CONSTRAINT plates_params_is_array  CHECK (jsonb_typeof(params) = 'array')
);

-- Install-time tripwires. The REAL sanitizer is the renderer's (lens-side,
-- allowlist-based, runs on every render — defense in depth); these regexes
-- just make a hostile or sloppy row fail loudly at install instead of
-- rendering as an empty shell later.
CREATE OR REPLACE FUNCTION rvbbit.upsert_plate(
    p_plate_id  text,
    p_title     text,
    p_template  text,
    p_queries   jsonb DEFAULT '{}'::jsonb,
    p_actions   jsonb DEFAULT '{}'::jsonb,
    p_params    jsonb DEFAULT '[]'::jsonb,
    p_kit       text  DEFAULT NULL,
    p_description text DEFAULT NULL,
    p_template_version integer DEFAULT 1
) RETURNS text
LANGUAGE plpgsql
AS $up$
DECLARE
    q record;
BEGIN
    IF btrim(coalesce(p_plate_id, '')) = '' THEN
        RAISE EXCEPTION 'plate_id required';
    END IF;
    IF btrim(coalesce(p_template, '')) = '' THEN
        RAISE EXCEPTION 'template required';
    END IF;
    IF p_template ~* '<\s*(script|style|iframe|object|embed|link|meta)\b' THEN
        RAISE EXCEPTION 'plate template may not contain script/style/iframe/object/embed/link/meta';
    END IF;
    IF p_template ~* '\son[a-z]+\s*=' THEN
        RAISE EXCEPTION 'plate template may not contain inline event handlers';
    END IF;
    IF p_template ~* 'javascript\s*:' THEN
        RAISE EXCEPTION 'plate template may not contain javascript: URLs';
    END IF;

    -- Queries must be SELECT-shaped (reads go through the governed read-only
    -- path at render; this is the matching install-time promise).
    FOR q IN SELECT key, value->>'sql' AS sql FROM jsonb_each(p_queries) LOOP
        IF q.sql IS NULL OR btrim(q.sql) = '' THEN
            RAISE EXCEPTION 'query % has no sql', q.key;
        END IF;
        IF q.sql !~* '^[[:space:]]*(SELECT|WITH)\y' THEN
            RAISE EXCEPTION 'query % must be SELECT-shaped', q.key;
        END IF;
    END LOOP;

    INSERT INTO rvbbit.plates AS p
        (plate_id, kit, title, description, template_version, template, queries, actions, params)
    VALUES
        (p_plate_id, p_kit, p_title, p_description, p_template_version, p_template,
         coalesce(p_queries, '{}'::jsonb), coalesce(p_actions, '{}'::jsonb), coalesce(p_params, '[]'::jsonb))
    ON CONFLICT (plate_id) DO UPDATE SET
        kit = EXCLUDED.kit,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        template_version = EXCLUDED.template_version,
        template = EXCLUDED.template,
        queries = EXCLUDED.queries,
        actions = EXCLUDED.actions,
        params = EXCLUDED.params,
        updated_at = clock_timestamp();

    RETURN p_plate_id;
END
$up$;

-- Action audit trail: every plate action invocation, success or failure.
CREATE TABLE IF NOT EXISTS rvbbit.plate_action_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plate_id    text NOT NULL,
    action      text NOT NULL,
    args        jsonb NOT NULL DEFAULT '{}'::jsonb,
    error       text,
    executed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
