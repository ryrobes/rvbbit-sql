//! Wider freshness worker cohorts and transaction-isolated serial passes.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0271_accel_tick_worker_slots.sql",
    name = "accel_tick_worker_slots",
    requires = ["workload_layout_tick"]
);
