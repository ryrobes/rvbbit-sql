//! Catcher heap — the row-oriented write absorber.
//!
//! For every rvbbit table `tbl`, we create a shadow heap table
//! `rvbbit.catcher_<oid>` with the same column shape. INSERT/UPDATE/DELETE
//! against `tbl` are translated to operations on the catcher, which gives
//! us PG's normal MVCC and crash safety for free.
//!
//! `rvbbit_compact(tbl)` drains catcher rows into a new immutable parquet
//! row group and truncates the catcher.
//!
//! Phase 1 stub.
