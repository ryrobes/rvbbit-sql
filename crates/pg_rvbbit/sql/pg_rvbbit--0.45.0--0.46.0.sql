-- pg_rvbbit 0.45.0 -> 0.46.0
-- Capability pack metadata for model backends plus active backend probes.

ALTER TABLE rvbbit.backends ADD COLUMN IF NOT EXISTS source_provider text;
ALTER TABLE rvbbit.backends ADD COLUMN IF NOT EXISTS source_model text;
ALTER TABLE rvbbit.backends ADD COLUMN IF NOT EXISTS source_revision text;
ALTER TABLE rvbbit.backends ADD COLUMN IF NOT EXISTS install_manifest jsonb;

DROP FUNCTION IF EXISTS rvbbit.backend_probe(text);
CREATE FUNCTION rvbbit.backend_probe(backend_name text) RETURNS jsonb
    AS 'MODULE_PATHNAME', 'backend_probe_wrapper'
    LANGUAGE c VOLATILE STRICT;

DROP FUNCTION IF EXISTS rvbbit.backend_probe_with_input(text,jsonb);
CREATE FUNCTION rvbbit.backend_probe_with_input(backend_name text, sample jsonb) RETURNS jsonb
    AS 'MODULE_PATHNAME', 'backend_probe_with_input_wrapper'
    LANGUAGE c VOLATILE STRICT;

DROP FUNCTION IF EXISTS rvbbit.register_backend(text,text,text,int,int,int,text,jsonb,text);
CREATE OR REPLACE FUNCTION rvbbit.register_backend(
    backend_name        text,
    backend_endpoint    text,
    backend_transport   text DEFAULT 'rvbbit',
    backend_batch_size  int  DEFAULT 32,
    backend_max_concur  int  DEFAULT 4,
    backend_timeout_ms  int  DEFAULT 30000,
    backend_auth_env    text DEFAULT NULL,
    backend_opts        jsonb DEFAULT '{}'::jsonb,
    backend_description text DEFAULT NULL,
    backend_source_provider text DEFAULT NULL,
    backend_source_model text DEFAULT NULL,
    backend_source_revision text DEFAULT NULL,
    backend_install_manifest jsonb DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $rb$
BEGIN
    INSERT INTO rvbbit.backends
        (name, transport, endpoint_url, batch_size, max_concurrent,
         timeout_ms, auth_header_env, transport_opts, description,
         source_provider, source_model, source_revision, install_manifest)
    VALUES
        (backend_name, backend_transport, backend_endpoint, backend_batch_size,
         backend_max_concur, backend_timeout_ms, backend_auth_env, backend_opts,
         backend_description, backend_source_provider, backend_source_model,
         backend_source_revision, backend_install_manifest)
    ON CONFLICT (name) DO UPDATE SET
        transport       = EXCLUDED.transport,
        endpoint_url    = EXCLUDED.endpoint_url,
        batch_size      = EXCLUDED.batch_size,
        max_concurrent  = EXCLUDED.max_concurrent,
        timeout_ms      = EXCLUDED.timeout_ms,
        auth_header_env = EXCLUDED.auth_header_env,
        transport_opts  = EXCLUDED.transport_opts,
        description     = EXCLUDED.description,
        source_provider = EXCLUDED.source_provider,
        source_model    = EXCLUDED.source_model,
        source_revision = EXCLUDED.source_revision,
        install_manifest = EXCLUDED.install_manifest;
END
$rb$;

CREATE OR REPLACE VIEW rvbbit.backend_health AS
SELECT
    b.name,
    b.transport,
    b.endpoint_url,
    b.batch_size,
    b.max_concurrent,
    b.timeout_ms,
    b.auth_header_env,
    b.transport_opts,
    b.description,
    b.source_provider,
    b.source_model,
    b.source_revision,
    b.install_manifest,
    coalesce(u.n_calls, 0) AS n_calls,
    coalesce(u.n_errors, 0) AS n_errors,
    u.avg_latency_ms,
    u.p50_latency_ms,
    u.p95_latency_ms,
    u.first_call_at,
    u.last_call_at,
    b.created_at
FROM rvbbit.backends b
LEFT JOIN rvbbit.specialist_usage u ON u.specialist = b.name;
