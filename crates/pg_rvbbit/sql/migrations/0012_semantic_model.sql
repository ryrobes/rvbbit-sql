-- 0012_semantic_model — a global knob for the LLM-based semantic operators' model.
--
-- Model resolution is per-call (opts.model) → else the operator's own rvbbit.operators.model; there
-- is no global default. set_cube_model covers only the 3 cube/metric AI operators. This adds
-- set_semantic_model for the GENERAL LLM semantic operators (about / classify / summarize / extract
-- / means / tags / triples / …) — everything that runs an LLM, EXCLUDING the cube/metric ops (those
-- keep their own set_cube_model knob, so the two are orthogonal and don't clobber each other).
-- "LLM-based" = the single-LLM-call path (steps IS NULL) or an operator with an llm step; pure
-- specialist/mcp/code operators (which ignore op.model) are left alone. CREATE OR REPLACE only →
-- hot-applyable. This migration only ADDS the knob; it does not change any current model.

-- predicate: does this operator actually call an LLM (so op.model matters)?
CREATE OR REPLACE FUNCTION rvbbit._operator_is_llm(p_steps jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_steps IS NULL
        OR (jsonb_typeof(p_steps) = 'array'
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(p_steps) s WHERE s->>'kind' = 'llm'));
$$;

-- set the model for all general LLM semantic operators (NULL resets to the shipped mini default).
-- Cube/metric AI operators are excluded — use set_cube_model for those.
CREATE OR REPLACE FUNCTION rvbbit.set_semantic_model(p_model text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE v_model text := coalesce(nullif(btrim(p_model), ''), 'openai/gpt-5.4-mini'); v_n integer;
BEGIN
    UPDATE rvbbit.operators SET model = v_model
     WHERE name NOT IN ('cube_enrich', 'propose_cube_draft', 'propose_metric_draft')
       AND rvbbit._operator_is_llm(steps);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END $fn$;

-- show the model distribution across the general LLM semantic operators.
CREATE OR REPLACE FUNCTION rvbbit.semantic_models()
RETURNS TABLE (model text, operators bigint, examples text)
LANGUAGE sql STABLE AS $$
    SELECT model, count(*),
           string_agg(name, ', ' ORDER BY name)
             FILTER (WHERE name = ANY('{about,classify,summarize,extract,means,tags,triples}'::text[]))
    FROM rvbbit.operators
    WHERE name NOT IN ('cube_enrich', 'propose_cube_draft', 'propose_metric_draft')
      AND rvbbit._operator_is_llm(steps)
    GROUP BY model
    ORDER BY count(*) DESC;
$$;
