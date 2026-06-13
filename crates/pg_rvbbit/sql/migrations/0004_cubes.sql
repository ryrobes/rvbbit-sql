-- 0004_cubes — the cube primitive (V1).
--
-- A cube is a wide, reasoned-about join MATERIALIZED as an rvbbit table (cubes.<name>),
-- so it inherits vortex/parquet acceleration + AS-OF + freshness + Drift for free. It is the
-- curated middle of a metrics -> cubes -> raw discovery gradient. define_cube mirrors
-- define_metric (advisory-lock versioned, append-only def); refresh_cube reuses the Temporal
-- Mirror's rvbbit.snapshot_load (TRUNCATE+load+compact+set_visible_floor) so each refresh is a
-- REPLACE *and* a retained AS-OF generation; the cube registers as a kind='cube' catalog node
-- so search_data finds it ahead of raw tables. See docs/CUBES_PLAN.md.

CREATE SCHEMA IF NOT EXISTS cubes;

-- immutable, append-only definitions (one row per version)
CREATE TABLE IF NOT EXISTS rvbbit.cube_defs (
    cube_id      bigint GENERATED ALWAYS AS IDENTITY,
    name         text        NOT NULL,
    version      integer     NOT NULL,
    sql          text        NOT NULL,
    grain        text,
    description  text,
    owner        text,
    refresh_cron text,
    category     text,
    labels       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);
CREATE INDEX IF NOT EXISTS cube_defs_name_created_idx
    ON rvbbit.cube_defs (name, created_at DESC, version DESC);

