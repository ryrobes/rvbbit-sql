-- 0286: encrypted, reference-addressed canonical credential store.
--
-- Secret values are AEAD ciphertext in Postgres. Key material remains in the
-- postmaster environment or mounted key file (RVBBIT_CREDENTIAL_KEYS[_FILE] /
-- RVBBIT_CREDENTIAL_KEY[_FILE]). The immutable reference
-- is authenticated as AEAD associated data, preventing ciphertext row swaps.
--
-- Existing backend callers keep set_secret/get_secret/list_secrets. New writes
-- use the canonical store; old plaintext rows remain readable until an admin
-- explicitly calls migrate_legacy_secrets(). MCP-specific functions let the
-- gateway retain its existing server/name API without retaining another file.

CREATE OR REPLACE FUNCTION rvbbit.credential_key_available()
RETURNS boolean
LANGUAGE c STABLE
AS '$libdir/pg_rvbbit', 'credential_key_available_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.credential_seal(
    credential_ref text,
    secret_value text
) RETURNS bytea
LANGUAGE c VOLATILE STRICT
AS '$libdir/pg_rvbbit', 'credential_seal_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.credential_unseal(
    credential_ref text,
    envelope bytea
) RETURNS text
LANGUAGE c VOLATILE STRICT
AS '$libdir/pg_rvbbit', 'credential_unseal_wrapper';

REVOKE ALL ON FUNCTION rvbbit.credential_seal(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.credential_unseal(text, bytea) FROM PUBLIC;

CREATE TABLE IF NOT EXISTS rvbbit.credentials (
    credential_ref text PRIMARY KEY,
    kind text NOT NULL,
    namespace text NOT NULL,
    name text NOT NULL,
    ciphertext bytea,
    version integer NOT NULL DEFAULT 1,
    key_version integer NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'active',
    description text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL DEFAULT session_user,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by text NOT NULL DEFAULT session_user,
    rotated_at timestamptz,
    CONSTRAINT credentials_ref_check
        CHECK (
            char_length(credential_ref) BETWEEN 3 AND 512
            AND credential_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:/@-]+$'
        ),
    CONSTRAINT credentials_segment_check
        CHECK (
            kind ~ '^[a-z][a-z0-9_-]{0,63}$'
            AND namespace ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'
            AND name ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'
        ),
    CONSTRAINT credentials_version_check CHECK (version >= 1 AND key_version >= 1),
    CONSTRAINT credentials_status_check CHECK (status IN ('active', 'revoked')),
    CONSTRAINT credentials_ciphertext_check
        CHECK ((status = 'active' AND ciphertext IS NOT NULL)
            OR (status = 'revoked' AND ciphertext IS NULL)),
    CONSTRAINT credentials_metadata_check CHECK (jsonb_typeof(metadata) = 'object'),
    UNIQUE (kind, namespace, name)
);

CREATE INDEX IF NOT EXISTS credentials_kind_namespace_idx
    ON rvbbit.credentials (kind, namespace, status, name);

CREATE TABLE IF NOT EXISTS rvbbit.credential_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    credential_ref text NOT NULL,
    event_type text NOT NULL,
    actor text NOT NULL DEFAULT session_user,
    consumer text,
    purpose text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT credential_events_type_check
        CHECK (event_type IN (
            'created','rotated','rewrapped','resolved','revoked','deleted','migrated'
        )),
    CONSTRAINT credential_events_details_check CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX IF NOT EXISTS credential_events_ref_created_idx
    ON rvbbit.credential_events (credential_ref, created_at DESC);

REVOKE ALL ON rvbbit.credentials FROM PUBLIC;
REVOKE ALL ON rvbbit.credential_events FROM PUBLIC;

DO $revoke_credential_sequence$
BEGIN
    IF to_regclass('rvbbit.credential_events_event_id_seq') IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON SEQUENCE rvbbit.credential_events_event_id_seq FROM PUBLIC';
    END IF;
END
$revoke_credential_sequence$;

