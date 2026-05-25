-- pg_rvbbit 0.30.0 -> 0.31.0
-- LLM providers unified into the model-backend registry (rvbbit.specialists).
--
-- An LLM provider is just a backend with a chat transport. `chat()` now
-- resolves a provider backend by name and dispatches through the same
-- Transport machinery specialists use, instead of a hardcoded OpenRouter
-- path. New transports: openai_chat (OpenAI-compatible — OpenRouter, a
-- local vLLM/Ollama, OpenAI, Together, …); anthropic + gemini are reserved
-- here for Phase 2.

-- Widen the transport CHECK to admit the chat transports.
ALTER TABLE rvbbit.specialists
    DROP CONSTRAINT IF EXISTS specialists_transport_check;
ALTER TABLE rvbbit.specialists
    ADD CONSTRAINT specialists_transport_check
    CHECK (transport IN ('rvbbit', 'gradio', 'openai', 'stub',
                         'openai_chat', 'anthropic', 'gemini'));

-- Seed the default LLM provider so existing operators keep working with
-- zero config — auth via the OPENROUTER_API_KEY env var. DO NOTHING on
-- conflict so a user who already registered 'openrouter' is left untouched.
INSERT INTO rvbbit.specialists
    (name, transport, endpoint_url, max_concurrent, timeout_ms,
     auth_header_env, description)
VALUES
    ('openrouter', 'openai_chat',
     'https://openrouter.ai/api/v1/chat/completions',
     8, 120000, 'OPENROUTER_API_KEY',
     'Default LLM provider — OpenRouter multi-model gateway.')
ON CONFLICT (name) DO NOTHING;

ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS shadow_heap_retained boolean NOT NULL DEFAULT false;

ALTER TABLE rvbbit.tables
    ADD COLUMN IF NOT EXISTS shadow_heap_dirty boolean NOT NULL DEFAULT false;

ALTER TABLE rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS pg_ms double precision;

ALTER TABLE rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;

ALTER TABLE rvbbit.route_profile_entries
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;

ALTER TABLE rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS duck_hive_ms double precision;

ALTER TABLE rvbbit.route_profile_points
    ADD COLUMN IF NOT EXISTS datafusion_hive_ms double precision;

ALTER TABLE rvbbit.route_profile_points
    DROP CONSTRAINT IF EXISTS route_profile_points_pg_ms_check;
ALTER TABLE rvbbit.route_profile_points
    ADD CONSTRAINT route_profile_points_pg_ms_check
    CHECK (pg_ms IS NULL OR pg_ms > 0);

ALTER TABLE rvbbit.route_profile_points
    DROP CONSTRAINT IF EXISTS route_profile_points_duck_hive_ms_check;
ALTER TABLE rvbbit.route_profile_points
    ADD CONSTRAINT route_profile_points_duck_hive_ms_check
    CHECK (duck_hive_ms IS NULL OR duck_hive_ms > 0);

ALTER TABLE rvbbit.route_profile_points
    DROP CONSTRAINT IF EXISTS route_profile_points_datafusion_hive_ms_check;
ALTER TABLE rvbbit.route_profile_points
    ADD CONSTRAINT route_profile_points_datafusion_hive_ms_check
    CHECK (datafusion_hive_ms IS NULL OR datafusion_hive_ms > 0);

ALTER TABLE IF EXISTS rvbbit.route_observations
    DROP CONSTRAINT IF EXISTS route_observations_candidate_check;
