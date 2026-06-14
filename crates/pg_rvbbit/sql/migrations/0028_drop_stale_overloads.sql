-- 0028_drop_stale_overloads — remove stale function overloads left behind by CREATE OR REPLACE
-- with a WIDENED signature.
--
-- CREATE OR REPLACE FUNCTION with extra trailing args does NOT replace the narrower function — it
-- creates a SECOND overload, and the prior one persists. On databases upgraded through those
-- migrations both overloads survive; on a fresh install only the current (wide) one is ever created.
-- This migration deletes the orphans so upgraded and fresh databases converge.
--
-- 1) refine_proposal: 0020_proposal_categories widened it (+p_category, +p_subcategory) without
--    dropping the prior 9-arg signature. Both then matched a 9-arg call. psycopg sends a Python int
--    id as the NARROWEST int type (smallint) and strings/NULLs as 'unknown', so the MCP's 9-arg call
--    became "function rvbbit.refine_proposal(smallint, unknown, ...) is not unique" (PG can't pick a
--    best candidate when two overloads differ only by trailing DEFAULTed args). Dropping the 9-arg
--    makes every shorter caller resolve uniquely to the 11-arg superset (identical body, plus the two
--    category fields default NULL). The lens already calls it with 11 literal args and is unaffected.
--    NOTE: the orthogonal double-precision->real mismatch on p_confidence (a Python float arrives as
--    double precision, which has no implicit cast to real) is fixed in the MCP wrapper with %s::real,
--    not here — dropping the overload does not address it.
--
-- 2) cube_enrich_valid: an old (raw jsonb) validator signature, superseded by the correct
--    (output text, inputs jsonb) contract. The validator dispatcher (validator.rs) only ever emits a
--    2-arg call, so the 1-arg overload is unreachable, and pg_depend shows zero dependents — dead
--    cleanup, not a behavior change.
--
-- Idempotent: IF EXISTS with the exact stale signatures. A no-op on a fresh install (these never
-- exist there) and on any DB where they were already removed.

DROP FUNCTION IF EXISTS rvbbit.refine_proposal(bigint, text, text, text, text, jsonb, text, text, real);
DROP FUNCTION IF EXISTS rvbbit.cube_enrich_valid(jsonb);
