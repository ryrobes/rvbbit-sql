-- 0006_cubes_v3 — Cubes V3: curation + authoring + health.
--
-- V1 made cubes (materialized rvbbit tables, AS-OF, freshness). V2 made them understood
-- (cube_columns + enrich_cube + lineage). V3 makes them easy to CREATE and govern:
--   * propose_cube       — an LLM drafts a join (from FK graph + semantic search + docs) for a
--                          human to bless; NEVER persists (define_cube does that).
--   * cube packs         — parameterized templates for known SaaS schemas (Salesforce first);
--                          bind canonical objects to a user's actual tables → near one-click cube.
--   * promote_cube_to_metric — a metric over cubes.<name> (zero-copy; reads the accelerated cube).
--   * cube_health        — freshness / staleness / drift / usage for the Studio Inspector;
--                          folded into describe_cube.
-- All additive + idempotent (CREATE OR REPLACE / IF NOT EXISTS / ON CONFLICT DO NOTHING). Reuses
-- existing infra only (create_operator/_exec_op_jsonb, data_search, accel_freshness, define_cube/
-- define_metric) → no Rust rebuild. See docs/CUBES_PLAN.md §9-11.

-- ── small helper: coarse type family for structural binding suggestions ─────
CREATE OR REPLACE FUNCTION rvbbit._type_family(p_type text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_type ~* 'uuid'                                        THEN 'id'
        WHEN p_type ~* 'int|numeric|double|real|decimal|float|money' THEN 'numeric'
        WHEN p_type ~* 'timestamp|date|time'                         THEN 'temporal'
        WHEN p_type ~* 'bool'                                        THEN 'boolean'
        WHEN p_type ~* 'char|text|name'                              THEN 'text'
        ELSE 'other' END;
$$;

-- ════════════════════════════════════════════════════════════════════════════
-- Cube packs — parameterized templates for known SaaS schemas
-- ════════════════════════════════════════════════════════════════════════════

-- immutable, append-only (a new pack version is a new row)
CREATE TABLE IF NOT EXISTS rvbbit.cube_packs (
    pack_id                bigint GENERATED ALWAYS AS IDENTITY,
    pack_key               text    NOT NULL,            -- e.g. 'salesforce.opportunities'
    version                integer NOT NULL,
    saas_provider          text    NOT NULL,            -- 'salesforce'
    canonical_object       text    NOT NULL,            -- 'Opportunity'
    cube_name_suggest      text,
    canonical_sql_template text    NOT NULL,            -- {{ placeholders }} for tables + columns
    column_docs            jsonb   NOT NULL DEFAULT '{}'::jsonb,   -- canonical column -> {doc,semantics,source_ref}
    canonical_fields       jsonb   NOT NULL DEFAULT '[]'::jsonb,   -- [{name,canonical_names[],types[]}] for binding suggest
    description            text,
    grain                  text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pack_key, version)
);

CREATE OR REPLACE VIEW rvbbit.cube_packs_latest AS
SELECT DISTINCT ON (pack_key) *
FROM rvbbit.cube_packs
ORDER BY pack_key, version DESC;

-- mutable application history ("discovered once, encoded forever")
CREATE TABLE IF NOT EXISTS rvbbit.pack_bindings (
    binding_id         bigint GENERATED ALWAYS AS IDENTITY,
    pack_key           text    NOT NULL,
    pack_version       integer NOT NULL,
    cube_name          text    NOT NULL,
    bindings           jsonb   NOT NULL,
    binding_confidence real    DEFAULT 0.8,
    resolved_sql       text,
    status             text    DEFAULT 'draft',         -- draft | applied
    applied_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pack_key, pack_version, cube_name)
);

-- ── seed: the Salesforce "opportunities" pack (the first, highest-pain SaaS schema) ──
INSERT INTO rvbbit.cube_packs
    (pack_key, version, saas_provider, canonical_object, cube_name_suggest,
     canonical_sql_template, column_docs, canonical_fields, description, grain)
