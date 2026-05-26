//! Phase 2 slice 4: delete_log → roaring-bitmap projection.
//!
//! The `rvbbit.delete_log` catalog table stores per-row tombstones at
//! (table_oid, rg_id, ordinal) with a deleted_generation. At scan time
//! we materialize the relevant entries into a HashMap<rg_id, RoaringBitmap>
//! that the custom scan node ANDs against its read iterator — rows whose
//! (rg_id, row_in_batch) bit is set are skipped.
//!
//! When `asof` is `Some(g)`, tombstones with `deleted_generation > g`
//! are excluded (they're in the future from the AS OF reader's view).
//! `asof = None` applies every tombstone, which is the default "latest"
//! semantics.

use std::collections::HashMap;

use pgrx::Spi;
use roaring::RoaringBitmap;

/// Load the tombstone bitmap for a single rvbbit table, optionally
/// narrowed to an AS OF generation. Returns an empty map when there are
/// no applicable tombstones — that's the common case and should be cheap.
///
/// SPI failures are returned as Err(String) so the caller can decide
/// whether to fail-open (treat as "no tombstones") or hard-error.
pub(crate) fn load_for_table(
    table_oid: u32,
    asof: Option<i64>,
) -> Result<HashMap<i64, RoaringBitmap>, String> {
    // Quick exit: if the delete_log catalog table doesn't exist (older
    // extension version) or there are no tombstones for this table at all,
    // skip the full query.
    let count: i64 = Spi::get_one::<i64>(&format!(
        "SELECT count(*)::bigint FROM rvbbit.delete_log \
         WHERE table_oid = {table_oid}::oid \
           {asof_filter}",
        asof_filter = match asof {
            Some(g) => format!("AND deleted_generation <= {g}"),
            None => String::new(),
        }
    ))
    .map_err(|e| format!("delete_log count SPI: {e}"))?
    .unwrap_or(0);
    if count == 0 {
        return Ok(HashMap::new());
    }

    let mut out: HashMap<i64, RoaringBitmap> = HashMap::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let sql = format!(
            "SELECT rg_id, ordinal FROM rvbbit.delete_log \
             WHERE table_oid = {table_oid}::oid \
               {asof_filter} \
             ORDER BY rg_id, ordinal",
            asof_filter = match asof {
                Some(g) => format!("AND deleted_generation <= {g}"),
                None => String::new(),
            }
        );
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let rg_id: i64 = row.get::<i64>(1)?.unwrap_or(-1);
            let ordinal: i32 = row.get::<i32>(2)?.unwrap_or(-1);
            if rg_id < 0 || ordinal < 0 {
                continue;
            }
            // RoaringBitmap is u32-keyed. Per-row-group ordinals are bounded
            // by the row group's row count, which the compactor caps at
            // ~1M — comfortably inside u32 range.
            out.entry(rg_id).or_default().insert(ordinal as u32);
        }
        Ok(())
    })
    .map_err(|e| format!("delete_log scan SPI: {e}"))?;

    Ok(out)
}

/// Convenience: total tombstone count across the per-rg bitmaps. Useful
/// for telemetry / sanity checks without a separate SPI roundtrip.
#[allow(dead_code)]
pub(crate) fn total_count(bitmaps: &HashMap<i64, RoaringBitmap>) -> u64 {
    bitmaps.values().map(|b| b.len()).sum()
}
