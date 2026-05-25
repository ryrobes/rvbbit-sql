-- pg_rvbbit 0.21.0 -> 0.22.0
-- Loop 21: heterogeneous takes.
--
-- A take can now be any node — an llm, specialist, or code engine — not
-- just an LLM model id. The takes config gains an optional `nodes` array
-- (each entry the same shape as a `steps` node); when present, each node
-- is one take and the ensemble runs them all and reduces to one answer.
--
-- set_operator_takes now accepts a config keyed by "factor" (homogeneous
-- takes) OR "nodes" (heterogeneous takes).

CREATE OR REPLACE FUNCTION rvbbit.set_operator_takes(
    op_name      text,
    takes_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF takes_config IS NOT NULL THEN
        IF jsonb_typeof(takes_config) <> 'object' THEN
            RAISE EXCEPTION 'rvbbit.set_operator_takes: takes_config must be a JSON object';
        END IF;
        IF takes_config->'factor' IS NULL AND takes_config->'nodes' IS NULL THEN
            RAISE EXCEPTION 'rvbbit.set_operator_takes: takes_config needs a "factor" (homogeneous takes) or "nodes" (heterogeneous takes)';
        END IF;
    END IF;
    UPDATE rvbbit.operators SET takes = takes_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_takes: unknown operator %', op_name;
    END IF;
END $$;
