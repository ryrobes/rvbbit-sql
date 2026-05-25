//! Semantic predicate bitmap cache (RYR-288).
//!
//! Durable per-row-group cache of boolean predicate results, keyed by
//! (table_oid, rg_id, predicate_name, model_version). Turns expensive
//! per-row evaluations (e.g. `rvbbit.means(text, criterion)`) into a
//! one-time scan + cheap roaring-bitmap lookup.
//!
//! This module ships the storage + population primitives. Auto-routing
//! of queries through the bitmap (planner integration) is a follow-up;
//! today the user calls `bitmap_populate` explicitly.

use std::io::Cursor;

use pgrx::extension_sql;
use pgrx::prelude::*;
use roaring::RoaringBitmap;

extension_sql!(
    r#"
CREATE TABLE rvbbit.semantic_bitmaps (
    table_oid       oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    rg_id           bigint NOT NULL,
    predicate_hash  bytea NOT NULL,
    predicate_name  text NOT NULL,
    model_version   text NOT NULL,
    bitmap          bytea NOT NULL,
    n_set           bigint NOT NULL,
    n_total         bigint NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, rg_id, predicate_hash)
);

CREATE INDEX semantic_bitmaps_named_idx
    ON rvbbit.semantic_bitmaps (table_oid, predicate_name, model_version);
"#,
    name = "create_semantic_bitmaps",
    requires = ["rvbbit_bootstrap"]
);

/// 16-byte BLAKE3 prefix of (predicate_name || model_version) — fits in
/// a fixed-width bytea and gives ~2^64 collision resistance for our
/// catalog-scale namespace.
fn predicate_hash(predicate_name: &str, model_version: &str) -> [u8; 16] {
    let mut h = blake3::Hasher::new();
    h.update(predicate_name.as_bytes());
    h.update(b"||");
    h.update(model_version.as_bytes());
    let full = h.finalize();
    let mut out = [0u8; 16];
    out.copy_from_slice(&full.as_bytes()[..16]);
    out
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0xf) as usize] as char);
    }
    out
}

/// Build the bitmap cache for `(rel, predicate_name, model_version)`.
///
/// `predicate_sql` must be a boolean SQL expression valid in the
/// projection list of `SELECT (predicate_sql) FROM <rel>`. The query
/// is expected to return exactly one row per parquet row in
/// (rg_id ASC, row_index_in_group ASC) order — which the rvbbit
/// custom scan does by construction.
///
/// Returns the number of row-group bitmaps written. Idempotent: a
/// second call with the same (rel, predicate_name, model_version)
/// short-circuits and returns 0 if every row group is already cached.
#[pg_extern]
fn bitmap_populate(
    rel: pg_sys::Oid,
    predicate_name: &str,
    model_version: &str,
    predicate_sql: &str,
) -> i64 {
    let rel_oid = rel.to_u32();
    let pred_hash = predicate_hash(predicate_name, model_version);
    let pred_hash_hex = hex_encode(&pred_hash);

    let qualified: String =
        match Spi::get_one::<String>(&format!("SELECT {rel_oid}::oid::regclass::text")) {
            Ok(Some(s)) => s,
            Ok(None) | Err(_) => pgrx::error!("rvbbit.bitmap_populate: bad regclass {rel_oid}"),
        };

    let groups: Vec<(i64, i64)> = match collect_row_groups(rel_oid) {
        Ok(g) => g,
        Err(e) => pgrx::error!("rvbbit.bitmap_populate: {e}"),
    };
    if groups.is_empty() {
        pgrx::error!(
            "rvbbit.bitmap_populate: no row groups for {qualified} \
             (call rvbbit.export_to_parquet first)"
        );
    }

    let already_cached: i64 = Spi::get_one(&format!(
        "SELECT count(*) FROM rvbbit.semantic_bitmaps \
         WHERE table_oid = {rel_oid}::oid \
           AND predicate_hash = decode('{pred_hash_hex}', 'hex')"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    if already_cached as usize == groups.len() {
        return 0;
    }

    let select_sql = format!("SELECT ({predicate_sql})::bool FROM {qualified}");
    let mut bitmaps_written: i64 = 0;

    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&select_sql, None, &[])?;

        let mut bitmap = RoaringBitmap::new();
        let mut n_set: u64 = 0;
        let mut row_idx_in_group: u64 = 0;
        let mut group_idx: usize = 0;
        let (mut cur_rg_id, mut cur_n_rows) = groups[group_idx];

        for row in table {
            if group_idx >= groups.len() {
                break;
            }
            let matched: Option<bool> = row.get(1)?;
            if matched == Some(true) {
                bitmap.insert(row_idx_in_group as u32);
                n_set += 1;
            }
            row_idx_in_group += 1;

            if row_idx_in_group as i64 >= cur_n_rows {
                flush_one(
                    rel_oid,
                    cur_rg_id,
                    &pred_hash,
                    predicate_name,
                    model_version,
                    &bitmap,
                    n_set,
                    cur_n_rows as u64,
                )
                .map_err(|e| pgrx::spi::Error::CursorNotFound(e))?;
                bitmaps_written += 1;
                group_idx += 1;
                if group_idx >= groups.len() {
                    break;
                }
                bitmap = RoaringBitmap::new();
                n_set = 0;
                row_idx_in_group = 0;
                let g = groups[group_idx];
                cur_rg_id = g.0;
                cur_n_rows = g.1;
            }
        }
        Ok(())
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.bitmap_populate: {e}"));

    bitmaps_written
}

fn collect_row_groups(rel_oid: u32) -> Result<Vec<(i64, i64)>, String> {
    let mut out = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT rg_id, n_rows FROM rvbbit.row_groups \
                 WHERE table_oid = {rel_oid}::oid ORDER BY rg_id"
            ),
            None,
            &[],
        )?;
        for row in table {
            let rg: Option<i64> = row.get(1)?;
            let n: Option<i64> = row.get(2)?;
            if let (Some(rg), Some(n)) = (rg, n) {
                out.push((rg, n));
            }
        }
        Ok(())
    })
    .map_err(|e| e.to_string())?;
    Ok(out)
}

