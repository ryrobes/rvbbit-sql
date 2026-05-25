-- pg_rvbbit 0.6.0 -> 0.7.0
-- Loop 3: knn_text bulk-lookup perf fix (no SQL surface change, just
-- a one-SPI-call optimization), plus rvbbit.text_evidence() for
-- sentence-level snippet highlighting.

CREATE FUNCTION rvbbit.text_evidence(text TEXT, query TEXT, top_n INT DEFAULT 3)
RETURNS TEXT[]
IMMUTABLE STRICT PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'text_evidence_wrapper';
