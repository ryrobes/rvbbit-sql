-- 0262: Cubes are an intentional history-bearing exception to the new
-- current-only registry default. A newly materialized cube opts into retained
-- history before its first snapshot refresh; redefining an existing cube does
-- not override an operator's explicit policy choice.

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
    v_created boolean := false;
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
    p_sql := rtrim(btrim(p_sql), E' \t\r\n;');
    CREATE SCHEMA IF NOT EXISTS cubes;
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.cube:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1
      INTO v_version
      FROM rvbbit.cube_defs
     WHERE name = p_name;
    INSERT INTO rvbbit.cube_defs
        (name, version, sql, grain, description, owner, refresh_cron, category, labels)
    VALUES
        (p_name, v_version, p_sql, p_grain, p_description, p_owner, p_refresh_cron, p_category,
         coalesce(p_labels, '{}'::jsonb));
    INSERT INTO rvbbit.cube_control (cube_name)
    VALUES (p_name)
    ON CONFLICT (cube_name) DO NOTHING;

    IF to_regclass(v_qual) IS NULL THEN
        EXECUTE format('CREATE TABLE %s USING rvbbit AS %s WITH NO DATA', v_qual, p_sql);
        v_created := true;
    END IF;
    IF v_created THEN
        PERFORM rvbbit.set_acceleration_storage_policy(
            v_qual::regclass,
            history_policy => 'retained'
        );
    END IF;

    PERFORM rvbbit.refresh_cube(p_name);
    BEGIN
        PERFORM rvbbit.register_cube_node(p_name);
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'rvbbit.define_cube: catalog registration for % failed: %', p_name, SQLERRM;
    END;
    IF nullif(btrim(p_category), '') IS NOT NULL AND p_category <> 'proposed' THEN
        BEGIN
            PERFORM rvbbit.set_category('cube', p_name, p_category, NULL);
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END IF;
    RETURN v_version;
END
$fn$;

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
    cur rvbbit.cube_defs%ROWTYPE;
    v_version integer;
    v_old_cols text;
    v_new_cols text;
    v_recreated boolean := false;
BEGIN
    SELECT * INTO cur
      FROM rvbbit.cube_defs
     WHERE name = p_name
     ORDER BY created_at DESC, version DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.redefine_cube: cube % does not exist (use define_cube)', p_name;
    END IF;
    IF nullif(btrim(p_sql), '') IS NULL THEN
        RAISE EXCEPTION 'rvbbit.redefine_cube: sql is required';
    END IF;
    p_sql := rtrim(btrim(p_sql), E' \t\r\n;');

    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.cube:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1
      INTO v_version
      FROM rvbbit.cube_defs
     WHERE name = p_name;
    INSERT INTO rvbbit.cube_defs
        (name, version, sql, grain, description, owner, refresh_cron, category, labels)
    VALUES (
        p_name,
        v_version,
        p_sql,
        coalesce(nullif(btrim(p_grain), ''), cur.grain),
        coalesce(nullif(btrim(p_description), ''), cur.description),
        coalesce(nullif(btrim(p_owner), ''), cur.owner),
        cur.refresh_cron,
        coalesce(nullif(btrim(p_category), ''), cur.category),
        cur.labels
    );

    IF to_regclass(v_qual) IS NOT NULL THEN
        SELECT string_agg(
                   a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                   ',' ORDER BY a.attnum
               )
          INTO v_old_cols
          FROM pg_attribute a
         WHERE a.attrelid = v_qual::regclass
           AND a.attnum > 0
           AND NOT a.attisdropped;
        BEGIN
            EXECUTE 'DROP TABLE IF EXISTS _redef_shape';
            EXECUTE format(
                'CREATE TEMP TABLE _redef_shape AS SELECT * FROM (%s) _q WITH NO DATA',
                p_sql
            );
            SELECT string_agg(
                       a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                       ',' ORDER BY a.attnum
                   )
              INTO v_new_cols
              FROM pg_attribute a
             WHERE a.attrelid = '_redef_shape'::regclass
               AND a.attnum > 0
               AND NOT a.attisdropped;
            EXECUTE 'DROP TABLE IF EXISTS _redef_shape';
        EXCEPTION WHEN OTHERS THEN
            v_new_cols := NULL;
        END;
    END IF;

    IF to_regclass(v_qual) IS NULL OR v_new_cols IS DISTINCT FROM v_old_cols THEN
        IF to_regclass(v_qual) IS NOT NULL THEN
            EXECUTE format('DROP TABLE %s', v_qual);
        END IF;
        EXECUTE format('CREATE TABLE %s USING rvbbit AS %s WITH NO DATA', v_qual, p_sql);
        v_recreated := true;
    END IF;
    IF v_recreated THEN
        PERFORM rvbbit.set_acceleration_storage_policy(
            v_qual::regclass,
            history_policy => 'retained'
        );
    END IF;

    PERFORM rvbbit.refresh_cube(p_name);
    BEGIN
        PERFORM rvbbit.register_cube_node(p_name);
    EXCEPTION WHEN OTHERS THEN
        NULL;
    END;
    IF nullif(btrim(p_category), '') IS NOT NULL THEN
        BEGIN
            PERFORM rvbbit.set_category(
                'cube', p_name, p_category, nullif(btrim(p_subcategory), '')
            );
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END IF;
    RETURN v_version;
END
$fn$;
