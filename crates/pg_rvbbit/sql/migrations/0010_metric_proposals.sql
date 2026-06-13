-- 0010_metric_proposals — extend the proposal/promote system to METRICS.
--
-- Cubes ship propose_cube (MCP tool logs a draft) → a human blesses it in the Proposals inbox →
-- define_cube. This brings metrics to the same loop. Metrics carry two things cubes don't —
-- {param} defaults and an optional KPI check_sql — so the proposals table gains those columns, and
-- a new propose_metric_draft operator teaches the LLM the metric conventions (prefer cubes as the
-- source; metrics produce a small governed number/series; {param} for filters; {metric:NAME} to
-- reference another metric). accept_proposal now dispatches on kind: cube → define_cube,
-- metric → define_metric. Additive + idempotent. See docs/CUBES_PLAN.md / METRICS plan.

-- ── 1) metric-specific proposal columns ────────────────────────────────────
ALTER TABLE rvbbit.proposals ADD COLUMN IF NOT EXISTS params    jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE rvbbit.proposals ADD COLUMN IF NOT EXISTS check_sql text;

-- ── 2) record_proposal — also capture params + check_sql ───────────────────
CREATE OR REPLACE FUNCTION rvbbit.record_proposal(
    p_kind text, p_draft jsonb, p_proposed_by text DEFAULT 'agent', p_proposed_via text DEFAULT 'mcp'
) RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE v_id bigint; v_kind text := coalesce(nullif(btrim(p_kind), ''), 'cube'); v_name text;
BEGIN
    IF p_draft IS NULL OR jsonb_typeof(p_draft) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.record_proposal: draft must be a json object';
    END IF;
    v_name := nullif(btrim(p_draft->>'name'), '');
    IF v_name IS NOT NULL THEN
        UPDATE rvbbit.proposals SET status = 'superseded', reviewed_at = now()
         WHERE status = 'pending' AND kind = v_kind AND name = v_name;
    END IF;
    INSERT INTO rvbbit.proposals
        (kind, status, name, subject, sql, grain, description, source_tables, fk_edges,
         candidate_tables, join_rationale, confidence, params, check_sql, proposed_by, proposed_via)
    VALUES (
        v_kind, 'pending', v_name,
        nullif(btrim(p_draft->>'subject'), ''),
        nullif(btrim(p_draft->>'sql'), ''),
        nullif(btrim(p_draft->>'grain'), ''),
        nullif(btrim(p_draft->>'description'), ''),
        -- cubes carry source_tables[]; metrics carry a single 'source' — fold it in so the inbox
        -- "Reads from" works for both.
        CASE WHEN jsonb_typeof(p_draft->'source_tables') = 'array'      THEN p_draft->'source_tables'
             WHEN nullif(btrim(p_draft->>'source'), '') IS NOT NULL     THEN jsonb_build_array(p_draft->>'source')
             ELSE '[]'::jsonb END,
        CASE WHEN jsonb_typeof(p_draft->'fk_edges') = 'array'         THEN p_draft->'fk_edges'         ELSE '[]'::jsonb END,
        CASE WHEN jsonb_typeof(p_draft->'candidate_tables') = 'array' THEN p_draft->'candidate_tables'
             WHEN jsonb_typeof(p_draft->'candidate_sources') = 'array' THEN p_draft->'candidate_sources' ELSE '[]'::jsonb END,
        nullif(btrim(p_draft->>'join_rationale'), ''),
        CASE WHEN (p_draft->>'confidence') ~ '^[0-9.]+$' THEN (p_draft->>'confidence')::real ELSE NULL END,
        CASE WHEN jsonb_typeof(p_draft->'params') = 'object' THEN p_draft->'params' ELSE '{}'::jsonb END,
        nullif(btrim(p_draft->>'check_sql'), ''),
        p_proposed_by, p_proposed_via)
    RETURNING proposal_id INTO v_id;
    RETURN v_id;
END $fn$;

-- ── 3) proposals() — expose params + check_sql ─────────────────────────────
DROP FUNCTION IF EXISTS rvbbit.proposals(text, text);
CREATE FUNCTION rvbbit.proposals(p_status text DEFAULT NULL, p_kind text DEFAULT NULL)
RETURNS TABLE (
    proposal_id bigint, kind text, status text, name text, subject text, sql text,
    grain text, description text, source_tables jsonb, fk_edges jsonb, join_rationale text,
    confidence real, params jsonb, check_sql text, proposed_by text, proposed_via text,
    result_name text, notes text, created_at timestamptz, reviewed_at timestamptz
) LANGUAGE sql STABLE AS $$
    SELECT proposal_id, kind, status, name, subject, sql, grain, description, source_tables,
           fk_edges, join_rationale, confidence, params, check_sql, proposed_by, proposed_via,
           result_name, notes, created_at, reviewed_at
    FROM rvbbit.proposals
    WHERE (p_status IS NULL OR status = p_status)
      AND (p_kind   IS NULL OR kind   = p_kind)
    ORDER BY (status = 'pending') DESC, created_at DESC;
