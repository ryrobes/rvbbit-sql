-- 0142: admit fleet/hare modes to the sidecar telemetry tables.
--
-- The mode CHECK on duck_sidecar_instances/heartbeats/query_events predates
-- the read fleet: it only allowed the three LOCAL topologies, so every
-- fleet worker's telemetry write (mode='fleet_tcp') has been rejected since
-- 3.1.5 — silently, because sidecar telemetry is best-effort by design. The
-- effect was invisible workers: no heartbeats, no per-query events, only
-- constraint noise in the brain's log. 'hare_http' rides along for the
-- serverless worker mode (telemetry-less today — hares hold no DSN — but the
-- mode string exists and a future opt-in callback shouldn't trip here).
ALTER TABLE IF EXISTS rvbbit.duck_sidecar_instances
    DROP CONSTRAINT IF EXISTS duck_sidecar_instances_mode_check;
ALTER TABLE IF EXISTS rvbbit.duck_sidecar_instances
    ADD CONSTRAINT duck_sidecar_instances_mode_check
    CHECK (mode IN ('local_oneshot', 'local_persistent', 'shared_broker', 'fleet_tcp', 'hare_http'));

ALTER TABLE IF EXISTS rvbbit.duck_sidecar_heartbeats
    DROP CONSTRAINT IF EXISTS duck_sidecar_heartbeats_mode_check;
ALTER TABLE IF EXISTS rvbbit.duck_sidecar_heartbeats
    ADD CONSTRAINT duck_sidecar_heartbeats_mode_check
    CHECK (mode IN ('local_oneshot', 'local_persistent', 'shared_broker', 'fleet_tcp', 'hare_http'));

ALTER TABLE IF EXISTS rvbbit.duck_sidecar_query_events
    DROP CONSTRAINT IF EXISTS duck_sidecar_query_events_mode_check;
ALTER TABLE IF EXISTS rvbbit.duck_sidecar_query_events
    ADD CONSTRAINT duck_sidecar_query_events_mode_check
    CHECK (mode IN ('local_oneshot', 'local_persistent', 'shared_broker', 'fleet_tcp', 'hare_http'));
