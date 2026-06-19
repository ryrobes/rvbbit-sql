-- 0066_vector_sources_tier — generalize the pgvector HNSW tier to ANY real[] embedding column.
--
-- The catalog had a bespoke pgvector tier (pgvector_tier.sql): canonical real[] in catalog_docs, a
-- centered-vector pgvector MIRROR (catalog_vec) + HNSW, with brute-force fallback. It's a good design but
-- hardwired to catalog_docs and — critically — rebuilds a SINGLE global catalog_vec table per call, so a
-- second corpus (the Document Brain) can't share it without clobbering. This promotes that mechanism to a
-- registry-driven primitive: register any (table, id_col, embed_col); get a per-source mirror + HNSW you
-- can refresh independently. The Brain registers brain_chunks here; brain_search uses it in 0067.
--
-- Design carried over from the catalog tier: real[] stays CANONICAL (pgvector is a peer index, soft-
-- required — everything degrades to brute force if pgvector is absent / a refresh fails). Per-source
-- `center` flag: catalog centers by μ (cosine rank-order invariant under translation) — the Brain does
-- NOT center, so its HNSW cosine is byte-identical in rank to the existing brute cosine_vec (zero
-- relevance shift; acceleration is a pure latency swap).

-- Soft-require pgvector (no-op / fallback if unavailable).
DO $$ BEGIN
    BEGIN CREATE EXTENSION IF NOT EXISTS vector;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'rvbbit: pgvector unavailable (%); vector tier falls back to brute force', SQLERRM;
    END;
END $$;

-- ── registry: one row per accelerated vector source ───────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.vector_sources (
    index_name   text PRIMARY KEY,            -- 'brain_chunks', 'db_catalog', …
    source_table regclass NOT NULL,           -- rvbbit.brain_chunks
    id_col       text NOT NULL,               -- chunk_id (bigint PK of the source)
    embed_col    text NOT NULL,               -- embedding (real[])
    center       boolean NOT NULL DEFAULT true,-- subtract corpus μ before indexing (catalog: yes; brain: no)
    dim          int,                          -- active embedding dim
    mean         real[],                       -- μ snapshot (NULL when center=false)
    n_rows       bigint NOT NULL DEFAULT 0,
    status       text NOT NULL DEFAULT 'unbuilt', -- unbuilt | building | ready | failed
    message      text,
    refreshed_at timestamptz
);

