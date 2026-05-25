-- pg_rvbbit 0.47.0 -> 0.47.1
-- Register explicit derived-layout refresh hook for non-blocking compact/load
-- workflows.

CREATE OR REPLACE FUNCTION rvbbit.refresh_layout_variants(
    "rel" oid
) RETURNS bigint
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'refresh_layout_variants_wrapper';
