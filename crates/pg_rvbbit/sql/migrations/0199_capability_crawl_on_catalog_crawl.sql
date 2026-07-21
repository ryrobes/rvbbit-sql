-- 0199: capability_crawl() rides the catalog crawl.
--
-- The scheduled catalog refresh (the tray installs `CALL rvbbit.catalog_crawl_run();`
-- under pg_cron) keeps the db_catalog graph fresh, but the capability graph
-- still only refreshed on MCP install (0198) or by hand. Hook the two
-- together at the one point every crawl path converges: the final
-- catalog_runs status flip to 'ok'. Serial function, durable procedure and
-- parallel procedure all end there, so existing installed cron jobs pick
-- this up from the extension update alone — no job re-install needed.
--
-- Gated on capability_search_stale() (a no-change crawl costs one probe
-- query, no re-embedding) and exception-guarded (capability trouble must
-- never mark the catalog run failed).

-- The 0198 probe only watched operators + capability_catalog, but the crawl
-- also indexes metrics, cubes and brain sources. Widen it — metric_defs and
-- cube_defs are append-only versioned (created_at IS the change signal).
CREATE OR REPLACE FUNCTION rvbbit.capability_search_stale() RETURNS boolean
LANGUAGE sql STABLE AS $fn$
    WITH g AS (
        SELECT count(*) AS n, max(updated_at) AS ts
        FROM rvbbit.catalog_docs WHERE graph_id = 'rvbbit_capabilities'
    )
    SELECT (SELECT n FROM g) = 0
        OR greatest(
               coalesce((SELECT max(updated_at) FROM rvbbit.operators), 'epoch'::timestamptz),
               coalesce((SELECT max(updated_at) FROM rvbbit.capability_catalog), 'epoch'::timestamptz),
               coalesce((SELECT max(created_at) FROM rvbbit.metric_defs), 'epoch'::timestamptz),
               coalesce((SELECT max(created_at) FROM rvbbit.cube_defs), 'epoch'::timestamptz),
               coalesce((SELECT max(created_at) FROM rvbbit.brain_sources), 'epoch'::timestamptz)
           ) > coalesce((SELECT ts FROM g), 'epoch'::timestamptz);
$fn$;

COMMENT ON FUNCTION rvbbit.capability_search_stale() IS
    'True when the rvbbit_capabilities graph is missing or older than its sources (operators, capability_catalog, metric_defs, cube_defs, brain_sources) — run rvbbit.capability_crawl() to refresh.';

CREATE OR REPLACE FUNCTION rvbbit._capability_crawl_on_catalog_ok()
RETURNS trigger LANGUAGE plpgsql AS $trg$
BEGIN
    -- capability_crawl() doesn't log to catalog_runs today; guard anyway so
    -- a future version that does can never recurse.
    IF NEW.graph_id = 'rvbbit_capabilities' THEN
        RETURN NULL;
    END IF;
    BEGIN
        IF rvbbit.capability_search_stale() THEN
            PERFORM rvbbit.capability_crawl();
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'catalog crawl: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
    END;
    RETURN NULL;
END $trg$;

DROP TRIGGER IF EXISTS rvbbit_capability_crawl_after_catalog ON rvbbit.catalog_runs;
CREATE TRIGGER rvbbit_capability_crawl_after_catalog
    AFTER UPDATE OF status ON rvbbit.catalog_runs
    FOR EACH ROW
    WHEN (NEW.status = 'ok' AND OLD.status <> 'ok')
    EXECUTE FUNCTION rvbbit._capability_crawl_on_catalog_ok();
ALTER TABLE rvbbit.catalog_runs ENABLE ALWAYS TRIGGER rvbbit_capability_crawl_after_catalog;
