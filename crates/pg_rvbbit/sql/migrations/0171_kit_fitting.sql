-- 0171: the Fitting — kits adapt to the customer's schema (KIT_PLATES_PLAN §21).
--
-- Nouns: a kit ships TARGETS (canonical view shapes it expects); the
-- Fitting Room (native lens app) proposes and previews mappings; an
-- accepted mapping is a FITTING — the connector between the customer's
-- real tables and the kit's canon, recorded as a row and materialized as
-- a plain VIEW. The kit's own contracts then gate modules on
-- fitting_violations() until every required target is fitted.
--
-- The app is stateless by doctrine: expectations (kit_targets) and
-- accepted connections (kit_fittings) are rows; the view is the artifact.

CREATE TABLE IF NOT EXISTS rvbbit.kit_targets (
    kit         text NOT NULL,
    target      text NOT NULL,          -- schema-qualified view name the kit expects
    description text,                   -- what this data IS (drives discovery)
    columns     jsonb NOT NULL,         -- [{name, type, description, required}]
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (kit, target)
);

CREATE OR REPLACE FUNCTION rvbbit.upsert_kit_target(
    p_kit text,
    p_target text,
    p_description text,
    p_columns jsonb
) RETURNS void
LANGUAGE plpgsql
AS $ukt$
BEGIN
    IF p_target !~ '^[a-zA-Z_][\w]*\.[a-zA-Z_][\w]*$' THEN
        RAISE EXCEPTION 'target must be a schema-qualified name (schema.view), got %', p_target;
    END IF;
    IF jsonb_typeof(p_columns) <> 'array' OR jsonb_array_length(p_columns) = 0 THEN
        RAISE EXCEPTION 'columns must be a non-empty jsonb array of {name, type, description, required}';
    END IF;
    INSERT INTO rvbbit.kit_targets (kit, target, description, columns)
    VALUES (p_kit, p_target, p_description, p_columns)
    ON CONFLICT (kit, target) DO UPDATE SET
        description = EXCLUDED.description,
        columns = EXCLUDED.columns,
        updated_at = clock_timestamp();
END
$ukt$;

CREATE TABLE IF NOT EXISTS rvbbit.kit_fittings (
    kit         text NOT NULL,
    target      text NOT NULL,
    select_sql  text NOT NULL,          -- the accepted mapping SELECT
    accepted_by text NOT NULL DEFAULT current_user,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    proposal    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- provenance: candidates, scores, notes
    PRIMARY KEY (kit, target)
);

-- Discovery: rank candidate source tables for a target using the catalog
-- KG / fingerprint search (the same index data_search uses). The Fitting
-- Room refines with samples + clover_llm; this is the cheap first cut.
CREATE OR REPLACE FUNCTION rvbbit.fitting_candidates(
    p_kit text,
    p_target text,
    p_k integer DEFAULT 8
) RETURNS TABLE (schema_name text, rel_name text, score double precision, matched_on text)
LANGUAGE plpgsql
AS $fc$
DECLARE
    t rvbbit.kit_targets%ROWTYPE;
    v_query text;
BEGIN
    SELECT * INTO t FROM rvbbit.kit_targets kt WHERE kt.kit = p_kit AND kt.target = p_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'fitting_candidates: no target %/%', p_kit, p_target;
    END IF;
    v_query := coalesce(t.description, '') || ' ' ||
               coalesce((SELECT string_agg(c->>'name' || ' ' || coalesce(c->>'description', ''), ' ')
                         FROM jsonb_array_elements(t.columns) c), '');
    RETURN QUERY
    SELECT ds.schema_name, ds.rel_name, max(ds.score) AS score,
           left(string_agg(DISTINCT coalesce(ds.col_name, '(table)'), ', '), 120) AS matched_on
    FROM rvbbit.data_search(v_query, greatest(p_k * 4, 24), NULL, NULL) ds
    WHERE ds.schema_name IS NOT NULL
      AND ds.rel_name IS NOT NULL
      AND ds.schema_name NOT IN ('rvbbit', 'pg_catalog', 'information_schema', 'cron')
    GROUP BY ds.schema_name, ds.rel_name
    ORDER BY max(ds.score) DESC
    LIMIT p_k;
END
$fc$;

-- Preview/verify a candidate mapping WITHOUT creating anything: does the
-- SELECT run, and does it produce the target's columns? Used by the
-- Fitting Room's preview pane; also the guts of fitting_apply's checks.
CREATE OR REPLACE FUNCTION rvbbit.fitting_check(
    p_kit text,
    p_target text,
    p_select_sql text
) RETURNS TABLE (check_name text, ok boolean, detail text)
LANGUAGE plpgsql
AS $fchk$
DECLARE
    t rvbbit.kit_targets%ROWTYPE;
    c jsonb;
    v_cols text[] := '{}';
    v_types text[] := '{}';
    v_idx int;