VALUES (
    'salesforce.opportunities', 1, 'salesforce', 'Opportunity', 'sf_opportunities',
    -- every output column is aliased to its canonical name so column_docs keys line up
    $tpl$SELECT o.{{ opp_id_col }} AS opp_id, o.{{ name_col }} AS name, o.{{ stage_col }} AS stage_name, o.{{ probability_col }} AS probability, o.{{ amount_col }} AS amount, o.{{ close_date_col }} AS close_date, o.{{ account_id_col }} AS account_id, a.{{ account_name_col }} AS account_name, o.{{ forecast_category_col }} AS forecast_category, o.{{ is_closed_col }} AS is_closed, o.{{ is_won_col }} AS is_won FROM {{ opportunities_table }} o LEFT JOIN {{ accounts_table }} a ON o.{{ account_id_col }} = a.{{ account_pk_col }} WHERE o.{{ opp_is_deleted_col }} = false$tpl$,
    $docs${
        "opp_id":            {"doc":"Unique opportunity id",                 "semantics":"Primary key, stable",                                "source_ref":"Salesforce.Opportunity.Id"},
        "name":              {"doc":"Opportunity name",                      "semantics":"Human-readable label",                               "source_ref":"Salesforce.Opportunity.Name"},
        "stage_name":        {"doc":"Pipeline stage",                        "semantics":"Categorical; pair with forecast_category",           "source_ref":"Salesforce.Opportunity.StageName"},
        "probability":       {"doc":"Win probability 0-100",                 "semantics":"Percent; divide by 100 for a weighted forecast",     "source_ref":"Salesforce.Opportunity.Probability"},
        "amount":            {"doc":"Deal amount",                           "semantics":"Currency context matters (multi-currency orgs store CurrencyIsoCode)","source_ref":"Salesforce.Opportunity.Amount"},
        "close_date":        {"doc":"Expected/actual close date",            "semantics":"Forecast-period anchor",                             "source_ref":"Salesforce.Opportunity.CloseDate"},
        "account_id":        {"doc":"Parent account FK",                     "semantics":"Many opportunities per account",                     "source_ref":"Salesforce.Opportunity.AccountId"},
        "account_name":      {"doc":"Account name (denormalized)",           "semantics":"Convenience join column",                            "source_ref":"Salesforce.Account.Name"},
        "forecast_category": {"doc":"Forecast category",                     "semantics":"Pipeline / BestCase / Commit / Closed",              "source_ref":"Salesforce.Opportunity.ForecastCategory"},
        "is_closed":         {"doc":"Whether the opportunity is closed",     "semantics":"Boolean",                                            "source_ref":"Salesforce.Opportunity.IsClosed"},
        "is_won":            {"doc":"Whether the opportunity was won",       "semantics":"Boolean",                                            "source_ref":"Salesforce.Opportunity.IsWon"}
    }$docs$::jsonb,
    $fields$[
        {"name":"opp_id_col",            "canonical_names":["id","opportunity_id","opp_id"],     "types":["text","uuid","varchar","bigint"]},
        {"name":"name_col",              "canonical_names":["name","opportunity_name"],          "types":["text","varchar"]},
        {"name":"stage_col",             "canonical_names":["stage","stage_name","stagename","status"],"types":["text","varchar"]},
        {"name":"probability_col",       "canonical_names":["probability","win_probability"],    "types":["numeric","float8","int4"]},
        {"name":"amount_col",            "canonical_names":["amount","value","deal_size"],       "types":["numeric","float8","bigint"]},
        {"name":"close_date_col",        "canonical_names":["close_date","closedate"],           "types":["date","timestamp","timestamptz"]},
        {"name":"account_id_col",        "canonical_names":["account_id","accountid","acct_id"], "types":["text","uuid","varchar","bigint"]},
        {"name":"account_pk_col",        "canonical_names":["id","account_id","accountid"],      "types":["text","uuid","varchar","bigint"]},
        {"name":"account_name_col",      "canonical_names":["name","account_name"],              "types":["text","varchar"]},
        {"name":"forecast_category_col", "canonical_names":["forecast_category","forecastcategory"],"types":["text","varchar"]},
        {"name":"is_closed_col",         "canonical_names":["is_closed","isclosed"],             "types":["bool"]},
        {"name":"is_won_col",            "canonical_names":["is_won","iswon"],                   "types":["bool"]},
        {"name":"opp_is_deleted_col",    "canonical_names":["is_deleted","isdeleted"],           "types":["bool"]}
    ]$fields$::jsonb,
    'A Salesforce opportunity (a pipeline deal) joined to its account, with stage, probability, amount, close date and forecast category.',
    'one row per opportunity'
)
ON CONFLICT (pack_key, version) DO NOTHING;

