-- Fix PL/pgSQL ambiguity in the cost-policy upsert.

CREATE OR REPLACE FUNCTION rvbbit.set_cost_policy(
    target_kind text,
    target_name text,
    policy text,
    fixed_cost_usd numeric DEFAULT NULL,
    input_per_mtok numeric DEFAULT NULL,
    output_per_mtok numeric DEFAULT NULL,
    model text DEFAULT NULL,
    notes text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    row_doc jsonb;
BEGIN
    INSERT INTO rvbbit.cost_policies
        (target_kind, target_name, policy, fixed_cost_usd, input_per_mtok,
         output_per_mtok, model, notes, updated_at)
    VALUES
        (target_kind, target_name, policy, fixed_cost_usd, input_per_mtok,
         output_per_mtok, model, notes, clock_timestamp())
    ON CONFLICT ON CONSTRAINT cost_policies_pkey
    DO UPDATE SET
        policy = EXCLUDED.policy,
        fixed_cost_usd = EXCLUDED.fixed_cost_usd,
        input_per_mtok = EXCLUDED.input_per_mtok,
        output_per_mtok = EXCLUDED.output_per_mtok,
        model = EXCLUDED.model,
        notes = EXCLUDED.notes,
        updated_at = clock_timestamp()
    RETURNING to_jsonb(rvbbit.cost_policies.*) INTO row_doc;
    RETURN row_doc;
END $$;
