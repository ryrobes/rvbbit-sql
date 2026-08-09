//! Unified automatic and governed derived-layout maintenance.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0272_unified_layout_tick.sql",
    name = "unified_layout_tick",
    requires = ["accel_tick_worker_slots"]
);