-- ── fuzzy_suggest_bindings: propose actual table/column for each canonical field ──
-- Structural + lexical match against information_schema (robust, no catalog crawl required —
-- a SUGGESTION engine; apply_cube_pack takes the explicit bindings the human/agent confirmed).
CREATE OR REPLACE FUNCTION rvbbit.fuzzy_suggest_bindings(p_pack_key text, p_schema text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_fields jsonb; v_field jsonb; v_name text; v_canon text[]; v_types text[];
    v_cands jsonb; v_out jsonb := '{}'::jsonb;
BEGIN
    SELECT canonical_fields INTO v_fields FROM rvbbit.cube_packs_latest WHERE pack_key = p_pack_key;
    IF v_fields IS NULL THEN
        RAISE EXCEPTION 'rvbbit.fuzzy_suggest_bindings: pack % not found', p_pack_key;
    END IF;

    FOR v_field IN SELECT value FROM jsonb_array_elements(v_fields) LOOP
        v_name := v_field->>'name';
        SELECT array_agg(lower(x)) INTO v_canon FROM jsonb_array_elements_text(v_field->'canonical_names') x;
        SELECT array_agg(x)        INTO v_types FROM jsonb_array_elements_text(v_field->'types') x;

        SELECT jsonb_agg(jsonb_build_object('table', tbl, 'column', col, 'score', round(score::numeric, 3))
                         ORDER BY score DESC)
          INTO v_cands
          FROM (
            SELECT table_schema || '.' || table_name AS tbl, column_name AS col,
                   ( CASE WHEN lower(column_name) = ANY(v_canon) THEN 1.0
                          WHEN EXISTS (SELECT 1 FROM unnest(v_canon) cn WHERE lower(column_name) LIKE '%' || cn || '%') THEN 0.6
                          ELSE 0.0 END
                   + CASE WHEN rvbbit._type_family(data_type)
                             = ANY (ARRAY(SELECT rvbbit._type_family(t) FROM unnest(v_types) t)) THEN 0.3 ELSE 0.0 END
                   ) AS score
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
              AND (p_schema IS NULL OR table_schema = p_schema)
          ) c
         WHERE score > 0.3
         LIMIT 5;

        v_out := v_out || jsonb_build_object(v_name, jsonb_build_object(
            'candidates',  coalesce(v_cands, '[]'::jsonb),
            'best_match',  coalesce(v_cands->0, 'null'::jsonb),
            'confidence',  coalesce((v_cands->0->>'score')::real, 0)));
    END LOOP;

    RETURN v_out;
END $fn$;

-- ── apply_cube_pack: substitute bindings into the template + validate (dry run) ──
CREATE OR REPLACE FUNCTION rvbbit.apply_cube_pack(p_pack_key text, p_bindings jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_tpl text; v_docs jsonb; v_obj text; v_grain text; v_desc text;
    v_sql text; k text; v text; v_unbound text;
BEGIN
    SELECT canonical_sql_template, column_docs, canonical_object, grain, description
      INTO v_tpl, v_docs, v_obj, v_grain, v_desc
      FROM rvbbit.cube_packs_latest WHERE pack_key = p_pack_key;
    IF v_tpl IS NULL THEN
        RAISE EXCEPTION 'rvbbit.apply_cube_pack: pack % not found', p_pack_key;
    END IF;

    -- PASS 1: substitute every {{ key }} from the bindings
    v_sql := v_tpl;
    FOR k, v IN SELECT key, value FROM jsonb_each_text(coalesce(p_bindings, '{}'::jsonb)) LOOP
        v_sql := replace(v_sql, '{{ ' || k || ' }}', v);
    END LOOP;

    -- any placeholder left unbound is an error
    v_unbound := (regexp_match(v_sql, '\{\{[^}]+\}\}'))[1];
    IF v_unbound IS NOT NULL THEN
        RETURN jsonb_build_object('status', 'error',
            'error', 'unbound placeholder ' || v_unbound, 'resolved_sql', v_sql);
    END IF;

    -- PASS 2: validate the resolved SQL plans (catches bad table/column bindings)
    BEGIN
        EXECUTE 'EXPLAIN ' || v_sql;
    EXCEPTION WHEN OTHERS THEN
        RETURN jsonb_build_object('status', 'error', 'error', SQLERRM, 'resolved_sql', v_sql);
    END;

    RETURN jsonb_build_object('status', 'ok', 'resolved_sql', v_sql,
        'column_docs', v_docs, 'grain', v_grain, 'description', v_desc,
        'canonical_object', v_obj);
END $fn$;

-- ── define_cube_from_pack: one-call instantiation (materialize + pre-seed curated docs) ──
CREATE OR REPLACE FUNCTION rvbbit.define_cube_from_pack(
    p_pack_key text, p_bindings jsonb, p_cube_name text,
    p_grain text DEFAULT NULL, p_description text DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    v_applied jsonb; v_version integer; v_pack_ver integer; v_docs jsonb;
    v_col text; v_meta jsonb;
BEGIN
    v_applied := rvbbit.apply_cube_pack(p_pack_key, p_bindings);
    IF v_applied->>'status' <> 'ok' THEN
        RAISE EXCEPTION 'rvbbit.define_cube_from_pack: pack apply failed: %', v_applied->>'error';
    END IF;
    SELECT version INTO v_pack_ver FROM rvbbit.cube_packs_latest WHERE pack_key = p_pack_key;

    v_version := rvbbit.define_cube(
        p_cube_name, v_applied->>'resolved_sql',
        coalesce(p_grain, v_applied->>'grain'),
        coalesce(p_description, v_applied->>'description'),
        NULL, NULL, 'pack_' || split_part(p_pack_key, '.', 1),
        jsonb_build_object('pack', p_pack_key));

    -- pre-seed the curated pack docs (edited_by='pack' so a later enrich_cube PRESERVES them
    -- — pack docs are authoritative, not LLM drafts).
    v_docs := v_applied->'column_docs';
    FOR v_col IN SELECT jsonb_object_keys(coalesce(v_docs, '{}'::jsonb)) LOOP
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'cubes' AND table_name = p_cube_name AND column_name = v_col);
        v_meta := v_docs->v_col;
        INSERT INTO rvbbit.cube_columns
            (cube_name, column_name, data_type, doc, semantics, source_ref, confidence, edited_by, updated_at)
        SELECT p_cube_name, v_col,
               (SELECT data_type FROM information_schema.columns
                 WHERE table_schema = 'cubes' AND table_name = p_cube_name AND column_name = v_col),
               nullif(btrim(v_meta->>'doc'), ''),
               nullif(btrim(v_meta->>'semantics'), ''),
               nullif(btrim(v_meta->>'source_ref'), ''),
               0.95, 'pack', now()
        ON CONFLICT (cube_name, column_name) DO UPDATE SET
            doc        = coalesce(EXCLUDED.doc, cube_columns.doc),
            semantics  = coalesce(EXCLUDED.semantics, cube_columns.semantics),
            source_ref = coalesce(EXCLUDED.source_ref, cube_columns.source_ref),
            confidence = 0.95, edited_by = 'pack', updated_at = now();
    END LOOP;

    -- re-embed now that the curated docs exist
    BEGIN PERFORM rvbbit.register_cube_node(p_cube_name);
    EXCEPTION WHEN OTHERS THEN NULL; END;

    INSERT INTO rvbbit.pack_bindings
        (pack_key, pack_version, cube_name, bindings, resolved_sql, status, applied_at)
    VALUES (p_pack_key, v_pack_ver, p_cube_name, p_bindings, v_applied->>'resolved_sql', 'applied', now())
    ON CONFLICT (pack_key, pack_version, cube_name) DO UPDATE SET
        bindings = EXCLUDED.bindings, resolved_sql = EXCLUDED.resolved_sql,
        status = 'applied', applied_at = now();

    RETURN v_version;
END $fn$;

-- ════════════════════════════════════════════════════════════════════════════
-- cube_health — freshness / staleness / drift / usage (Studio Inspector + alerts)
-- ════════════════════════════════════════════════════════════════════════════
-- Joins cube_control + accel_freshness (every cube is USING rvbbit, so it has a live
-- accel_freshness row keyed by 'cubes.<name>'::regclass::text).
CREATE OR REPLACE FUNCTION rvbbit.cube_health(p_name text)
RETURNS jsonb LANGUAGE plpgsql STABLE AS $fn$
DECLARE v_reg regclass; v_key text; v_out jsonb;
BEGIN
    v_reg := to_regclass('cubes.' || quote_ident(p_name));
    IF v_reg IS NULL THEN
        RETURN jsonb_build_object('cube', p_name, 'status', 'missing');
    END IF;
    v_key := v_reg::text;

    SELECT jsonb_build_object(
        'cube', p_name,
        -- a cube's "refresh" is its snapshot_load (cube_control.refreshed_at) — the authoritative
        -- freshness clock. accel_freshness.seconds_since_refresh tracks the PARQUET rebuild, which is
        -- NULL for a brand-new cube, so prefer the cube's own refresh time for the status.
        'freshness', jsonb_build_object(
            'last_refreshed_at',     ctl.refreshed_at,
            'last_refresh_at',       f.last_refresh_at,
            'seconds_since_refresh', coalesce(extract(epoch FROM (now() - ctl.refreshed_at))::bigint,
                                              f.seconds_since_refresh),
            'last_refresh_rows',     ctl.last_rows,
            'current_parquet_rows',  f.parquet_rows,
            'row_delta',             coalesce(f.parquet_rows, 0) - coalesce(ctl.last_rows, 0),
            'status', CASE
                WHEN ctl.last_error IS NOT NULL                              THEN 'error'
                WHEN f.shadow_heap_dirty                                     THEN 'dirty'
                WHEN coalesce(extract(epoch FROM (now() - ctl.refreshed_at)),
                              f.seconds_since_refresh, 999999) > 86400       THEN 'stale'
                ELSE 'fresh' END),
        'staleness', jsonb_build_object(
            'dirty_since',  f.dirty_since,
            'seconds_dirty', f.seconds_dirty,
            'dirty',         coalesce(f.shadow_heap_dirty, false)),
        'drift', jsonb_build_object(
            'unmirrored_rows', f.est_unmirrored_rows,
            'drift_rows',      f.drift_rows,
            'drift_ratio',     f.drift_ratio,
            'recommendation', CASE
                WHEN f.drift_ratio IS NULL  THEN 'unknown'
                WHEN f.drift_ratio < 0.1    THEN 'skip'
                WHEN f.drift_ratio < 0.5    THEN 'delta'
                ELSE 'full rebuild' END),
        'usage', jsonb_build_object(
            'heap_seq_scans',   f.heap_seq_scans,
            'last_rebuild_ms',  f.last_rebuild_ms,
            'last_rebuild_rows', f.last_rebuild_rows),
        'last_error', ctl.last_error)
    INTO v_out
    FROM rvbbit.cube_control ctl
    LEFT JOIN rvbbit.accel_freshness f ON f.table_name = v_key
    WHERE ctl.cube_name = p_name;

    RETURN coalesce(v_out, jsonb_build_object('cube', p_name, 'status', 'unknown'));
END $fn$;

-- ── describe_cube: re-create V2 body, appending the 'health' key ────────────
-- VOLATILE: transitively runs EXPLAIN (lineage via _cube_source_tables).
CREATE OR REPLACE FUNCTION rvbbit.describe_cube(p_name text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_qual text := 'cubes.' || quote_ident(p_name);
    v_sql text; v_sample jsonb; v_out jsonb;
BEGIN
    SELECT sql INTO v_sql FROM rvbbit.cube_catalog WHERE name = p_name;
    IF v_sql IS NULL THEN RETURN NULL; END IF;

    IF to_regclass(v_qual) IS NOT NULL THEN
        BEGIN
            EXECUTE format(
                'SELECT jsonb_agg(r) FROM (SELECT (SELECT jsonb_object_agg(k, left(v, 200)) '
                'FROM jsonb_each_text(to_jsonb(t)) e(k, v)) AS r FROM %s t LIMIT 5) z',
                v_qual) INTO v_sample;
        EXCEPTION WHEN OTHERS THEN
            v_sample := NULL;
        END;
    END IF;

    SELECT jsonb_build_object(
        'name', c.name,
        'description', coalesce(c.description, ctl.auto_description),
        'grain', coalesce(c.grain, ctl.auto_grain),
        'human_description', c.description, 'auto_description', ctl.auto_description,
        'category', c.category, 'version', c.version, 'sql', c.sql,
        'refresh_cron', c.refresh_cron, 'refreshed_at', ctl.refreshed_at,
        'rows', ctl.last_rows, 'enriched_at', ctl.enriched_at,
        'source_tables', to_jsonb(rvbbit._cube_source_tables(c.sql)),
        'columns', (
            SELECT jsonb_agg(jsonb_build_object(
                       'name', ic.column_name, 'type', ic.data_type,
                       'doc', cc.doc, 'semantics', cc.semantics,
                       'source_ref', cc.source_ref, 'confidence', cc.confidence,
                       'edited_by', cc.edited_by)
                   ORDER BY ic.ordinal_position)
            FROM information_schema.columns ic
            LEFT JOIN rvbbit.cube_columns cc
                   ON cc.cube_name = c.name AND cc.column_name = ic.column_name
            WHERE ic.table_schema = 'cubes' AND ic.table_name = c.name),
        'sample', coalesce(v_sample, '[]'::jsonb),
        'health', rvbbit.cube_health(c.name))
    INTO v_out
    FROM rvbbit.cube_catalog c
    LEFT JOIN rvbbit.cube_control ctl ON ctl.cube_name = c.name
    WHERE c.name = p_name;

    RETURN v_out;
END $fn$;

-- ════════════════════════════════════════════════════════════════════════════
-- promote_cube_to_metric — a blessed metric over an accelerated cube (zero-copy)
-- ════════════════════════════════════════════════════════════════════════════
-- Creates a metric whose SQL is SELECT * FROM cubes.<name>; the cube's AS-OF/freshness flow
-- through. For a DERIVED/aggregated metric, write the metric SQL by hand instead (this is the
-- pass-through promotion; the grain may differ once you aggregate, hence p_grain override).
CREATE OR REPLACE FUNCTION rvbbit.promote_cube_to_metric(
    p_cube_name text, p_metric_name text,
    p_description text DEFAULT NULL, p_owner text DEFAULT NULL, p_grain text DEFAULT NULL
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE v_cube_grain text; v_sql text; v_version integer;
BEGIN
    SELECT grain INTO v_cube_grain FROM rvbbit.cube_catalog WHERE name = p_cube_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.promote_cube_to_metric: cube % not found', p_cube_name;
    END IF;
    v_sql := 'SELECT * FROM cubes.' || quote_ident(p_cube_name);
    v_version := rvbbit.define_metric(
        p_metric_name, v_sql, '{}'::jsonb,
        coalesce(p_grain, v_cube_grain, 'one row per ' || p_cube_name),
        coalesce(p_description, 'Promotion of cube ' || p_cube_name),
        p_owner, jsonb_build_object('cube_source', p_cube_name), NULL);
    RETURN v_version;
END $fn$;

-- ════════════════════════════════════════════════════════════════════════════
-- propose_cube — an LLM drafts a join for a human to bless (NEVER persists)
-- ════════════════════════════════════════════════════════════════════════════

-- the drafting LLM operator (same create_operator/_exec_op_jsonb path as cube_enrich)
DO $do$
BEGIN
    PERFORM rvbbit.create_operator(
        op_name        => 'propose_cube_draft',
        op_shape       => 'scalar',
        op_arg_names   => ARRAY['context'],
        op_arg_types   => ARRAY['text'],
        op_return_type => 'jsonb',
        op_model       => 'openai/gpt-5.4-mini',
        op_parser      => 'json',
        op_max_tokens  => 8000,
        op_temperature => 0.2,
        op_description =>
            'Draft a candidate cube (a wide reasoned-about join SQL + grain + description) from a ' ||
            'subject, candidate tables, FK edges and catalog docs. Returns strict JSON for a human ' ||
            'to bless via define_cube. Used by rvbbit.propose_cube.',
        op_system =>
            'You are a senior data architect. Given a SUBJECT and a JSON context (candidate tables, ' ||
            'their columns+types, foreign-key edges between them, and any existing table docs), design ' ||
            'ONE wide, analysis-ready "cube" — a documented join that captures the subject as a single ' ||
            'table. Return ONLY a JSON object with EXACTLY these keys: ' ||
            '"name" (a short snake_case identifier matching ^[a-z_][a-z0-9_]*$, <=30 chars), ' ||
            '"sql" (a single Postgres SELECT that joins ONLY the provided tables using the given FK ' ||
            'edges where possible; alias EVERY output column to a clear snake_case name; prefer INNER ' ||
            'joins for the core fact and LEFT joins for optional attributes; schema-qualify tables), ' ||
            '"grain" (one sentence: what one row represents), ' ||
            '"description" (1-2 sentences: what the cube is and what it answers), ' ||
            '"source_tables" (JSON array of the schema.table names actually used), ' ||
            '"join_rationale" (one brief sentence on the join choices), ' ||
            '"confidence" (0.0-1.0). ' ||
            'Never invent tables or columns not present in the context. No markdown, no code fence, ' ||
            'no commentary — JSON object only.',
        -- the operator has ONE arg `context` (the whole JSON blob); reference {{ context }} only —
        -- {{ subject }} etc. are NOT operator inputs and would resolve to empty (mirrors cube_enrich).
        op_user =>
            E'CUBE PROPOSAL CONTEXT (JSON — contains subject, candidate_tables, fk_edges, column_samples, source_docs):\n{{ context }}\n\nReturn the cube proposal JSON only.',
        op_tests => NULL
    );

    PERFORM rvbbit.set_operator_retry(
        'propose_cube_draft',
        $cfg${
          "until": {"function": "rvbbit.propose_cube_draft_valid"},
          "max_attempts": 3,
          "instructions": "Return ONLY a JSON object with non-empty name (snake_case, <=30 chars, ^[a-z_][a-z0-9_]*$), sql (a single SELECT over the provided tables), grain, description, source_tables (array), join_rationale and confidence (0..1). No markdown or extra keys."
        }$cfg$::jsonb
    );
END $do$;

-- validator (raw text pre-parse, signature mirrors cube_enrich_valid)
CREATE OR REPLACE FUNCTION rvbbit.propose_cube_draft_valid(output text, inputs jsonb DEFAULT '{}'::jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE doc jsonb; v_name text;
BEGIN
    IF output IS NULL OR btrim(output) = '' THEN RETURN false; END IF;
    BEGIN
        doc := output::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN false;
    END;
    IF jsonb_typeof(doc) <> 'object' THEN RETURN false; END IF;
    v_name := nullif(btrim(doc->>'name'), '');
    RETURN v_name IS NOT NULL
       AND v_name ~ '^[a-z_][a-z0-9_]*$'
       AND nullif(btrim(doc->>'sql'), '')   IS NOT NULL
       AND nullif(btrim(doc->>'grain'), '') IS NOT NULL;
END $$;

-- the orchestrator: assemble context (candidates + FK graph + columns + docs) → draft
CREATE OR REPLACE FUNCTION rvbbit.propose_cube(
    p_subject     text,
    p_seed_tables text[] DEFAULT NULL,
    p_schema      text   DEFAULT NULL,
    p_max_tables  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_cands text[]; v_oids oid[]; v_fk jsonb;
    v_cols jsonb := '{}'::jsonb; v_docs jsonb := '{}'::jsonb;
    v_ctx text; v_out jsonb; s text; sch text; tbl text;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: subject is required';
    END IF;

    -- 1) candidate tables: seed → semantic search → information_schema fallback
    IF p_seed_tables IS NOT NULL AND cardinality(p_seed_tables) > 0 THEN
        v_cands := p_seed_tables;
    ELSE
        SELECT array_agg(DISTINCT schema_name || '.' || rel_name)
          INTO v_cands
          FROM rvbbit.data_search(p_subject, p_max_tables, ARRAY['db_table'], 'db_catalog')
         WHERE rel_name IS NOT NULL
           AND (p_schema IS NULL OR schema_name = p_schema);
        IF v_cands IS NULL OR cardinality(v_cands) = 0 THEN
            -- catalog not crawled → fall back to a bounded information_schema scan
            SELECT array_agg(t) INTO v_cands FROM (
                SELECT table_schema || '.' || table_name AS t
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
                  AND (p_schema IS NULL OR table_schema = p_schema)
                ORDER BY table_schema, table_name
                LIMIT p_max_tables) z;
        END IF;
    END IF;
    IF v_cands IS NULL OR cardinality(v_cands) = 0 THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: no candidate tables for subject % (pass p_seed_tables or run catalog_crawl)', p_subject;
    END IF;

    -- resolve to oids (for the FK query — match on oid, not bare name)
    SELECT array_agg(o) INTO v_oids FROM (
        SELECT to_regclass(t)::oid AS o FROM unnest(v_cands) t
    ) z WHERE o IS NOT NULL;

    -- 2) FK edges among the candidates (first column of each FK)
    SELECT jsonb_agg(jsonb_build_object(
             'from_table', pfn.nspname || '.' || pf.relname, 'from_column', a.attname,
             'to_table',   pcn.nspname || '.' || pc.relname, 'to_column',   a2.attname))
      INTO v_fk
      FROM pg_constraint con
      JOIN pg_class pf      ON pf.oid  = con.conrelid
      JOIN pg_namespace pfn ON pfn.oid = pf.relnamespace
      JOIN pg_class pc      ON pc.oid  = con.confrelid
      JOIN pg_namespace pcn ON pcn.oid = pc.relnamespace
      JOIN pg_attribute a   ON a.attrelid  = con.conrelid  AND a.attnum  = con.conkey[1]
      JOIN pg_attribute a2  ON a2.attrelid = con.confrelid AND a2.attnum = con.confkey[1]
     WHERE con.contype = 'f' AND pf.oid = ANY(v_oids) AND pc.oid = ANY(v_oids);

    -- 3) per-table columns + 4) per-table catalog docs
    FOREACH s IN ARRAY v_cands LOOP
        IF to_regclass(s) IS NULL THEN CONTINUE; END IF;
        sch := split_part(s, '.', 1); tbl := split_part(s, '.', 2);
        IF tbl = '' THEN tbl := sch; sch := NULL; END IF;
        v_cols := v_cols || jsonb_build_object(s, (
            SELECT jsonb_agg(jsonb_build_object('name', column_name, 'type', data_type)
                             ORDER BY ordinal_position)
              FROM information_schema.columns
             WHERE table_name = tbl AND (sch IS NULL OR table_schema = sch)));
        v_docs := v_docs || jsonb_build_object(s, (
            SELECT left(doc, 500) FROM rvbbit.catalog_docs
              WHERE rel_name = tbl AND col_name IS NULL AND (sch IS NULL OR schema_name = sch)
              ORDER BY updated_at DESC NULLS LAST LIMIT 1));
    END LOOP;

    v_ctx := jsonb_build_object(
        'subject', p_subject,
        'candidate_tables', to_jsonb(v_cands),
        'fk_edges', coalesce(v_fk, '[]'::jsonb),
        'column_samples', v_cols,
        'source_docs', v_docs)::text;

    v_out := rvbbit.propose_cube_draft(v_ctx);   -- the LLM call (validated + retried)
    IF v_out IS NULL
       OR nullif(btrim(v_out->>'name'), '') IS NULL
       OR nullif(btrim(v_out->>'sql'), '')  IS NULL THEN
        RAISE EXCEPTION 'rvbbit.propose_cube: draft generation failed for subject %', p_subject;
    END IF;

    -- return the draft + what it was drafted from (the UI shows both; nothing is persisted)
    RETURN v_out || jsonb_build_object(
        'candidate_tables', to_jsonb(v_cands),
        'fk_edges', coalesce(v_fk, '[]'::jsonb));
END $fn$;
