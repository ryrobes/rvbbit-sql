-- 0148_engine_rows.sql
-- Direct engine→tuple result path: rvbbit._engine_rows decodes the sidecar's
-- Arrow IPC result straight into typed Datums against the call-site column
-- definition list, replacing the arrow→jsonb→jsonb_to_recordset triple
-- materialization (~11µs/output row — found via the DoomQL frame benchmark:
-- 15.5ms of engine work arriving 103ms later on 8K-row results).
-- The rewriter emits this shape when rvbbit.rows_direct is on (default);
-- the *_query_json pipeline remains as kill switch and in-function fallback.

CREATE OR REPLACE FUNCTION rvbbit._engine_rows(
    engine   text,
    layout   text,
    query    text,
    max_rows integer
) RETURNS SETOF record
LANGUAGE c STRICT VOLATILE PARALLEL UNSAFE
-- NOTE: literal library path, not MODULE_PATHNAME — that macro only expands
-- inside extension scripts, and migrations apply via psql/rvbbit.migrate().
AS '$libdir/pg_rvbbit', 'rvbbit_engine_rows_c';
