-- pg_rvbbit 0.3.0 -> 0.4.0
-- EXPLAIN SEMANTIC scaffold (RYR-290): static analyzer that detects
-- rvbbit.<op>(...) calls, looks up operator metadata + token estimates,
-- and reports bitmap cache availability.

CREATE FUNCTION rvbbit.explain_semantic(query TEXT)
RETURNS TABLE(line TEXT)
LANGUAGE c
AS '$libdir/pg_rvbbit', 'explain_semantic_wrapper';
