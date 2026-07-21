-- 0203: Burrow mode enrollment (docs/BURROW_PLAN.md)
--
-- Postgres-as-IdP: sessions are PG roles; every surface executes
-- SET (LOCAL) ROLE under the viewer. Two cluster roles anchor it:
--   rvbbit_users — the login allowlist (membership = may sign in)
--   rvbbit_admin — admin surfaces gate on pg_has_role(sub, 'rvbbit_admin')
-- and SET ROLE requires the SERVICE role to be a member of each user role.
-- rvbbit.burrow_enroll() does all three grants in one call.
--
-- Role creation is cluster-level: guarded so a no-CREATEROLE upgrade path
-- degrades to a NOTICE instead of failing the migration.

DO $roles$
BEGIN
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rvbbit_users') THEN
            CREATE ROLE rvbbit_users NOLOGIN;
            COMMENT ON ROLE rvbbit_users IS
                'Burrow login allowlist: members may sign in to DataRabbit/MCP with their PG credentials (docs/BURROW_PLAN.md)';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rvbbit_admin') THEN
            CREATE ROLE rvbbit_admin NOLOGIN;
            COMMENT ON ROLE rvbbit_admin IS
                'Burrow admin marker: admin surfaces gate on membership (docs/BURROW_PLAN.md)';
        END IF;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'burrow: skipping rvbbit_users/rvbbit_admin creation (needs CREATEROLE) — create them manually to use Burrow mode';
    END;
END $roles$;

-- Enroll a Postgres account into Burrow: allowlist membership, SET ROLE
-- reachability for the service account (the caller's session role — run
-- this connected as the role rvbbit executes under), and optionally the
-- admin marker. SECURITY DEFINER so a designated admin can enroll without
-- holding cluster GRANT rights themselves.
CREATE OR REPLACE FUNCTION rvbbit.burrow_enroll(p_role text, p_admin boolean DEFAULT false)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER AS $fn$
DECLARE
    v_svc text := session_user;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_role AND rolcanlogin) THEN
        RAISE EXCEPTION 'burrow_enroll: % is not a LOGIN role', p_role;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_role AND rolsuper) THEN
        RAISE EXCEPTION 'burrow_enroll: refusing to enroll superuser % (sessions must be least-privilege)', p_role;
    END IF;
    EXECUTE format('GRANT rvbbit_users TO %I', p_role);
    -- the service account must be a member to SET ROLE into the user
    EXECUTE format('GRANT %I TO %I', p_role, v_svc);
    IF p_admin THEN
        EXECUTE format('GRANT rvbbit_admin TO %I', p_role);
    END IF;
    RETURN p_role || ' enrolled' || CASE WHEN p_admin THEN ' (admin)' ELSE '' END;
END $fn$;

COMMENT ON FUNCTION rvbbit.burrow_enroll(text, boolean) IS
    'Burrow mode: enroll a PG account — allowlist (rvbbit_users), SET ROLE reachability for the service account, optional rvbbit_admin. docs/BURROW_PLAN.md';
