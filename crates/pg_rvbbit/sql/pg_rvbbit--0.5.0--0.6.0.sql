-- pg_rvbbit 0.5.0 -> 0.6.0
-- Loop 2: rvbbit.knn_text() — top-k semantic retrieval primitive that
-- closes the gap between per-row similarity and full table sort.

CREATE FUNCTION rvbbit.knn_text(
    rel oid,
    col TEXT,
    query TEXT,
    k INT,
    specialist TEXT DEFAULT ''
)
RETURNS TABLE(value TEXT, score DOUBLE PRECISION)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'knn_text_wrapper';
