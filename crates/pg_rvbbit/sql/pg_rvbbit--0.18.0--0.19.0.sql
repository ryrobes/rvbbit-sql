-- pg_rvbbit 0.18.0 -> 0.19.0
-- Loop 17 + 18: wards and takes — completing the semantic flow system.
--
-- wards: pre/post validator gates on an operator.
--   {"pre":[{validator,mode}], "post":[{validator,mode}]}
--   mode is 'blocking' (fail the call) or 'advisory' (warn, continue).
-- takes: run the operator N times and reduce to one answer.
--   {"factor":int, "models":[...], "reduce":"vote"|"first_valid"|"evaluator",
--    "filter":<validator>, "evaluator":{"model":text,"instructions":text}}

ALTER TABLE rvbbit.operators ADD COLUMN IF NOT EXISTS wards jsonb;
ALTER TABLE rvbbit.operators ADD COLUMN IF NOT EXISTS takes jsonb;

CREATE OR REPLACE FUNCTION rvbbit.set_operator_wards(
    op_name      text,
    wards_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF wards_config IS NOT NULL AND jsonb_typeof(wards_config) <> 'object' THEN
        RAISE EXCEPTION 'rvbbit.set_operator_wards: wards_config must be a JSON object';
    END IF;
    UPDATE rvbbit.operators SET wards = wards_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_wards: unknown operator %', op_name;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.set_operator_takes(
    op_name      text,
    takes_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF takes_config IS NOT NULL THEN
        IF jsonb_typeof(takes_config) <> 'object' THEN
            RAISE EXCEPTION 'rvbbit.set_operator_takes: takes_config must be a JSON object';
        END IF;
        IF takes_config->'factor' IS NULL THEN
            RAISE EXCEPTION 'rvbbit.set_operator_takes: takes_config needs a "factor"';
        END IF;
    END IF;
    UPDATE rvbbit.operators SET takes = takes_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_takes: unknown operator %', op_name;
    END IF;
END $$;