BEGIN
    SELECT * INTO t FROM rvbbit.kit_targets kt WHERE kt.kit = p_kit AND kt.target = p_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'fitting_check: no target %/%', p_kit, p_target;
    END IF;
    IF p_select_sql !~* '^[[:space:]]*(SELECT|WITH)\y' OR p_select_sql ~ ';' THEN
        check_name := 'shape'; ok := false; detail := 'mapping must be a single SELECT (no semicolons)';
        RETURN NEXT; RETURN;
    END IF;

    -- Column discovery via a zero-row probe into a temp view.
    BEGIN
        EXECUTE 'CREATE OR REPLACE TEMP VIEW _fitting_probe AS ' || p_select_sql;
    EXCEPTION WHEN others THEN
        check_name := 'runs'; ok := false; detail := SQLERRM;
        RETURN NEXT; RETURN;
    END;
    check_name := 'runs'; ok := true; detail := 'SELECT is valid';
    RETURN NEXT;

    SELECT array_agg(a.attname::text ORDER BY a.attnum),
           array_agg(format_type(a.atttypid, a.atttypmod) ORDER BY a.attnum)
    INTO v_cols, v_types
    FROM pg_attribute a
    JOIN pg_class cl ON cl.oid = a.attrelid
    WHERE cl.relname = '_fitting_probe'
      AND cl.relnamespace = pg_my_temp_schema()
      AND a.attnum > 0 AND NOT a.attisdropped;

    FOR c IN SELECT * FROM jsonb_array_elements(t.columns) LOOP
        check_name := 'column ' || (c->>'name');
        v_idx := array_position(v_cols, c->>'name');
        IF v_idx IS NULL THEN
            ok := NOT coalesce((c->>'required')::boolean, true);
            detail := CASE WHEN ok THEN 'optional column absent' ELSE 'REQUIRED column missing from mapping' END;
        ELSE
            ok := true;
            detail := 'present as ' || v_types[v_idx] ||
                      CASE WHEN c->>'type' IS NOT NULL AND position(lower(c->>'type') IN lower(v_types[v_idx])) = 0
                           THEN ' (target expects ' || (c->>'type') || ' — verify)' ELSE '' END;
        END IF;
        RETURN NEXT;
    END LOOP;
    EXECUTE 'DROP VIEW IF EXISTS _fitting_probe';
END
$fchk$;

-- Accept a fitting: verify, CREATE OR REPLACE the canonical VIEW, record
-- the row. Fails loudly if any REQUIRED column is missing.
CREATE OR REPLACE FUNCTION rvbbit.fitting_apply(
    p_kit text,
    p_target text,
    p_select_sql text,
    p_proposal jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE (check_name text, ok boolean, detail text)
LANGUAGE plpgsql
AS $fa$
DECLARE
    v_bad int := 0;
    v_schema text := split_part(p_target, '.', 1);
BEGIN
    FOR check_name, ok, detail IN SELECT * FROM rvbbit.fitting_check(p_kit, p_target, p_select_sql) LOOP
        IF NOT ok THEN v_bad := v_bad + 1; END IF;
        RETURN NEXT;
    END LOOP;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'fitting_apply: % check(s) failed — fix the mapping (see rvbbit.fitting_check)', v_bad;
    END IF;
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', v_schema);
    EXECUTE format('CREATE OR REPLACE VIEW %s AS %s', p_target, p_select_sql);
    INSERT INTO rvbbit.kit_fittings (kit, target, select_sql, proposal)
    VALUES (p_kit, p_target, p_select_sql, coalesce(p_proposal, '{}'::jsonb))
    ON CONFLICT (kit, target) DO UPDATE SET
        select_sql = EXCLUDED.select_sql,
        proposal = EXCLUDED.proposal,
        accepted_by = current_user,
        accepted_at = clock_timestamp();
    check_name := 'applied'; ok := true; detail := p_target || ' fitted';
    RETURN NEXT;
END
$fa$;

-- Contract fuel: violation rows for every target not yet (correctly)
-- fitted. Kits ship a mapping contract as simply:
--   SELECT * FROM rvbbit.fitting_violations('<kit>')
CREATE OR REPLACE FUNCTION rvbbit.fitting_violations(p_kit text)
RETURNS TABLE (target text, problem text)
LANGUAGE plpgsql
AS $fv$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM rvbbit.kit_targets kt WHERE kt.kit = p_kit LOOP
        IF NOT EXISTS (SELECT 1 FROM rvbbit.kit_fittings f WHERE f.kit = p_kit AND f.target = t.target) THEN
            target := t.target; problem := 'not fitted — open the Fitting Room';
            RETURN NEXT;
        ELSIF to_regclass(t.target) IS NULL THEN
            target := t.target; problem := 'fitting recorded but the view is missing — re-apply';
            RETURN NEXT;
        END IF;
    END LOOP;
END
$fv$;

-- export_kit v6: targets travel with the kit (fittings are per-box truth).
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
