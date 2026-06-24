-- Ensure operator definition edits take effect immediately in the editing
-- backend. The executor memoizes operator definitions briefly for scan speed;
-- without this flush, an UPDATE followed by a same-session call can reuse the
-- old steps/prompt until the memo TTL expires.
CREATE OR REPLACE FUNCTION rvbbit.touch_operators_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    BEGIN
        PERFORM rvbbit.flush_cache();
    EXCEPTION WHEN undefined_function THEN
        -- Fresh CREATE EXTENSION can seed/update operators before pgrx-defined
        -- functions are registered in the generated SQL. Normal installations
        -- and upgrades still flush immediately once flush_cache exists.
        NULL;
    END;
    RETURN NEW;
END $$;
