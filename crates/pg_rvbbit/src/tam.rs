//! Table Access Method registration.
//!
//! Phase 1 will populate a `pg_sys::TableAmRoutine` with callbacks that
//! route INSERT/UPDATE/DELETE through the catcher heap and reads through
//! the custom scan node in `scan.rs`. Phase 0 is intentionally empty —
//! we just want the extension to load.
