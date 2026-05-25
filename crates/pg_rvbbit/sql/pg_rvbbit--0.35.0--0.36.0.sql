-- pg_rvbbit 0.35.0 -> 0.36.0
-- MCP Phase 4 — resources, selective result caching, active health probe.

-- Some pre-0.36 development databases may already have these objects because
-- the runtime catalog bootstrap created them before the extension version was
-- advanced. Adopt them first; otherwise Postgres rejects CREATE IF NOT EXISTS
-- inside an extension update when the existing object is not an extension
-- member.
DO $$
BEGIN
    IF to_regclass('rvbbit.mcp_resources') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD TABLE rvbbit.mcp_resources;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    IF to_regclass('rvbbit.mcp_cache') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD TABLE rvbbit.mcp_cache;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    IF to_regprocedure('rvbbit.mcp_resource(text,text)') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD FUNCTION rvbbit.mcp_resource(text,text);
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    IF to_regprocedure('rvbbit.mcp_resource_text(text,text)') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD FUNCTION rvbbit.mcp_resource_text(text,text);
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    IF to_regprocedure('rvbbit.mcp_probe(text)') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD FUNCTION rvbbit.mcp_probe(text);
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    IF to_regprocedure('rvbbit.set_mcp_tool_caching(text,text,integer)') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD FUNCTION rvbbit.set_mcp_tool_caching(text,text,integer);
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    IF to_regprocedure('rvbbit.purge_mcp_cache(text,text)') IS NOT NULL THEN
        BEGIN
            ALTER EXTENSION pg_rvbbit ADD FUNCTION rvbbit.purge_mcp_cache(text,text);
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;
END
$$;

-- --- resources ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS rvbbit.mcp_resources (
    server         text NOT NULL REFERENCES rvbbit.mcp_servers(name) ON DELETE CASCADE,
    uri            text NOT NULL,
    name           text,
    description    text,
    mime_type      text,
    discovered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server, uri)
);

CREATE OR REPLACE FUNCTION rvbbit.mcp_resource(server text, uri text) RETURNS jsonb
    AS 'MODULE_PATHNAME', 'mcp_resource_wrapper'
    LANGUAGE c VOLATILE;

CREATE OR REPLACE FUNCTION rvbbit.mcp_resource_text(server text, uri text) RETURNS text
    AS 'MODULE_PATHNAME', 'mcp_resource_text_wrapper'
    LANGUAGE c VOLATILE;

-- --- selective caching --------------------------------------------------
ALTER TABLE rvbbit.mcp_tools
    ADD COLUMN IF NOT EXISTS cacheable boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS ttl_seconds int;

ALTER TABLE rvbbit.mcp_invocations
    ADD COLUMN IF NOT EXISTS cache_hit boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS rvbbit.mcp_cache (
    server      text NOT NULL,
    tool        text NOT NULL,
    args_hash   text NOT NULL,
    args        jsonb,
    output      jsonb NOT NULL,
    cached_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (server, tool, args_hash)
);

CREATE OR REPLACE FUNCTION rvbbit.set_mcp_tool_caching(
    server_name text,
    tool_name   text,
    ttl_seconds int DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $sc$
BEGIN
    UPDATE rvbbit.mcp_tools
    SET cacheable = true, ttl_seconds = set_mcp_tool_caching.ttl_seconds
    WHERE server = server_name AND name = tool_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rvbbit.set_mcp_tool_caching: tool %.% not in rvbbit.mcp_tools (refresh first?)',
            server_name, tool_name;
    END IF;
END
$sc$;

CREATE OR REPLACE FUNCTION rvbbit.purge_mcp_cache(
    server_name text,
    tool_name   text DEFAULT NULL
) RETURNS int
LANGUAGE plpgsql
AS $pc$
DECLARE n int;
BEGIN
    IF tool_name IS NULL THEN
        DELETE FROM rvbbit.mcp_cache WHERE server = server_name;
    ELSE
        DELETE FROM rvbbit.mcp_cache WHERE server = server_name AND tool = tool_name;
    END IF;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$pc$;

-- --- active health probe ------------------------------------------------
CREATE OR REPLACE FUNCTION rvbbit.mcp_probe(server text) RETURNS jsonb
    AS 'MODULE_PATHNAME', 'mcp_probe_wrapper'
    LANGUAGE c VOLATILE;

-- --- mcp_health view: gain n_resources ----------------------------------
-- DROP + CREATE (not CREATE OR REPLACE) — the new shape adds a column in
-- the middle of the column list, which CREATE OR REPLACE VIEW won't allow.
DROP VIEW IF EXISTS rvbbit.mcp_health;
CREATE VIEW rvbbit.mcp_health AS
SELECT
    s.name,
    s.transport,
    coalesce(t.n_tools, 0)                  AS n_tools,
    coalesce(r.n_resources, 0)              AS n_resources,
    t.last_discovered_at,
    i.last_call_at,
    i.last_error_at,
    s.created_at
FROM rvbbit.mcp_servers s
LEFT JOIN (
    SELECT server, count(*)::int AS n_tools, max(discovered_at) AS last_discovered_at
    FROM rvbbit.mcp_tools GROUP BY server
) t ON t.server = s.name
LEFT JOIN (
    SELECT server, count(*)::int AS n_resources
    FROM rvbbit.mcp_resources GROUP BY server
) r ON r.server = s.name
LEFT JOIN (
    SELECT server,
           max(invocation_at) FILTER (WHERE error IS NULL)     AS last_call_at,
           max(invocation_at) FILTER (WHERE error IS NOT NULL) AS last_error_at
    FROM rvbbit.mcp_invocations GROUP BY server
) i ON i.server = s.name;