-- deterministic mirror table name for an index (validated identifier)
CREATE OR REPLACE FUNCTION rvbbit._vector_mirror(p_index text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'vec_' || regexp_replace(lower(p_index), '[^a-z0-9_]', '_', 'g');
$fn$;

-- register (or update) a vector source
CREATE OR REPLACE FUNCTION rvbbit.vector_register(
    p_index text, p_source_table regclass, p_id_col text, p_embed_col text, p_center boolean DEFAULT true
) RETURNS text LANGUAGE sql VOLATILE AS $fn$
    INSERT INTO rvbbit.vector_sources (index_name, source_table, id_col, embed_col, center, status)
    VALUES (p_index, p_source_table, p_id_col, p_embed_col, p_center, 'unbuilt')
    ON CONFLICT (index_name) DO UPDATE SET
        source_table = EXCLUDED.source_table, id_col = EXCLUDED.id_col,
        embed_col = EXCLUDED.embed_col, center = EXCLUDED.center
    RETURNING index_name;
$fn$;

-- readiness: pgvector present + mirror exists + 'ready' at the query dim
CREATE OR REPLACE FUNCTION rvbbit.vector_ready(p_index text, p_dim int)
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT to_regtype('vector') IS NOT NULL
       AND EXISTS (SELECT 1 FROM rvbbit.vector_sources s
                    WHERE s.index_name = p_index AND s.status = 'ready' AND s.dim = p_dim
                      AND to_regclass('rvbbit.' || rvbbit._vector_mirror(p_index)) IS NOT NULL);
$fn$;

-- ── (re)build a source's centered/raw mirror + HNSW; idempotent, never raises ──
CREATE OR REPLACE FUNCTION rvbbit.vector_refresh(p_index text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    s        rvbbit.vector_sources%ROWTYPE;
    v_mirror text;
    v_dim    int;
    v_n      bigint;
    v_mu     real[];
BEGIN
    SELECT * INTO s FROM rvbbit.vector_sources WHERE index_name = p_index;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'reason', 'not registered'); END IF;
    IF to_regtype('vector') IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'pgvector not installed');
    END IF;
    v_mirror := rvbbit._vector_mirror(p_index);

    -- active dim = most common embedding length
    EXECUTE format('SELECT array_length(%I,1), count(*) FROM %s WHERE %I IS NOT NULL '
                   'GROUP BY array_length(%I,1) ORDER BY count(*) DESC LIMIT 1',
                   s.embed_col, s.source_table, s.embed_col, s.embed_col)
       INTO v_dim, v_n;
    IF v_dim IS NULL THEN RETURN jsonb_build_object('ok', false, 'reason', 'no embedded rows'); END IF;

    UPDATE rvbbit.vector_sources SET status='building', dim=v_dim, message=NULL, refreshed_at=now()
     WHERE index_name = p_index;

    BEGIN
        IF s.center THEN
            EXECUTE format(
                'SELECT array_agg(m ORDER BY i) FROM (SELECT t.i, avg(t.e)::real m '
                'FROM %s d CROSS JOIN LATERAL unnest(d.%I) WITH ORDINALITY t(e,i) '
                'WHERE d.%I IS NOT NULL AND array_length(d.%I,1)=$1 GROUP BY t.i) z',
                s.source_table, s.embed_col, s.embed_col, s.embed_col)
               INTO v_mu USING v_dim;
        ELSE
            v_mu := NULL;
        END IF;

        EXECUTE format('DROP TABLE IF EXISTS rvbbit.%I', v_mirror);
        EXECUTE format('CREATE TABLE rvbbit.%I (id bigint PRIMARY KEY, vec vector(%s) NOT NULL)', v_mirror, v_dim);

        IF s.center THEN
            EXECUTE format(
                'INSERT INTO rvbbit.%I (id, vec) SELECT d.%I, '
                '(SELECT array_agg(u.de-u.me ORDER BY u.i) FROM unnest(d.%I,$1) WITH ORDINALITY u(de,me,i))::vector '
                'FROM %s d WHERE d.%I IS NOT NULL AND array_length(d.%I,1)=$2',
                v_mirror, s.id_col, s.embed_col, s.source_table, s.embed_col, s.embed_col)
               USING v_mu, v_dim;
        ELSE
            EXECUTE format(
                'INSERT INTO rvbbit.%I (id, vec) SELECT d.%I, d.%I::vector '
                'FROM %s d WHERE d.%I IS NOT NULL AND array_length(d.%I,1)=$1',
                v_mirror, s.id_col, s.embed_col, s.source_table, s.embed_col, s.embed_col)
               USING v_dim;
        END IF;

        EXECUTE format('CREATE INDEX %I ON rvbbit.%I USING hnsw (vec vector_cosine_ops)', v_mirror || '_hnsw', v_mirror);

        UPDATE rvbbit.vector_sources SET mean=v_mu, n_rows=v_n, status='ready', message=NULL, refreshed_at=now()
         WHERE index_name = p_index;
        RETURN jsonb_build_object('ok', true, 'index', p_index, 'dim', v_dim, 'n_rows', v_n, 'centered', s.center);
    EXCEPTION WHEN OTHERS THEN
        UPDATE rvbbit.vector_sources SET status='failed', message=SQLERRM, refreshed_at=now()
         WHERE index_name = p_index;
        RETURN jsonb_build_object('ok', false, 'reason', SQLERRM);
    END;
END $fn$;

-- ── raw ANN: top-k by cosine over the mirror (centered query if the source centers) ──
-- Pure HNSW order-by-limit, NO predicate (callers over-fetch then filter — avoids HNSW filtered-scan
-- under-return). Returns empty if not ready / dim-mismatch, so callers fall back to brute force.
CREATE OR REPLACE FUNCTION rvbbit.vector_ann(p_index text, q_vec real[], k int)
RETURNS TABLE (id bigint, score float8) LANGUAGE plpgsql STABLE AS $fn$
DECLARE s rvbbit.vector_sources%ROWTYPE; v_mirror text; v_qc real[];
BEGIN
    IF q_vec IS NULL OR array_length(q_vec,1) IS NULL THEN RETURN; END IF;
    SELECT * INTO s FROM rvbbit.vector_sources WHERE index_name = p_index AND status='ready';
    IF NOT FOUND OR s.dim IS DISTINCT FROM array_length(q_vec,1) THEN RETURN; END IF;
    IF to_regtype('vector') IS NULL THEN RETURN; END IF;
    v_mirror := rvbbit._vector_mirror(p_index);

    IF s.center AND s.mean IS NOT NULL THEN
        SELECT array_agg(u.qe-u.me ORDER BY u.i) INTO v_qc
          FROM unnest(q_vec, s.mean) WITH ORDINALITY u(qe,me,i);
    ELSE
        v_qc := q_vec;
    END IF;

    RETURN QUERY EXECUTE format(
        'SELECT m.id, (1.0-(m.vec <=> $1::vector))::float8 FROM rvbbit.%I m ORDER BY m.vec <=> $1::vector LIMIT $2',
        v_mirror) USING v_qc, k;
END $fn$;

-- ── register the Document Brain's chunk embeddings (raw cosine; matches brute exactly) ──
SELECT rvbbit.vector_register('brain_chunks', 'rvbbit.brain_chunks'::regclass, 'chunk_id', 'embedding', false);
