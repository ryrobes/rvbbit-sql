-- pg_rvbbit 0.12.0 -> 0.13.0
-- Loop 12 / RYR-292: incremental semantic materialized views.
-- rvbbit.semantic_mvs catalog + three UDFs: create, refresh, drop.

CREATE TABLE rvbbit.semantic_mvs (
    mv_name         text PRIMARY KEY,
    source_oid      oid NOT NULL,
    pk_col          text NOT NULL,
    projection_sql  text NOT NULL,
    projection_col  text NOT NULL,
    projection_type text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_refreshed  timestamptz,
    n_rows_total    bigint NOT NULL DEFAULT 0
);

CREATE FUNCTION rvbbit.semantic_mv_create(
    mv_name TEXT,
    source_rel oid,
    pk_col TEXT,
    projection_sql TEXT,
    projection_col TEXT DEFAULT 'value',
    projection_type TEXT DEFAULT 'text'
) RETURNS BIGINT
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'semantic_mv_create_wrapper';

CREATE FUNCTION rvbbit.semantic_mv_refresh(mv_name TEXT) RETURNS BIGINT
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'semantic_mv_refresh_wrapper';

CREATE FUNCTION rvbbit.semantic_mv_drop(mv_name TEXT) RETURNS BIGINT
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'semantic_mv_drop_wrapper';
