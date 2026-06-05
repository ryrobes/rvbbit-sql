//! Generic hybrid search — semantic + keyword retrieval over ANY table column.
//!
//! Promotes the catalog's RRF hybrid (`sql/catalog_kg.sql`) into a table-generic
//! primitive: `rvbbit.hybrid_search(rel, col, query, k)` fuses the dense ranker
//! (`rvbbit.knn_text`, cache + Lance accelerated) with a Postgres-FTS lexical
//! ranker over the column's own values, by Reciprocal Rank Fusion.
//!
//! Pure SQL/PLpgSQL kept in `sql/generic_search.sql` so it is both loadable
//! standalone (`psql -f`) for dev iteration and compiled into the extension here.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/generic_search.sql",
    name = "generic_search",
    requires = ["rvbbit_bootstrap", "create_embedding_cache"]
);
