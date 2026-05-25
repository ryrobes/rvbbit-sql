-- pg_rvbbit 0.11.0 -> 0.12.0
-- Loop 11 / RYR-291: per-group HyperLogLog++ sketches for text columns.
-- Added at compact time; cross-group union via rvbbit.approx_distinct.

CREATE FUNCTION rvbbit.approx_distinct(rel oid, col TEXT)
RETURNS BIGINT
STABLE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'approx_distinct_wrapper';
