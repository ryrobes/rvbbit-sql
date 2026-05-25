//! Delete log → in-memory bitmap projection.
//!
//! At scan time we query `rvbbit.delete_log` for entries with
//! `deleted_xid` visible under the current snapshot, group by `rg_id`,
//! and build a `roaring::RoaringBitmap` per row group. The custom scan
//! node ANDs that bitmap against the row group's read iterator.
//!
//! Phase 3 stub.
