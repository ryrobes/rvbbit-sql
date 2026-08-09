//! Shared bounded concurrency for expensive accelerator maintenance.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0273_bounded_heavy_maintenance_slots.sql",
    name = "bounded_heavy_maintenance_slots",
    requires = ["unified_layout_tick"]
);
