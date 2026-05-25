-- pg_rvbbit 0.4.0 -> 0.5.0
-- RYR-289 capstone: JIT embeddings + content-addressed cache, plus
-- RYR-301 polish (judgment cache observability) and a 'stub'
-- specialist transport for deterministic tests.

-- Relax the specialists CHECK to allow the new stub transport.
ALTER TABLE rvbbit.specialists DROP CONSTRAINT IF EXISTS specialists_transport_check;
ALTER TABLE rvbbit.specialists
    ADD CONSTRAINT specialists_transport_check
    CHECK (transport IN ('rvbbit', 'gradio', 'openai', 'stub'));

-- Content-addressed embedding cache (RYR-289).
CREATE TABLE rvbbit.embedding_cache (
    text_hash    bytea NOT NULL,
    specialist   text NOT NULL,
    model        text NOT NULL,
    dim          int NOT NULL,
    embedding    real[] NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (text_hash, specialist)
);

CREATE INDEX embedding_cache_specialist_idx
    ON rvbbit.embedding_cache(specialist, computed_at);

-- Embedding UDFs.
CREATE FUNCTION rvbbit.embed(text TEXT, specialist TEXT DEFAULT '')
RETURNS REAL[]
STABLE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'embed_wrapper';

CREATE FUNCTION rvbbit.similarity(a TEXT, b TEXT, specialist TEXT DEFAULT '')
RETURNS DOUBLE PRECISION
STABLE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'similarity_wrapper';

CREATE FUNCTION rvbbit.embed_distance(a TEXT, b TEXT, specialist TEXT DEFAULT '')
RETURNS DOUBLE PRECISION
STABLE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'embed_distance_wrapper';

CREATE FUNCTION rvbbit.cosine_vec(a REAL[], b REAL[])
RETURNS DOUBLE PRECISION
IMMUTABLE STRICT PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'cosine_vec_wrapper';

CREATE FUNCTION rvbbit.materialize_embeddings(rel oid, col TEXT, specialist TEXT DEFAULT '')
RETURNS BIGINT
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'materialize_embeddings_wrapper';

CREATE FUNCTION rvbbit.embedding_cache_stats()
RETURNS TABLE(
    specialist TEXT,
    model TEXT,
    n_entries BIGINT,
    dim INT,
    total_bytes BIGINT,
    oldest_at TIMESTAMPTZ,
    newest_at TIMESTAMPTZ
)
STABLE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'embedding_cache_stats_wrapper';

CREATE FUNCTION rvbbit.embedding_purge(specialist TEXT)
RETURNS BIGINT
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'embedding_purge_wrapper';

-- Judgment cache polish (RYR-301).
CREATE FUNCTION rvbbit.judgment_stats(op_name TEXT)
RETURNS TABLE(
    op_name TEXT,
    n_invocations BIGINT,
    n_unique_inputs BIGINT,
    total_tokens_in BIGINT,
    total_tokens_out BIGINT,
    total_cost_usd NUMERIC,
    total_latency_ms BIGINT,
    first_at TIMESTAMPTZ,
    last_at TIMESTAMPTZ
)
STABLE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'judgment_stats_wrapper';

CREATE FUNCTION rvbbit.judgment_purge(op_name TEXT)
RETURNS BIGINT
VOLATILE PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'judgment_purge_wrapper';
