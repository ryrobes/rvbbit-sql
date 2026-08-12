//! Fair, bounded accelerator worker candidate claims.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0274_accel_worker_fair_claims.sql",
    name = "accel_worker_fair_claims",
    requires = ["bounded_heavy_maintenance_slots"]
);
