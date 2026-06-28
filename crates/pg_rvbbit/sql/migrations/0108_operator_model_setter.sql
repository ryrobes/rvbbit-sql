-- 0108_operator_model_setter
--
-- Stable per-operator model setter for UI and SQL callers. System-level
-- defaults use set_semantic_model / set_cube_model; this is the one-row
-- counterpart for a specific rvbbit.operators entry.

CREATE OR REPLACE FUNCTION rvbbit.set_operator_model(
    op_name text,
    p_model text
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    v_model text := nullif(btrim(p_model), '');
    v_purged bigint;
BEGIN
    IF v_model IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_operator_model: model must not be empty';
    END IF;

    UPDATE rvbbit.operators SET model = v_model WHERE name = op_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_operator_model: unknown operator %', op_name;
    END IF;

    SELECT rvbbit.judgment_purge(op_name) INTO v_purged;
    RETURN v_model;
END $$;

