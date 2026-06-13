-- 0021_edit_primitives — direct (versioned) edits for metrics and cubes.
--
-- Metrics are already versioned (metric_defs is (name,version); define_metric appends), so a metric
-- edit is just define_metric on the same name → a new version, fully reversible. revise_metric is a
-- convenience that merges the agent's changes onto the current version. Cubes: cube_defs IS already
-- versioned too, but define_cube only re-shapes the materialized table when it doesn't exist — so a
-- def edit that changes columns would break refresh. redefine_cube makes cube edits safe: it appends
-- a cube_defs version and, when the output SHAPE changed, recreates cubes.<name> (else just refreshes,
-- preserving the AS-OF generations); cube_versions/revert_cube give the history + a one-call rollback.
-- Additive + idempotent.

-- ── revise_metric: edit a metric in place (appends a new version) ───────────
-- p_check_sql: NULL keeps the current check; '' clears it; any other value sets it.
CREATE OR REPLACE FUNCTION rvbbit.revise_metric(
    p_name        text,
    p_sql         text  DEFAULT NULL,
    p_grain       text  DEFAULT NULL,
    p_description text  DEFAULT NULL,
    p_params      jsonb DEFAULT NULL,
    p_check_sql   text  DEFAULT NULL,
    p_owner       text  DEFAULT NULL,
    p_category    text  DEFAULT NULL,
    p_subcategory text  DEFAULT NULL,
    p_labels      jsonb DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE cur rvbbit.metric_defs%ROWTYPE; v_version integer;
BEGIN
    SELECT * INTO cur FROM rvbbit.metric_defs
     WHERE name = p_name ORDER BY created_at DESC, version DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.revise_metric: metric % does not exist (use define_metric)', p_name;
    END IF;
    v_version := rvbbit.define_metric(
        p_name,
        coalesce(nullif(btrim(p_sql), ''), cur.sql),
        coalesce(p_params, cur.params, '{}'::jsonb),
        coalesce(nullif(btrim(p_grain), ''), cur.grain),
        coalesce(nullif(btrim(p_description), ''), cur.description),
        coalesce(nullif(btrim(p_owner), ''), cur.owner),
        coalesce(p_labels, cur.labels, '{}'::jsonb),
        CASE WHEN p_check_sql IS NULL THEN cur.check_sql
             WHEN btrim(p_check_sql) = '' THEN NULL
             ELSE p_check_sql END);
    IF nullif(btrim(p_category), '') IS NOT NULL THEN
        BEGIN PERFORM rvbbit.set_category('metric', p_name, p_category, nullif(btrim(p_subcategory), ''));
        EXCEPTION WHEN OTHERS THEN NULL; END;
    END IF;
    RETURN v_version;
END $fn$;

-- ── redefine_cube: edit a cube's definition safely (versioned + shape-aware) ─
CREATE OR REPLACE FUNCTION rvbbit.redefine_cube(
    p_name        text,
    p_sql         text,
    p_grain       text DEFAULT NULL,
    p_description text DEFAULT NULL,
    p_owner       text DEFAULT NULL,
    p_category    text DEFAULT NULL,
    p_subcategory text DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_qual text := 'cubes.' || quote_ident(p_name);
    cur rvbbit.cube_defs%ROWTYPE; v_version integer;
    v_old_cols text; v_new_cols text;
BEGIN
    SELECT * INTO cur FROM rvbbit.cube_defs
     WHERE name = p_name ORDER BY created_at DESC, version DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.redefine_cube: cube % does not exist (use define_cube)', p_name;
    END IF;
    IF nullif(btrim(p_sql), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.redefine_cube: sql is required';
    END IF;
    p_sql := rtrim(btrim(p_sql), E' \t\r\n;');

    -- append a new versioned def
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.cube:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version FROM rvbbit.cube_defs WHERE name = p_name;
    INSERT INTO rvbbit.cube_defs (name, version, sql, grain, description, owner, refresh_cron, category, labels)
    VALUES (p_name, v_version, p_sql,
            coalesce(nullif(btrim(p_grain), ''), cur.grain),
            coalesce(nullif(btrim(p_description), ''), cur.description),
            coalesce(nullif(btrim(p_owner), ''), cur.owner),
            cur.refresh_cron,
            coalesce(nullif(btrim(p_category), ''), cur.category),
            cur.labels);

    -- does the new SQL's output shape differ from the live table? (compare with format_type so the
    -- introspections are apples-to-apples)
    IF to_regclass(v_qual) IS NOT NULL THEN
        SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod), ',' ORDER BY a.attnum)
          INTO v_old_cols
          FROM pg_attribute a
         WHERE a.attrelid = v_qual::regclass AND a.attnum > 0 AND NOT a.attisdropped;
        BEGIN
            EXECUTE 'DROP TABLE IF EXISTS _redef_shape';
            EXECUTE format('CREATE TEMP TABLE _redef_shape AS SELECT * FROM (%s) _q WITH NO DATA', p_sql);
            SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod), ',' ORDER BY a.attnum)
              INTO v_new_cols
              FROM pg_attribute a
             WHERE a.attrelid = '_redef_shape'::regclass AND a.attnum > 0 AND NOT a.attisdropped;
            EXECUTE 'DROP TABLE IF EXISTS _redef_shape';
        EXCEPTION WHEN OTHERS THEN
            v_new_cols := NULL;     -- couldn't introspect → treat as a shape change (recreate)
        END;
    END IF;

    IF to_regclass(v_qual) IS NULL OR v_new_cols IS DISTINCT FROM v_old_cols THEN
        -- shape changed (or new): recreate the materialized table from the new SQL
        IF to_regclass(v_qual) IS NOT NULL THEN EXECUTE format('DROP TABLE %s', v_qual); END IF;
        EXECUTE format('CREATE TABLE %s USING rvbbit AS %s WITH NO DATA', v_qual, p_sql);
    END IF;

    PERFORM rvbbit.refresh_cube(p_name);   -- reload from the new def (compatible shape preserves gens)
    BEGIN PERFORM rvbbit.register_cube_node(p_name); EXCEPTION WHEN OTHERS THEN NULL; END;
    IF nullif(btrim(p_category), '') IS NOT NULL THEN
        BEGIN PERFORM rvbbit.set_category('cube', p_name, p_category, nullif(btrim(p_subcategory), ''));
        EXCEPTION WHEN OTHERS THEN NULL; END;
    END IF;
    RETURN v_version;
END $fn$;

-- list a cube's definition history (newest first).
CREATE OR REPLACE FUNCTION rvbbit.cube_versions(p_name text)
RETURNS TABLE (version integer, sql text, grain text, description text, category text, created_at timestamptz)
LANGUAGE sql STABLE AS $$
    SELECT version, sql, grain, description, category, created_at
    FROM rvbbit.cube_defs WHERE name = p_name ORDER BY version DESC;
$$;

-- roll a cube back to a prior version's definition (appends a new version restoring it).
CREATE OR REPLACE FUNCTION rvbbit.revert_cube(p_name text, p_version integer)
RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE v_sql text; v_grain text; v_desc text;
BEGIN
    SELECT sql, grain, description INTO v_sql, v_grain, v_desc
      FROM rvbbit.cube_defs WHERE name = p_name AND version = p_version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.revert_cube: cube % has no version %', p_name, p_version;
    END IF;
    RETURN rvbbit.redefine_cube(p_name, v_sql, v_grain, v_desc);
END $fn$;
