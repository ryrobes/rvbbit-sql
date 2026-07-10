-- 0139: hare pre-work — query capsules + presigned artifact GETs.
--
-- A capsule is everything a credential-less serverless worker (a "hare" —
-- warrens burrow, hares don't) needs to answer one query: vetted SQL + the
-- catalog slice the fleet sidecar would otherwise fetch over its DSN, with
-- row-group URLs presigned so the worker holds zero store credentials.
-- Brain-side dispatch is deliberately NOT wired into the router yet; these
-- are the primitives the Cloud Run experiment drives by hand first.
-- Design: docs/HARE_PLAN.md.
--
-- C bindings ($libdir literal because migrate() runs via SPI where
-- MODULE_PATHNAME is not substituted — 0044/0135 precedent). Both are
-- superuser-gated in the Rust body: a presigned URL IS temporary data
-- access, so minting one is a privilege until role-scoped policies exist.
CREATE OR REPLACE FUNCTION rvbbit.capsule(
    sql text,
    ttl_secs integer DEFAULT 900,
    presign boolean DEFAULT true
) RETURNS jsonb LANGUAGE c AS '$libdir/pg_rvbbit', 'capsule_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.presign(
    uri text,
    ttl_secs integer DEFAULT 900
) RETURNS text LANGUAGE c AS '$libdir/pg_rvbbit', 'presign_wrapper';
