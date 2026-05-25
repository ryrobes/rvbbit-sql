-- pg_rvbbit 0.27.0 -> 0.28.0
-- Durable adaptive route profile export/import.

CREATE FUNCTION rvbbit.route_export_profile(
    profile_name text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_export_profile_wrapper';

CREATE FUNCTION rvbbit.route_import_profile(
    profile_name text,
    profile jsonb,
    active boolean
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_import_profile_wrapper';
