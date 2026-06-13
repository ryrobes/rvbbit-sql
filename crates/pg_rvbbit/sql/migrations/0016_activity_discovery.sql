-- 0016_activity_discovery — the proactive flip: mine what employees actually query, propose cubes.
--
-- The strongest signal there is: real people running the same multi-table join over and over. This
-- mines rvbbit.mcp_activity (every MCP run_sql/validate_sql logs its SQL text), extracts each query's
-- base table-set via _cube_source_tables (EXPLAIN-based — catches RAW joins, not just accelerated
-- objects[]), and groups by table-set. A set queried >= K times with no covering cube becomes a
-- cube proposal authored by propose_cube and dropped in the queue as proposed_by='warehouse'. The
-- human still blesses it in the inbox. Pure SQL + pg_cron (no Rust). P3 of the learning loop.
--
-- discover_tick is NOT auto-scheduled (it makes LLM calls) — call it manually, or schedule it:
--   SELECT cron.schedule('rvbbit-discover', '0 6 * * *', $$SELECT rvbbit.discover_tick()$$);

-- read-only: the candidate recurring table-sets + whether a cube already covers / a proposal exists.
CREATE OR REPLACE FUNCTION rvbbit.discovery_candidates(
    p_days int DEFAULT 14, p_min_queries int DEFAULT 3, p_limit int DEFAULT 20
) RETURNS TABLE (tables text[], query_count bigint, users bigint, covered boolean, already_proposed boolean)
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE v_covered text[];
BEGIN
    -- tables already covered by an existing cube (skip candidate sets fully inside these)
    SELECT coalesce(array_agg(DISTINCT t), '{}'::text[]) INTO v_covered FROM (
        SELECT unnest(rvbbit._cube_source_tables(sql)) AS t FROM rvbbit.cube_catalog
    ) z;

    RETURN QUERY
    WITH q AS (
        -- distinct logged SQL (literal-fragmented, but recombined by table-set below), hottest first
        SELECT args->>'sql' AS sql, count(*) AS n, count(DISTINCT coalesce(caller, '?')) AS u
        FROM rvbbit.mcp_activity
        WHERE tool IN ('run_sql', 'validate_sql') AND ok
          AND ts > now() - make_interval(days => greatest(p_days, 1))
          AND nullif(btrim(args->>'sql'), '') IS NOT NULL
        GROUP BY args->>'sql'
        ORDER BY count(*) DESC
        LIMIT 200                                   -- cap EXPLAIN cost
    ),
    sets AS (
        SELECT (SELECT array_agg(x ORDER BY x) FROM unnest(rvbbit._cube_source_tables(q.sql)) x) AS tbls,
               q.n, q.u
        FROM q
    ),
    agg AS (
        SELECT tbls, sum(n)::bigint AS qc, max(u)::bigint AS uu
        FROM sets
        WHERE tbls IS NOT NULL AND cardinality(tbls) >= 2     -- multi-table = a join worth cubing
        GROUP BY tbls
        HAVING sum(n) >= greatest(p_min_queries, 1)
    )
    SELECT a.tbls, a.qc, a.uu,
           a.tbls <@ v_covered AS covered,
           EXISTS (
               SELECT 1 FROM rvbbit.proposals p
                WHERE p.kind = 'cube'
                  AND p.status IN ('pending', 'rejected', 'withdrawn')   -- don't re-nag dismissed sets
                  AND p.source_tables @> to_jsonb(a.tbls)
                  AND p.source_tables <@ to_jsonb(a.tbls)) AS already_proposed
    FROM agg a
    ORDER BY a.qc DESC
    LIMIT greatest(p_limit, 1);
END $fn$;

-- the sweeper: propose a cube for each uncovered, not-yet-proposed recurring table-set.
CREATE OR REPLACE FUNCTION rvbbit.discover_tick(
    p_days int DEFAULT 14, p_min_queries int DEFAULT 3, p_max_candidates int DEFAULT 5
) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE rec record; v_subject text; v_draft jsonb; v_made int := 0; v_results jsonb := '[]'::jsonb;
BEGIN
    FOR rec IN
        SELECT tables, query_count, users
        FROM rvbbit.discovery_candidates(p_days, p_min_queries, greatest(p_max_candidates, 1) * 4)
        WHERE NOT covered AND NOT already_proposed
        ORDER BY query_count DESC
    LOOP
        EXIT WHEN v_made >= greatest(p_max_candidates, 1);
        v_subject := 'frequently joined together in queries: ' || array_to_string(rec.tables, ', ')
                   || ' — seen in ' || rec.query_count
                   || CASE WHEN rec.query_count = 1 THEN ' query' ELSE ' queries' END
                   || ' by ' || rec.users || ' user(s); a cube would encode this join once.';
        BEGIN
            v_draft := rvbbit.propose_cube(v_subject, rec.tables);
            v_draft := v_draft || jsonb_build_object('subject', v_subject);
            PERFORM rvbbit.record_proposal('cube', v_draft, 'warehouse', 'activity_sweeper');
            v_made := v_made + 1;
            v_results := v_results || jsonb_build_object(
                'name', v_draft->>'name', 'tables', to_jsonb(rec.tables), 'queries', rec.query_count);
        EXCEPTION WHEN OTHERS THEN
            CONTINUE;    -- a bad logged SQL / failed draft must not stop the sweep
        END;
    END LOOP;
    RETURN jsonb_build_object('proposed', v_made, 'window_days', p_days, 'results', v_results);
END $fn$;
