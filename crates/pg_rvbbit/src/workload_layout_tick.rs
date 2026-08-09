//! Accepted cluster/Hive layout reconciliation and bounded worker slots.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

#[allow(unused_imports)]
use crate::compact::refresh_workload_layout_variants;

extension_sql_file!(
    "../sql/migrations/0270_workload_layout_tick.sql",
    name = "workload_layout_tick",
    requires = [
        "accel_tick_parallel_workers",
        refresh_workload_layout_variants
    ]
);
