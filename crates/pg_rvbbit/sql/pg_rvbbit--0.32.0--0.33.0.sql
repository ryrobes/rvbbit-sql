-- pg_rvbbit 0.32.0 -> 0.33.0
-- MCP (Model Context Protocol) integration — Phase 1.
--
-- A `mcp-gateway` sidecar (Python) hosts MCP subprocesses; PG backends
-- talk HTTP to it. Catalog: rvbbit.mcp_servers (registry),
-- rvbbit.mcp_tools (auto-populated on refresh), rvbbit.mcp_invocations
-- (per-call audit). UDFs: register_mcp_server, drop_mcp_server,
-- refresh_mcp_server, mcp_call, mcp_text.
--
-- This is additive — no existing object is touched.

CREATE TABLE rvbbit.mcp_servers (
    name             text PRIMARY KEY,
    transport        text NOT NULL DEFAULT 'stdio',
    command          text,
    args             text[],
    env              jsonb,
    url              text,
    auth_header_env  text,
    timeout_ms       int  NOT NULL DEFAULT 30000,
    description      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mcp_servers_transport_check
        CHECK (transport IN ('stdio', 'http')),
    CONSTRAINT mcp_servers_stdio_needs_command
        CHECK (transport <> 'stdio' OR command IS NOT NULL),
    CONSTRAINT mcp_servers_http_needs_url
        CHECK (transport <> 'http'  OR url IS NOT NULL)
);

CREATE TABLE rvbbit.mcp_tools (
    server         text NOT NULL REFERENCES rvbbit.mcp_servers(name) ON DELETE CASCADE,
    name           text NOT NULL,
    description    text,
    input_schema   jsonb,
    discovered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server, name)
);

CREATE TABLE rvbbit.mcp_invocations (
    id             bigserial PRIMARY KEY,
    server         text NOT NULL,
    tool           text NOT NULL,
    args           jsonb,
    output         jsonb,
    error          text,
    latency_ms     int,
    query_id       uuid,
    invocation_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX mcp_invocations_server_time_idx
    ON rvbbit.mcp_invocations (server, invocation_at DESC);
CREATE INDEX mcp_invocations_query_idx
    ON rvbbit.mcp_invocations (query_id);

CREATE OR REPLACE FUNCTION rvbbit.register_mcp_server(
    server_name        text,
    server_transport   text   DEFAULT 'stdio',
    server_command     text   DEFAULT NULL,
    server_args        text[] DEFAULT NULL,
    server_env         jsonb  DEFAULT NULL,
    server_url         text   DEFAULT NULL,
    server_auth_env    text   DEFAULT NULL,
    server_timeout_ms  int    DEFAULT 30000,
    server_description text   DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $rm$
BEGIN
    INSERT INTO rvbbit.mcp_servers
        (name, transport, command, args, env, url, auth_header_env,
         timeout_ms, description)
    VALUES
        (server_name, server_transport, server_command, server_args,
         server_env, server_url, server_auth_env, server_timeout_ms,
         server_description)
    ON CONFLICT (name) DO UPDATE SET
        transport       = EXCLUDED.transport,
        command         = EXCLUDED.command,
        args            = EXCLUDED.args,
        env             = EXCLUDED.env,
        url             = EXCLUDED.url,
        auth_header_env = EXCLUDED.auth_header_env,
        timeout_ms      = EXCLUDED.timeout_ms,
        description     = EXCLUDED.description;
END
$rm$;

CREATE OR REPLACE FUNCTION rvbbit.drop_mcp_server(server_name text)
RETURNS void
LANGUAGE plpgsql
AS $rm$
BEGIN
    DELETE FROM rvbbit.mcp_servers WHERE name = server_name;
END
$rm$;

-- C UDFs (Rust pgrx). The bootstrap (extension_sql! in catalog.rs)
-- generates equivalent CREATE FUNCTION DDL for fresh installs.
CREATE FUNCTION rvbbit.mcp_call(server text, tool text, args jsonb) RETURNS jsonb
    AS 'MODULE_PATHNAME', 'mcp_call_wrapper'
    LANGUAGE c VOLATILE;

CREATE FUNCTION rvbbit.refresh_mcp_server(server text) RETURNS int4
    AS 'MODULE_PATHNAME', 'refresh_mcp_server_wrapper'
    LANGUAGE c VOLATILE;

CREATE FUNCTION rvbbit.mcp_text(response jsonb) RETURNS text
    AS 'MODULE_PATHNAME', 'mcp_text_wrapper'
    LANGUAGE c IMMUTABLE STRICT;
