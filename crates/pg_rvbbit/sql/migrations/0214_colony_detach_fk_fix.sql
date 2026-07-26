-- 0214_colony_detach_fk_fix
--
-- deregister_peer_backend (0213) failed with a foreign key violation on any
-- backend that had ever completed a real call: peer_capability_requests had
-- a hard FK to peer_backends, so a historical 'done'/'failed' row (not just
-- the pending/claimed ones deregister already reassigns) blocked the
-- DELETE. Found live — the first backend registered through the real lens
-- UI, after one successful call through it, couldn't be detached.
--
-- Fix matches the existing house convention for this exact situation
-- (rvbbit.ml_models.backend_name: "points at rvbbit.backends by convention,
-- but is not an FK so audit survives backend deletion") — drop the FK so
-- a detached backend's call history remains readable instead of blocking
-- (or, with CASCADE, silently destroying) the very thing "Stop sharing"
-- is supposed to do.

ALTER TABLE rvbbit.peer_capability_requests
    DROP CONSTRAINT IF EXISTS peer_capability_requests_backend_name_fkey;
