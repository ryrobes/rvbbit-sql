-- 0143_secrets.sql — SQL-settable secret store.
--
-- Backends resolve their auth key from an env var today (auth_header_env names
-- the var). This adds a second, SQL-settable source so keys can be set from the
-- UI / SQL without host access: the extension resolves a backend's key at
-- spec-load time as env-var FIRST, then this table. Broadly useful for any
-- future integration that needs a key wired from inside the database.
--
-- Values are plaintext (MVP); direct table access is revoked from PUBLIC so a
-- secret is not browsable, and get_secret() only returns a value when the name
-- is actually an active backend's auth_header_env — i.e. it is a resolver for
-- keys already in use, not a general secret-reading oracle. Encryption
-- (pgcrypto / Fernet, as the MCP gateway does) is a later hardening.

CREATE TABLE IF NOT EXISTS rvbbit.secrets (
    name        text PRIMARY KEY,
    value       text NOT NULL,
    description text,
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by  text NOT NULL DEFAULT current_user
);

REVOKE ALL ON rvbbit.secrets FROM PUBLIC;

-- Write path — gated like other rvbbit DDL (superuser in this release).
CREATE OR REPLACE FUNCTION rvbbit.set_secret(
    secret_name        text,
    secret_value       text,
    secret_description text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $ss$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF secret_name IS NULL OR btrim(secret_name) = '' THEN
        RAISE EXCEPTION 'secret name is required';
    END IF;
    IF secret_value IS NULL OR secret_value = '' THEN
        RAISE EXCEPTION 'secret value is required';
    END IF;
    INSERT INTO rvbbit.secrets (name, value, description, updated_by)
    VALUES (btrim(secret_name), secret_value, secret_description, current_user)
    ON CONFLICT (name) DO UPDATE SET
        value       = EXCLUDED.value,
        description  = COALESCE(EXCLUDED.description, rvbbit.secrets.description),
        updated_at   = clock_timestamp(),
        updated_by   = current_user;
END
$ss$;

CREATE OR REPLACE FUNCTION rvbbit.delete_secret(secret_name text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
AS $ds$
DECLARE
    n integer;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    DELETE FROM rvbbit.secrets WHERE name = btrim(secret_name);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n > 0;
END
$ds$;

-- Resolver — callable by any role (semantic queries run as any user), but only
-- returns a value when the name is a live backend's auth_header_env. This is
-- what spec-load calls on the leader; the resolved token is then cached in the
-- SpecialistSpec so no pool-thread ever needs SPI.
CREATE OR REPLACE FUNCTION rvbbit.get_secret(secret_name text)
RETURNS text
LANGUAGE sql
SECURITY DEFINER
STABLE
AS $gs$
    SELECT s.value
    FROM rvbbit.secrets s
    WHERE s.name = secret_name
      AND EXISTS (
          SELECT 1 FROM rvbbit.backends b WHERE b.auth_header_env = s.name
      );
$gs$;

REVOKE ALL ON FUNCTION rvbbit.get_secret(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rvbbit.get_secret(text) TO PUBLIC;

-- Enumeration — names + metadata only, never values; gated so only admins can
-- list what exists. The UI uses this to show "key is set".
CREATE OR REPLACE FUNCTION rvbbit.list_secrets()
RETURNS TABLE (name text, description text, updated_at timestamptz, updated_by text)
LANGUAGE plpgsql
SECURITY DEFINER
AS $ls$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    RETURN QUERY
        SELECT s.name, s.description, s.updated_at, s.updated_by
        FROM rvbbit.secrets s ORDER BY s.name;
END
$ls$;
