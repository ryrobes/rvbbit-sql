-- pg_rvbbit 0.17.0 -> 0.18.0
-- Loop 16: validators + retry.
--
-- Operators gain an optional `retry` jsonb plan:
--   {"until": <validator>, "max_attempts": int, "instructions": text}
-- The operator re-runs (with `instructions` appended to the prompt) until
-- its output passes the validator, up to max_attempts. A validator is a
-- SQL boolean expression or a Postgres function — rvbbit is inside
-- Postgres, so SQL itself is the validator language, no sandbox needed.

ALTER TABLE rvbbit.operators ADD COLUMN IF NOT EXISTS retry jsonb;

CREATE OR REPLACE FUNCTION rvbbit.set_operator_retry(
    op_name      text,
    retry_config jsonb
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF retry_config IS NOT NULL THEN
        IF jsonb_typeof(retry_config) <> 'object' THEN
            RAISE EXCEPTION 'rvbbit.set_operator_retry: retry_config must be a JSON object';
        END IF;
        IF retry_config->'until' IS NULL THEN
            RAISE EXCEPTION 'rvbbit.set_operator_retry: retry_config needs an "until" validator';
        END IF;
    END IF;
    UPDATE rvbbit.operators SET retry = retry_config WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_retry: unknown operator %', op_name;
    END IF;
END $$;
