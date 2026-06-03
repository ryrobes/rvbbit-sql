//! Catalog drift: diff fingerprint snapshots across crawl runs.
//!
//! Read-only SQL over `rvbbit.catalog_snapshots` (written by `catalog_crawl`).
//! Surface: `catalog_runs_list`, `catalog_run_at`, `catalog_value_drift`,
//! `catalog_drift`, `catalog_drift_summary`, `catalog_object_history`.
//! See docs/CATALOG_KG_PLAN.md §11.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/catalog_drift.sql",
    name = "catalog_drift",
    requires = ["catalog_kg"]
);
