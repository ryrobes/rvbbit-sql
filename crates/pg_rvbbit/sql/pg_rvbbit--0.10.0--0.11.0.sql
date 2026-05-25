-- pg_rvbbit 0.10.0 -> 0.11.0
-- Loop 7: rvbbit.diff — semantic set difference (novelty detection).
-- For each row in query_a, score = 1 - max cosine to any row in query_b.
-- Returns top-N by novelty; useful for "what's new since last week"
-- workflows.

CREATE FUNCTION rvbbit.diff(
    query_a TEXT,
    query_b TEXT,
    k INT,
    specialist TEXT DEFAULT ''
)
RETURNS TABLE(text TEXT, novelty DOUBLE PRECISION)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'diff_wrapper';
