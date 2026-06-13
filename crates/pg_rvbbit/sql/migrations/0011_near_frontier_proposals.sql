-- 0011_near_frontier_proposals — default the cube/metric AI to a near-frontier model.
--
-- propose_cube / propose_metric reason over the LIVE schema to author joins/aggregations, and
-- cube_enrich documents columns. On a small model (gpt-5.4-mini) the drafters can hallucinate
-- column/table names. Bump the default to NEAR-FRONTIER (openai/gpt-5.4) for all three cube/metric
-- AI operators, and extend set_cube_model to cover propose_metric_draft (added in 0010). Still
-- per-operator-editable (rvbbit.operators.model / the lens Operators window) and per-call
-- (opts.model); set_cube_model('openai/gpt-5.5') goes frontier, set_cube_model('openai/gpt-5.4-mini')
-- goes back to cheap. CREATE OR REPLACE only → hot-applyable.

-- set the model for ALL cube/metric AI operators (NULL resets to the near-frontier default).
CREATE OR REPLACE FUNCTION rvbbit.set_cube_model(p_model text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE v_model text := coalesce(nullif(btrim(p_model), ''), 'openai/gpt-5.4');
BEGIN
    UPDATE rvbbit.operators SET model = v_model
     WHERE name IN ('cube_enrich', 'propose_cube_draft', 'propose_metric_draft');
    RETURN v_model;
END $fn$;

CREATE OR REPLACE FUNCTION rvbbit.cube_models()
RETURNS TABLE (operator text, model text, max_tokens int)
LANGUAGE sql STABLE AS $$
    SELECT name, model, max_tokens
    FROM rvbbit.operators
    WHERE name IN ('cube_enrich', 'propose_cube_draft', 'propose_metric_draft')
    ORDER BY name;
$$;

-- apply the near-frontier default to this install (idempotent UPDATE under the hood)
SELECT rvbbit.set_cube_model('openai/gpt-5.4');
