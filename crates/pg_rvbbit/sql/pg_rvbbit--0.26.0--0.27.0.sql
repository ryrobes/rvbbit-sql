-- pg_rvbbit 0.26.0 -> 0.27.0
-- First-class lifecycle operations for adaptive route profiles.

CREATE FUNCTION rvbbit.route_activate_profile(
    profile_name text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_activate_profile_wrapper';

CREATE FUNCTION rvbbit.route_retire_profile(
    profile_name text
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_retire_profile_wrapper';

CREATE FUNCTION rvbbit.route_clone_profile(
    source_profile text,
    target_profile text,
    active boolean
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_clone_profile_wrapper';

CREATE FUNCTION rvbbit.route_merge_profiles(
    target_profile text,
    source_profiles jsonb,
    active boolean
) RETURNS jsonb
STRICT VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_merge_profiles_wrapper';
