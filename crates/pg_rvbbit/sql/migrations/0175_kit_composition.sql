-- 0175: kit composition — foundation kits (KIT_PLATES_PLAN §22).
--
-- Sub-kits are just kits; domain kits DEPEND on them and extend around
-- them. Three seams closed:
--   requires.kits    — preflight understands ["scheduling>=0.3.0", "crm"]
--   dependents guard — remove_kit refuses to strand a dependent kit
--   cross-kit listen — a plate may LISTEN to other kits' reactivity
--                      events (an hvac overlay refreshes when a
--                      scheduling action fires). set_plate_listens().
-- Tweaks-are-rows doctrine: foundations expose variability as config
-- tables; domain kits seed rows, never fork code.

-- kit_preflight v2: kits requirement ("name" or "name>=x.y.z").
CREATE OR REPLACE FUNCTION rvbbit.kit_preflight(p_requires jsonb)
RETURNS TABLE (requirement text, ok boolean, detail text)
LANGUAGE plpgsql
AS $kp$
DECLARE
    v text;
    v_name text;
    v_min text;
    v_have text;
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
    FOR v IN SELECT jsonb_array_elements_text(coalesce(p_requires->'kits', '[]'::jsonb)) LOOP
        v_name := btrim(split_part(v, '>=', 1));
        v_min := nullif(btrim(split_part(v, '>=', 2)), '');
        requirement := 'kit ' || v;
        SELECT k.version INTO v_have FROM rvbbit.kits k WHERE k.kit = v_name;
        IF v_have IS NULL THEN
            ok := false;
            detail := 'foundation kit not set up — set up ' || quote_literal(v_name) || ' from the Plates shelf first';
        ELSIF v_min IS NULL THEN
            ok := true; detail := 'set up (v' || v_have || ')';
        ELSE
            BEGIN
                ok := string_to_array(v_have, '.')::int[] >= string_to_array(v_min, '.')::int[];
                detail := CASE WHEN ok THEN 'set up (v' || v_have || ')'
                               ELSE 'v' || v_have || ' is older than required v' || v_min || ' — upgrade the ' || v_name || ' kit first' END;
            EXCEPTION WHEN invalid_text_representation THEN
                ok := true; detail := 'set up (v' || v_have || ', non-numeric version — not compared)';
            END;
        END IF;
        RETURN NEXT;
    END LOOP;
END
$kp$;

-- remove_kit v2: refuse to strand dependents unless forced.
DROP FUNCTION IF EXISTS rvbbit.remove_kit(text);
CREATE OR REPLACE FUNCTION rvbbit.remove_kit(p_kit text, p_force boolean DEFAULT false)
RETURNS TABLE (kind text, name text, action text)
LANGUAGE plpgsql
AS $rk$
DECLARE
    k rvbbit.kits%ROWTYPE;
    m text[];
    v_deps text;
BEGIN
    SELECT string_agg(d.kit, ', ') INTO v_deps
    FROM rvbbit.kits d,
         jsonb_array_elements_text(coalesce(d.requires->'kits', '[]'::jsonb)) req
    WHERE d.kit <> p_kit AND btrim(split_part(req, '>=', 1)) = p_kit;
    IF v_deps IS NOT NULL AND NOT p_force THEN
        RAISE EXCEPTION 'remove_kit: % is a foundation for: % — remove those kits first, or call remove_kit(%L, true)',
            p_kit, v_deps, p_kit;
    END IF;

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

    FOR kind, name IN SELECT 'target', target FROM rvbbit.kit_targets WHERE kit = p_kit LOOP
        action := 'removed (fitting row too; the VIEW stays — drop manually if truly done)'; RETURN NEXT;
    END LOOP;
    DELETE FROM rvbbit.kit_targets WHERE kit = p_kit;
    DELETE FROM rvbbit.kit_fittings WHERE kit = p_kit;

    FOR kind, name IN SELECT 'operator', o.name FROM rvbbit.operators o WHERE o.kit = p_kit LOOP
        action := 'removed'; RETURN NEXT;
    END LOOP;
    DELETE FROM rvbbit.operators WHERE operators.kit = p_kit;

    -- The catalog entry stays: uninstalling returns the kit to "available".

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

-- Cross-kit reactivity: a plate may listen to other kits' data events.
ALTER TABLE rvbbit.plates ADD COLUMN IF NOT EXISTS listens jsonb;

CREATE OR REPLACE FUNCTION rvbbit.set_plate_listens(p_plate_id text, p_kits text[])
RETURNS void
LANGUAGE plpgsql
AS $spl$
BEGIN
    UPDATE rvbbit.plates
    SET listens = CASE WHEN p_kits IS NULL OR array_length(p_kits, 1) IS NULL
                       THEN NULL ELSE to_jsonb(p_kits) END,
        updated_at = clock_timestamp()
    WHERE plate_id = p_plate_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'set_plate_listens: no plate %', p_plate_id;
    END IF;
END
$spl$;

-- export_kit v7: cross-kit listens travel.
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
    v_roles text;
    v_targets text;
    v_listens text;
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
        format('SELECT rvbbit.set_plate_role(%L, %L);', p.plate_id, p.requires_role),
        E'\n' ORDER BY p.plate_id)
    INTO v_roles
    FROM rvbbit.plates p WHERE p.kit = p_kit AND p.requires_role IS NOT NULL;

    SELECT string_agg(
        format('SELECT rvbbit.set_plate_listens(%L, ARRAY[%s]);', p.plate_id,
               (SELECT string_agg(quote_literal(x), ', ') FROM jsonb_array_elements_text(p.listens) x)),
        E'\n' ORDER BY p.plate_id)
    INTO v_listens
    FROM rvbbit.plates p WHERE p.kit = p_kit AND p.listens IS NOT NULL;

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
        format('SELECT rvbbit.upsert_kit_target(%L, %L, %s, %s::jsonb);',
            kt.kit, kt.target,
            CASE WHEN kt.description IS NULL THEN 'NULL' ELSE quote_literal(kt.description) END,
            rvbbit._kit_dq(kt.columns::text, 'ktg')),
        E'\n' ORDER BY kt.target)
    INTO v_targets
    FROM rvbbit.kit_targets kt WHERE kt.kit = p_kit;

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
        || E'-- ── plate roles (opt-in surface gating) ──\n' || coalesce(v_roles, '-- (none)') || E'\n\n'
        || E'-- ── plate listens (cross-kit reactivity) ──\n' || coalesce(v_listens, '-- (none)') || E'\n\n'
        || E'-- ── contracts (empty result = green) ──\n' || coalesce(v_contracts, '-- (none)') || E'\n\n'
        || E'-- ── rules (decision tables; first match wins) ──\n' || coalesce(v_rules, '-- (none)') || E'\n\n'
        || E'-- ── rule sets (subject registrations for live observability) ──\n' || coalesce(v_rule_sets, '-- (none)') || E'\n\n'
        || E'-- ── targets (canonical shapes for the Fitting Room; fittings stay local) ──\n' || coalesce(v_targets, '-- (none)') || E'\n\n'
        || E'-- ── operators (kit-scoped) ──\n' || coalesce(v_ops, '-- (none)') || E'\n\n'
        || E'-- ── metric_defs (reserved: definitions bound to kit canonical views) ──\n-- (none)\n\n'
        || E'-- ── cube_defs (reserved) ──\n-- (none)\n\n'
        || format(E'-- After COMMIT, self-test the arrival:\n--   SELECT * FROM rvbbit.validate_kit(%L) WHERE NOT ok;\n', k.kit);

    RETURN v_out;
END
$ek$;
