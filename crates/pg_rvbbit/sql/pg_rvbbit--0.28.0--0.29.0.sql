-- pg_rvbbit 0.28.0 -> 0.29.0
-- Native backend DuckDB route entry point. The rewriter calls this only
-- after the adaptive router selects the guarded Duck vector path.

CREATE FUNCTION rvbbit.duck_query_json(
    "query" text,
    "column_names" jsonb,
    "max_rows" integer
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'duck_query_json_wrapper';