CREATE OR REPLACE FUNCTION rvbbit.credential_ref(
    credential_kind text,
    credential_namespace text,
    credential_name text
) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, rvbbit
AS $credential_ref$
DECLARE
    k text := lower(btrim(credential_kind));
    ns text := btrim(credential_namespace);
    n text := btrim(credential_name);
BEGIN
    IF k !~ '^[a-z][a-z0-9_-]{0,63}$'
       OR ns !~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'
       OR n !~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$' THEN
        RAISE EXCEPTION 'credential kind, namespace, or name has an invalid format';
    END IF;
    RETURN k || '/' || ns || '/' || n;
END
$credential_ref$;

CREATE OR REPLACE FUNCTION rvbbit.put_credential(
    credential_kind text,
    credential_namespace text,
    credential_name text,
    secret_value text,
    credential_description text DEFAULT NULL,
    credential_metadata jsonb DEFAULT '{}'::jsonb
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $put_credential$
DECLARE
    ref text;
    previous_version integer;
    next_event text;
    safe_metadata jsonb := coalesce(credential_metadata, '{}'::jsonb);
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF secret_value IS NULL OR secret_value = '' THEN
        RAISE EXCEPTION 'credential value is required';
    END IF;
    IF NOT rvbbit.credential_key_available() THEN
        RAISE EXCEPTION 'canonical credential encryption is unavailable; configure an RVBBIT credential key or key file';
    END IF;
    IF jsonb_typeof(safe_metadata) <> 'object' THEN
        RAISE EXCEPTION 'credential metadata must be a JSON object';
    END IF;
    IF safe_metadata ?| ARRAY[
        'value','secret','token','password','private_key','privateKey','credentials'
    ] THEN
        RAISE EXCEPTION 'credential metadata contains a forbidden secret-like field';
    END IF;

    ref := rvbbit.credential_ref(
        credential_kind, credential_namespace, credential_name
    );
    SELECT version INTO previous_version
    FROM rvbbit.credentials
    WHERE credential_ref = ref;
    next_event := CASE WHEN previous_version IS NULL THEN 'created' ELSE 'rotated' END;

    INSERT INTO rvbbit.credentials (
        credential_ref, kind, namespace, name, ciphertext, version,
        key_version, status, description, metadata, created_by, updated_by,
        rotated_at
    ) VALUES (
        ref,
        lower(btrim(credential_kind)),
        btrim(credential_namespace),
        btrim(credential_name),
        rvbbit.credential_seal(ref, secret_value),
        1,
        1,
        'active',
        credential_description,
        safe_metadata,
        session_user,
        session_user,
        NULL
    )
    ON CONFLICT (credential_ref) DO UPDATE SET
        ciphertext = rvbbit.credential_seal(ref, secret_value),
        version = rvbbit.credentials.version + 1,
        key_version = rvbbit.credentials.key_version + 1,
        status = 'active',
        description = coalesce(EXCLUDED.description, rvbbit.credentials.description),
        metadata = EXCLUDED.metadata,
        updated_at = clock_timestamp(),
        updated_by = session_user,
        rotated_at = clock_timestamp();

    INSERT INTO rvbbit.credential_events (
        credential_ref, event_type, actor, details
    ) VALUES (
        ref,
        next_event,
        session_user,
        jsonb_build_object(
            'kind', lower(btrim(credential_kind)),
            'namespace', btrim(credential_namespace),
            'name', btrim(credential_name),
            'version', coalesce(previous_version, 0) + 1
        )
    );
    RETURN ref;
END
$put_credential$;

CREATE OR REPLACE FUNCTION rvbbit.resolve_credential(
    requested_ref text,
    credential_consumer text,
    credential_purpose text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $resolve_credential$
DECLARE
    sealed bytea;
    resolved text;
BEGIN
    IF requested_ref IS NULL OR btrim(requested_ref) = '' THEN
        RAISE EXCEPTION 'credential reference is required';
    END IF;
    IF credential_consumer IS NULL OR btrim(credential_consumer) = ''
       OR credential_purpose IS NULL OR btrim(credential_purpose) = '' THEN
        RAISE EXCEPTION 'credential consumer and purpose are required';
    END IF;
    SELECT ciphertext INTO sealed
    FROM rvbbit.credentials
    WHERE credential_ref = btrim(requested_ref)
      AND status = 'active';
    IF sealed IS NULL THEN
        RETURN NULL;
    END IF;
    resolved := rvbbit.credential_unseal(btrim(requested_ref), sealed);
    INSERT INTO rvbbit.credential_events (
        credential_ref, event_type, actor, consumer, purpose
    ) VALUES (
        btrim(requested_ref), 'resolved', session_user,
        left(btrim(credential_consumer), 128),
        left(btrim(credential_purpose), 128)
    );
    RETURN resolved;
END
$resolve_credential$;

REVOKE ALL ON FUNCTION rvbbit.resolve_credential(text, text, text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION rvbbit.delete_credential(
    requested_ref text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $delete_credential$
DECLARE
    n integer;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    DELETE FROM rvbbit.credentials
    WHERE credential_ref = btrim(requested_ref);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        INSERT INTO rvbbit.credential_events (
            credential_ref, event_type, actor
        ) VALUES (btrim(requested_ref), 'deleted', session_user);
    END IF;
    RETURN n > 0;
END
$delete_credential$;

CREATE OR REPLACE FUNCTION rvbbit.list_credentials(
    requested_kind text DEFAULT NULL,
    requested_namespace text DEFAULT NULL
) RETURNS TABLE (
    credential_ref text,
    kind text,
    namespace text,
    name text,
    version integer,
    status text,
    description text,
    metadata jsonb,
    updated_at timestamptz,
    updated_by text
)
LANGUAGE plpgsql SECURITY DEFINER STABLE
SET search_path = pg_catalog, rvbbit
AS $list_credentials$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    RETURN QUERY
    SELECT c.credential_ref, c.kind, c.namespace, c.name, c.version,
           c.status, c.description, c.metadata, c.updated_at, c.updated_by
    FROM rvbbit.credentials c
    WHERE (requested_kind IS NULL OR c.kind = lower(btrim(requested_kind)))
      AND (requested_namespace IS NULL OR c.namespace = btrim(requested_namespace))
    ORDER BY c.kind, c.namespace, c.name;
END
$list_credentials$;

-- Existing AI-provider/backend UI compatibility. Environment variables still
-- win in Rust. New database writes are encrypted under backend/default/NAME.
CREATE OR REPLACE FUNCTION rvbbit.set_secret(
    secret_name text,
    secret_value text,
    secret_description text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $set_secret_canonical$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    PERFORM rvbbit.put_credential(
        'backend', 'default', btrim(secret_name), secret_value,
        secret_description,
        jsonb_build_object('compatibility_api', 'rvbbit.set_secret')
    );
    DELETE FROM rvbbit.secrets WHERE name = btrim(secret_name);
END
$set_secret_canonical$;

CREATE OR REPLACE FUNCTION rvbbit.get_secret(secret_name text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $get_secret_canonical$
DECLARE
    ref text;
    result text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM rvbbit.backends b
        WHERE b.auth_header_env = secret_name
    ) THEN
        RETURN NULL;
    END IF;
    ref := rvbbit.credential_ref('backend', 'default', btrim(secret_name));
    IF EXISTS (
        SELECT 1 FROM rvbbit.credentials c
        WHERE c.credential_ref = ref AND c.status = 'active'
    ) THEN
        RETURN rvbbit.resolve_credential(ref, 'pg_rvbbit', 'backend_auth');
    END IF;
    SELECT s.value INTO result
    FROM rvbbit.secrets s
    WHERE s.name = btrim(secret_name);
    RETURN result;
END
$get_secret_canonical$;

REVOKE ALL ON FUNCTION rvbbit.get_secret(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rvbbit.get_secret(text) TO PUBLIC;

CREATE OR REPLACE FUNCTION rvbbit.delete_secret(secret_name text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $delete_secret_canonical$
DECLARE
    legacy_count integer;
    canonical_deleted boolean;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    canonical_deleted := rvbbit.delete_credential(
        rvbbit.credential_ref('backend', 'default', btrim(secret_name))
    );
    DELETE FROM rvbbit.secrets WHERE name = btrim(secret_name);
    GET DIAGNOSTICS legacy_count = ROW_COUNT;
    RETURN canonical_deleted OR legacy_count > 0;
END
$delete_secret_canonical$;

CREATE OR REPLACE FUNCTION rvbbit.list_secrets()
RETURNS TABLE (
    name text,
    description text,
    updated_at timestamptz,
    updated_by text
)
LANGUAGE plpgsql SECURITY DEFINER STABLE
SET search_path = pg_catalog, rvbbit
AS $list_secrets_canonical$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    RETURN QUERY
    SELECT DISTINCT ON (listed.name)
           listed.name, listed.description, listed.updated_at, listed.updated_by
    FROM (
        SELECT c.name, c.description, c.updated_at, c.updated_by, 0 AS precedence
        FROM rvbbit.credentials c
        WHERE c.kind = 'backend'
          AND c.namespace = 'default'
          AND c.status = 'active'
        UNION ALL
        SELECT s.name, s.description, s.updated_at, s.updated_by, 1 AS precedence
        FROM rvbbit.secrets s
    ) listed
    ORDER BY listed.name, listed.precedence;
END
$list_secrets_canonical$;

CREATE OR REPLACE FUNCTION rvbbit.migrate_legacy_secrets()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $migrate_legacy_secrets$
DECLARE
    legacy record;
    migrated integer := 0;
    ref text;
    canonical_preserved boolean;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF NOT rvbbit.credential_key_available() THEN
        RAISE EXCEPTION 'canonical credential encryption is unavailable; configure an RVBBIT credential key or key file';
    END IF;
    FOR legacy IN
        SELECT s.name, s.value, s.description FROM rvbbit.secrets s ORDER BY s.name
    LOOP
        ref := rvbbit.credential_ref('backend', 'default', legacy.name);
        canonical_preserved := EXISTS (
            SELECT 1 FROM rvbbit.credentials c WHERE c.credential_ref = ref
        );
        IF NOT canonical_preserved THEN
            ref := rvbbit.put_credential(
                'backend', 'default', legacy.name, legacy.value,
                legacy.description,
                jsonb_build_object('migrated_from', 'rvbbit.secrets')
            );
        END IF;
        INSERT INTO rvbbit.credential_events (
            credential_ref, event_type, actor,
            details
        ) VALUES (
            ref, 'migrated', session_user,
            jsonb_build_object(
                'source', 'rvbbit.secrets',
                'canonical_preserved', canonical_preserved
            )
        );
        DELETE FROM rvbbit.secrets WHERE name = legacy.name;
        migrated := migrated + 1;
    END LOOP;
    RETURN migrated;
END
$migrate_legacy_secrets$;

-- Re-encrypt every active envelope with the primary configured key. Operators
-- configure NEW,OLD in RVBBIT_CREDENTIAL_KEYS[_FILE], run this once, verify the
-- receipt count, and may then remove OLD. Credential values and value versions
-- do not change; key_version and metadata-only audit receipts do.
CREATE OR REPLACE FUNCTION rvbbit.rewrap_credentials()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $rewrap_credentials$
DECLARE
    target record;
    plaintext text;
    rewrapped integer := 0;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF NOT rvbbit.credential_key_available() THEN
        RAISE EXCEPTION 'canonical credential encryption is unavailable; configure an RVBBIT credential key or key file';
    END IF;
    FOR target IN
        SELECT c.credential_ref, c.ciphertext, c.key_version
        FROM rvbbit.credentials c
        WHERE c.status = 'active'
        ORDER BY c.credential_ref
        FOR UPDATE
    LOOP
        plaintext := rvbbit.credential_unseal(
            target.credential_ref, target.ciphertext
        );
        UPDATE rvbbit.credentials c
        SET ciphertext = rvbbit.credential_seal(
                target.credential_ref, plaintext
            ),
            key_version = c.key_version + 1,
            updated_at = clock_timestamp(),
            updated_by = session_user,
            rotated_at = clock_timestamp()
        WHERE c.credential_ref = target.credential_ref;
        plaintext := NULL;
        INSERT INTO rvbbit.credential_events (
            credential_ref, event_type, actor, details
        ) VALUES (
            target.credential_ref,
            'rewrapped',
            session_user,
            jsonb_build_object('key_version', target.key_version + 1)
        );
        rewrapped := rewrapped + 1;
    END LOOP;
    RETURN rewrapped;
END
$rewrap_credentials$;

-- MCP compatibility: the gateway keeps its existing server/name contract,
-- while Postgres becomes the sole persistent copy.
CREATE OR REPLACE FUNCTION rvbbit.set_mcp_credential(
    server_name text,
    secret_name text,
    secret_value text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $set_mcp_credential$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    RETURN rvbbit.put_credential(
        'mcp', btrim(server_name), btrim(secret_name), secret_value,
        'MCP server credential',
        jsonb_build_object('server', btrim(server_name))
    );
END
$set_mcp_credential$;

CREATE OR REPLACE FUNCTION rvbbit.resolve_mcp_credential(
    server_name text,
    secret_name text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $resolve_mcp_credential$
DECLARE
    ref text;
BEGIN
    ref := rvbbit.credential_ref('mcp', btrim(server_name), btrim(secret_name));
    RETURN rvbbit.resolve_credential(ref, 'mcp-gateway', 'server_runtime');
END
$resolve_mcp_credential$;

CREATE OR REPLACE FUNCTION rvbbit.delete_mcp_credential(
    server_name text,
    secret_name text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $delete_mcp_credential$
DECLARE
    ref text := rvbbit.credential_ref('mcp', btrim(server_name), btrim(secret_name));
    next_version integer;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    UPDATE rvbbit.credentials
    SET ciphertext = NULL,
        status = 'revoked',
        version = version + 1,
        updated_at = clock_timestamp(),
        updated_by = session_user
    WHERE credential_ref = ref
    RETURNING version INTO next_version;
    IF next_version IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO rvbbit.credential_events (
        credential_ref, event_type, actor, details
    ) VALUES (
        ref, 'revoked', session_user,
        jsonb_build_object(
            'kind', 'mcp',
            'namespace', btrim(server_name),
            'name', btrim(secret_name),
            'version', next_version
        )
    );
    RETURN true;
END
$delete_mcp_credential$;

CREATE OR REPLACE FUNCTION rvbbit.list_mcp_credentials()
RETURNS TABLE (server_name text, secret_name text)
LANGUAGE plpgsql SECURITY DEFINER STABLE
SET search_path = pg_catalog, rvbbit
AS $list_mcp_credentials$
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    RETURN QUERY
    SELECT c.namespace, c.name
    FROM rvbbit.credentials c
    WHERE c.kind = 'mcp' AND c.status = 'active'
    ORDER BY c.namespace, c.name;
END
$list_mcp_credentials$;

REVOKE ALL ON FUNCTION rvbbit.set_mcp_credential(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.resolve_mcp_credential(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.delete_mcp_credential(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.list_mcp_credentials() FROM PUBLIC;

COMMENT ON TABLE rvbbit.credentials IS
    'Canonical encrypted credentials. Ciphertext is reference-bound; key material remains outside Postgres storage.';
COMMENT ON TABLE rvbbit.credential_events IS
    'Metadata-only credential lifecycle and resolution audit. Never contains secret values.';
COMMENT ON FUNCTION rvbbit.resolve_credential(text, text, text) IS
    'Internal purpose-tagged resolver. Revoked from PUBLIC; returns plaintext only to an authorized service path.';

-- Mutation and enumeration functions are an explicit service/admin surface.
-- Superusers retain access; future appliance service roles receive narrow
-- grants during role provisioning. Only presence and backend-scoped resolution
-- remain callable by ordinary query roles.
REVOKE ALL ON FUNCTION rvbbit.put_credential(text, text, text, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.delete_credential(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.list_credentials(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.set_secret(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.delete_secret(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.list_secrets() FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.migrate_legacy_secrets() FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.rewrap_credentials() FROM PUBLIC;
