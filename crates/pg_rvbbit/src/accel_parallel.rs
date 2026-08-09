//! Bounded parallel accelerator freshness workers.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0269_accel_tick_parallel_workers.sql",
    name = "accel_tick_parallel_workers",
    requires = ["routing_lock_isolation"]
);
