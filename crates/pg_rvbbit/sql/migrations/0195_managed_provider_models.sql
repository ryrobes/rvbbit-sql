-- 0195: managed (capability-installed) LLM backends get first-class model
-- identity. clover_llm is installed by the managed/clover capability — the
-- panel must treat it as capability-owned, and the rest of the system
-- should see its callable model like any other provider's:
--
--   1. Stamp the callable alias into transport_opts.model (the openai_chat
--      transport now falls back to it when a request omits the model).
--   2. sync_managed_provider_models(): managed backends → rvbbit
--      .provider_models rows (source='capability'), so model pickers,
--      datalists, and rates plumbing see them. Idempotent; prunes rows
--      whose backend or alias vanished.
--   3. provider_test gains DEFAULT '' on model — empty means "use the
--      backend's default" via the new transport fallback.

-- 1. The clover-llm-v1 deployment serves the 'gemma4' alias.
UPDATE rvbbit.backends
SET transport_opts = transport_opts || '{"model": "gemma4"}'::jsonb
WHERE install_manifest->>'capability' = 'managed/clover'
  AND coalesce(transport_opts->>'model', '') = '';

-- 2. Managed backends → provider_models.
CREATE OR REPLACE FUNCTION rvbbit.sync_managed_provider_models() RETURNS integer
LANGUAGE sql
AS $fn$
WITH managed AS (
    SELECT b.name AS provider,
           coalesce(nullif(b.transport_opts->>'model', ''), b.source_model) AS model,
           b.source_model
    FROM rvbbit.backends b
    WHERE b.install_manifest IS NOT NULL
      AND b.transport IN ('openai_chat', 'anthropic', 'gemini')
      AND coalesce(nullif(b.transport_opts->>'model', ''), b.source_model) IS NOT NULL
), up AS (
    INSERT INTO rvbbit.provider_models (provider, model, display_name, family, source, available)
    SELECT provider, model,
           coalesce(source_model, model),
           split_part(coalesce(source_model, model), '/', 1),
           'capability', true
    FROM managed
    ON CONFLICT (provider, model) DO UPDATE SET
        available = true,
        source = 'capability',
        display_name = EXCLUDED.display_name,
        updated_at = clock_timestamp()
    RETURNING 1
), pruned AS (
    DELETE FROM rvbbit.provider_models pm
    WHERE pm.source = 'capability'
      AND NOT EXISTS (SELECT 1 FROM managed m WHERE m.provider = pm.provider AND m.model = pm.model)
    RETURNING 1
)
SELECT coalesce((SELECT count(*) FROM up), 0)::int + coalesce((SELECT count(*) FROM pruned), 0)::int;
$fn$;

COMMENT ON FUNCTION rvbbit.sync_managed_provider_models() IS
    'Mirror capability-installed LLM backends into rvbbit.provider_models (source=capability) so managed models appear in every model picker. Idempotent; prunes stale rows.';

SELECT rvbbit.sync_managed_provider_models();

-- 3. provider_test: empty model = the backend's transport_opts default.
CREATE OR REPLACE FUNCTION rvbbit.provider_test(
    provider text,
    model text DEFAULT '',
    prompt text DEFAULT 'Reply with exactly: OK'
) RETURNS jsonb LANGUAGE c VOLATILE AS '$libdir/pg_rvbbit', 'provider_test_wrapper';
