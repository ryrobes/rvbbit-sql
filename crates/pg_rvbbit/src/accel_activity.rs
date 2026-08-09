//! Accelerator maintenance lanes and their observable activity surface.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0266_accel_activity_lanes.sql",
    name = "accel_activity_lanes",
    requires = ["accel_tick_layer3"]
);
