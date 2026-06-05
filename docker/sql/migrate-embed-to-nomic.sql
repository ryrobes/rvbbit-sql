-- =====================================================================
-- Migrate the local 'embed' backend from bge-small-en-v1.5 (384d) to
-- nomic-embed-text-v1.5 (768d)  —  Track B, P5.
--
-- ONLINE migration: thanks to the dim-match guard in catalog_dense_knn, a 768d
-- query simply ignores leftover 384d docs (degrading to LEXICAL-ONLY for them)
-- between steps 1 and 3 — search keeps working the whole time, it just loses the
-- semantic signal for not-yet-re-embedded docs until the re-crawl completes.
--
-- Requires the rebuilt extension (the P3/P5 `embed(text, specialist, mode)`
-- signature + the nomic default). Re-run any time; idempotent.
-- =====================================================================

-- 1. Point the 'embed' backend at nomic-embed-text-v1.5 and reload. Inlined
--    (rather than \i register-local-embed.sql, which would resolve against
--    psql's CWD) so this script runs from anywhere.
SELECT rvbbit.register_backend(
    backend_name        => 'embed',
    backend_endpoint    => 'local://embed',
    backend_transport   => 'local_embed',
    backend_batch_size  => 128,
    backend_max_concur  => 1,
    backend_timeout_ms  => 120000,
    backend_opts        => '{"model":"nomic-embed-text-v1.5"}'::jsonb,
    backend_description => 'Default local CPU text embedding backend (nomic-embed-text-v1.5, 768d).'
);
SELECT rvbbit.reload_backends();

-- 2. Drop stale 384d cache entries (optional — the nomic 'search_document:'
--    prefix already changes the cache key, so old entries would just be ignored;
--    purging keeps the cache clean and the corpus single-dim).
SELECT rvbbit.embedding_purge('embed') AS purged_entries;

-- 3. Re-embed the catalog at 768d (documents now embed in 'document' mode →
--    nomic's search_document: prefix). This overwrites catalog_docs.embedding.
SELECT rvbbit.catalog_crawl(
    schemas  => NULL,
    graph    => 'db_catalog',
    do_embed => true
) AS recrawl;

-- 4. If pgvector is installed, rebuild the HNSW tier at the new dim (768).
--    No-op / harmless when pgvector is absent.
SELECT rvbbit.pgvector_refresh_catalog('db_catalog') AS pgvector_refresh;

-- 5. Verify: the active model + the catalog's embedding dim should now be 768.
SELECT (transport_opts->>'model') AS model FROM rvbbit.backends WHERE name = 'embed';
SELECT DISTINCT array_length(embedding, 1) AS dim
FROM rvbbit.catalog_docs
WHERE graph_id = 'db_catalog' AND embedding IS NOT NULL;
