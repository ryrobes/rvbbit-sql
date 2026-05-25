-- pg_rvbbit 0.14.0 -> 0.15.0
-- Loop 21: projected text-transform AVG(length(text)) rollups.
-- Used by the regexp_replace URL-host shape, but exposed as a transform
-- driven SRF so additional deterministic text transforms can reuse it.

ALTER TABLE rvbbit.specialists DROP CONSTRAINT IF EXISTS specialists_transport_check;
ALTER TABLE rvbbit.specialists
    ADD CONSTRAINT specialists_transport_check
    CHECK (transport IN ('rvbbit', 'gradio', 'openai', 'stub'));

CREATE FUNCTION rvbbit.top_text_transform_avg_len(
    rel oid,
    text_col TEXT,
    transform TEXT,
    min_count bigint,
    k INT
)
RETURNS TABLE(
    key TEXT,
    sum_len bigint,
    count bigint,
    min_text TEXT
)
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'top_text_transform_avg_len_wrapper';
