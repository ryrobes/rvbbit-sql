-- 0009_cube_model — configure the model the cube LLM operators use.
--
-- The cube AI runs through two operators: `cube_enrich` (drafts per-column docs / grain /
-- description) and `propose_cube_draft` (drafts a join from a subject). Both ship pointing at
-- openai/gpt-5.4-mini. The model is NOT hard-coded in Rust — it lives in `rvbbit.operators.model`,
-- the designed config surface: editing it is runtime (no rebuild), PER-DATABASE, and auto-
-- invalidates that operator's result cache. set_cube_model is a one-call convenience over that for
-- both cube operators; cube_models() shows the current config. (A per-CALL override also works via
-- opts.model on the operator, and the lens Operators window can edit either operator directly.)

-- set the model for the cube LLM operators (NULL/'' resets to the shipped default). Returns it.
CREATE OR REPLACE FUNCTION rvbbit.set_cube_model(p_model text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE v_model text := coalesce(nullif(btrim(p_model), ''), 'openai/gpt-5.4-mini');
BEGIN
    UPDATE rvbbit.operators SET model = v_model
     WHERE name IN ('cube_enrich', 'propose_cube_draft');
    RETURN v_model;
END $fn$;

-- show the model (+ token budget) each cube operator currently uses.
CREATE OR REPLACE FUNCTION rvbbit.cube_models()
RETURNS TABLE (operator text, model text, max_tokens int)
LANGUAGE sql STABLE AS $$
    SELECT name, model, max_tokens
    FROM rvbbit.operators
    WHERE name IN ('cube_enrich', 'propose_cube_draft')
    ORDER BY name;
$$;
