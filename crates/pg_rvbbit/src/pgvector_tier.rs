//! pgvector HNSW tier (Track B, P4) — Tier-1 behind `rvbbit.dense_knn_tiered`.
//!
//! pgvector is a PEER ANN index over the canonical `catalog_docs.embedding
//! real[]` store: `rvbbit.catalog_vec` holds store-time-centered vectors at the
//! active dim with an HNSW cosine index. Centering is uniform, so Tier-1 cosine
//! rank order equals the brute-force tier (latency differs, relevance does not).
//!
//! pgvector is SOFT-required (auto-created when available, never hard-failing),
//! and the SQL is defensive: every `vector`/`<=>`/HNSW reference is in dynamic
//! SQL so the functions load on a pgvector-less box, and a refresh failure marks
//! the index 'failed' → the dispatcher falls back to brute force.
//!
//! Pure SQL/PLpgSQL kept in `sql/pgvector_tier.sql` so it is both loadable
//! standalone (`psql -f`) and compiled into the extension here.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/pgvector_tier.sql",
    name = "pgvector_tier",
    requires = ["rvbbit_bootstrap", "catalog_kg"]
);
