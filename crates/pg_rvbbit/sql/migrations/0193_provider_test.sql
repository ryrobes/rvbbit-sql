-- 0193: rvbbit.provider_test — one real completion through the exact
-- production path (backend spec → transport → auth env-or-secret). The AI
-- Providers panel's Test button: proves endpoint + credentials + model in
-- one call and returns the receipt (or a redacted error). Works for ANY
-- backend, including custom/local OpenAI-compatible endpoints where the
-- public model-catalog fetchers don't apply.
--
-- Wrapper DDL for a same-version .so addition (0044/0135/0139 precedent).

CREATE OR REPLACE FUNCTION rvbbit.provider_test(
    provider text,
    model text,
    prompt text DEFAULT 'Reply with exactly: OK'
) RETURNS jsonb LANGUAGE c VOLATILE AS '$libdir/pg_rvbbit', 'provider_test_wrapper';

COMMENT ON FUNCTION rvbbit.provider_test(text, text, text) IS
    'Run one tiny completion against a named provider backend to prove endpoint+auth+model. Returns {ok, content, latency_ms, tokens, cost} or {ok:false, error (redacted)}.';
