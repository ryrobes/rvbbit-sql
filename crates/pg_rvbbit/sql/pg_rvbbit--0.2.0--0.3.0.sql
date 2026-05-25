-- pg_rvbbit 0.2.0 -> 0.3.0
-- Semantic predicate bitmap cache (RYR-288). Adds rvbbit.semantic_bitmaps
-- catalog table + population/observability UDFs.

CREATE TABLE rvbbit.semantic_bitmaps (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    rg_id           bigint NOT NULL,
    predicate_hash  bytea NOT NULL,
    predicate_name  text NOT NULL,
    model_version   text NOT NULL,
    bitmap          bytea NOT NULL,
    n_set           bigint NOT NULL,
    n_total         bigint NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, rg_id, predicate_hash)
);

CREATE INDEX semantic_bitmaps_named_idx
    ON rvbbit.semantic_bitmaps (table_oid, predicate_name, model_version);

CREATE FUNCTION rvbbit.bitmap_populate(
    rel oid,
    predicate_name TEXT,
    model_version TEXT,
    predicate_sql TEXT
) RETURNS BIGINT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'bitmap_populate_wrapper';

CREATE FUNCTION rvbbit.bitmap_drop(
    rel oid,
    predicate_name TEXT,
    model_version TEXT
) RETURNS BIGINT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'bitmap_drop_wrapper';

CREATE FUNCTION rvbbit.bitmap_stats(rel oid)
RETURNS TABLE(
    predicate_name TEXT,
    model_version TEXT,
    n_groups BIGINT,
    rows_set BIGINT,
    rows_total BIGINT,
    selectivity DOUBLE PRECISION,
    bytes_stored BIGINT
)
LANGUAGE c
AS '$libdir/pg_rvbbit', 'bitmap_stats_wrapper';

CREATE FUNCTION rvbbit.bitmap_test_decode(
    rel oid,
    rg_id BIGINT,
    predicate_name TEXT,
    model_version TEXT
) RETURNS INT[]
LANGUAGE c
AS '$libdir/pg_rvbbit', 'bitmap_test_decode_wrapper';