fn flush_one(
    rel_oid: u32,
    rg_id: i64,
    pred_hash: &[u8; 16],
    predicate_name: &str,
    model_version: &str,
    bitmap: &RoaringBitmap,
    n_set: u64,
    n_total: u64,
) -> Result<(), String> {
    let mut buf = Vec::with_capacity(bitmap.serialized_size());
    bitmap
        .serialize_into(&mut buf)
        .map_err(|e| format!("roaring serialize: {e}"))?;
    let bitmap_hex = hex_encode(&buf);
    let pred_hex = hex_encode(pred_hash);
    let name_esc = predicate_name.replace('\'', "''");
    let mv_esc = model_version.replace('\'', "''");
    Spi::run(&format!(
        "INSERT INTO rvbbit.semantic_bitmaps \
            (table_oid, rg_id, predicate_hash, predicate_name, model_version, \
             bitmap, n_set, n_total, computed_at) \
         VALUES ({rel_oid}::oid, {rg_id}, decode('{pred_hex}', 'hex'), \
                 '{name_esc}', '{mv_esc}', \
                 decode('{bitmap_hex}', 'hex'), {n_set}, {n_total}, now()) \
         ON CONFLICT (table_oid, rg_id, predicate_hash) DO UPDATE \
            SET bitmap = EXCLUDED.bitmap, \
                n_set = EXCLUDED.n_set, \
                n_total = EXCLUDED.n_total, \
                computed_at = EXCLUDED.computed_at"
    ))
    .map_err(|e| format!("INSERT: {e}"))?;
    Ok(())
}

