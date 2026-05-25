-- pg_rvbbit 0.1.0 -> 0.2.0
-- Adds the first slice of the local model tier (RYR-289): OpenAI BPE
-- token counts via tiktoken-rs. Cheapest tier — no model files, no
-- network. Feeds EXPLAIN SEMANTIC (RYR-290) cost previews.

CREATE FUNCTION rvbbit.token_count(text TEXT, encoding TEXT DEFAULT 'cl100k_base')
RETURNS INT
IMMUTABLE STRICT PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'token_count_wrapper';

CREATE FUNCTION rvbbit.tokenize(text TEXT, encoding TEXT DEFAULT 'cl100k_base')
RETURNS INT[]
IMMUTABLE STRICT PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'tokenize_wrapper';

CREATE FUNCTION rvbbit.token_encodings()
RETURNS TEXT[]
IMMUTABLE STRICT PARALLEL SAFE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'token_encodings_wrapper';
