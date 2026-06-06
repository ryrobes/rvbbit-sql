//! Self-introspecting catalog knowledge graph + free-text data search.
//!
//! Crawls user tables, fingerprints them (structural stats + example distinct
//! values), and materializes a `db_catalog` knowledge graph using the existing
//! KG primitives (`kg_assert_node` / `kg_assert_edge` / `kg_link_evidence`),
//! plus a deterministic fingerprint-document store (`rvbbit.catalog_docs`) that
//! powers `rvbbit.data_search(query, k)`.
//!
//! The implementation is pure SQL/PLpgSQL kept in `sql/catalog_kg.sql` so it is
//! both loadable standalone (`psql -f`) for dev iteration and compiled into the
//! extension here. See docs/CATALOG_KG_PLAN.md.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/catalog_kg.sql",
    name = "catalog_kg",
    // triples_bootstrap: data_crawl() calls rvbbit.triples_row() from that block.
    requires = ["rvbbit_bootstrap", "kg_bootstrap", "create_embedding_cache", "triples_bootstrap"]
);
