-- 0040_route_optimization_candidates
--
-- Phase-1 assist for the routing overlay: surface the query shapes that are still served by
-- the base rules and would most benefit from benchmarking, ranked by frequency × latency.
-- The Lens lists these; the user supplies a representative SQL per shape to
-- rvbbit.route_optimize_query / route_optimize_queries (we don't store query text, only the
-- shape signature, so picking the SQL stays a human/explicit step in Phase 1).
--
-- route_executions already carries shape_key / shape_family / route_source / elapsed_ms, so no
-- join is needed.

CREATE OR REPLACE VIEW rvbbit.route_optimization_candidates AS
SELECT shape_key,
       shape_family,
       count(*)                                               AS executions,
       round(avg(elapsed_ms)::numeric, 2)                     AS avg_ms,
       round((count(*) * avg(elapsed_ms))::numeric, 2)        AS potential_ms,  -- where benching pays
       max(executed_at)                                       AS last_seen
FROM rvbbit.route_executions
WHERE executed_at > now() - interval '1 day'
  AND status = 'ok'
  AND route_source NOT IN ('forced', 'overlay')   -- still on base rules
  AND shape_key IS NOT NULL
  AND shape_key <> ''
  AND shape_key NOT IN (SELECT shape_key FROM rvbbit.route_overlay)
GROUP BY shape_key, shape_family
ORDER BY potential_ms DESC NULLS LAST;
