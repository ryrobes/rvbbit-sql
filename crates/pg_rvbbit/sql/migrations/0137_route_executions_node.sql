-- 0137_route_executions_node.sql
-- Fleet identity on execution breadcrumbs too (decisions got theirs in 0134):
-- which engine endpoint served the query, NULL = the brain's local engines.
-- Written at event-build time from the same resolution the dispatcher uses,
-- so the Adaptive Routing node filter shows exactly what routed where.
ALTER TABLE rvbbit.route_executions ADD COLUMN IF NOT EXISTS node text;
CREATE INDEX IF NOT EXISTS route_executions_node_idx
    ON rvbbit.route_executions (node) WHERE node IS NOT NULL;
CREATE INDEX IF NOT EXISTS route_decisions_node_idx
    ON rvbbit.route_decisions (node) WHERE node IS NOT NULL;
