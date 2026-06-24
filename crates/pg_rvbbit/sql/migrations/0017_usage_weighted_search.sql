-- 0017_usage_weighted_search — boost data_search by what employees actually query.
--
-- data_search ranks by semantic+lexical relevance (RRF). This wraps it and folds in a USAGE weight
-- from rvbbit.mcp_popular_objects (touches per object across logged activity) so the things people
-- actually query climb — popular-and-relevant beats relevant-but-never-used. It's a thin SQL
-- wrapper, NOT a change to the (Rust) ranker, so it's hot-applyable and can't regress core search.
-- boosted_score = score * (1 + usage_weight * usage_norm), usage_norm = touches / max(touches).
-- P2 of the learning loop. Additive + idempotent.

CREATE OR REPLACE FUNCTION rvbbit.search_data_weighted(
    p_query text,
    p_k int DEFAULT 20,
    p_kinds text[] DEFAULT NULL,
    p_graph text DEFAULT 'db_catalog',
    p_usage_weight float8 DEFAULT 0.5
) RETURNS TABLE (
    node_id bigint, kind text, schema_name text, rel_name text, col_name text,
    score double precision, doc text, usage_touches bigint, boosted_score double precision
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF to_regclass('rvbbit.mcp_popular_objects') IS NULL THEN
        RETURN QUERY
        SELECT h.node_id, h.kind, h.schema_name, h.rel_name, h.col_name, h.score, h.doc,
               0::bigint AS usage_touches,
               h.score AS boosted_score
        FROM rvbbit.data_search(p_query, greatest(p_k, 1), p_kinds, p_graph) h
        ORDER BY h.score DESC
        LIMIT greatest(p_k, 1);
        RETURN;
    END IF;

    RETURN QUERY
    WITH hits AS (
        -- over-fetch, then re-rank + trim, so a popular item can climb into the top-k
        SELECT * FROM rvbbit.data_search(p_query, greatest(p_k, 1) * 2, p_kinds, p_graph)
    ),
    pop AS (
        SELECT object, touches,
               touches::float8 / nullif(max(touches) OVER (), 0) AS usage_norm
        FROM rvbbit.mcp_popular_objects
    )
    SELECT h.node_id, h.kind, h.schema_name, h.rel_name, h.col_name, h.score, h.doc,
           coalesce(u.touches, 0)::bigint AS usage_touches,
           h.score * (1 + coalesce(p_usage_weight, 0.5) * coalesce(u.usage_norm, 0)) AS boosted_score
    FROM hits h
    LEFT JOIN LATERAL (
        -- best-matching usage row: prefer the schema-qualified object, fall back to the bare name
        SELECT touches, usage_norm FROM pop
         WHERE object = coalesce(h.schema_name || '.', '') || h.rel_name
            OR object = h.rel_name
         ORDER BY (object = coalesce(h.schema_name || '.', '') || h.rel_name) DESC, touches DESC
         LIMIT 1
    ) u ON true
    ORDER BY boosted_score DESC
    LIMIT greatest(p_k, 1);
END;
$$;