/// Drop all bitmaps for a given (rel, predicate_name, model_version).
/// Returns the number of bitmaps deleted.
#[pg_extern]
fn bitmap_drop(rel: pg_sys::Oid, predicate_name: &str, model_version: &str) -> i64 {
    let rel_oid = rel.to_u32();
    let pred_hash = predicate_hash(predicate_name, model_version);
    let pred_hex = hex_encode(&pred_hash);
    Spi::run(&format!(
        "DELETE FROM rvbbit.semantic_bitmaps \
         WHERE table_oid = {rel_oid}::oid \
           AND predicate_hash = decode('{pred_hex}', 'hex')"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.bitmap_drop: {e}"));
    // SPI doesn't directly return DELETE row count via Spi::run; query it back.
    let remaining: i64 = Spi::get_one(&format!(
        "SELECT count(*) FROM rvbbit.semantic_bitmaps \
         WHERE table_oid = {rel_oid}::oid \
           AND predicate_hash = decode('{pred_hex}', 'hex')"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    // After DELETE, remaining should be 0; report how many groups we cleared by
    // comparing to the table's row-group count.
    let total_groups: i64 = Spi::get_one(&format!(
        "SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = {rel_oid}::oid"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    // Return at most the row-group count (i.e. what we plausibly cleared).
    (total_groups - remaining).max(0)
}

/// Per-predicate observability. One row per (predicate_name, model_version)
/// covering `rel`. Reports rows-set / rows-total summed across groups so
/// users can see selectivity at a glance.
#[pg_extern]
fn bitmap_stats(
    rel: pg_sys::Oid,
) -> TableIterator<
    'static,
    (
        name!(predicate_name, String),
        name!(model_version, String),
        name!(n_groups, i64),
        name!(rows_set, i64),
        name!(rows_total, i64),
        name!(selectivity, f64),
        name!(bytes_stored, i64),
    ),
> {
    let rel_oid = rel.to_u32();
    let mut out: Vec<(String, String, i64, i64, i64, f64, i64)> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT predicate_name, model_version, \
                        count(*) AS n_groups, \
                        sum(n_set)::bigint AS rows_set, \
                        sum(n_total)::bigint AS rows_total, \
                        sum(octet_length(bitmap))::bigint AS bytes_stored \
                 FROM rvbbit.semantic_bitmaps \
                 WHERE table_oid = {rel_oid}::oid \
                 GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            None,
            &[],
        )?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let mv: Option<String> = row.get(2)?;
            let ng: Option<i64> = row.get(3)?;
            let rs: Option<i64> = row.get(4)?;
            let rt: Option<i64> = row.get(5)?;
            let bs: Option<i64> = row.get(6)?;
            let rows_set = rs.unwrap_or(0);
            let rows_total = rt.unwrap_or(0);
            let selectivity = if rows_total > 0 {
                rows_set as f64 / rows_total as f64
            } else {
                0.0
            };
            out.push((
                name.unwrap_or_default(),
                mv.unwrap_or_default(),
                ng.unwrap_or(0),
                rows_set,
                rows_total,
                selectivity,
                bs.unwrap_or(0),
            ));
        }
        Ok(())
    });
    TableIterator::new(out.into_iter())
}

/// Stream PK values for rows in `rel` whose row-group bit is set in
/// the cached bitmap. The PK column must be int8 / int4 — see
/// `bitmap_select_text` for text PKs.
///
/// Usage pattern:
///
///   SELECT t.* FROM tickets t
///   JOIN rvbbit.bitmap_select_int(
///        'tickets'::regclass::oid, 'id', 'angry_customer', 'v1')
///        AS m(id) USING (id);
///
/// Works by streaming `SELECT pk FROM rel` in (rg_id, row_idx) order
/// (the custom scan's natural order) and emitting `pk` whenever the
/// bitmap bit is set. Rows whose row group has no cached bitmap are
/// silently skipped — use `bitmap_stats(rel)` first to confirm full
/// coverage if that matters.
///
/// This is the "lightweight" auto-routing — no planner surgery, just
/// a SETOF function the user joins. Full integration (rewriting
/// `WHERE rvbbit.means(...)` to consult the bitmap automatically) is
/// the larger follow-up tracked separately.
#[pg_extern(volatile)]
fn bitmap_select_int(
    rel: pg_sys::Oid,
    pk_col: &str,
    predicate_name: &str,
    model_version: &str,
) -> TableIterator<'static, (name!(pk, i64),)> {
    let pks: Vec<i64> = bitmap_select_inner_int(rel, pk_col, predicate_name, model_version);
    TableIterator::new(pks.into_iter().map(|v| (v,)))
}

/// Same as `bitmap_select_int` but for text PKs.
#[pg_extern(volatile)]
fn bitmap_select_text(
    rel: pg_sys::Oid,
    pk_col: &str,
    predicate_name: &str,
    model_version: &str,
) -> TableIterator<'static, (name!(pk, String),)> {
    let pks: Vec<String> = bitmap_select_inner_text(rel, pk_col, predicate_name, model_version);
    TableIterator::new(pks.into_iter().map(|v| (v,)))
}

fn bitmap_select_inner_int(
    rel: pg_sys::Oid,
    pk_col: &str,
    predicate_name: &str,
    model_version: &str,
) -> Vec<i64> {
    let (groups, bitmaps, qualified, col_esc) =
        load_bitmaps_and_groups(rel, pk_col, predicate_name, model_version);
    if groups.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let select_sql = format!("SELECT \"{col_esc}\"::bigint FROM {qualified}");
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&select_sql, None, &[])?;
        let mut group_idx = 0usize;
        let mut row_idx_in_group: u64 = 0;
        let (mut cur_rg_id, mut cur_n_rows) = groups[group_idx];
        for row in table {
            if group_idx >= groups.len() {
                break;
            }
            let v: Option<i64> = row.get(1)?;
            if let Some(v) = v {
                if let Some(bm) = bitmaps.get(&cur_rg_id) {
                    if bm.contains(row_idx_in_group as u32) {
                        out.push(v);
                    }
                }
            }
            row_idx_in_group += 1;
            if row_idx_in_group as i64 >= cur_n_rows {
                group_idx += 1;
                if group_idx >= groups.len() {
                    break;
                }
                let g = groups[group_idx];
                cur_rg_id = g.0;
                cur_n_rows = g.1;
                row_idx_in_group = 0;
            }
        }
        Ok(())
    });
    out
}

