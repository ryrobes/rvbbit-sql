-- 0168: kit lifecycle hardening (KIT_PLATES_PLAN §19).
--
-- Four holes closed: PREFLIGHT (kits declare requirements; installs fail
-- with a human sentence before touching anything), VERSION REGRESSION
-- (stale artifacts can't silently downgrade), UNINSTALL (remove_kit strips
-- every kit-owned row and REPORTS data objects — never drops them; the
-- DROP CASCADE scar is doctrine), and SELF-TEST (validate_kit dry-runs
-- every plate query, action, rule, and contract against the actual box).
-- export_kit v4 opens generated scripts with the preflight assert.
-- Floor note: preflight protects targets >= THIS migration; older targets
-- still fail on the first missing function — 0168 is the turtle at the
-- bottom.

ALTER TABLE rvbbit.kits ADD COLUMN IF NOT EXISTS requires jsonb NOT NULL DEFAULT '{}'::jsonb;

-- upsert_kit v2: requires + version-regression guard. The old 5-arg
-- signature is DROPPED (not overloaded — the psycopg ambiguity trap);
-- old exported scripts call positionally and bind the new default.
DROP FUNCTION IF EXISTS rvbbit.upsert_kit(text, text, text, text, text);
CREATE OR REPLACE FUNCTION rvbbit.upsert_kit(
    p_kit text,
    p_title text,
    p_description text DEFAULT NULL,
    p_setup_sql text DEFAULT NULL,
    p_version text DEFAULT '0.1.0',
    p_requires jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
AS $uk$
DECLARE
    v_have text;
    v_have_a int[];
    v_new_a int[];
BEGIN
    SELECT version INTO v_have FROM rvbbit.kits WHERE kit = p_kit;
    IF v_have IS NOT NULL THEN
        BEGIN
            v_have_a := string_to_array(v_have, '.')::int[];
            v_new_a := string_to_array(p_version, '.')::int[];
            IF v_new_a < v_have_a THEN
                RAISE EXCEPTION 'kit % is at v% — refusing to downgrade to v%. If intentional: DELETE FROM rvbbit.kits WHERE kit = %L; then reinstall.',
                    p_kit, v_have, p_version, p_kit;
            END IF;
        EXCEPTION WHEN invalid_text_representation THEN
            NULL; -- non-numeric versions: no ordering, no guard
        END;
    END IF;
    INSERT INTO rvbbit.kits (kit, version, title, description, setup_sql, requires)
    VALUES (p_kit, p_version, p_title, p_description, p_setup_sql, coalesce(p_requires, '{}'::jsonb))
    ON CONFLICT (kit) DO UPDATE SET
        version = EXCLUDED.version,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        setup_sql = EXCLUDED.setup_sql,
        requires = EXCLUDED.requires,
        updated_at = clock_timestamp();
END
$uk$;

-- Preflight: check a requirements object against THIS box.
-- Shape: {"min_migration": "0167_rule_observability",
--         "extensions": ["pg_cron"], "operators": ["clover_llm"]}
CREATE OR REPLACE FUNCTION rvbbit.kit_preflight(p_requires jsonb)
RETURNS TABLE (requirement text, ok boolean, detail text)
LANGUAGE plpgsql
AS $kp$
DECLARE
    v text;
BEGIN
    IF p_requires ? 'min_migration' THEN
        v := p_requires->>'min_migration';
        requirement := 'migration ' || v;
        ok := EXISTS (SELECT 1 FROM rvbbit.schema_migrations m WHERE m.name >= v);
        detail := CASE WHEN ok THEN 'present'
                       ELSE 'this rvbbit install is older than the kit needs — upgrade the extension (ALTER EXTENSION pg_rvbbit UPDATE; SELECT rvbbit.migrate();)' END;
        RETURN NEXT;
    END IF;
    FOR v IN SELECT jsonb_array_elements_text(coalesce(p_requires->'extensions', '[]'::jsonb)) LOOP
        requirement := 'extension ' || v;
        ok := EXISTS (SELECT 1 FROM pg_extension e WHERE e.extname = v);
        detail := CASE WHEN ok THEN 'installed' ELSE 'CREATE EXTENSION ' || quote_ident(v) || '; (or install it on the server first)' END;
        RETURN NEXT;
    END LOOP;
    FOR v IN SELECT jsonb_array_elements_text(coalesce(p_requires->'operators', '[]'::jsonb)) LOOP
        requirement := 'operator ' || v;
        ok := EXISTS (SELECT 1 FROM rvbbit.operators o WHERE o.name = v);
        detail := CASE WHEN ok THEN 'installed' ELSE 'missing — install the capability that provides it (see rvbbit.ai catalog)' END;
        RETURN NEXT;
    END LOOP;
END
$kp$;

CREATE OR REPLACE FUNCTION rvbbit.kit_preflight_assert(p_requires jsonb)
RETURNS void
LANGUAGE plpgsql
AS $kpa$
DECLARE
    v_fail text;
BEGIN
    SELECT string_agg(requirement || ': ' || detail, E'\n  ')
    INTO v_fail
    FROM rvbbit.kit_preflight(p_requires) WHERE NOT ok;
    IF v_fail IS NOT NULL THEN
        RAISE EXCEPTION E'kit preflight failed:\n  %', v_fail;
    END IF;
END
$kpa$;

-- Uninstall: strip every kit-owned ROW; report (never drop) data objects
-- named by setup_sql. Customer data outlives the kit by doctrine.
CREATE OR REPLACE FUNCTION rvbbit.remove_kit(p_kit text)
RETURNS TABLE (kind text, name text, action text)
LANGUAGE plpgsql
AS $rk$
DECLARE
    k rvbbit.kits%ROWTYPE;
    m text[];
BEGIN
    SELECT * INTO k FROM rvbbit.kits WHERE kit = p_kit;

    FOR kind, name IN SELECT 'plate', plate_id FROM rvbbit.plates WHERE kit = p_kit LOOP
        action := 'removed'; RETURN NEXT;
    END LOOP;
    DELETE FROM rvbbit.plates WHERE kit = p_kit;

    FOR kind, name IN SELECT 'contract', module || '/' || contract_id FROM rvbbit.kit_contracts WHERE kit = p_kit LOOP
        action := 'removed'; RETURN NEXT;
    END LOOP;
    DELETE FROM rvbbit.kit_contracts WHERE kit = p_kit;

    FOR kind, name IN SELECT 'rule', rule_set || '/' || rule_id FROM rvbbit.kit_rules WHERE kit = p_kit LOOP
        action := 'removed'; RETURN NEXT;
    END LOOP;
    DELETE FROM rvbbit.kit_rules WHERE kit = p_kit;
    DELETE FROM rvbbit.kit_rule_sets WHERE kit = p_kit;
    DELETE FROM rvbbit.kit_rule_stats WHERE kit = p_kit;
    DELETE FROM rvbbit.kit_rule_log WHERE kit = p_kit;

    FOR kind, name IN SELECT 'operator', o.name FROM rvbbit.operators o WHERE o.kit = p_kit LOOP
        action := 'removed'; RETURN NEXT;
    END LOOP;
    DELETE FROM rvbbit.operators WHERE operators.kit = p_kit;

    -- The catalog entry stays: uninstalling a kit returns it to "available"
    -- (the store listing outlives the install).

    IF k.setup_sql IS NOT NULL THEN
        FOR m IN SELECT regexp_matches(k.setup_sql, 'CREATE\s+(TABLE|SCHEMA|VIEW|ROLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][\w.]*)', 'gi') LOOP
            kind := lower(m[1]); name := m[2]; action := 'left in place (holds your data — drop manually if truly done)';
            RETURN NEXT;
        END LOOP;
    END IF;

    DELETE FROM rvbbit.kits WHERE kit = p_kit;
    kind := 'kit'; name := p_kit; action := 'removed';
    RETURN NEXT;
END
$rk$;

-- Self-test: does this kit's plumbing actually run on THIS box? Dry-runs
-- every plate query (defaults bound), EXPLAINs every action with dummy
-- args, parses every rule against an empty subject, probes rule-set
-- subjects, and evaluates contracts. Read-only side effects only.
CREATE OR REPLACE FUNCTION rvbbit.validate_kit(p_kit text)
RETURNS TABLE (item text, kind text, ok boolean, detail text)
LANGUAGE plpgsql
AS $vk$
DECLARE
    p record;
    q record;
    a record;
    r record;
    rs record;
    c record;
    v_sql text;
    v_name text;
    v_params jsonb;
BEGIN
    FOR p IN SELECT * FROM rvbbit.plates WHERE plates.kit = p_kit LOOP
        v_params := coalesce(p.params, '[]'::jsonb);
        FOR q IN SELECT key, value FROM jsonb_each(coalesce(p.queries, '{}'::jsonb)) LOOP
            item := p.plate_id || ' · query ' || q.key;
            kind := 'query';
            IF q.value ? 'database' THEN
                ok := true;
                detail := 'routed to database ' || (q.value->>'database') || ' — not validated here';
                RETURN NEXT;
                CONTINUE;
            END IF;
            v_sql := q.value->>'sql';
            -- bind {{ params.x }} with declared defaults
            FOR v_name IN SELECT jsonb_array_elements(v_params)->>'name' LOOP
                v_sql := regexp_replace(v_sql,
                    '\{\{\s*params\.' || v_name || '\s*\}\}',
                    coalesce((
                        SELECT CASE
                            WHEN jsonb_typeof(e->'default') = 'number' THEN e->>'default'
                            WHEN e->'default' IS NULL OR jsonb_typeof(e->'default') = 'null' THEN 'NULL'
                            ELSE quote_literal(e->>'default') END
                        FROM jsonb_array_elements(v_params) e WHERE e->>'name' = v_name), 'NULL'),
                    'g');
            END LOOP;
            BEGIN
                EXECUTE 'SELECT 1 FROM (' || rtrim(btrim(v_sql), ';') || ') _probe LIMIT 1';
                ok := true; detail := 'runs';
            EXCEPTION WHEN others THEN
                ok := false; detail := SQLERRM;
            END;
            RETURN NEXT;
        END LOOP;

        FOR a IN SELECT key, value FROM jsonb_each(coalesce(p.actions, '{}'::jsonb)) LOOP
            item := p.plate_id || ' · action ' || a.key;
            kind := 'action';
            v_sql := a.value->>'sql';
            FOR v_name, v_params IN
                SELECT e->>'name', e FROM jsonb_array_elements(coalesce(a.value->'args', '[]'::jsonb)) e
            LOOP
                v_sql := regexp_replace(v_sql,
                    '\{\{\s*' || v_name || '\s*\}\}',
                    CASE coalesce(v_params->>'type', 'text')
                        WHEN 'number' THEN '0' WHEN 'boolean' THEN 'false' ELSE quote_literal('') END,
                    'g');
            END LOOP;
            BEGIN
                EXECUTE 'EXPLAIN ' || rtrim(btrim(v_sql), ';');
                ok := true; detail := 'parses and plans (not executed)';
            EXCEPTION WHEN others THEN
                ok := false; detail := SQLERRM;
            END;
            RETURN NEXT;
        END LOOP;
        v_params := NULL;
    END LOOP;

    FOR r IN SELECT * FROM rvbbit.kit_rules WHERE kit_rules.kit = p_kit AND active LOOP
        item := r.rule_set || ' · rule ' || r.rule_id;
        kind := 'rule';
        BEGIN
            EXECUTE 'SELECT (' || r.when_sql || ') FROM (SELECT ''{}''::jsonb AS subject) _s';
            ok := true; detail := 'evaluates';
        EXCEPTION WHEN others THEN
            ok := false; detail := SQLERRM;
        END;
        RETURN NEXT;
    END LOOP;

    FOR rs IN SELECT * FROM rvbbit.kit_rule_sets WHERE kit_rule_sets.kit = p_kit LOOP
        item := rs.rule_set || ' · subject';
        kind := 'rule_set';
        BEGIN
            EXECUTE 'SELECT 1 FROM (' || rtrim(btrim(rs.subject_sql), ';') || ') _s LIMIT 1';
            ok := true; detail := 'runs';
        EXCEPTION WHEN others THEN
            ok := false; detail := SQLERRM;
        END;
        RETURN NEXT;
    END LOOP;

    FOR c IN SELECT * FROM rvbbit.kit_contract_status(p_kit) LOOP
        item := c.module || ' · contract ' || c.contract_id;
        kind := 'contract';
        ok := c.sample IS NULL OR c.sample NOT LIKE 'contract error:%';
        detail := CASE WHEN ok
                       THEN CASE WHEN c.ok THEN 'evaluable · currently green' ELSE 'evaluable · currently red (' || c.violations || ' violations)' END
                       ELSE c.sample END;
        RETURN NEXT;
    END LOOP;
END
$vk$;

-- export_kit v4: generated scripts open with the preflight assert, carry
-- requires into upsert_kit, and close with the self-test hint.
CREATE OR REPLACE FUNCTION rvbbit.export_kit(p_kit text)
RETURNS text
LANGUAGE plpgsql
AS $ek$
DECLARE
    k rvbbit.kits%ROWTYPE;
    v_out text;
    v_plates text;
    v_modules text;
    v_contracts text;
    v_rules text;
    v_rule_sets text;
    v_ops text;
BEGIN
    SELECT * INTO k FROM rvbbit.kits WHERE kit = p_kit;
    IF NOT FOUND THEN
        IF NOT EXISTS (SELECT 1 FROM rvbbit.plates WHERE kit = p_kit) THEN
            RAISE EXCEPTION 'export_kit: no kit named %', p_kit;
        END IF;
        k.kit := p_kit;
        k.version := '0.0.0';
        k.title := p_kit;
        k.description := NULL;
        k.setup_sql := NULL;
    END IF;

    v_out := format(E'-- rvbbit kit: %s v%s\n-- generated by rvbbit.export_kit() · api rvbbit.kit/v1\n-- Install: run this whole file in ONE transaction (validate with ROLLBACK first).\n\n',
                    k.kit, k.version);

    IF k.requires IS NOT NULL AND k.requires <> '{}'::jsonb THEN
        v_out := v_out || E'-- ── preflight (fails with a human sentence before touching anything) ──\n'
              || format(E'SELECT rvbbit.kit_preflight_assert(%s::jsonb);\n\n',
                        rvbbit._kit_dq(k.requires::text, 'kreq'));
    END IF;

    v_out := v_out || format(E'SELECT rvbbit.upsert_kit(%L, %L, %s, %s, %L, %s::jsonb);\n\n',
        k.kit, k.title,
        CASE WHEN k.description IS NULL THEN 'NULL' ELSE quote_literal(k.description) END,
        CASE WHEN k.setup_sql IS NULL THEN 'NULL' ELSE rvbbit._kit_dq(k.setup_sql, 'ksetup') END,
        k.version,
        rvbbit._kit_dq(coalesce(k.requires, '{}'::jsonb)::text, 'kreq2'));

    IF k.setup_sql IS NOT NULL THEN
        v_out := v_out || E'-- ── setup (kit-owned schemas/tables/views/roles) ──\n'
              || k.setup_sql || E'\n\n';
    END IF;

    SELECT string_agg(
        format(E'SELECT rvbbit.upsert_plate(%L, %L, %s, %s::jsonb, %s::jsonb, %s::jsonb, %L, %s, %s);',
            p.plate_id, p.title,
            rvbbit._kit_dq(p.template, 'ktpl'),
            rvbbit._kit_dq(p.queries::text, 'kq'),
            rvbbit._kit_dq(p.actions::text, 'ka'),
            rvbbit._kit_dq(p.params::text, 'kp'),
            p.kit,
            CASE WHEN p.description IS NULL THEN 'NULL' ELSE quote_literal(p.description) END,
            p.template_version),
        E'\n' ORDER BY p.plate_id)
    INTO v_plates
    FROM rvbbit.plates p WHERE p.kit = p_kit;

    SELECT string_agg(
        format('UPDATE rvbbit.plates SET module = %L WHERE plate_id = %L;', p.module, p.plate_id),
        E'\n' ORDER BY p.plate_id)
    INTO v_modules
    FROM rvbbit.plates p WHERE p.kit = p_kit AND p.module IS NOT NULL;

    SELECT string_agg(
        format('SELECT rvbbit.upsert_kit_contract(%L, %L, %L, %s, %s);',
            c.kit, c.module, c.contract_id,
            rvbbit._kit_dq(c.violations_sql, 'kv'),
            CASE WHEN c.description IS NULL THEN 'NULL' ELSE quote_literal(c.description) END),
        E'\n' ORDER BY c.module, c.contract_id)
    INTO v_contracts
    FROM rvbbit.kit_contracts c WHERE c.kit = p_kit;

    SELECT string_agg(
        format('SELECT rvbbit.upsert_kit_rule(%L, %L, %L, %s, %s::jsonb, %s, %s);',
            r.kit, r.rule_set, r.rule_id,
            rvbbit._kit_dq(r.when_sql, 'kw'),
            rvbbit._kit_dq(r.verdict::text, 'kvj'),
            r.priority,
            CASE WHEN r.description IS NULL THEN 'NULL' ELSE quote_literal(r.description) END),
        E'\n' ORDER BY r.rule_set, r.priority, r.rule_id)
    INTO v_rules
    FROM rvbbit.kit_rules r WHERE r.kit = p_kit AND r.active;

    SELECT string_agg(
        format('SELECT rvbbit.upsert_kit_rule_set(%L, %L, %s, %s);',
            rs.kit, rs.rule_set,
            rvbbit._kit_dq(rs.subject_sql, 'krs'),
            CASE WHEN rs.description IS NULL THEN 'NULL' ELSE quote_literal(rs.description) END),
        E'\n' ORDER BY rs.rule_set)
    INTO v_rule_sets
    FROM rvbbit.kit_rule_sets rs WHERE rs.kit = p_kit;

    SELECT string_agg(
        format(E'DELETE FROM rvbbit.operators WHERE name = %L;\nINSERT INTO rvbbit.operators SELECT * FROM jsonb_populate_record(NULL::rvbbit.operators, %s::jsonb);',
            o.name,
            rvbbit._kit_dq(to_jsonb(o)::text, 'kop')),
        E'\n' ORDER BY o.name)
    INTO v_ops
    FROM rvbbit.operators o WHERE o.kit = p_kit;

    v_out := v_out
        || E'-- ── plates ──\n' || coalesce(v_plates, '-- (none)') || E'\n\n'
        || E'-- ── module assignments ──\n' || coalesce(v_modules, '-- (none)') || E'\n\n'
        || E'-- ── contracts (empty result = green) ──\n' || coalesce(v_contracts, '-- (none)') || E'\n\n'
        || E'-- ── rules (decision tables; first match wins) ──\n' || coalesce(v_rules, '-- (none)') || E'\n\n'
        || E'-- ── rule sets (subject registrations for live observability) ──\n' || coalesce(v_rule_sets, '-- (none)') || E'\n\n'
        || E'-- ── operators (kit-scoped) ──\n' || coalesce(v_ops, '-- (none)') || E'\n\n'
        || E'-- ── metric_defs (reserved: definitions bound to kit canonical views) ──\n-- (none)\n\n'
        || E'-- ── cube_defs (reserved) ──\n-- (none)\n\n'
        || format(E'-- After COMMIT, self-test the arrival:\n--   SELECT * FROM rvbbit.validate_kit(%L) WHERE NOT ok;\n', k.kit);

    RETURN v_out;
END
$ek$;

-- publish_kit v2: requirements ride the manifest.
CREATE OR REPLACE FUNCTION rvbbit.publish_kit(p_kit text)
RETURNS text
LANGUAGE plpgsql
AS $pk$
DECLARE
    k rvbbit.kits%ROWTYPE;
    v_sql text;
    v_id text;
    v_manifest jsonb;
BEGIN
    SELECT * INTO k FROM rvbbit.kits WHERE kit = p_kit;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'publish_kit: register the kit first with rvbbit.upsert_kit()';
    END IF;
    v_sql := rvbbit.export_kit(p_kit);
    v_id := 'kit/' || p_kit;
    v_manifest := jsonb_build_object(
        'api_version', 'rvbbit.capability/v1',
        'kind', 'kit',
        'name', p_kit,
        'title', coalesce(k.title, p_kit),
        'description', coalesce(k.description, ''),
        'version', k.version,
        'requires', coalesce(k.requires, '{}'::jsonb),
        'tags', jsonb_build_array('kit', 'plates'),
        'install_sql', v_sql
    );
    INSERT INTO rvbbit.capability_catalog
        (id, name, title, description, tags, kind, operators, manifest, catalog_entry, catalog_source, active)
    VALUES
        (v_id, p_kit, coalesce(k.title, p_kit), coalesce(k.description, ''),
         ARRAY['kit', 'plates'], 'kit', ARRAY[]::text[], v_manifest, v_manifest, 'local', true)
    ON CONFLICT (id) DO UPDATE SET
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        manifest = EXCLUDED.manifest,
        catalog_entry = EXCLUDED.catalog_entry,
        active = true,
        updated_at = clock_timestamp();
    RETURN v_id;
END
$pk$;
