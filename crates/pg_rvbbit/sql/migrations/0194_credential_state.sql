-- 0194: rvbbit.credential_state — where would a named credential resolve
-- from right now? Mirrors the real auth precedence (env var first, then
-- rvbbit.secrets). Presence only, never the value: the AI Providers panel
-- uses it to say "via env" / "stored secret" / "missing" honestly — the
-- lens cannot see the database process's environment.
--
-- Wrapper DDL for a same-version .so addition (0044/0135/0139/0193
-- precedent).

CREATE OR REPLACE FUNCTION rvbbit.credential_state(
    name text
) RETURNS text LANGUAGE c VOLATILE STRICT AS '$libdir/pg_rvbbit', 'credential_state_wrapper';

COMMENT ON FUNCTION rvbbit.credential_state(text) IS
    'Resolution source for a named credential: env (container environment, wins), secret (rvbbit.secrets), missing, or none for empty names. Presence only — never returns the value.';