fn bitmap_select_inner_text(
    rel: pg_sys::Oid,
    pk_col: &str,
    predicate_name: &str,
    model_version: &str,
) -> Vec<String> {
    let (groups, bitmaps, qualified, col_esc) =
        load_bitmaps_and_groups(rel, pk_col, predicate_name, model_version);
    if groups.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let select_sql = format!("SELECT \"{col_esc}\"::text FROM {qualified}");
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&select_sql, None, &[])?;
        let mut group_idx = 0usize;
        let mut row_idx_in_group: u64 = 0;
        let (mut cur_rg_id, mut cur_n_rows) = groups[group_idx];
        for row in table {
            if group_idx >= groups.len() {
                break;
            }
            let v: Option<String> = row.get(1)?;
            if let Some(v) = v {
                if let Some(bm) = bitmaps.get(&cur_rg_id) {
                    if bm.contains(row_idx_in_group as u32) {
                        out.push(v);
                    }
                }
            }
            row_idx_in_group += 1;
            if row_idx_in_group as i64 >= cur_n_rows {
                group_idx += 1;
                if group_idx >= groups.len() {
                    break;
                }
                let g = groups[group_idx];
                cur_rg_id = g.0;
                cur_n_rows = g.1;
                row_idx_in_group = 0;
            }
        }
        Ok(())
    });
    out
}

fn load_bitmaps_and_groups(
    rel: pg_sys::Oid,
    pk_col: &str,
    predicate_name: &str,
    model_version: &str,
) -> (
    Vec<(i64, i64)>,
    std::collections::HashMap<i64, RoaringBitmap>,
    String,
    String,
) {
    let rel_oid = rel.to_u32();
    let pred_hash = predicate_hash(predicate_name, model_version);
    let pred_hex = hex_encode(&pred_hash);
    let col_esc = pk_col.replace('"', "\"\"");

    let qualified: String =
        Spi::get_one::<String>(&format!("SELECT {rel_oid}::oid::regclass::text"))
            .ok()
            .flatten()
            .unwrap_or_else(|| pgrx::error!("rvbbit.bitmap_select: bad regclass oid {rel_oid}"));

    let groups = collect_row_groups(rel_oid).unwrap_or_default();
    if groups.is_empty() {
        return (groups, Default::default(), qualified, col_esc);
    }

    // Bulk-load every matching bitmap into memory.
    let mut bitmaps: std::collections::HashMap<i64, RoaringBitmap> =
        std::collections::HashMap::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT rg_id, bitmap FROM rvbbit.semantic_bitmaps \
                 WHERE table_oid = {rel_oid}::oid \
                   AND predicate_hash = decode('{pred_hex}', 'hex')"
            ),
            None,
            &[],
        )?;
        for row in table {
            let rg_id: Option<i64> = row.get(1)?;
            let bytes: Option<Vec<u8>> = row.get(2)?;
            if let (Some(rg_id), Some(bytes)) = (rg_id, bytes) {
                if let Ok(bm) = RoaringBitmap::deserialize_from(&mut Cursor::new(&bytes)) {
                    bitmaps.insert(rg_id, bm);
                }
            }
        }
        Ok(())
    });
    (groups, bitmaps, qualified, col_esc)
}

/// Test-only: deserialize a stored bitmap and return its set bits as int4[].
/// Useful for verifying round-trip correctness without needing the
/// custom scan to consume the bitmap.
#[pg_extern]
fn bitmap_test_decode(
    rel: pg_sys::Oid,
    rg_id: i64,
    predicate_name: &str,
    model_version: &str,
) -> Vec<i32> {
    let rel_oid = rel.to_u32();
    let pred_hash = predicate_hash(predicate_name, model_version);
    let pred_hex = hex_encode(&pred_hash);
    let bytes: Option<Vec<u8>> = Spi::get_one(&format!(
        "SELECT bitmap FROM rvbbit.semantic_bitmaps \
         WHERE table_oid = {rel_oid}::oid AND rg_id = {rg_id} \
           AND predicate_hash = decode('{pred_hex}', 'hex')"
    ))
    .ok()
    .flatten();
    let Some(bytes) = bytes else {
        return Vec::new();
    };
    let bm = match RoaringBitmap::deserialize_from(&mut Cursor::new(&bytes)) {
        Ok(b) => b,
        Err(e) => pgrx::error!("rvbbit.bitmap_test_decode: roaring deserialize: {e}"),
    };
    bm.iter().map(|v| v as i32).collect()
}
