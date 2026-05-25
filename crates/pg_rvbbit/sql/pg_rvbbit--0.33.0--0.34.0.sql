-- pg_rvbbit 0.33.0 -> 0.34.0
-- MCP Phase 2.
--   - kind:"mcp" is now a node kind in operator pipelines (handled in
--     Rust; no DDL).
--   - rvbbit.mcp_rows(server, tool, args) -> SETOF jsonb: relational
--     surface for MCP tools whose text payload is array-shaped JSON.

CREATE FUNCTION rvbbit.mcp_rows(server text, tool text, args jsonb) RETURNS SETOF jsonb
    AS 'MODULE_PATHNAME', 'mcp_rows_wrapper'
    LANGUAGE c VOLATILE;
