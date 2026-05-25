-- pg_rvbbit 0.7.0 -> 0.8.0
-- Loop 4: rvbbit.topics — SQL-native k-means topic clustering over
-- cached embeddings. The single SQL call alternative to "export to
-- Python + sklearn + import results."

CREATE FUNCTION rvbbit.topics(
    query_sql TEXT,
    k INT,
    specialist TEXT DEFAULT '',
    max_iter INT DEFAULT 20,
    seed BIGINT DEFAULT 174
)
RETURNS TABLE(cluster_id INT, count BIGINT, exemplar TEXT)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'topics_wrapper';
