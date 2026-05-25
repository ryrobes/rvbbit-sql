-- pg_rvbbit 0.13.0 -> 0.14.0
-- Loop 13 / RYR-300: bitmap_select_* SETOF helpers. Use the cached
-- semantic bitmaps (RYR-288) as an explicit row-id filter via JOIN.
-- Full planner-side auto-routing stays a follow-up.

CREATE FUNCTION rvbbit.bitmap_select_int(
    rel oid,
    pk_col TEXT,
    predicate_name TEXT,
    model_version TEXT
)
RETURNS TABLE(pk BIGINT)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'bitmap_select_int_wrapper';

CREATE FUNCTION rvbbit.bitmap_select_text(
    rel oid,
    pk_col TEXT,
    predicate_name TEXT,
    model_version TEXT
)
RETURNS TABLE(pk TEXT)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'bitmap_select_text_wrapper';
