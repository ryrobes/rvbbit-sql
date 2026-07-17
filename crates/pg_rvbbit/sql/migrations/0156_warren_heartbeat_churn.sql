-- 0156: stop warren_nodes from being an autovacuum treadmill.
--
-- Every agent heartbeat UPDATEs last_heartbeat, and last_heartbeat lived in
-- warren_nodes_status_idx (status, last_heartbeat DESC) — so no heartbeat
-- could ever be a HOT update: each one wrote a dead heap tuple PLUS new
-- entries in every index on the table. At the agent's poll cadence that
-- crossed the ~50-dead-tuple autovacuum trigger every minute or two, forever.
--
-- Three dials, all independent of the agent-side heartbeat dampening:
--
-- 1. Drop the status index. Every reader goes through views that scan the
--    whole table, and warren fleets are a handful of rows — a one-page seq
--    scan beats any index. With no index containing last_heartbeat (labels'
--    GIN survives: unchanged values don't block HOT), heartbeats become
--    HOT-eligible and page pruning reclaims them without vacuum.
DROP INDEX IF EXISTS rvbbit.warren_nodes_status_idx;

-- 2. Leave HOT headroom on the page so update chains stay local.
ALTER TABLE rvbbit.warren_nodes SET (fillfactor = 70);

-- 3. Belt: even residual churn shouldn't page autovacuum every minute.
ALTER TABLE rvbbit.warren_nodes SET (
    autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_scale_factor = 0
);
