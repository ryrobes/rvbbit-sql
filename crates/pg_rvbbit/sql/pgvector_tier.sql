-- =====================================================================
-- pgvector HNSW tier (Track B, P4) — Tier-1 behind rvbbit.dense_knn_tiered.
--
-- pgvector is a PEER ANN index over the canonical `catalog_docs.embedding
-- real[]` store: a SEPARATE `rvbbit.catalog_vec` table holds STORE-TIME-CENTERED
-- vectors (embedding - μ) at the active dim, with an HNSW cosine index. Because
-- mean-centering is a uniform linear translation, cosine rank order on centered
-- vectors is identical to the brute-force tier — so swapping tiers changes
-- latency, never relevance (exact for kinds=NULL queries; the snapshot μ is the
-- whole-graph mean, so a kinds-filtered query is an approximation of Tier-3's
-- kinds-specific μ — close, since centering only removes the anisotropy mode).
--
-- DEFENSIVE BY DESIGN: every `vector`/`<=>`/HNSW reference is inside DYNAMIC SQL
-- so these functions LOAD on a pgvector-less box; a refresh failure marks the
-- index 'failed' and the dispatcher's readiness check falls back to brute force.
-- So a pgvector bug degrades to "no acceleration", never "search broken".
--
-- pgvector is SOFT-required: auto-created when available, never hard-failing.
-- =====================================================================

-- Soft-require: create pgvector if the files are present + we have privilege.
-- Non-fatal — a box without it just uses the brute-force / Lance tiers.
DO $$ BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS vector;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'rvbbit: pgvector unavailable (%); dense search uses brute-force/Lance fallback', SQLERRM;
    END;
END $$;

-- Index registry (mirrors rvbbit.lance_text_indexes). One row per graph; holds
-- the snapshot μ used to center the stored vectors + the active dim + status.
CREATE TABLE IF NOT EXISTS rvbbit.pgvector_indexes (
    graph_id     text NOT NULL,
    dim          int  NOT NULL,
    mean         real[],                            -- corpus mean snapshot (μ)
    n_docs       bigint NOT NULL DEFAULT 0,
    status       text NOT NULL DEFAULT 'building',  -- building | ready | failed
    message      text,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id)
);

-- Readiness: pgvector present + the centered-vector table exists + a 'ready'
-- index whose dim matches the query's. Used by the dispatcher to decide Tier-1.
CREATE OR REPLACE FUNCTION rvbbit.pgvector_catalog_ready(p_graph text, p_dim int)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT to_regtype('vector') IS NOT NULL
       AND to_regclass('rvbbit.catalog_vec') IS NOT NULL
       AND EXISTS (
            SELECT 1 FROM rvbbit.pgvector_indexes pi
             WHERE pi.graph_id = p_graph AND pi.status = 'ready' AND pi.dim = p_dim
       );
$$;

-- (Re)build the centered-vector table + HNSW index for a graph at its active
-- (most common) embedding dim. Idempotent; safe to call after each catalog_crawl
-- or on a cron heartbeat. Returns a jsonb status; NEVER raises (failure → the
-- index is marked 'failed' and the dispatcher keeps using brute force).
CREATE OR REPLACE FUNCTION rvbbit.pgvector_refresh_catalog(graph text DEFAULT 'db_catalog')
RETURNS jsonb LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_graph text := COALESCE(NULLIF(btrim(graph), ''), 'db_catalog');
    v_dim   int;
    v_n     bigint;
    v_mu    real[];
BEGIN
    IF to_regtype('vector') IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'pgvector not installed');
    END IF;

    -- Active dim = the most common embedding length in this graph.
    SELECT d, c INTO v_dim, v_n
      FROM (SELECT array_length(embedding, 1) AS d, count(*) AS c
              FROM rvbbit.catalog_docs
             WHERE graph_id = v_graph AND embedding IS NOT NULL
             GROUP BY array_length(embedding, 1)
             ORDER BY count(*) DESC
             LIMIT 1) q;
    IF v_dim IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'no embedded docs');
    END IF;

    INSERT INTO rvbbit.pgvector_indexes (graph_id, dim, status, refreshed_at)
         VALUES (v_graph, v_dim, 'building', now())
    ON CONFLICT (graph_id) DO UPDATE
       SET dim = EXCLUDED.dim, status = 'building', message = NULL, refreshed_at = now();

    BEGIN
        -- Corpus mean μ over the active-dim docs (one element per dimension).
        SELECT array_agg(m ORDER BY i) INTO v_mu
          FROM (SELECT t.i, avg(t.e)::real AS m
                  FROM rvbbit.catalog_docs d
                       CROSS JOIN LATERAL unnest(d.embedding) WITH ORDINALITY AS t(e, i)
                 WHERE d.graph_id = v_graph AND d.embedding IS NOT NULL
                   AND array_length(d.embedding, 1) = v_dim
                 GROUP BY t.i) z;

        -- (Re)create the centered-vector table at this dim (dynamic — dim-typed).
        EXECUTE 'DROP TABLE IF EXISTS rvbbit.catalog_vec';
        EXECUTE format(
            'CREATE TABLE rvbbit.catalog_vec ('
            || 'graph_id text NOT NULL, node_id bigint NOT NULL, '
            || 'vec vector(%s) NOT NULL, PRIMARY KEY (graph_id, node_id))', v_dim);

        -- Centered vectors (embedding - μ)::vector. Dynamic so `::vector` is opaque
        -- on a pgvector-less load; μ/graph/dim bound as params (no interpolation).
        EXECUTE $q$
            INSERT INTO rvbbit.catalog_vec (graph_id, node_id, vec)
            SELECT d.graph_id, d.node_id,
                   (SELECT array_agg(u.de - u.me ORDER BY u.i)
                      FROM unnest(d.embedding, $1) WITH ORDINALITY AS u(de, me, i))::vector
              FROM rvbbit.catalog_docs d
             WHERE d.graph_id = $2 AND d.embedding IS NOT NULL
               AND array_length(d.embedding, 1) = $3
        $q$ USING v_mu, v_graph, v_dim;

        EXECUTE 'CREATE INDEX catalog_vec_hnsw ON rvbbit.catalog_vec '
             || 'USING hnsw (vec vector_cosine_ops)';

        UPDATE rvbbit.pgvector_indexes
           SET mean = v_mu, n_docs = v_n, status = 'ready', message = NULL, refreshed_at = now()
         WHERE graph_id = v_graph;

        RETURN jsonb_build_object('ok', true, 'dim', v_dim, 'n_docs', v_n);
    EXCEPTION WHEN OTHERS THEN
        UPDATE rvbbit.pgvector_indexes
           SET status = 'failed', message = SQLERRM, refreshed_at = now()
         WHERE graph_id = v_graph;
        RETURN jsonb_build_object('ok', false, 'reason', SQLERRM);
    END;
