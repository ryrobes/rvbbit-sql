-- pg_rvbbit 0.31.0 -> 0.32.0
-- Rename rvbbit.specialists -> rvbbit.backends.
--
-- The registry holds LLM providers as well as specialist sidecars, so the
-- old name was a misnomer. The DDL helper becomes register_backend (with
-- backend_* args); the reload UDF becomes reload_backends. The Rust
-- module / type names and the `specialist` node kind stay — they describe
-- node behavior, not the registry.

-- Table rename. Data, indexes, and the openrouter seed row carry over.
ALTER TABLE rvbbit.specialists RENAME TO backends;
ALTER TABLE rvbbit.backends
    RENAME CONSTRAINT specialists_transport_check TO backends_transport_check;

-- The plpgsql register_* helper changes signature (arg names) AND body
-- (target table); drop the old and create the new.
DROP FUNCTION IF EXISTS rvbbit.register_specialist(text, text, text, int, int, int, text, jsonb, text);

CREATE OR REPLACE FUNCTION rvbbit.register_backend(
    backend_name        text,
    backend_endpoint    text,
    backend_transport   text DEFAULT 'rvbbit',
    backend_batch_size  int  DEFAULT 32,
    backend_max_concur  int  DEFAULT 4,
    backend_timeout_ms  int  DEFAULT 30000,
    backend_auth_env    text DEFAULT NULL,
    backend_opts        jsonb DEFAULT '{}'::jsonb,
    backend_description text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $rb$
BEGIN
    INSERT INTO rvbbit.backends
        (name, transport, endpoint_url, batch_size, max_concurrent,
         timeout_ms, auth_header_env, transport_opts, description)
    VALUES
        (backend_name, backend_transport, backend_endpoint, backend_batch_size,
         backend_max_concur, backend_timeout_ms, backend_auth_env, backend_opts,
         backend_description)
    ON CONFLICT (name) DO UPDATE SET
        transport       = EXCLUDED.transport,
        endpoint_url    = EXCLUDED.endpoint_url,
        batch_size      = EXCLUDED.batch_size,
        max_concurrent  = EXCLUDED.max_concurrent,
        timeout_ms      = EXCLUDED.timeout_ms,
        auth_header_env = EXCLUDED.auth_header_env,
        transport_opts  = EXCLUDED.transport_opts,
        description     = EXCLUDED.description;
END
$rb$;

-- reload_specialists -> reload_backends. The C symbol changes too, so
-- drop the old function (its symbol no longer exists in the new .so) and
-- create the new one pointing at the new wrapper.
DROP FUNCTION IF EXISTS rvbbit.reload_specialists();
CREATE FUNCTION rvbbit.reload_backends() RETURNS int4
    AS 'MODULE_PATHNAME', 'reload_backends_wrapper'
    LANGUAGE c VOLATILE;
