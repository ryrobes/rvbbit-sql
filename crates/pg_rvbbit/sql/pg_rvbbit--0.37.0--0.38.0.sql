-- pg_rvbbit 0.37.0 -> 0.38.0
-- Adaptive routing v1 hardening: operator status helpers.

CREATE OR REPLACE FUNCTION rvbbit.route_profiles() RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_profiles_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_status() RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_status_wrapper';