$$;

-- ── 4) the metric-drafting LLM operator ────────────────────────────────────
DO $do$
BEGIN
    PERFORM rvbbit.create_operator(
        op_name        => 'propose_metric_draft',
        op_shape       => 'scalar',
        op_arg_names   => ARRAY['context'],
        op_arg_types   => ARRAY['text'],
        op_return_type => 'jsonb',
        op_model       => 'openai/gpt-5.4-mini',
        op_parser      => 'json',
        op_max_tokens  => 6000,
        op_temperature => 0.2,
        op_description =>
            'Draft a metric (a named, governed SQL producing a small number/series over a cube or ' ||
            'table) from a subject + candidate sources. Returns strict JSON for a human to bless ' ||
            'via define_metric. Used by rvbbit.propose_metric.',
        op_system =>
            'You are an analytics engineer designing a METRIC: a named, governed SQL that returns a ' ||
            'SMALL result — a single number or a short time series — over the provided sources. You ' ||
            'are given a JSON context (subject, candidate_sources with their columns, source docs, and ' ||
            'a sample of existing metrics). STRONGLY PREFER a cube (a "cubes.*" source) when one fits — ' ||
            'cubes are the curated layer. Return ONLY a JSON object with EXACTLY these keys: ' ||
            '"name" (snake_case identifier matching ^[a-z_][a-z0-9_]*$, <=40 chars), ' ||
            '"sql" (a single Postgres SELECT that aggregates over ONE provided source; you MAY use ' ||
            '{param} tokens for optional filters — e.g. WHERE region = {region} — and you MAY reference ' ||
            'another metric as a subquery via {metric:NAME}; alias output columns clearly), ' ||
            '"grain" (one sentence: what one result row represents, e.g. "one row per calendar month" ' ||
            'or "a single scalar value"), "description" (1-2 sentences), ' ||
            '"params" (a JSON object giving a DEFAULT value for every {param} used in the sql; {} if none), ' ||
            '"check_sql" (OPTIONAL — only if this reads like a KPI/target: a boolean assertion that runs ' ||
            'against a CTE named `metric` and yields a column `ok`, e.g. "SELECT (SELECT value FROM metric) ' ||
            '>= {target} AS ok"; use null if it is not a KPI), "source" (the cubes.x or schema.table used), ' ||
            '"confidence" (0.0-1.0), "notes" (one brief sentence). ' ||
            'Never invent sources or columns not in the context. No markdown, no code fence — JSON only.',
        op_user =>
            E'METRIC PROPOSAL CONTEXT (JSON — subject, candidate_sources, columns, source_docs, existing_metrics):\n{{ context }}\n\nReturn the metric proposal JSON only.',
        op_tests => NULL
    );

    PERFORM rvbbit.set_operator_retry(
        'propose_metric_draft',
        $cfg${
          "until": {"function": "rvbbit.propose_metric_draft_valid"},
          "max_attempts": 3,
          "instructions": "Return ONLY a JSON object with non-empty name (snake_case, <=40 chars), sql (a single aggregating SELECT over one provided source), grain, description, params (object — defaults for any {param}, {} if none), optional check_sql, source, confidence. No markdown or extra keys."
        }$cfg$::jsonb
    );
END $do$;

CREATE OR REPLACE FUNCTION rvbbit.propose_metric_draft_valid(output text, inputs jsonb DEFAULT '{}'::jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE doc jsonb; v_name text;
BEGIN
    IF output IS NULL OR btrim(output) = '' THEN RETURN false; END IF;
    BEGIN doc := output::jsonb; EXCEPTION WHEN OTHERS THEN RETURN false; END;
    IF jsonb_typeof(doc) <> 'object' THEN RETURN false; END IF;
    v_name := nullif(btrim(doc->>'name'), '');
    RETURN v_name IS NOT NULL
       AND v_name ~ '^[a-z_][a-z0-9_]*$'
       AND nullif(btrim(doc->>'sql'), '')   IS NOT NULL
       AND nullif(btrim(doc->>'grain'), '') IS NOT NULL;
END $$;

-- ── 5) propose_metric — assemble context (cubes-first) → draft ─────────────
CREATE OR REPLACE FUNCTION rvbbit.propose_metric(
    p_subject      text,
    p_seed_sources text[] DEFAULT NULL,
    p_schema       text   DEFAULT NULL,
    p_max_sources  int    DEFAULT 8
) RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_srcs text[]; v_cols jsonb := '{}'::jsonb; v_docs jsonb := '{}'::jsonb; v_metrics jsonb;
    v_ctx text; v_out jsonb; s text; sch text; tbl text;
