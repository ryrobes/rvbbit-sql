//! Approximate distinct counts over per-group HLL sketches (RYR-291).
//!
//!   SELECT rvbbit.approx_distinct('hits'::regclass, 'URL');
//!
//! Reads `rvbbit.row_groups.stats[i].hll_b64` for every group that
//! covers the column, deserializes + unions, and returns the
//! cardinality estimate (~2.6% RMSE at precision 12).
//!
//! Only text columns get HLL state today; for numeric columns the
//! function returns NULL. Cross-group union is the whole point —
//! `SUM(distinct_estimate)` over per-group counts would overestimate
//! by the duplication factor.

use pgrx::prelude::*;
use rvbbit_storage::Hll;

/// HLL-based approximate distinct count for a text column.
/// Returns NULL if the column has no per-group sketches (e.g. numeric
/// columns, or row groups written before RYR-291).
#[pg_extern(stable, parallel_safe)]
fn approx_distinct(rel: pg_sys::Oid, col: &str) -> Option<i64> {
    let rel_oid = rel.to_u32();
    let col_esc = col.replace('\'', "''");
    let mut acc: Option<Hll> = None;

    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let sql = format!(
            "SELECT s->>'hll_b64' \
             FROM rvbbit.row_groups, \
                  jsonb_array_elements(stats) AS s \
             WHERE table_oid = {rel_oid}::oid \
               AND s->>'name' = '{col_esc}' \
               AND s->>'hll_b64' IS NOT NULL"
        );
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let b64: Option<String> = row.get(1)?;
            let Some(b64) = b64 else { continue };
            let Some(hll) = Hll::from_b64(&b64) else {
                continue;
            };
            match acc.as_mut() {
                None => acc = Some(hll),
                Some(a) => a.merge(&hll),
            }
        }
        Ok(())
    });

    acc.map(|h| h.count() as i64)
}
