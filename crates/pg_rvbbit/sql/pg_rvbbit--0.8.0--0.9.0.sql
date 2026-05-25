-- pg_rvbbit 0.8.0 -> 0.9.0
-- Loop 5 / RYR-303 Tier B: Lars-inspired composite semantic operators
-- that run on top of the cached embedding substrate.

-- outliers: find the N most-unusual texts (isolation OR criterion-relevance)
CREATE FUNCTION rvbbit.outliers(
    query_sql TEXT,
    n INT,
    criterion TEXT DEFAULT '',
    specialist TEXT DEFAULT ''
)
RETURNS TABLE(text TEXT, score DOUBLE PRECISION)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'outliers_wrapper';

-- dedupe_groups: cluster near-duplicates via similarity threshold + union-find
CREATE FUNCTION rvbbit.dedupe_groups(
    query_sql TEXT,
    threshold DOUBLE PRECISION DEFAULT 0.7,
    specialist TEXT DEFAULT ''
)
RETURNS TABLE(group_id INT, representative TEXT, size BIGINT, members TEXT[])
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'dedupe_groups_wrapper';

-- semantic_case: pick result by argmax over condition embedding similarities
CREATE FUNCTION rvbbit.semantic_case(
    text TEXT,
    conditions TEXT[],
    results TEXT[],
    default_val TEXT DEFAULT '',
    min_score DOUBLE PRECISION DEFAULT 0.0,
    specialist TEXT DEFAULT ''
)
RETURNS TEXT
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'semantic_case_wrapper';
