-- Upgrade pg_rvbbit 2.0.15 -> 2.0.16
--
-- Cross-cutting org taxonomy: one mutable 2-level category tree shared by metrics
-- and alerts. Purely additive — a new table + functions, and the two catalog
-- views gain (category, subcategory) via a LEFT JOIN. No existing table altered.

CREATE TABLE IF NOT EXISTS rvbbit.entity_categories (
    entity_kind  text NOT NULL,            -- 'metric' | 'alert'
    entity_name  text NOT NULL,
    category     text,
    subcategory  text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_kind, entity_name),
    CONSTRAINT entity_categories_subcat_requires_cat
        CHECK (subcategory IS NULL OR category IS NOT NULL)
);

CREATE OR REPLACE FUNCTION rvbbit.set_category(
    p_kind text, p_name text, p_category text DEFAULT NULL, p_subcategory text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql AS $fn$
DECLARE
    v_cat text := nullif(btrim(coalesce(p_category, '')), '');
    v_sub text := nullif(btrim(coalesce(p_subcategory, '')), '');
BEGIN
    IF p_kind NOT IN ('metric', 'alert') THEN
        RAISE EXCEPTION 'rvbbit.set_category: kind must be metric or alert (got %)', p_kind;
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
END;
$fn$;

CREATE OR REPLACE FUNCTION rvbbit.category_options(p_kind text DEFAULT NULL)
RETURNS TABLE(category text, subcategory text)
LANGUAGE sql STABLE AS $fn$
    SELECT DISTINCT category, subcategory
    FROM rvbbit.entity_categories
    WHERE category IS NOT NULL
      AND (p_kind IS NULL OR entity_kind = p_kind)
    ORDER BY category, subcategory NULLS FIRST;
$fn$;

CREATE OR REPLACE VIEW rvbbit.metric_catalog AS
SELECT DISTINCT ON (m.name)
    m.name, m.version, m.sql, m.params, m.grain, m.description, m.owner, m.labels, m.check_sql, m.created_at,
    ec.category, ec.subcategory
FROM rvbbit.metric_defs m
LEFT JOIN rvbbit.entity_categories ec ON ec.entity_kind = 'metric' AND ec.entity_name = m.name
ORDER BY m.name, m.created_at DESC, m.version DESC;

CREATE OR REPLACE VIEW rvbbit.alert_catalog AS
SELECT DISTINCT ON (r.name)
    r.name, r.version, r.condition_spec, r.fire_policy, r.action_spec,
    r.cardinality, r.fan_out_cap, r.description, r.owner, r.labels, r.created_at,
    coalesce(c.enabled, true)                             AS enabled,
    c.muted_until,
    (c.muted_until IS NOT NULL AND c.muted_until > now()) AS muted,
    coalesce(c.cadence_tier, 'normal')                    AS cadence_tier,
    ec.category, ec.subcategory
FROM rvbbit.alert_rules r
LEFT JOIN rvbbit.alert_control c ON c.name = r.name
LEFT JOIN rvbbit.entity_categories ec ON ec.entity_kind = 'alert' AND ec.entity_name = r.name
ORDER BY r.name, r.created_at DESC, r.version DESC;