BEGIN
    IF p_subject IS NULL OR btrim(p_subject) = '' THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: subject is required';
    END IF;

    -- candidate sources: seed → semantic search over cubes+tables → cubes-first fallback
    IF p_seed_sources IS NOT NULL AND cardinality(p_seed_sources) > 0 THEN
        v_srcs := p_seed_sources;
    ELSE
        SELECT array_agg(DISTINCT schema_name || '.' || rel_name)
          INTO v_srcs
          FROM rvbbit.data_search(p_subject, p_max_sources, ARRAY['cube', 'db_table'], 'db_catalog')
         WHERE rel_name IS NOT NULL
           AND (p_schema IS NULL OR schema_name = p_schema OR schema_name = 'cubes');
        IF v_srcs IS NULL OR cardinality(v_srcs) = 0 THEN
            SELECT array_agg(t) INTO v_srcs FROM (
                SELECT 'cubes.' || name AS t FROM rvbbit.cubes()
                UNION ALL
                SELECT table_schema || '.' || table_name
                  FROM information_schema.tables
                 WHERE table_type = 'BASE TABLE'
                   AND table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit', 'cubes')
                   AND (p_schema IS NULL OR table_schema = p_schema)
                LIMIT p_max_sources) z;
        END IF;
    END IF;
    IF v_srcs IS NULL OR cardinality(v_srcs) = 0 THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: no candidate sources for subject % (pass p_seed_sources or build cubes/crawl)', p_subject;
    END IF;

    -- per-source columns + catalog docs (cubes carry rich docs)
    FOREACH s IN ARRAY v_srcs LOOP
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

    -- a few existing metrics (style + {metric:NAME} reuse hints)
    SELECT jsonb_agg(jsonb_build_object('name', name, 'grain', grain, 'description', description))
      INTO v_metrics
      FROM (SELECT name, grain, description FROM rvbbit.metric_catalog ORDER BY name LIMIT 20) m;

    v_ctx := jsonb_build_object(
        'subject', p_subject,
        'candidate_sources', to_jsonb(v_srcs),
        'columns', v_cols,
        'source_docs', v_docs,
        'existing_metrics', coalesce(v_metrics, '[]'::jsonb))::text;

    v_out := rvbbit.propose_metric_draft(v_ctx);
    IF v_out IS NULL
       OR nullif(btrim(v_out->>'name'), '') IS NULL
       OR nullif(btrim(v_out->>'sql'), '')  IS NULL THEN
        RAISE EXCEPTION 'rvbbit.propose_metric: draft generation failed for subject %', p_subject;
    END IF;

    RETURN v_out || jsonb_build_object('candidate_sources', to_jsonb(v_srcs));
END $fn$;

-- ── 6) accept_proposal — dispatch on kind (cube → define_cube, metric → define_metric) ──
CREATE OR REPLACE FUNCTION rvbbit.accept_proposal(
    p_id bigint, p_name text DEFAULT NULL, p_sql text DEFAULT NULL,
    p_grain text DEFAULT NULL, p_description text DEFAULT NULL, p_enrich boolean DEFAULT false
) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE
    r rvbbit.proposals%ROWTYPE; v_name text; v_sql text; v_grain text; v_desc text;
    v_owner text; v_version int;
BEGIN
    SELECT * INTO r FROM rvbbit.proposals WHERE proposal_id = p_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'rvbbit.accept_proposal: proposal % not found', p_id; END IF;
    IF r.status <> 'pending' THEN
        RAISE EXCEPTION 'rvbbit.accept_proposal: proposal % is % (not pending)', p_id, r.status;
    END IF;
    v_name  := coalesce(nullif(btrim(p_name), ''), r.name);
    v_sql   := coalesce(nullif(btrim(p_sql), ''), r.sql);
    v_grain := coalesce(nullif(btrim(p_grain), ''), r.grain);
    v_desc  := coalesce(nullif(btrim(p_description), ''), r.description);
    v_owner := coalesce(nullif(btrim(r.proposed_by), ''), 'proposal');
    IF v_name IS NULL OR v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.accept_proposal: name and sql are required to accept';
    END IF;

    IF r.kind = 'cube' THEN
        v_version := rvbbit.define_cube(v_name, v_sql, v_grain, v_desc, v_owner, NULL, 'proposed');
        IF p_enrich THEN
            BEGIN PERFORM rvbbit.enrich_cube(v_name); EXCEPTION WHEN OTHERS THEN NULL; END;
        END IF;
    ELSIF r.kind = 'metric' THEN
        v_version := rvbbit.define_metric(
            v_name, v_sql, coalesce(r.params, '{}'::jsonb), v_grain, v_desc, v_owner,
            jsonb_build_object('proposed', true), nullif(btrim(r.check_sql), ''));
    ELSE
        RAISE EXCEPTION 'rvbbit.accept_proposal: kind % not supported', r.kind;
    END IF;

    UPDATE rvbbit.proposals
       SET status = 'accepted', result_name = v_name, reviewed_at = now()
     WHERE proposal_id = p_id;
    RETURN jsonb_build_object('status', 'accepted', 'kind', r.kind, 'name', v_name, 'version', v_version);
END $fn$;