ALTER TABLE IF EXISTS rvbbit.route_observations
    ADD CONSTRAINT route_observations_candidate_check
    CHECK (candidate IN ('duck_vector', 'duck_hive', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    DROP CONSTRAINT IF EXISTS route_profile_entries_choice_check;
ALTER TABLE IF EXISTS rvbbit.route_profile_entries
    ADD CONSTRAINT route_profile_entries_choice_check
    CHECK (choice IN ('duck_vector', 'duck_hive', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

CREATE OR REPLACE FUNCTION rvbbit.mark_shadow_heap_dirty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE rvbbit.tables
    SET shadow_heap_dirty = true
    WHERE table_oid = TG_RELID
      AND shadow_heap_retained;
    RETURN NULL;
END;
$$;

-- Async route decision telemetry. These rows are not training timings;
-- they describe the route chosen by the normal backend rewriter and are
-- written by a best-effort background logger.
CREATE TABLE IF NOT EXISTS rvbbit.route_decisions (
    id            bigserial PRIMARY KEY,
    decided_at    timestamptz NOT NULL DEFAULT now(),
    backend_pid   integer NOT NULL,
    database_name text NOT NULL,
    role_name     text NOT NULL,
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    shape_family  text NOT NULL,
    route         text NOT NULL,
    candidate     text,
    route_source  text NOT NULL,
    reason        text NOT NULL DEFAULT '',
    confidence    double precision,
    cache_hit     boolean NOT NULL DEFAULT false,
    rewritten     boolean NOT NULL DEFAULT false,
    features      jsonb NOT NULL DEFAULT '{}'::jsonb,
    route_doc     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
    CHECK (confidence IS NULL OR confidence >= 0)
);

CREATE INDEX IF NOT EXISTS route_decisions_time_idx
    ON rvbbit.route_decisions (decided_at DESC);

CREATE INDEX IF NOT EXISTS route_decisions_shape_idx
    ON rvbbit.route_decisions (shape_key, candidate, decided_at DESC);

CREATE INDEX IF NOT EXISTS route_decisions_source_idx
    ON rvbbit.route_decisions (route_source, decided_at DESC);

ALTER TABLE rvbbit.route_decisions
    DROP CONSTRAINT IF EXISTS route_decisions_candidate_check;
ALTER TABLE rvbbit.route_decisions
    ADD CONSTRAINT route_decisions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

CREATE OR REPLACE VIEW rvbbit.route_decision_summary AS
SELECT
    shape_key,
    shape_family,
    candidate,
    route,
    route_source,
    count(*)::bigint AS decisions,
    count(*) FILTER (WHERE cache_hit)::bigint AS cache_hits,
    count(*) FILTER (WHERE rewritten)::bigint AS rewritten_count,
    min(decided_at) AS first_seen,
    max(decided_at) AS last_seen,
    (array_agg(reason ORDER BY decided_at DESC))[1] AS last_reason
FROM rvbbit.route_decisions
GROUP BY shape_key, shape_family, candidate, route, route_source;

CREATE TABLE IF NOT EXISTS rvbbit.route_executions (
    id            bigserial PRIMARY KEY,
    executed_at   timestamptz NOT NULL DEFAULT now(),
    backend_pid   integer NOT NULL,
    database_name text NOT NULL,
    role_name     text NOT NULL,
    query_hash    text NOT NULL,
    shape_key     text NOT NULL,
    shape_family  text NOT NULL,
    route         text NOT NULL,
    candidate     text,
    route_source  text NOT NULL,
    reason        text NOT NULL DEFAULT '',
    confidence    double precision,
    cache_hit     boolean NOT NULL DEFAULT false,
    rewritten     boolean NOT NULL DEFAULT false,
    elapsed_ms    double precision NOT NULL,
    rows_returned bigint NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'ok',
    features      jsonb NOT NULL DEFAULT '{}'::jsonb,
    route_doc     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore')),
    CHECK (confidence IS NULL OR confidence >= 0),
    CHECK (elapsed_ms >= 0),
    CHECK (rows_returned >= 0)
);

CREATE INDEX IF NOT EXISTS route_executions_time_idx
    ON rvbbit.route_executions (executed_at DESC);

CREATE INDEX IF NOT EXISTS route_executions_shape_idx
    ON rvbbit.route_executions (shape_key, candidate, executed_at DESC);

CREATE INDEX IF NOT EXISTS route_executions_source_idx
    ON rvbbit.route_executions (route_source, executed_at DESC);

ALTER TABLE rvbbit.route_executions
    DROP CONSTRAINT IF EXISTS route_executions_candidate_check;
ALTER TABLE rvbbit.route_executions
    ADD CONSTRAINT route_executions_candidate_check
    CHECK (candidate IS NULL OR candidate IN ('duck_vector', 'duck_hive', 'datafusion_vector', 'datafusion_hive', 'rvbbit_native', 'pg_rowstore'));

CREATE OR REPLACE VIEW rvbbit.route_runtime_summary AS
SELECT
    shape_key,
    shape_family,
    candidate,
    route,
    route_source,
    count(*)::bigint AS executions,
    count(*) FILTER (WHERE cache_hit)::bigint AS cache_hits,
    count(*) FILTER (WHERE rewritten)::bigint AS rewritten_count,
    count(*) FILTER (WHERE status = 'ok')::bigint AS ok_count,
    count(*) FILTER (WHERE status <> 'ok')::bigint AS error_count,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS median_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms,
    min(elapsed_ms) AS min_ms,
    max(elapsed_ms) AS max_ms,
    avg(elapsed_ms) AS avg_ms,
    min(executed_at) AS first_seen,
    max(executed_at) AS last_seen,
    (array_agg(reason ORDER BY executed_at DESC))[1] AS last_reason
FROM rvbbit.route_executions
GROUP BY shape_key, shape_family, candidate, route, route_source;

CREATE OR REPLACE FUNCTION rvbbit.route_decision_log_status() RETURNS jsonb
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_decision_log_status_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.route_cache_reset() RETURNS bigint
VOLATILE
LANGUAGE c
AS 'MODULE_PATHNAME', 'route_cache_reset_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.compact(rel regclass, keep_heap boolean)
RETURNS TABLE (rg_id bigint, n_rows bigint, n_bytes bigint, heap_freed_bytes bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    written_rows  bigint;
    heap_size_pre bigint;
    max_rg_id_pre bigint;
    shred         record;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(rel) THEN
        RAISE EXCEPTION '% is not an rvbbit table', rel;
    END IF;

    heap_size_pre := pg_total_relation_size(rel);
    SELECT COALESCE(max(rg.rg_id), -1)
    INTO max_rg_id_pre
    FROM rvbbit.row_groups rg
    WHERE rg.table_oid = rel;

    EXECUTE format('ANALYZE %s', rel);
    SELECT rvbbit.export_to_parquet(rel) INTO written_rows;

    IF keep_heap THEN
        UPDATE rvbbit.tables
        SET shadow_heap_retained = true,
            shadow_heap_dirty = false
        WHERE table_oid = rel;
        EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', rel);
        EXECUTE format(
            'CREATE TRIGGER rvbbit_shadow_heap_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION rvbbit.mark_shadow_heap_dirty()',
            rel
        );
        RAISE NOTICE 'rvbbit.compact: preserving clean shadow heap for %; parquet remains authoritative until the heap is mutated', rel;
    ELSE
        UPDATE rvbbit.tables
        SET shadow_heap_retained = false,
            shadow_heap_dirty = false
        WHERE table_oid = rel;
        EXECUTE format('DROP TRIGGER IF EXISTS rvbbit_shadow_heap_dirty ON %s', rel);
        EXECUTE format('TRUNCATE TABLE %s', rel);
    END IF;

    FOR shred IN
        SELECT s.column_name, s.source_expr, s.data_type
        FROM rvbbit.shreds s
        WHERE s.table_oid = rel
    LOOP
        BEGIN
            EXECUTE format(
                'ALTER TABLE %s ADD COLUMN IF NOT EXISTS %I %s',
                rel, shred.column_name, shred.data_type
            );
        EXCEPTION WHEN duplicate_column THEN
        END;
    END LOOP;

    RETURN QUERY
        SELECT rg.rg_id, rg.n_rows, rg.n_bytes, heap_size_pre - pg_total_relation_size(rel)
        FROM rvbbit.row_groups rg
        WHERE rg.table_oid = rel
          AND rg.rg_id > max_rg_id_pre
        ORDER BY rg.rg_id DESC;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.compact(rel regclass)
RETURNS TABLE (rg_id bigint, n_rows bigint, n_bytes bigint, heap_freed_bytes bigint)
LANGUAGE sql
AS $$
    SELECT * FROM rvbbit.compact(
        rel,
        lower(coalesce(current_setting('rvbbit.compact_keep_heap', true), 'off'))
            IN ('1', 'true', 'on', 'yes')
    );
$$;

CREATE OR REPLACE FUNCTION rvbbit.shadow_heap_status(rel regclass)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    heap_bytes bigint,
    heap_total_bytes bigint,
    parquet_rows bigint,
    parquet_bytes bigint,
    row_groups bigint,
    delete_rows bigint,
    parquet_authoritative boolean,
    shadow_heap_present boolean,
    shadow_heap_retained boolean,
    shadow_heap_dirty boolean
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        rel::oid,
        rel::text,
        pg_relation_size(rel)::bigint,
        pg_total_relation_size(rel)::bigint,
        coalesce((SELECT sum(rg.n_rows)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT sum(rg.n_bytes)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT count(*)::bigint FROM rvbbit.row_groups rg WHERE rg.table_oid = rel), 0),
        coalesce((SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = rel), 0),
        coalesce((SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = rel), 0) = 0
            AND (
                pg_relation_size(rel) = 0
                OR coalesce((SELECT t.shadow_heap_retained AND NOT t.shadow_heap_dirty FROM rvbbit.tables t WHERE t.table_oid = rel), false)
            ),
        pg_relation_size(rel) > 0
            AND coalesce((SELECT t.shadow_heap_retained FROM rvbbit.tables t WHERE t.table_oid = rel), false),
        coalesce((SELECT t.shadow_heap_retained FROM rvbbit.tables t WHERE t.table_oid = rel), false),
        coalesce((SELECT t.shadow_heap_dirty FROM rvbbit.tables t WHERE t.table_oid = rel), false);
$$;
