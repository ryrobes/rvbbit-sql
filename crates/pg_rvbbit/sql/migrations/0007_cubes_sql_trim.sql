-- 0007_cubes_sql_trim — make define_cube tolerant of a trailing semicolon.
--
-- define_cube embeds the cube SQL into `CREATE TABLE cubes.<name> USING rvbbit AS <sql> WITH NO
-- DATA`. A trailing ';' on <sql> splits that into two statements and orphans `WITH NO DATA`,
-- which fails with "syntax error at or near DATA". Editors / pasted queries routinely carry a
-- trailing ';', so strip it (and trailing whitespace) once at the top — the cleaned SQL is what
-- gets stored in cube_defs, so refresh_cube/snapshot_load see clean SQL too. CREATE OR REPLACE
-- only; safe to apply standalone via psql (no rebuild required).

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
    -- strip trailing semicolons + whitespace (see header) before embedding/storing
    p_sql := rtrim(btrim(p_sql), E' \t\r\n;');
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
END $fn$;
