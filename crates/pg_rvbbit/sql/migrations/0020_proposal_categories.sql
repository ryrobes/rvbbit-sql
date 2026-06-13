-- 0020_proposal_categories — capture, suggest, and set categories through the proposal flow.
--
-- Proposals were uncategorized because category was never wired: the proposals table had no
-- category column, the drafters never suggested one, and accept_proposal never set one (metrics
-- have no category param at all — categories live in the shared rvbbit.entity_categories taxonomy
-- via set_category). This wires it end-to-end: proposals carry category/subcategory; the drafters
-- suggest them; record_proposal captures them; refine_proposal lets a human edit them; accept_proposal
-- sets them on the created cube/metric via set_category (the taxonomy the lens catalogs read).
-- Additive + idempotent.

ALTER TABLE rvbbit.proposals ADD COLUMN IF NOT EXISTS category    text;
ALTER TABLE rvbbit.proposals ADD COLUMN IF NOT EXISTS subcategory text;

-- record_proposal — also capture category/subcategory from the draft
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
         candidate_tables, join_rationale, confidence, params, check_sql, category, subcategory,
         proposed_by, proposed_via)
    VALUES (
        v_kind, 'pending', v_name,
        nullif(btrim(p_draft->>'subject'), ''),
        nullif(btrim(p_draft->>'sql'), ''),
        nullif(btrim(p_draft->>'grain'), ''),
        nullif(btrim(p_draft->>'description'), ''),
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
        nullif(btrim(p_draft->>'category'), ''),
        nullif(btrim(p_draft->>'subcategory'), ''),
        p_proposed_by, p_proposed_via)
    RETURNING proposal_id INTO v_id;
    RETURN v_id;
END $fn$;

-- proposals() — expose category/subcategory
DROP FUNCTION IF EXISTS rvbbit.proposals(text, text);
CREATE FUNCTION rvbbit.proposals(p_status text DEFAULT NULL, p_kind text DEFAULT NULL)
RETURNS TABLE (
    proposal_id bigint, kind text, status text, name text, subject text, sql text,
    grain text, description text, source_tables jsonb, fk_edges jsonb, join_rationale text,
    confidence real, params jsonb, check_sql text, category text, subcategory text,
    proposed_by text, proposed_via text, result_name text, notes text,
    created_at timestamptz, reviewed_at timestamptz
) LANGUAGE sql STABLE AS $$
    SELECT proposal_id, kind, status, name, subject, sql, grain, description, source_tables,
           fk_edges, join_rationale, confidence, params, check_sql, category, subcategory,
           proposed_by, proposed_via, result_name, notes, created_at, reviewed_at
    FROM rvbbit.proposals
    WHERE (p_status IS NULL OR status = p_status)
      AND (p_kind   IS NULL OR kind   = p_kind)
    ORDER BY (status = 'pending') DESC, created_at DESC;
$$;

-- refine_proposal — also edit category/subcategory on a pending draft
CREATE OR REPLACE FUNCTION rvbbit.refine_proposal(
    p_id             bigint,
    p_name           text  DEFAULT NULL,
    p_sql            text  DEFAULT NULL,
    p_grain          text  DEFAULT NULL,
    p_description    text  DEFAULT NULL,
    p_params         jsonb DEFAULT NULL,
    p_check_sql      text  DEFAULT NULL,
    p_join_rationale text  DEFAULT NULL,
    p_confidence     real  DEFAULT NULL,
    p_category       text  DEFAULT NULL,
    p_subcategory    text  DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE r rvbbit.proposals%ROWTYPE;
BEGIN
    SELECT * INTO r FROM rvbbit.proposals WHERE proposal_id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.refine_proposal: proposal % not found', p_id;
    END IF;
    IF r.status <> 'pending' THEN
        RAISE EXCEPTION 'rvbbit.refine_proposal: proposal % is % (only pending proposals can be refined)', p_id, r.status;
    END IF;
    UPDATE rvbbit.proposals SET
        name           = coalesce(nullif(btrim(p_name), ''), name),
        sql            = coalesce(nullif(btrim(p_sql), ''), sql),
        grain          = coalesce(nullif(btrim(p_grain), ''), grain),
        description    = coalesce(nullif(btrim(p_description), ''), description),
        params         = coalesce(p_params, params),
        check_sql      = coalesce(nullif(btrim(p_check_sql), ''), check_sql),
        join_rationale = coalesce(nullif(btrim(p_join_rationale), ''), join_rationale),
        confidence     = coalesce(p_confidence, confidence),
        category       = coalesce(nullif(btrim(p_category), ''), category),
        subcategory    = coalesce(nullif(btrim(p_subcategory), ''), subcategory)
    WHERE proposal_id = p_id;
    RETURN jsonb_build_object('status', 'refined', 'proposal_id', p_id);
END $fn$;

-- accept_proposal — pass the category to the cube def + set the shared taxonomy on accept
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
        v_version := rvbbit.define_cube(v_name, v_sql, v_grain, v_desc, v_owner, NULL,
                        coalesce(nullif(btrim(r.category), ''), 'proposed'));
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

    -- the curated category lands in the shared taxonomy (what the lens catalogs read)
    IF nullif(btrim(r.category), '') IS NOT NULL THEN
        BEGIN
            PERFORM rvbbit.set_category(r.kind, v_name, r.category, nullif(btrim(r.subcategory), ''));
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END IF;

    UPDATE rvbbit.proposals
       SET status = 'accepted', result_name = v_name, reviewed_at = now()
     WHERE proposal_id = p_id;
    RETURN jsonb_build_object('status', 'accepted', 'kind', r.kind, 'name', v_name, 'version', v_version);
END $fn$;

-- teach the drafters to suggest a 2-level category (idempotent append).
UPDATE rvbbit.operators
   SET system_prompt = system_prompt ||
       E'\nAlso suggest a "category" and optional "subcategory" (a short 2-level subject area, e.g. ' ||
       'category="Sales", subcategory="Pipeline") so the asset is filed in the catalog, not left ' ||
       'uncategorized. Reuse an existing category name when one fits.'
 WHERE name IN ('propose_cube_draft', 'propose_metric_draft')
   AND position('suggest a "category"' IN system_prompt) = 0;
