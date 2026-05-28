-- pg_rvbbit 0.59.0 -> 0.60.0
-- Managed Python runtime catalog for operator `kind: python` nodes.

CREATE TABLE IF NOT EXISTS rvbbit.python_envs (
    name            text PRIMARY KEY,
    python_version  text NOT NULL DEFAULT '3.12',
    requirements    text[] NOT NULL DEFAULT ARRAY[]::text[],
    env_hash        text NOT NULL,
    endpoint_url    text,
    timeout_ms      int NOT NULL DEFAULT 1000,
    status          text NOT NULL DEFAULT 'registered',
    status_message  text,
    created_by      oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT python_envs_status_check CHECK (
        status IN ('registered', 'building', 'ready', 'failed', 'disabled')
    ),
    CONSTRAINT python_envs_timeout_check CHECK (timeout_ms BETWEEN 1 AND 600000)
);

CREATE TABLE IF NOT EXISTS rvbbit.python_handlers (
    name          text PRIMARY KEY,
    env_name      text NOT NULL REFERENCES rvbbit.python_envs(name) ON DELETE RESTRICT,
    code          text NOT NULL,
    code_hash     text NOT NULL,
    entrypoint    text NOT NULL DEFAULT 'run',
    description   text,
    created_by    oid NOT NULL DEFAULT (current_user::regrole::oid),
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT python_handlers_entrypoint_check CHECK (entrypoint ~ '^[A-Za-z_][A-Za-z0-9_]*$')
);

CREATE OR REPLACE FUNCTION rvbbit.touch_python_envs_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_envs_touch_updated_at ON rvbbit.python_envs;
CREATE TRIGGER python_envs_touch_updated_at
    BEFORE UPDATE ON rvbbit.python_envs
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_python_envs_updated_at();

CREATE OR REPLACE FUNCTION rvbbit.touch_python_handlers_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_handlers_touch_updated_at ON rvbbit.python_handlers;
CREATE TRIGGER python_handlers_touch_updated_at
    BEFORE UPDATE ON rvbbit.python_handlers
    FOR EACH ROW EXECUTE FUNCTION rvbbit.touch_python_handlers_updated_at();

INSERT INTO rvbbit.settings (key, value)
VALUES ('python_runtime_endpoint', to_jsonb('http://rvbbit-python-runtime:8080/run'::text))
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION rvbbit.python_runtime_endpoint()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(
        (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'python_runtime_endpoint'),
        'http://rvbbit-python-runtime:8080/run'
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.set_python_runtime_endpoint(endpoint_url text)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    normalized text := nullif(btrim(endpoint_url), '');
BEGIN
    IF normalized IS NULL THEN
        RAISE EXCEPTION 'rvbbit.set_python_runtime_endpoint: endpoint_url cannot be empty';
    END IF;
    INSERT INTO rvbbit.settings (key, value, updated_at)
    VALUES ('python_runtime_endpoint', to_jsonb(normalized), clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = clock_timestamp();
    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;
    RETURN normalized;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.python_env_hash(
    python_version text,
    requirements text[]
) RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT md5(
        coalesce(python_version, '') || E'\x1f' ||
        coalesce(
            (
                SELECT string_agg(req, E'\x1e' ORDER BY req)
                FROM unnest(coalesce(requirements, ARRAY[]::text[])) AS r(req)
                WHERE btrim(req) <> ''
            ),
            ''
        )
    )
$$;

CREATE OR REPLACE FUNCTION rvbbit.require_python_admin()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) THEN
        RAISE EXCEPTION 'rvbbit Python runtime DDL requires a superuser in this release';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.create_python_env(
    env_name text,
    python_version text DEFAULT '3.12',
    requirements text[] DEFAULT ARRAY[]::text[],
    endpoint_url text DEFAULT NULL,
    timeout_ms int DEFAULT 1000
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_name text := nullif(btrim(env_name), '');
    normalized_version text := coalesce(nullif(btrim(python_version), ''), '3.12');
    normalized_requirements text[];
    normalized_endpoint text := nullif(btrim(endpoint_url), '');
    computed_hash text;
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_name IS NULL THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: env_name cannot be empty';
    END IF;
    IF normalized_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: env_name must be an identifier-like name';
    END IF;
    normalized_requirements := ARRAY(
        SELECT btrim(req)
        FROM unnest(coalesce(requirements, ARRAY[]::text[])) AS r(req)
        WHERE btrim(req) <> ''
        ORDER BY btrim(req)
    );
    IF EXISTS (
        SELECT 1 FROM unnest(normalized_requirements) AS r(req)
        WHERE req ~ E'[\\r\\n]'
    ) THEN
        RAISE EXCEPTION 'rvbbit.create_python_env: requirements cannot contain newlines';
    END IF;
    computed_hash := rvbbit.python_env_hash(normalized_version, normalized_requirements);

    INSERT INTO rvbbit.python_envs
        (name, python_version, requirements, env_hash, endpoint_url, timeout_ms,
         status, status_message)
    VALUES
        (normalized_name, normalized_version, normalized_requirements, computed_hash,
         normalized_endpoint, greatest(coalesce(timeout_ms, 1000), 1), 'registered', NULL)
    ON CONFLICT (name) DO UPDATE SET
        python_version = EXCLUDED.python_version,
        requirements = EXCLUDED.requirements,
        env_hash = EXCLUDED.env_hash,
        endpoint_url = EXCLUDED.endpoint_url,
        timeout_ms = EXCLUDED.timeout_ms,
        status = 'registered',
        status_message = NULL;

    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;

    SELECT to_jsonb(e) INTO row_doc FROM rvbbit.python_envs e WHERE e.name = normalized_name;
    RETURN row_doc;
END
$$;

CREATE OR REPLACE FUNCTION rvbbit.create_python_handler(
    handler_name text,
    env_name text,
    code text,
    entrypoint text DEFAULT 'run',
    description text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    normalized_handler text := nullif(btrim(handler_name), '');
    normalized_env text := nullif(btrim(env_name), '');
    normalized_entrypoint text := coalesce(nullif(btrim(entrypoint), ''), 'run');
    normalized_code text := coalesce(code, '');
    row_doc jsonb;
BEGIN
    PERFORM rvbbit.require_python_admin();
    IF normalized_handler IS NULL THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: handler_name cannot be empty';
    END IF;
    IF normalized_handler !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: handler_name must be an identifier-like name';
    END IF;
    IF normalized_env IS NULL OR NOT EXISTS (
        SELECT 1 FROM rvbbit.python_envs WHERE name = normalized_env
    ) THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: unknown env "%"', env_name;
    END IF;
    IF normalized_code = '' THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: code cannot be empty';
    END IF;
    IF normalized_entrypoint !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'rvbbit.create_python_handler: entrypoint must be an identifier';
    END IF;

    INSERT INTO rvbbit.python_handlers
        (name, env_name, code, code_hash, entrypoint, description)
    VALUES
        (normalized_handler, normalized_env, normalized_code, md5(normalized_code),
         normalized_entrypoint, description)
    ON CONFLICT (name) DO UPDATE SET
        env_name = EXCLUDED.env_name,
        code = EXCLUDED.code,
        code_hash = EXCLUDED.code_hash,
        entrypoint = EXCLUDED.entrypoint,
        description = EXCLUDED.description;

    BEGIN
        PERFORM rvbbit.reload_python_runtime();
    EXCEPTION WHEN undefined_function THEN
        NULL;
    END;

    SELECT to_jsonb(h) INTO row_doc FROM rvbbit.python_handlers h WHERE h.name = normalized_handler;
    RETURN row_doc;
END
$$;
