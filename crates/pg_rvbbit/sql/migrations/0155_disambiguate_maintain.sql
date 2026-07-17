-- 0155_disambiguate_maintain.sql
-- Two eras created two rvbbit.maintain overloads: 0088's lifecycle surface
-- (maintain(text, text, boolean, integer, boolean) RETURNS TABLE) and the
-- 0138-era system sweep (Rust, maintain(bigint, ...) RETURNS jsonb). Both
-- are fully defaulted, so the bare `SELECT rvbbit.maintain();` that
-- install_maintenance_jobs schedules is AMBIGUOUS — the maintenance cron
-- jobs fail with "function rvbbit.maintain() is not unique".
--
-- The system sweep keeps the short name (it owns the cron contract and all
-- documented call sites). The lifecycle overload — whose only caller is the
-- maintain_cube() wrapper — becomes rvbbit.maintain_lifecycle.

DO $$
BEGIN
    IF to_regprocedure('rvbbit.maintain(text, text, boolean, integer, boolean)') IS NOT NULL THEN
        ALTER FUNCTION rvbbit.maintain(text, text, boolean, integer, boolean)
            RENAME TO maintain_lifecycle;
    END IF;
END $$;

-- Retarget the thin wrapper (same signature and behavior).
CREATE OR REPLACE FUNCTION rvbbit.maintain_cube(
    p_name text,
    p_dry_run boolean DEFAULT false,
    p_force boolean DEFAULT false
) RETURNS TABLE (
    target_kind text,
    target_name text,
    lifecycle_state text,
    maintenance_action text,
    executed boolean,
    status text,
    rows_written bigint,
    details jsonb,
    error text
) LANGUAGE sql AS $$
    SELECT *
    FROM rvbbit.maintain_lifecycle('cube', p_name, p_dry_run, NULL, p_force);
$$;