-- mutable control (one row per cube; survives re-definition)
CREATE TABLE IF NOT EXISTS rvbbit.cube_control (
    cube_name    text PRIMARY KEY,
    enabled      boolean DEFAULT true,
    refreshed_at timestamptz,
    last_rows    bigint,
    last_error   text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- latest definition per cube
CREATE OR REPLACE VIEW rvbbit.cube_catalog AS
SELECT DISTINCT ON (name)
    name, version, sql, grain, description, owner, refresh_cron, category, labels, created_at
FROM rvbbit.cube_defs
ORDER BY name, created_at DESC, version DESC;

-- register a cube as a kind='cube' catalog node so data_search finds it (best-effort vector)
CREATE OR REPLACE FUNCTION rvbbit.register_cube_node(p_name text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
    v_node bigint; v_doc text; v_desc text; v_grain text; v_cols text;
    v_vec real[]; v_graph text := 'db_catalog';
BEGIN
    SELECT description, grain INTO v_desc, v_grain FROM rvbbit.cube_catalog WHERE name = p_name;
    SELECT string_agg(column_name || ' (' || data_type || ')', ', ' ORDER BY ordinal_position)
      INTO v_cols
      FROM information_schema.columns WHERE table_schema = 'cubes' AND table_name = p_name;
    v_doc := format('Cube cubes.%s — %s. Grain: %s. Columns: %s',
                    p_name, coalesce(v_desc, '(no description)'),
                    coalesce(v_grain, 'unspecified'), coalesce(v_cols, ''));
    v_node := rvbbit.kg_assert_node('cube', 'cubes.' || p_name,
                jsonb_build_object('schema', 'cubes', 'cube_name', p_name,
                                   'grain', v_grain, 'description', v_desc),
                1.0, '', 0.0, v_graph);
    BEGIN
        v_vec := rvbbit.embed(v_doc, '', 'document');
    EXCEPTION WHEN OTHERS THEN
        v_vec := NULL;            -- no embedder configured -> register lexically only
    END;
    INSERT INTO rvbbit.catalog_docs
        (node_id, graph_id, kind, schema_name, rel_name, col_name, doc, embedding, embedded_at, updated_at)
    VALUES (v_node, v_graph, 'cube', 'cubes', p_name, NULL, v_doc, v_vec,
            CASE WHEN v_vec IS NOT NULL THEN now() END, now())
    ON CONFLICT (graph_id, node_id) DO UPDATE SET
        kind = EXCLUDED.kind, doc = EXCLUDED.doc, embedding = EXCLUDED.embedding,
        embedded_at = EXCLUDED.embedded_at, updated_at = now();
END;
$fn$;

-- refresh: in-place snapshot reload (REPLACE + a retained AS-OF generation) via the mirror's path
CREATE OR REPLACE FUNCTION rvbbit.refresh_cube(p_name text)
RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    v_dest regclass := to_regclass('cubes.' || quote_ident(p_name));
    v_sql text; v_rows bigint;
BEGIN
    IF v_dest IS NULL THEN
        RAISE EXCEPTION 'rvbbit.refresh_cube: cube % does not exist (define it first)', p_name;
    END IF;
    SELECT sql INTO v_sql FROM rvbbit.cube_catalog WHERE name = p_name;
    IF v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.refresh_cube: no definition for cube %', p_name;
    END IF;
    BEGIN
        SELECT rows_loaded INTO v_rows FROM rvbbit.snapshot_load(v_dest, v_sql);
        UPDATE rvbbit.cube_control
           SET refreshed_at = now(), last_rows = v_rows, last_error = NULL, updated_at = now()
         WHERE cube_name = p_name;
    EXCEPTION WHEN OTHERS THEN
        UPDATE rvbbit.cube_control SET last_error = SQLERRM, updated_at = now() WHERE cube_name = p_name;
        RAISE;
    END;
    RETURN v_rows;
END;
$fn$;

-- define (or re-define) a cube: versioned def + materialize cubes.<name> + register in catalog
CREATE OR REPLACE FUNCTION rvbbit.define_cube(
    p_name         text,
    p_sql          text,
    p_grain        text  DEFAULT NULL,
    p_description  text  DEFAULT NULL,
    p_owner        text  DEFAULT NULL,
    p_refresh_cron text  DEFAULT NULL,
    p_category     text  DEFAULT NULL,
    p_labels       jsonb DEFAULT '{}'::jsonb
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_version integer;
    v_qual    text := 'cubes.' || quote_ident(p_name);
BEGIN
    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_cube: name is required';
    END IF;
    IF p_name !~ '^[a-z_][a-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.define_cube: name must be a lowercase identifier (got %)', p_name;
    END IF;
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RAISE EXCEPTION 'rvbbit.define_cube: sql is required';
    END IF;
    CREATE SCHEMA IF NOT EXISTS cubes;
    -- serialize version allocation per cube name
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.cube:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version FROM rvbbit.cube_defs WHERE name = p_name;
    INSERT INTO rvbbit.cube_defs
        (name, version, sql, grain, description, owner, refresh_cron, category, labels)
    VALUES
        (p_name, v_version, p_sql, p_grain, p_description, p_owner, p_refresh_cron, p_category,
         coalesce(p_labels, '{}'::jsonb));
    INSERT INTO rvbbit.cube_control (cube_name) VALUES (p_name) ON CONFLICT (cube_name) DO NOTHING;
    -- materialize the rvbbit table shell from the SQL's shape if it doesn't exist yet
    -- (to change a cube's COLUMN SHAPE, drop_cube first; a same-shape redefine just reloads)
    IF to_regclass(v_qual) IS NULL THEN
        EXECUTE format('CREATE TABLE %s USING rvbbit AS %s WITH NO DATA', v_qual, p_sql);
    END IF;
    PERFORM rvbbit.refresh_cube(p_name);          -- initial load + compact + snapshot floor
    BEGIN
        PERFORM rvbbit.register_cube_node(p_name);  -- best-effort catalog/semantic registration
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'rvbbit.define_cube: catalog registration for % failed: %', p_name, SQLERRM;
    END;
    RETURN v_version;
END;
$fn$;

-- drop a cube entirely (table + def + control + catalog node)
CREATE OR REPLACE FUNCTION rvbbit.drop_cube(p_name text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE v_qual text := 'cubes.' || quote_ident(p_name);
BEGIN
    IF to_regclass(v_qual) IS NOT NULL THEN
        EXECUTE format('DROP TABLE %s', v_qual);
    END IF;
    DELETE FROM rvbbit.cube_defs    WHERE name = p_name;
    DELETE FROM rvbbit.cube_control WHERE cube_name = p_name;
    BEGIN
        DELETE FROM rvbbit.catalog_docs WHERE kind = 'cube' AND schema_name = 'cubes' AND rel_name = p_name;
        DELETE FROM rvbbit.kg_nodes     WHERE kind = 'cube' AND label = 'cubes.' || p_name;
    EXCEPTION WHEN OTHERS THEN NULL;   -- catalog may not be present
    END;
END;
$fn$;

-- list cubes (the agent's curated entry point — look here before raw tables)
CREATE OR REPLACE FUNCTION rvbbit.cubes()
RETURNS TABLE (name text, grain text, description text, category text,
               version integer, refreshed_at timestamptz, rows bigint)
LANGUAGE sql STABLE AS $fn$
    SELECT c.name, c.grain, c.description, c.category, c.version, ctl.refreshed_at, ctl.last_rows
    FROM rvbbit.cube_catalog c
    LEFT JOIN rvbbit.cube_control ctl ON ctl.cube_name = c.name
    ORDER BY c.name;
$fn$;

-- describe one cube: def + grain + columns + freshness (the agent's grounding)
CREATE OR REPLACE FUNCTION rvbbit.describe_cube(p_name text)
RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT jsonb_build_object(
        'name', c.name, 'grain', c.grain, 'description', c.description, 'category', c.category,
        'version', c.version, 'sql', c.sql, 'refresh_cron', c.refresh_cron,
        'refreshed_at', ctl.refreshed_at, 'rows', ctl.last_rows,
        'columns', (SELECT jsonb_agg(jsonb_build_object('name', column_name, 'type', data_type)
                            ORDER BY ordinal_position)
                    FROM information_schema.columns
                    WHERE table_schema = 'cubes' AND table_name = c.name))
    FROM rvbbit.cube_catalog c
    LEFT JOIN rvbbit.cube_control ctl ON ctl.cube_name = c.name
    WHERE c.name = p_name;
$fn$;