END $fn$;

-- Tier-1 dense ranker: HNSW cosine over the centered vectors. Centers the query
-- by the SAME snapshot μ, so this equals the brute-force centered cosine. Returns
-- empty (→ dispatcher falls back) if not ready or dim-mismatched. The `<=>` /
-- `::vector` live in dynamic SQL so this loads without pgvector.
CREATE OR REPLACE FUNCTION rvbbit.catalog_dense_knn_pgvector(
    q_vec     real[],
    graph     text,
    kinds     text[],
    k         int,
    min_score float8 DEFAULT 0.10)
RETURNS TABLE (node_id bigint, score float8)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_mu  real[];
    v_dim int;
    v_qc  real[];
BEGIN
    IF q_vec IS NULL OR array_length(q_vec, 1) IS NULL THEN RETURN; END IF;
    IF to_regtype('vector') IS NULL OR to_regclass('rvbbit.catalog_vec') IS NULL THEN RETURN; END IF;

    SELECT pi.mean, pi.dim INTO v_mu, v_dim
      FROM rvbbit.pgvector_indexes pi
     WHERE pi.graph_id = graph AND pi.status = 'ready';
    IF v_mu IS NULL OR v_dim IS DISTINCT FROM array_length(q_vec, 1) THEN RETURN; END IF;

    -- center the query by the snapshot mean: q - μ
    SELECT array_agg(u.qe - u.me ORDER BY u.i) INTO v_qc
      FROM unnest(q_vec, v_mu) WITH ORDINALITY AS u(qe, me, i);

    -- Over-fetch the nearest by distance FIRST (pure HNSW order-by-limit, no
    -- predicate to confuse the index), THEN apply the kinds/floor filter in the
    -- outer query. This avoids HNSW filtered-scan under-return (where the nearest
    -- k get filtered out, yielding < k), keeping Tier-1 recall ≈ the brute-force
    -- tier without depending on pgvector 0.8 hnsw.iterative_scan.
    RETURN QUERY EXECUTE $q$
        SELECT s.node_id, s.score
          FROM (
            SELECT cv.node_id, d.kind,
                   (1.0 - (cv.vec <=> $1::vector))::float8 AS score
              FROM rvbbit.catalog_vec cv
              JOIN rvbbit.catalog_docs d
                ON d.graph_id = cv.graph_id AND d.node_id = cv.node_id
             WHERE cv.graph_id = $2
             ORDER BY cv.vec <=> $1::vector
             LIMIT $5 * 8
          ) s
         WHERE ($3::text[] IS NULL OR s.kind = ANY ($3))
           AND s.score > $4
         ORDER BY s.score DESC
         LIMIT $5
    $q$ USING v_qc, graph, kinds, min_score, k;
END $fn$;

-- Override the P2 dispatcher to add the readiness check: use Tier-1 only when
-- pgvector has a ready index AT THE QUERY'S DIM (so a dim-mismatched or unbuilt
-- index cleanly falls through to the brute-force tier instead of returning
-- nothing). Identical signature → drop-in.
CREATE OR REPLACE FUNCTION rvbbit.dense_knn_tiered(
    q_vec     real[],
    graph     text,
    kinds     text[],
    k         int,
    min_score float8 DEFAULT 0.10)
RETURNS TABLE (node_id bigint, score float8)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_dim int := array_length(q_vec, 1);
    v_hit boolean := false;
BEGIN
    IF q_vec IS NOT NULL AND rvbbit.pgvector_catalog_ready(graph, v_dim) THEN
        -- Try Tier-1, but NEVER let a pgvector-tier failure break search: any
        -- error (a bad idiom, the rvbbit router mishandling the dynamic SQL, …)
        -- falls through to the always-correct brute-force tier.
        BEGIN
            RETURN QUERY SELECT * FROM rvbbit.catalog_dense_knn_pgvector(q_vec, graph, kinds, k, min_score);
            v_hit := true;
        EXCEPTION WHEN OTHERS THEN
            v_hit := false;
        END;
        IF v_hit THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT * FROM rvbbit.catalog_dense_knn(q_vec, graph, kinds, k, min_score);
END $fn$;
