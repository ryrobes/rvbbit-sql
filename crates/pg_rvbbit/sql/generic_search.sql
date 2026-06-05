-- =====================================================================
-- Generic hybrid search — the turnkey "semantic + keyword over ANY table
-- column" primitive (Track B, P1).
--
-- Promotes the catalog's RRF hybrid (sql/catalog_kg.sql) to work over an
-- arbitrary (relation, text column): the DENSE side reuses rvbbit.knn_text
-- (JIT-embedded + cached, Lance-accelerated when a text index exists), the
-- LEXICAL side is Postgres FTS + literal substring over the column's own
-- values, and the two are fused by Reciprocal Rank Fusion — rank-based, so
-- the raw-cosine anisotropy never has to be calibrated against ts_rank.
--
-- Pure SQL/PLpgSQL: loadable standalone (psql -f) and compiled into the
-- extension via src/generic_search.rs.
-- =====================================================================

-- Lexical ranker over an arbitrary text column. The column's distinct values
-- ARE the documents (no fingerprint doc here). position() = literal substring
-- (no LIKE wildcard interpretation, so 'patient_id' can't wildcard-match).
CREATE OR REPLACE FUNCTION rvbbit.lexical_knn(
    rel      regclass,
    text_col text,
    query    text,
    k        int)
RETURNS TABLE (value text, score float8)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_q text := btrim(COALESCE(query, ''));
BEGIN
    IF v_q = '' THEN RETURN; END IF;
    -- Identifiers via quote_ident; the query text via %L (format's literal
    -- quoting — injection-safe). We do NOT use a bound $1 + EXECUTE ... USING
    -- here: rvbbit's planner/parse hooks break dynamic parameter binding for
    -- some EXECUTE'd statements ("there is no parameter $1"), so the query text
    -- is inlined as a quoted literal instead. k is int-typed.
    RETURN QUERY EXECUTE format($q$
        WITH lex AS (
            SELECT DISTINCT %1$s::text AS value
              FROM %2$s
             WHERE %1$s IS NOT NULL
        ), scored AS (
            SELECT value,
                   ts_rank_cd(to_tsvector('english', value),
                              websearch_to_tsquery('english', %4$L)) AS fts,
                   (position(lower(%4$L) IN lower(value)) > 0)::int   AS hit
              FROM lex
        )
        SELECT value, (2.0 * hit + fts)::float8 AS sc
          FROM scored
         WHERE fts > 0 OR hit > 0
         ORDER BY sc DESC
         LIMIT %3$s
    $q$, quote_ident(text_col), rel::text, k, v_q);
END $fn$;

-- Hybrid search over (rel, text_col): RRF-fuse the dense (knn_text) and lexical
-- rankers. Returns the component scores alongside the fused relevance so callers
-- can inspect/threshold either signal. dense_score / lex_score are 0.0 (NOT null)
-- when that signal did not rank the value, so a caller's `WHERE dense_score > x`
-- behaves deterministically instead of silently dropping lexical-only hits.
-- VOLATILE because knn_text writes the embedding cache on first sight of a value.
--
-- NOTE: the dense side (knn_text) is RAW cosine — NOT mean-centered like the
-- catalog path — so `dense_floor` is a raw-cosine threshold (default 0.0 = off;
-- an absolute raw-cosine floor is model-specific, hence not defaulted on).
-- Centering the generic dense tier is a Track-B follow-up (ANN/seam).
CREATE OR REPLACE FUNCTION rvbbit.hybrid_search(
    rel         regclass,
    text_col    text,
    query       text,
    k           int    DEFAULT 20,
    specialist  text   DEFAULT '',
    dense_floor float8 DEFAULT 0.0)
RETURNS TABLE (value text, dense_score float8, lex_score float8, fused_score float8)
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_pool int    := GREATEST(k * 4, 50);  -- candidate pool per ranker
    v_rrf  float8 := 60;                    -- RRF damping constant
BEGIN
    RETURN QUERY
        WITH d AS (
            SELECT dk.value, dk.score AS dscore,
                   row_number() OVER (ORDER BY dk.score DESC) AS r
              FROM rvbbit.knn_text(rel, text_col, query, v_pool, specialist) dk
             WHERE dk.score >= dense_floor
        ),
        l AS (
            SELECT lk.value, lk.score AS lscore,
                   row_number() OVER (ORDER BY lk.score DESC) AS r
              FROM rvbbit.lexical_knn(rel, text_col, query, v_pool) lk
        ),
        fused AS (
            SELECT COALESCE(d.value, l.value)      AS value,
                   COALESCE(d.dscore, 0.0::float8) AS dscore,
                   COALESCE(l.lscore, 0.0::float8) AS lscore,
                   COALESCE(1.0 / (v_rrf + d.r), 0) + COALESCE(1.0 / (v_rrf + l.r), 0) AS rrf
              FROM d FULL OUTER JOIN l ON d.value = l.value
        )
        SELECT f.value, f.dscore, f.lscore, f.rrf
          FROM fused f
         -- deterministic tiebreak: on an RRF tie, prefer the value with the
         -- stronger (precise) lexical signal over uncalibrated dense noise,
         -- then by value so the order is stable.
         ORDER BY f.rrf DESC, f.lscore DESC, f.value
         LIMIT k;
END $fn$;
