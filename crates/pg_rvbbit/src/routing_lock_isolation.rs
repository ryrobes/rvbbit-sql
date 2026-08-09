//! Relation-local router invalidation and safe accelerator heartbeat defaults.
//!
//! The same idempotent SQL is embedded for fresh extension installs and also
//! registered as a schema migration for upgraded installations.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/migrations/0268_routing_lock_isolation.sql",
    name = "routing_lock_isolation",
    requires = ["accel_activity_lanes"]
);
