-- 0022_cube_categories — make cube categories mutable + shared (like metrics).
--
-- Cubes already had a category, but on cube_defs (the IMMUTABLE versioned def) — so you couldn't
-- recategorize without a redefine, and it was a separate free-text field from the shared
-- entity_categories taxonomy metrics use. This moves the canonical cube category to entity_categories
-- (mutable, set via set_category('cube', name, …)), keeps cube_defs.category as a fallback, and
-- backfills existing cubes. Net: one category system across cubes + metrics, recategorize without a
-- new version, and category_options('cube') works for autocomplete. Additive + idempotent.

-- set_category must accept kind='cube' (it previously allowed only metric/alert, so every
-- set_category('cube', …) — including accept_proposal's — was silently failing under its
-- best-effort wrapper). Extend the allowed kinds.
CREATE OR REPLACE FUNCTION rvbbit.set_category(
    p_kind text, p_name text, p_category text DEFAULT NULL, p_subcategory text DEFAULT NULL
) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
    v_cat text := nullif(btrim(coalesce(p_category, '')), '');
    v_sub text := nullif(btrim(coalesce(p_subcategory, '')), '');
BEGIN
    IF p_kind NOT IN ('metric', 'alert', 'cube') THEN
        RAISE EXCEPTION 'rvbbit.set_category: kind must be metric, alert or cube (got %)', p_kind;
    END IF;
    IF v_sub IS NOT NULL AND v_cat IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_category: subcategory requires a category';
    END IF;
    IF v_cat IS NULL THEN
        DELETE FROM rvbbit.entity_categories WHERE entity_kind = p_kind AND entity_name = p_name;
        RETURN;
    END IF;
    INSERT INTO rvbbit.entity_categories (entity_kind, entity_name, category, subcategory, updated_at)
    VALUES (p_kind, p_name, v_cat, v_sub, now())
    ON CONFLICT (entity_kind, entity_name) DO UPDATE
       SET category = EXCLUDED.category, subcategory = EXCLUDED.subcategory, updated_at = now();
END $fn$;

-- backfill: each cube's latest cube_defs.category → the shared taxonomy (skip the 'proposed' placeholder)
INSERT INTO rvbbit.entity_categories (entity_kind, entity_name, category, subcategory)
SELECT 'cube', d.name, d.category, NULL
FROM (
    SELECT DISTINCT ON (name) name, category
    FROM rvbbit.cube_defs
    ORDER BY name, created_at DESC, version DESC
) d
WHERE nullif(btrim(d.category), '') IS NOT NULL AND d.category <> 'proposed'
ON CONFLICT (entity_kind, entity_name) DO NOTHING;

-- cube_catalog now reads the category from the shared taxonomy (falling back to cube_defs.category),
-- and exposes subcategory. (CREATE OR REPLACE keeps existing columns in place + adds subcategory last.)
CREATE OR REPLACE VIEW rvbbit.cube_catalog AS
SELECT DISTINCT ON (d.name)
    d.name, d.version, d.sql, d.grain, d.description, d.owner, d.refresh_cron,
    coalesce(ec.category, d.category) AS category,
    d.labels, d.created_at,
    ec.subcategory AS subcategory
FROM rvbbit.cube_defs d
LEFT JOIN rvbbit.entity_categories ec ON ec.entity_kind = 'cube' AND ec.entity_name = d.name
ORDER BY d.name, d.created_at DESC, d.version DESC;

-- define_cube also writes the category to the shared (mutable) taxonomy.
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
    p_sql := rtrim(btrim(p_sql), E' \t\r\n;');
    CREATE SCHEMA IF NOT EXISTS cubes;
    PERFORM pg_advisory_xact_lock(hashtextextended('rvbbit.cube:' || p_name, 0));
    SELECT coalesce(max(version), 0) + 1 INTO v_version FROM rvbbit.cube_defs WHERE name = p_name;
    INSERT INTO rvbbit.cube_defs
        (name, version, sql, grain, description, owner, refresh_cron, category, labels)
    VALUES
        (p_name, v_version, p_sql, p_grain, p_description, p_owner, p_refresh_cron, p_category,
         coalesce(p_labels, '{}'::jsonb));
    INSERT INTO rvbbit.cube_control (cube_name) VALUES (p_name) ON CONFLICT (cube_name) DO NOTHING;
    IF to_regclass(v_qual) IS NULL THEN
        EXECUTE format('CREATE TABLE %s USING rvbbit AS %s WITH NO DATA', v_qual, p_sql);
    END IF;
    PERFORM rvbbit.refresh_cube(p_name);
    BEGIN
        PERFORM rvbbit.register_cube_node(p_name);
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'rvbbit.define_cube: catalog registration for % failed: %', p_name, SQLERRM;
    END;
    -- the category lands in the shared (mutable) taxonomy too (skip the 'proposed' placeholder)
    IF nullif(btrim(p_category), '') IS NOT NULL AND p_category <> 'proposed' THEN
        BEGIN PERFORM rvbbit.set_category('cube', p_name, p_category, NULL);
        EXCEPTION WHEN OTHERS THEN NULL; END;
    END IF;
    RETURN v_version;
END $fn$;
