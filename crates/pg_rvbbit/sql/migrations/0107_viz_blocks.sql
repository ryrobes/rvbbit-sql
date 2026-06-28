-- 0107_viz_blocks
--
-- Versioned canonical visualization blocks. These are SQL templates that emit
-- Lens UI-artifact rowsets, plus durable links to known catalog objects
-- (tables, columns, metrics, KPIs, cubes, queries, dashboards). The SQL is still
-- plain SQL, so a block can be previewed directly or wrapped in a real VIEW by
-- tooling when the template has been fully instantiated.

CREATE TABLE IF NOT EXISTS rvbbit.viz_block_defs (
    block_id        bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    name            text        NOT NULL,
    version         integer     NOT NULL,
    title           text,
    intent          text        NOT NULL DEFAULT 'overview',
    description     text,
    owner           text,
    sql_template    text        NOT NULL,
    input_schema    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    layout_template jsonb       NOT NULL DEFAULT '{}'::jsonb,
    params          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    tags            text[]      NOT NULL DEFAULT '{}'::text[],
    labels          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    enabled         boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

CREATE INDEX IF NOT EXISTS viz_block_defs_name_created_idx
    ON rvbbit.viz_block_defs (name, created_at DESC, version DESC);

CREATE INDEX IF NOT EXISTS viz_block_defs_intent_idx
    ON rvbbit.viz_block_defs (intent, name);

CREATE TABLE IF NOT EXISTS rvbbit.viz_object_links (
    link_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_name    text             NOT NULL,
    block_version integer,
    object_kind   text             NOT NULL,
    object_key    text             NOT NULL,
    role          text             NOT NULL DEFAULT 'source',
    confidence    double precision NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    link_source   text             NOT NULL DEFAULT 'declared',
    conditions    jsonb            NOT NULL DEFAULT '{}'::jsonb,
    notes         text,
    created_at    timestamptz      NOT NULL DEFAULT now(),
    updated_at    timestamptz      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS viz_object_links_object_idx
    ON rvbbit.viz_object_links (object_kind, object_key, confidence DESC);

CREATE INDEX IF NOT EXISTS viz_object_links_block_idx
    ON rvbbit.viz_object_links (block_name, block_version);

CREATE UNIQUE INDEX IF NOT EXISTS viz_object_links_unique_idx
    ON rvbbit.viz_object_links
    (block_name, coalesce(block_version, 0), object_kind, object_key, role, link_source);

CREATE OR REPLACE VIEW rvbbit.viz_block_catalog AS
SELECT DISTINCT ON (name)
    name,
    version,
    coalesce(title, name) AS title,
    intent,
    description,
    owner,
    sql_template,
    input_schema,
    layout_template,
    params,
    tags,
    labels,
    enabled,
    created_at
FROM rvbbit.viz_block_defs
ORDER BY name, created_at DESC, version DESC;

CREATE OR REPLACE FUNCTION rvbbit._viz_apply_params(
    p_sql    text,
    p_params jsonb DEFAULT '{}'::jsonb
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_sql text := p_sql;
    v_key text;
    v_val text;
BEGIN
    IF p_sql IS NULL THEN
        RETURN NULL;
    END IF;
    IF p_params IS NULL THEN
        RETURN v_sql;
    END IF;
    IF jsonb_typeof(p_params) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'rvbbit._viz_apply_params: params must be a JSON object';
    END IF;

    FOR v_key, v_val IN SELECT key, value FROM jsonb_each_text(p_params)
    LOOP
        v_sql := replace(v_sql, '{' || v_key || '!}', coalesce(v_val, ''));
        v_sql := replace(v_sql, '{' || v_key || '}', quote_nullable(v_val));
    END LOOP;

    RETURN v_sql;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.link_viz_block(
    p_block_name    text,
    p_object_kind   text,
    p_object_key    text,
    p_role          text             DEFAULT 'source',
    p_confidence    double precision DEFAULT 1.0,
    p_link_source   text             DEFAULT 'declared',
    p_conditions    jsonb            DEFAULT '{}'::jsonb,
    p_block_version integer          DEFAULT NULL,
    p_notes         text             DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql AS $fn$
DECLARE
    v_link_id bigint;
BEGIN
    IF nullif(btrim(coalesce(p_block_name, '')), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.link_viz_block: block_name is required';
    END IF;
    IF nullif(btrim(coalesce(p_object_kind, '')), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.link_viz_block: object_kind is required';
    END IF;
    IF nullif(btrim(coalesce(p_object_key, '')), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.link_viz_block: object_key is required';
    END IF;
    IF coalesce(p_confidence, 1.0) < 0 OR coalesce(p_confidence, 1.0) > 1 THEN
        RAISE EXCEPTION 'rvbbit.link_viz_block: confidence must be between 0 and 1';
    END IF;
    IF p_conditions IS NOT NULL AND jsonb_typeof(p_conditions) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'rvbbit.link_viz_block: conditions must be a JSON object';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM rvbbit.viz_block_defs
        WHERE name = p_block_name
          AND (p_block_version IS NULL OR version = p_block_version)
    ) THEN
        RAISE EXCEPTION 'rvbbit.link_viz_block: block "%" version % does not exist',
            p_block_name, coalesce(p_block_version::text, 'latest');
    END IF;

    SELECT link_id INTO v_link_id
    FROM rvbbit.viz_object_links
    WHERE block_name = p_block_name
      AND coalesce(block_version, 0) = coalesce(p_block_version, 0)
      AND object_kind = p_object_kind
      AND object_key = p_object_key
      AND role = coalesce(nullif(btrim(p_role), ''), 'source')
      AND link_source = coalesce(nullif(btrim(p_link_source), ''), 'declared')
    LIMIT 1;

    IF v_link_id IS NULL THEN
        INSERT INTO rvbbit.viz_object_links
            (block_name, block_version, object_kind, object_key, role,
             confidence, link_source, conditions, notes)
        VALUES
            (p_block_name, p_block_version, p_object_kind, p_object_key,
             coalesce(nullif(btrim(p_role), ''), 'source'),
             coalesce(p_confidence, 1.0),
             coalesce(nullif(btrim(p_link_source), ''), 'declared'),
             coalesce(p_conditions, '{}'::jsonb),
             p_notes)
        RETURNING link_id INTO v_link_id;
    ELSE
        UPDATE rvbbit.viz_object_links
           SET confidence = coalesce(p_confidence, confidence),
               conditions = coalesce(p_conditions, conditions),
               notes = coalesce(p_notes, notes),
               updated_at = now()
         WHERE link_id = v_link_id;
    END IF;

    RETURN v_link_id;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.define_viz_block(
    p_name            text,
    p_sql_template    text,
    p_input_schema    jsonb  DEFAULT '{}'::jsonb,
    p_layout_template jsonb  DEFAULT '{}'::jsonb,
    p_title           text   DEFAULT NULL,
    p_intent          text   DEFAULT 'overview',
    p_description     text   DEFAULT NULL,
    p_owner           text   DEFAULT NULL,
    p_params          jsonb  DEFAULT '{}'::jsonb,
    p_tags            text[] DEFAULT '{}'::text[],
    p_labels          jsonb  DEFAULT '{}'::jsonb,
    p_enabled         boolean DEFAULT true,
    p_links           jsonb  DEFAULT '[]'::jsonb
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
    v_link record;
BEGIN
    IF nullif(btrim(coalesce(p_name, '')), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: name is required';
    END IF;
    IF nullif(btrim(coalesce(p_sql_template, '')), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: sql_template is required';
    END IF;
    IF p_input_schema IS NOT NULL AND jsonb_typeof(p_input_schema) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: input_schema must be a JSON object';
    END IF;
    IF p_layout_template IS NOT NULL AND jsonb_typeof(p_layout_template) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: layout_template must be a JSON object';
    END IF;
    IF p_params IS NOT NULL AND jsonb_typeof(p_params) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: params must be a JSON object';
    END IF;
    IF p_labels IS NOT NULL AND jsonb_typeof(p_labels) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: labels must be a JSON object';
    END IF;
    IF p_links IS NOT NULL AND jsonb_typeof(p_links) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'rvbbit.define_viz_block: links must be a JSON array';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.viz_block:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version
    FROM rvbbit.viz_block_defs
    WHERE name = p_name;

    INSERT INTO rvbbit.viz_block_defs
        (name, version, title, intent, description, owner, sql_template,
         input_schema, layout_template, params, tags, labels, enabled)
    VALUES
        (p_name, v_version, nullif(btrim(coalesce(p_title, '')), ''),
         coalesce(nullif(btrim(p_intent), ''), 'overview'),
         p_description, p_owner, p_sql_template,
         coalesce(p_input_schema, '{}'::jsonb),
         coalesce(p_layout_template, '{}'::jsonb),
         coalesce(p_params, '{}'::jsonb),
         coalesce(p_tags, '{}'::text[]),
         coalesce(p_labels, '{}'::jsonb),
         coalesce(p_enabled, true));

    FOR v_link IN
        SELECT *
        FROM jsonb_to_recordset(coalesce(p_links, '[]'::jsonb)) AS x(
            object_kind text,
            object_key text,
            role text,
            confidence double precision,
            link_source text,
            conditions jsonb,
            notes text,
            block_version integer
        )
    LOOP
        PERFORM rvbbit.link_viz_block(
            p_name,
            v_link.object_kind,
            v_link.object_key,
            coalesce(v_link.role, 'source'),
            coalesce(v_link.confidence, 1.0),
            coalesce(v_link.link_source, 'declared'),
            coalesce(v_link.conditions, '{}'::jsonb),
            coalesce(v_link.block_version, NULL),
            v_link.notes
        );
    END LOOP;

    RETURN v_version;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.viz_block_sql(
    p_name    text,
    p_params  jsonb    DEFAULT '{}'::jsonb,
    p_version integer  DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_sql text;
    v_defaults jsonb;
    v_effective jsonb;
BEGIN
    SELECT sql_template, coalesce(params, '{}'::jsonb)
      INTO v_sql, v_defaults
    FROM rvbbit.viz_block_defs
    WHERE name = p_name
      AND (p_version IS NULL OR version = p_version)
    ORDER BY version DESC
    LIMIT 1;

    IF v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.viz_block_sql: viz block "%" version % not found',
            p_name, coalesce(p_version::text, 'latest');
    END IF;

    v_effective := v_defaults || coalesce(p_params, '{}'::jsonb);
    v_sql := rvbbit._viz_apply_params(v_sql, v_effective);

    IF v_sql ~ '\{[A-Za-z_][A-Za-z0-9_]*!?\}' THEN
        RAISE EXCEPTION 'rvbbit.viz_block_sql: unresolved template token in "%": %',
            p_name, (regexp_match(v_sql, '\{[A-Za-z_][A-Za-z0-9_]*!?\}'))[1];
    END IF;

    RETURN v_sql;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.preview_viz_block_sql(
    p_sql_template text,
    p_params       jsonb DEFAULT '{}'::jsonb
) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_sql text;
BEGIN
    IF nullif(btrim(coalesce(p_sql_template, '')), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.preview_viz_block_sql: sql_template is required';
    END IF;

    v_sql := rvbbit._viz_apply_params(p_sql_template, coalesce(p_params, '{}'::jsonb));

    IF v_sql ~ '\{[A-Za-z_][A-Za-z0-9_]*!?\}' THEN
        RAISE EXCEPTION 'rvbbit.preview_viz_block_sql: unresolved template token: %',
            (regexp_match(v_sql, '\{[A-Za-z_][A-Za-z0-9_]*!?\}'))[1];
    END IF;

    RETURN v_sql;
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.preview_viz_block(
    p_name    text,
    p_params  jsonb    DEFAULT '{}'::jsonb,
    p_version integer  DEFAULT NULL
) RETURNS text
LANGUAGE sql STABLE AS $fn$
    SELECT rvbbit.viz_block_sql(p_name, p_params, p_version);
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.viz_block_versions(p_name text)
RETURNS TABLE (
    version integer,
    title text,
    intent text,
    description text,
    owner text,
    sql_template text,
    input_schema jsonb,
    layout_template jsonb,
    params jsonb,
    tags text[],
    labels jsonb,
    enabled boolean,
    created_at timestamptz
) LANGUAGE sql STABLE AS $fn$
    SELECT version, title, intent, description, owner, sql_template,
           input_schema, layout_template, params, tags, labels, enabled, created_at
    FROM rvbbit.viz_block_defs
    WHERE name = p_name
    ORDER BY version DESC;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.viz_blocks_for_object(
    p_object_kind text DEFAULT NULL,
    p_object_key  text DEFAULT NULL,
    p_intent      text DEFAULT NULL
) RETURNS TABLE (
    name text,
    version integer,
    title text,
    intent text,
    description text,
    sql_template text,
    input_schema jsonb,
    layout_template jsonb,
    params jsonb,
    tags text[],
    labels jsonb,
    object_kind text,
    object_key text,
    role text,
    confidence double precision,
    link_source text,
    conditions jsonb,
    block_version integer
) LANGUAGE sql STABLE AS $fn$
    SELECT c.name, c.version, c.title, c.intent, c.description,
           c.sql_template, c.input_schema, c.layout_template, c.params,
           c.tags, c.labels,
           l.object_kind, l.object_key, l.role, l.confidence, l.link_source,
           l.conditions, l.block_version
    FROM rvbbit.viz_object_links l
    JOIN rvbbit.viz_block_catalog c
      ON c.name = l.block_name
     AND (l.block_version IS NULL OR l.block_version = c.version)
    WHERE c.enabled
      AND (p_object_kind IS NULL OR l.object_kind = p_object_kind)
      AND (p_object_key IS NULL OR l.object_key = p_object_key)
      AND (p_intent IS NULL OR c.intent = p_intent)
    ORDER BY l.confidence DESC, c.intent, c.name;
$fn$;
