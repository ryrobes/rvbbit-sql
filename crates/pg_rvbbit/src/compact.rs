//! Export a regular Postgres relation to a parquet row group.
//!
//! Entry point:
//!     SELECT rvbbit.export_to_parquet('trips'::regclass);
//!
//! Walks pg_attribute for the relation, picks an Arrow type per column,
//! streams the whole table through a SPI cursor into Arrow builders,
//! writes one parquet file under $PGDATA/rvbbit/<oid>/<rg>.parquet, and
//! registers the row group in rvbbit.row_groups so the planner's
//! custom-scan path picks it up on the next query.
//!
//! Supported PG types (PG type oid → Arrow):
//!   bool       → Boolean
//!   int2       → Int16
//!   int4       → Int32
//!   int8       → Int64
//!   float4     → Float32
//!   float8     → Float64
//!   text/varchar/char → Utf8
//!   timestamp / timestamptz → Timestamp(Microsecond, UTC)
//!   date       → Int32  (PG epoch days; read path converts)
//!   jsonb      → Binary (PG jsonb body bytes, no varlena header)
//!   bytea      → Binary
//!
//! Unsupported types error clearly so the user knows what to drop or
//! rewrite. Numeric / interval / range types come later.
//!
//! NOTE: this writes one row group containing all rows. For very large
//! tables (>>10M rows) we'll want to chunk by row count and emit multiple
//! row groups so each one fits comfortably in memory.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use arrow::array::{
    ArrayRef, BinaryBuilder, BooleanBuilder, Float32Builder, Float64Builder, Int16Builder,
    Int32Builder, Int64Builder, ListBuilder, RecordBatch, StringBuilder,
    TimestampMicrosecondBuilder,
};
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
use pgrx::prelude::*;
use pgrx::Spi;
use rvbbit_storage::row_group::RowGroupWriter;

const SCAN_LAYOUT_DIR: &str = "scan";
const CLUSTER_LAYOUT_PREFIX: &str = "cluster:";
const HIVE_LAYOUT_PREFIX: &str = "hive:";

/// Parse a JSON text string through PG's jsonb_in and return the binary
/// varlena BODY bytes (i.e. without the 4-byte varlena header).
unsafe fn jsonb_text_to_binary_body(text: &str) -> Option<Vec<u8>> {
    let mut buf = Vec::with_capacity(text.len() + 1);
    buf.extend_from_slice(text.as_bytes());
    buf.push(0);
    type CUnwindPGFn = unsafe extern "C-unwind" fn(pg_sys::FunctionCallInfo) -> pg_sys::Datum;
    let jsonb_in: CUnwindPGFn = std::mem::transmute(pg_sys::jsonb_in as *const ());
    let jsonb_datum = pg_sys::DirectFunctionCall1Coll(
        Some(jsonb_in),
        pg_sys::InvalidOid,
        pg_sys::Datum::from(buf.as_ptr() as usize),
    );
    drop(buf);
    let raw_varlena = jsonb_datum.cast_mut_ptr::<pg_sys::varlena>();
    if raw_varlena.is_null() {
        return None;
    }
    let detoasted = pg_sys::pg_detoast_datum(raw_varlena);
    let header = std::ptr::read_unaligned(detoasted as *const u32);
    let total_len = (header >> 2) as usize;
    if total_len < 4 {
        return None;
    }
    let body_len = total_len - 4;
    let body_ptr = (detoasted as *const u8).add(4);
    let body = std::slice::from_raw_parts(body_ptr, body_len).to_vec();
    if detoasted != raw_varlena {
        pg_sys::pfree(detoasted as *mut _);
    }
    Some(body)
}

/// Per-column descriptor built from pg_attribute introspection. Carries
/// enough state to render the column in the SELECT, dispatch SPI row
/// reads, and emit the right Arrow type.
#[derive(Clone)]
struct ColumnPlan {
    /// PG column name. Used verbatim in SELECT and as Arrow field name.
    name: String,
    /// PG type oid (e.g. INT4OID, FLOAT8OID).
    pg_type: u32,
    /// Is the column NOT NULL? (Reflected in Arrow field nullability.)
    not_null: bool,
    /// Optional SQL expression to wrap the column in for the SELECT.
    /// Currently used for timestamp → epoch-micros bigint conversion
    /// and jsonb → text conversion.
    select_expr: String,
    /// Arrow data type emitted for this column.
    arrow_type: DataType,
    /// True for physical table columns. False for synthetic shred columns,
    /// which can be exported but cannot be referenced as source ORDER BY keys.
    base_column: bool,
}

/// All currently-handled column builders. One variant per supported
/// Arrow type — keeps the per-row dispatch a single match.
enum ColumnBuilder {
    Bool(BooleanBuilder),
    Int16(Int16Builder),
    Int32(Int32Builder),
    Int64(Int64Builder),
    Float32(Float32Builder),
    Float64(Float64Builder),
    Utf8(StringBuilder),
    TsMicros(TimestampMicrosecondBuilder),
    JsonbBinary(BinaryBuilder),
    /// PG real[] (FLOAT4ARRAY, oid 1021) → Arrow List<Float32>. The list
    /// is variable-length at the Arrow level even though embedding columns
    /// are usually fixed-dimension; the dim invariant is enforced upstream
    /// in Lance (lance_dim) and at refresh time. List<Float32> in parquet
    /// is uniformly readable and survives a future shift to FixedSizeList
    /// without a parquet rewrite.
    F32List(ListBuilder<Float32Builder>),
}

impl ColumnBuilder {
    fn for_type(t: &DataType) -> Self {
        match t {
            DataType::Boolean => ColumnBuilder::Bool(BooleanBuilder::new()),
            DataType::Int16 => ColumnBuilder::Int16(Int16Builder::new()),
            DataType::Int32 => ColumnBuilder::Int32(Int32Builder::new()),
            DataType::Int64 => ColumnBuilder::Int64(Int64Builder::new()),
            DataType::Float32 => ColumnBuilder::Float32(Float32Builder::new()),
            DataType::Float64 => ColumnBuilder::Float64(Float64Builder::new()),
            DataType::Utf8 => ColumnBuilder::Utf8(StringBuilder::new()),
            DataType::Timestamp(TimeUnit::Microsecond, _) => {
                ColumnBuilder::TsMicros(TimestampMicrosecondBuilder::new().with_timezone("UTC"))
            }
            DataType::Binary => ColumnBuilder::JsonbBinary(BinaryBuilder::new()),
            DataType::List(field) if field.data_type() == &DataType::Float32 => {
                ColumnBuilder::F32List(ListBuilder::new(Float32Builder::new()))
            }
            other => panic!("ColumnBuilder::for_type: unhandled {:?}", other),
        }
    }

    fn finish(self) -> ArrayRef {
        match self {
            ColumnBuilder::Bool(mut b) => Arc::new(b.finish()),
            ColumnBuilder::Int16(mut b) => Arc::new(b.finish()),
            ColumnBuilder::Int32(mut b) => Arc::new(b.finish()),
            ColumnBuilder::Int64(mut b) => Arc::new(b.finish()),
            ColumnBuilder::Float32(mut b) => Arc::new(b.finish()),
            ColumnBuilder::Float64(mut b) => Arc::new(b.finish()),
            ColumnBuilder::Utf8(mut b) => Arc::new(b.finish()),
            ColumnBuilder::TsMicros(mut b) => Arc::new(b.finish()),
            ColumnBuilder::JsonbBinary(mut b) => Arc::new(b.finish()),
            ColumnBuilder::F32List(mut b) => Arc::new(b.finish()),
        }
    }
}

/// Returns (arrow_type, select_expression). The select_expression is what
/// gets emitted in the SELECT list — for most types it's just the column
/// name; for timestamps we project to epoch microseconds, for jsonb we
/// project to ::text (then convert back to body bytes when ingesting).
fn plan_for_pg_type(pg_type: u32, col_name: &str) -> Result<(DataType, String), String> {
    let quoted = format!("\"{}\"", col_name.replace('"', "\"\""));
    Ok(match pg_type {
        16 => (DataType::Boolean, quoted),  // BOOLOID
        21 => (DataType::Int16, quoted),    // INT2OID
        23 => (DataType::Int32, quoted),    // INT4OID
        20 => (DataType::Int64, quoted),    // INT8OID
        700 => (DataType::Float32, quoted), // FLOAT4OID
        701 => (DataType::Float64, quoted), // FLOAT8OID
        // text(25) / varchar(1043) / bpchar(1042) / name(19)
        25 | 1043 | 1042 | 19 => (DataType::Utf8, quoted),
        // timestamp(1114) / timestamptz(1184) — both stored as epoch micros.
        1114 | 1184 => (
            DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into())),
            format!("(EXTRACT(EPOCH FROM {quoted}) * 1000000)::bigint"),
        ),
        // date(1082) → Int32 (PG epoch days)
        1082 => (DataType::Int32, format!("({quoted} - 'epoch'::date)")),
        3802 => (DataType::Binary, format!("{quoted}::text")), // JSONBOID
        17 => (DataType::Binary, quoted),                      // BYTEAOID
        // real[] / float4[] — oid 1021. Vectors land here; Lance ingest
        // re-packs into FixedSizeList<Float32; dim> downstream.
        1021 => (
            DataType::List(Arc::new(Field::new("item", DataType::Float32, true))),
            quoted,
        ),
        other => {
            return Err(format!(
                "rvbbit.export_to_parquet: unsupported PG type oid {other} \
                 for column '{col_name}' (supported: bool, int2, int4, int8, \
                 float4, float8, text, varchar, char, name, timestamp, \
                 timestamptz, date, jsonb, bytea)"
            ));
        }
    })
}

fn introspect_columns(rel_oid: u32) -> Result<Vec<ColumnPlan>, String> {
    // attname is the PG `name` type, not text — explicit ::text cast so
    // SPI's row.get::<String>() doesn't choke on the oid mismatch.
    let sql = format!(
        "SELECT attname::text, atttypid::oid::int, attnotnull \
         FROM pg_attribute \
         WHERE attrelid = {rel_oid}::oid AND attnum > 0 AND NOT attisdropped \
         ORDER BY attnum"
    );
    let mut plans: Vec<ColumnPlan> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: String = row.get::<String>(1)?.unwrap_or_default();
            let pg_type: i32 = row.get::<i32>(2)?.unwrap_or(0);
            let not_null: bool = row.get::<bool>(3)?.unwrap_or(false);
            let pg_type_u32 = pg_type as u32;
            let (arrow_type, select_expr) = plan_for_pg_type(pg_type_u32, &name)
                .map_err(|e| pgrx::spi::Error::CursorNotFound(e))?;
            plans.push(ColumnPlan {
                name,
                pg_type: pg_type_u32,
                not_null,
                select_expr,
                arrow_type,
                base_column: true,
            });
        }
        Ok(())
    })
    .map_err(|e| format!("introspecting columns: {e}"))?;
    if plans.is_empty() {
        return Err(format!("no columns found for relation oid {rel_oid}"));
    }
    Ok(plans)
}

/// Max rows per parquet row group. 256K rows keeps min/max zones tight enough
/// for automatic reclustering to matter while avoiding thousands of tiny files
/// at the scales we currently target. Override via env
/// `RVBBIT_COMPACT_CHUNK_ROWS`.
fn chunk_rows_setting() -> usize {
    std::env::var("RVBBIT_COMPACT_CHUNK_ROWS")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(262_144)
}

/// Larger, scan-friendly chunks for the canonical layout. This is the layout
/// broad scans use by default; clustered segmented variants are optional.
fn scan_chunk_rows_setting() -> usize {
    std::env::var("RVBBIT_COMPACT_SCAN_CHUNK_ROWS")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(1_048_576)
}

fn dual_layout_enabled() -> bool {
    matches!(
        std::env::var("RVBBIT_COMPACT_DUAL_LAYOUT")
            .unwrap_or_else(|_| "off".to_string())
            .to_ascii_lowercase()
            .as_str(),
        "1" | "on" | "true" | "yes"
    )
}

fn sync_variant_layouts_enabled() -> bool {
    matches!(
        compact_setting(
            "RVBBIT_COMPACT_VARIANTS_SYNC",
            "rvbbit.compact_variants_sync"
        )
        .unwrap_or_else(|| "off".to_string())
        .to_ascii_lowercase()
        .as_str(),
        "1" | "on" | "true" | "yes"
    )
}

fn cluster_variant_limit() -> usize {
    std::env::var("RVBBIT_COMPACT_CLUSTER_VARIANTS")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(3)
}

fn compact_setting(env_name: &str, guc_name: &str) -> Option<String> {
    let guc_sql = format!(
        "SELECT nullif(current_setting('{}', true), '')",
        guc_name.replace('\'', "''")
    );
    if let Ok(Some(value)) = Spi::get_one::<String>(&guc_sql) {
        return Some(value);
    }
    std::env::var(env_name).ok()
}

fn cluster_layout_for_key(key: &str) -> String {
    format!("{CLUSTER_LAYOUT_PREFIX}{key}")
}

fn hive_layout_enabled() -> bool {
    matches!(
        compact_setting("RVBBIT_COMPACT_HIVE_LAYOUT", "rvbbit.compact_hive_layout")
            .unwrap_or_else(|| "off".to_string())
            .to_ascii_lowercase()
            .as_str(),
        "1" | "on" | "true" | "yes"
    )
}

fn hive_variant_limit() -> usize {
    compact_setting(
        "RVBBIT_COMPACT_HIVE_VARIANTS",
        "rvbbit.compact_hive_variants",
    )
    .and_then(|s| s.parse::<usize>().ok())
    .filter(|n| *n > 0)
    .unwrap_or(2)
}

fn hive_min_distinct_setting() -> f64 {
    compact_setting(
        "RVBBIT_COMPACT_HIVE_MIN_DISTINCT",
        "rvbbit.compact_hive_min_distinct",
    )
    .and_then(|s| s.parse::<f64>().ok())
    .filter(|n| *n >= 1.0)
    .unwrap_or(2.0)
}

fn hive_max_distinct_setting() -> f64 {
    compact_setting(
        "RVBBIT_COMPACT_HIVE_MAX_DISTINCT",
        "rvbbit.compact_hive_max_distinct",
    )
    .and_then(|s| s.parse::<f64>().ok())
    .filter(|n| *n >= 1.0)
    .unwrap_or(256.0)
}

fn hive_layout_for_key(key: &str) -> String {
    format!("{HIVE_LAYOUT_PREFIX}{key}")
}

fn layout_dir_name(layout: &str) -> String {
    layout
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn auto_clustering_enabled() -> bool {
    !matches!(
        std::env::var("RVBBIT_COMPACT_AUTO_CLUSTER")
            .unwrap_or_else(|_| "on".to_string())
            .to_ascii_lowercase()
            .as_str(),
        "0" | "off" | "false" | "no"
    )
}

fn override_cluster_keys(plans: &[ColumnPlan]) -> Option<Vec<String>> {
    let raw = std::env::var("RVBBIT_COMPACT_CLUSTER_KEYS").ok()?;
    let mut out = Vec::new();
    for key in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        if plans.iter().any(|p| p.base_column && p.name == key) {
            out.push(key.to_string());
        }
    }
    Some(out)
}

fn override_hive_keys(plans: &[ColumnPlan]) -> Option<Vec<String>> {
    let raw = compact_setting("RVBBIT_COMPACT_HIVE_KEYS", "rvbbit.compact_hive_keys")?;
    let mut out = Vec::new();
    for key in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        if plans
            .iter()
            .any(|p| p.base_column && p.name == key && is_hive_partitionable_type(p.pg_type))
        {
            out.push(key.to_string());
        }
    }
    Some(out)
}

/// Pick a small physical clustering key list without user DDL. This is a
/// storage-layout hint, not a semantic index: queries remain normal SQL, and
/// row-group min/max pruning consumes the tighter ranges later.
fn auto_cluster_keys(rel_oid: u32, plans: &[ColumnPlan]) -> Vec<String> {
    if let Some(keys) = override_cluster_keys(plans) {
        return keys.into_iter().take(3).collect();
    }
    if !auto_clustering_enabled() {
        return Vec::new();
    }

    let stats = pg_stats_for_table(rel_oid);
    let index_hints = pg_index_hints_for_table(rel_oid);
    let mut candidates: Vec<(i32, String)> = plans
        .iter()
        .filter(|p| p.base_column && is_clusterable_type(p.pg_type))
        .filter_map(|p| {
            let score = cluster_score(p, stats.get(&p.name), index_hints.get(&p.name));
            (score > 0).then(|| (score, p.name.clone()))
        })
        .collect();
    candidates.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));

    let mut out: Vec<String> = Vec::new();
    for (_, name) in candidates {
        if out.len() >= 3 {
            break;
        }
        if !out.iter().any(|existing| existing == &name) {
            out.push(name);
        }
    }
    out
}

/// Pick low-cardinality Hive partition candidates for external engines. These
/// variants are experimental and only produced when explicitly enabled; they
/// trade write/storage cost for whole-folder pruning in DuckDB/DataFusion.
fn auto_hive_partition_keys(rel_oid: u32, plans: &[ColumnPlan]) -> Vec<String> {
    if let Some(keys) = override_hive_keys(plans) {
        return keys.into_iter().take(hive_variant_limit()).collect();
    }
    if !hive_layout_enabled() {
        return Vec::new();
    }

    let stats = pg_stats_for_table(rel_oid);
    let index_hints = pg_index_hints_for_table(rel_oid);
    let min_distinct = hive_min_distinct_setting();
    let max_distinct = hive_max_distinct_setting();
    let mut candidates: Vec<(i32, String)> = plans
        .iter()
        .filter(|p| p.base_column && is_hive_partitionable_type(p.pg_type))
        .filter_map(|p| {
            let stat = stats.get(&p.name)?;
            let n_distinct = stat.n_distinct;
            if n_distinct < min_distinct || n_distinct > max_distinct {
                return None;
            }
            let score = hive_partition_score(p, n_distinct, index_hints.get(&p.name));
            (score > 0).then(|| (score, p.name.clone()))
        })
        .collect();
    candidates.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));

    let mut out = Vec::new();
    for (_, name) in candidates {
        if out.len() >= hive_variant_limit() {
            break;
        }
        if !out.iter().any(|existing| existing == &name) {
            out.push(name);
        }
    }
    out
}

#[derive(Default, Clone, Copy)]
struct PgColumnStat {
    n_distinct: f64,
    correlation_abs: f64,
}

#[derive(Default, Clone, Copy)]
struct PgIndexHint {
    best_position: i32,
    primary: bool,
    unique: bool,
    clustered: bool,
}

fn pg_stats_for_table(rel_oid: u32) -> HashMap<String, PgColumnStat> {
    let mut out = HashMap::new();
    let sql = format!(
        "SELECT a.attname::text, s.n_distinct::float8, abs(coalesce(s.correlation, 0))::float8 \
         FROM pg_attribute a \
         LEFT JOIN pg_stats s \
           ON s.schemaname = (SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = a.attrelid) \
          AND s.tablename = (SELECT c.relname FROM pg_class c WHERE c.oid = a.attrelid) \
          AND s.attname = a.attname::text \
         WHERE a.attrelid = {rel_oid}::oid AND a.attnum > 0 AND NOT a.attisdropped"
    );
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let n_distinct: Option<f64> = row.get(2)?;
            let correlation_abs: Option<f64> = row.get(3)?;
            if let Some(name) = name {
                out.insert(
                    name,
                    PgColumnStat {
                        n_distinct: n_distinct.unwrap_or(0.0),
                        correlation_abs: correlation_abs.unwrap_or(0.0),
                    },
                );
            }
        }
        Ok(())
    });
    out
}

fn pg_index_hints_for_table(rel_oid: u32) -> HashMap<String, PgIndexHint> {
    let mut out = HashMap::new();
    let sql = format!(
        "SELECT a.attname::text, k.ord::int, i.indisprimary, i.indisunique, i.indisclustered \
         FROM pg_index i \
         JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true \
         JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum \
         WHERE i.indrelid = {rel_oid}::oid \
           AND i.indisvalid AND i.indisready \
           AND k.ord <= i.indnkeyatts \
           AND k.attnum > 0"
    );
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let position: Option<i32> = row.get(2)?;
            let primary: Option<bool> = row.get(3)?;
            let unique: Option<bool> = row.get(4)?;
            let clustered: Option<bool> = row.get(5)?;
            let (Some(name), Some(position)) = (name, position) else {
                continue;
            };
            let entry = out.entry(name).or_insert(PgIndexHint {
                best_position: position,
                primary: false,
                unique: false,
                clustered: false,
            });
            entry.best_position = entry.best_position.min(position);
            entry.primary |= primary.unwrap_or(false);
            entry.unique |= unique.unwrap_or(false);
            entry.clustered |= clustered.unwrap_or(false);
        }
        Ok(())
    });
    out
}

fn is_clusterable_type(pg_type: u32) -> bool {
    matches!(
        pg_type,
        20 | 21 | 23 | 25 | 700 | 701 | 1042 | 1043 | 1082 | 1114 | 1184
    )
}

fn is_hive_partitionable_type(pg_type: u32) -> bool {
    matches!(pg_type, 16 | 20 | 21 | 23 | 25 | 1042 | 1043)
}

fn hive_partition_score(
    plan: &ColumnPlan,
    n_distinct: f64,
    index_hint: Option<&PgIndexHint>,
) -> i32 {
    if let Some(hint) = index_hint {
        if hint.primary || hint.unique {
            return 0;
        }
    }

    let lower = plan.name.to_ascii_lowercase();
    let mut score = if n_distinct <= 8.0 {
        7_000
    } else if n_distinct <= 32.0 {
        6_000
    } else if n_distinct <= 128.0 {
        4_500
    } else {
        3_000
    };

    if matches!(plan.pg_type, 16) {
        score += 1_000;
    }
    if matches!(plan.pg_type, 25 | 1042 | 1043) {
        score += 500;
    }
    for token in [
        "status", "type", "flag", "region", "nation", "mode", "segment", "category", "priority",
        "brand", "model", "engine", "country",
    ] {
        if lower.contains(token) {
            score += 2_000;
            break;
        }
    }
    if lower.ends_with("id") || lower.ends_with("_id") || lower.ends_with("key") {
        score -= 2_000;
    }
    if lower.contains("hash") {
        score -= 4_000;
    }
    if let Some(hint) = index_hint {
        if hint.clustered {
            score += 1_000;
        }
        if hint.best_position == 1 {
            score += 750;
        }
    }

    score
}

fn cluster_score(
    plan: &ColumnPlan,
    stat: Option<&PgColumnStat>,
    index_hint: Option<&PgIndexHint>,
) -> i32 {
    let lower = plan.name.to_ascii_lowercase();
    let mut score = 0;

    if matches!(plan.pg_type, 1082 | 1114 | 1184) {
        score += 10_000;
    }
    if lower.ends_with("date") || lower.contains("time") {
        score += 3_000;
    }
    if lower.ends_with("key") || lower.ends_with("_key") || lower.ends_with("id") {
        score += 2_500;
    }
    if lower.contains("hash") {
        score += 2_000;
    }
    if lower == "id" || lower.ends_with("_id") {
        score += 1_000;
    }

    if let Some(stat) = stat {
        let nd = stat.n_distinct;
        if nd < 0.0 {
            // Negative pg_stats.n_distinct is a fraction of table cardinality.
            score += ((-nd * 2_000.0).min(2_000.0)) as i32;
        } else if nd >= 1_000.0 {
            score += 1_500;
        } else if nd >= 100.0 {
            score += 800;
        } else if nd > 0.0 && nd <= 8.0 {
            score -= 1_000;
        }
        if stat.correlation_abs >= 0.95 {
            // Already near heap order; useful but not worth displacing a
            // more selective key purely because the name matched.
            score += 250;
        }
    }

    if let Some(hint) = index_hint {
        score += match hint.best_position {
            1 => 20_000,
            2 => 14_000,
            3 => 10_000,
            _ => 6_000,
        };
        if hint.clustered {
            score += 8_000;
        }
        if hint.primary {
            score += 4_000;
        } else if hint.unique {
            score += 2_500;
        }
    }

    // Avoid leading with arbitrary wide text dimensions unless there is a
    // strong name/stat signal. They are expensive sort keys and rarely produce
    // useful range pruning under SQL collations.
    if matches!(plan.pg_type, 25 | 1042 | 1043) && score < 2_500 {
        score = 0;
    }
    score
}

#[pg_extern]
fn export_to_parquet(rel: pg_sys::Oid) -> Result<i64, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let scan_chunk_rows = scan_chunk_rows_setting();

    let qualified: String = Spi::get_one(&format!("SELECT {rel_oid}::oid::regclass::text"))?
        .ok_or("relation does not exist")?;

    let first_rg_id: i64 = Spi::get_one(&format!(
        "SELECT coalesce(max(rg_id), -1) + 1 \
         FROM rvbbit.row_groups WHERE table_oid = {rel_oid}::oid"
    ))?
    .unwrap_or(0);

    // Phase 2 generation allocation. The advisory_xact_lock is per-table:
    // class id 0x52564254 (ASCII "RVBT") + table_oid packed into one bigint,
    // so two concurrent compacts on the SAME table serialize but two on
    // DIFFERENT tables proceed in parallel. UPDATE...RETURNING gives us the
    // value BEFORE the increment, which is the generation we stamp on this
    // compaction's row groups.
    let generation: i64 = Spi::get_one(&format!(
        "WITH locked AS (
             SELECT pg_advisory_xact_lock(
                 ((1380336724::bigint) << 32) | {rel_oid}::bigint
             ) AS x
         ),
         bumped AS (
             UPDATE rvbbit.tables
                SET next_generation = next_generation + 1
              WHERE table_oid = {rel_oid}::oid
            RETURNING next_generation - 1 AS g
         )
         SELECT g FROM bumped, locked"
    ))?
    .unwrap_or(1);

    let mut plans = introspect_columns(rel_oid)?;
    extend_plans_with_legacy_shreds(&mut plans);

    // Path root: $PGDATA/rvbbit/<oid>/
    let data_dir: String =
        Spi::get_one("SHOW data_directory")?.ok_or("data_directory GUC is NULL")?;
    let mut path_root = PathBuf::from(data_dir);
    path_root.push("rvbbit");
    path_root.push(rel_oid.to_string());
    std::fs::create_dir_all(&path_root)?;

    let schema = schema_for_plans(&plans);

    let scan_root = path_root.join(SCAN_LAYOUT_DIR);
    let chunks = write_layout_chunks(
        &qualified,
        &plans,
        &schema,
        &scan_root,
        first_rg_id,
        scan_chunk_rows,
        &[],
    )?;
    register_primary_chunks(rel_oid, &chunks, generation)?;

    let mut total_rows: i64 = 0;
    for meta in &chunks {
        total_rows += meta.n_rows;
    }

    // Phase 2 slice 7: record the generation timeline so AS OF TIMESTAMP
    // queries (rvbbit.set_as_of) can resolve a wall-clock time to the right
    // generation. We only INSERT when this compaction actually wrote rows;
    // a no-op compact (empty heap) doesn't extend the timeline.
    if total_rows > 0 {
        let n_groups = chunks.len() as i32;
        Spi::run(&format!(
            "INSERT INTO rvbbit.generations (table_oid, generation, n_rows, n_row_groups) \
             VALUES ({rel_oid}::oid, {generation}, {total_rows}, {n_groups})"
        ))?;
    }

    if sync_variant_layouts_enabled() && total_rows > 0 {
        refresh_layout_variants_impl(rel_oid, &qualified, &plans, &schema, &path_root)?;
    }

    // Phase 4 Lance auto-refresh. When the operator has opted this table
    // into Lance acceleration (rvbbit.lance_enable), every compact()
    // rebuilds the Lance dataset to match the current table state. The
    // catalog row holds the URL + column name + dim. The Lance index, if
    // any was built, will need to be rebuilt by the operator after this —
    // future work to make that automatic too. For now this is best-effort:
    // a Lance refresh failure is logged but doesn't fail the compact.
    if let Ok(Some(lance_url)) = Spi::get_one::<String>(&format!(
        "SELECT lance_url FROM rvbbit.tables \
         WHERE table_oid = {rel_oid}::oid AND lance_url IS NOT NULL"
    )) {
        if let Ok(Some(vec_col)) = Spi::get_one::<String>(&format!(
            "SELECT lance_vector_column FROM rvbbit.tables \
             WHERE table_oid = {rel_oid}::oid"
        )) {
            if let Ok(Some(dim)) = Spi::get_one::<i32>(&format!(
                "SELECT lance_dim FROM rvbbit.tables \
                 WHERE table_oid = {rel_oid}::oid"
            )) {
                if let Err(e) =
                    crate::lance::refresh_lance_dataset(rel_oid, "id", &vec_col, dim, &lance_url)
                {
                    pgrx::warning!(
                        "rvbbit.compact: Lance auto-refresh of {qualified} failed: {e} \
                         (parquet write completed; Lance dataset may be stale)"
                    );
                }
            }
        }
    }

    register_legacy_llm_shreds_if_present(rel_oid, &plans)?;

    Ok(total_rows)
}

#[pg_extern]
fn refresh_layout_variants(rel: pg_sys::Oid) -> Result<i64, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let qualified: String = Spi::get_one(&format!("SELECT {rel_oid}::oid::regclass::text"))?
        .ok_or("relation does not exist")?;

    let mut plans = introspect_columns(rel_oid)?;
    extend_plans_with_legacy_shreds(&mut plans);
    let schema = schema_for_plans(&plans);

    let data_dir: String =
        Spi::get_one("SHOW data_directory")?.ok_or("data_directory GUC is NULL")?;
    let mut path_root = PathBuf::from(data_dir);
    path_root.push("rvbbit");
    path_root.push(rel_oid.to_string());
    std::fs::create_dir_all(&path_root)?;

    refresh_layout_variants_impl(rel_oid, &qualified, &plans, &schema, &path_root)
}

fn refresh_layout_variants_impl(
    rel_oid: u32,
    qualified: &str,
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
    path_root: &PathBuf,
) -> Result<i64, Box<dyn std::error::Error>> {
    let cluster_chunk_rows = chunk_rows_setting();
    let cluster_keys = auto_cluster_keys(rel_oid, plans);
    let hive_keys = auto_hive_partition_keys(rel_oid, plans);
    let mut rows_written = 0_i64;

    if dual_layout_enabled() && !cluster_keys.is_empty() {
        for cluster_key in cluster_keys.iter().take(cluster_variant_limit()) {
            let layout = cluster_layout_for_key(cluster_key);
            clear_variant_layout(rel_oid, &layout, path_root)?;
            let cluster_root = path_root.join(layout_dir_name(&layout));
            let variant_chunks = write_layout_chunks(
                qualified,
                plans,
                schema,
                &cluster_root,
                0,
                cluster_chunk_rows,
                std::slice::from_ref(cluster_key),
            )?;
            rows_written += variant_chunks.iter().map(|c| c.n_rows).sum::<i64>();
            register_variant_chunks(rel_oid, &layout, &variant_chunks)?;
        }
    }

    if hive_layout_enabled() && !hive_keys.is_empty() {
        for hive_key in hive_keys.iter().take(hive_variant_limit()) {
            let layout = hive_layout_for_key(hive_key);
            clear_variant_layout(rel_oid, &layout, path_root)?;
            let hive_root = path_root.join(layout_dir_name(&layout));
            let variant_chunks = write_hive_layout_chunks(
                qualified,
                plans,
                &hive_root,
                0,
                cluster_chunk_rows,
                hive_key,
            )?;
            rows_written += variant_chunks.iter().map(|c| c.n_rows).sum::<i64>();
            register_variant_chunks(rel_oid, &layout, &variant_chunks)?;
        }
    }

    // Drop backend-local caches that depend on rvbbit.row_groups state.
    // Without this, the same session would keep planning + scanning from
    // the pre-compact metadata snapshot.
    crate::planner::invalidate_planner_aggregates(rel_oid);
    crate::custom_scan::invalidate_scan_metadata(rel_oid);
    crate::columnar_cache::invalidate_table(rel_oid);

    Ok(rows_written)
}

fn clear_variant_layout(
    rel_oid: u32,
    layout: &str,
    path_root: &PathBuf,
) -> Result<(), Box<dyn std::error::Error>> {
    let layout_escaped = layout.replace('\'', "''");
    Spi::run(&format!(
        "DELETE FROM rvbbit.row_group_variants \
         WHERE table_oid = {rel_oid}::oid AND layout = '{layout_escaped}'"
    ))?;
    let layout_root = path_root.join(layout_dir_name(layout));
    match std::fs::remove_dir_all(&layout_root) {
        Ok(_) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => return Err(Box::new(err)),
    }
    Ok(())
}

fn schema_for_plans(plans: &[ColumnPlan]) -> Arc<Schema> {
    Arc::new(Schema::new(
        plans
            .iter()
            .map(|c| {
                let nullable = !c.not_null || matches!(c.arrow_type, DataType::Binary);
                Field::new(&c.name, c.arrow_type.clone(), nullable)
            })
            .collect::<Vec<_>>(),
    ))
}

fn select_list_for_plans(plans: &[ColumnPlan]) -> String {
    plans
        .iter()
        .map(|c| {
            let quoted = quote_ident(&c.name);
            if c.select_expr == quoted {
                quoted
            } else {
                format!("{} AS {}", c.select_expr, quoted)
            }
        })
        .collect::<Vec<_>>()
        .join(", ")
}

fn order_by_clause(cluster_keys: &[String]) -> String {
    if cluster_keys.is_empty() {
        String::new()
    } else {
        format!(
            " ORDER BY {}",
            cluster_keys
                .iter()
                .map(|c| quote_ident(c))
                .collect::<Vec<_>>()
                .join(", ")
        )
    }
}

fn write_layout_chunks(
    qualified: &str,
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
    path_root: &PathBuf,
    first_rg_id: i64,
    chunk_rows: usize,
    cluster_keys: &[String],
) -> Result<Vec<rvbbit_storage::metadata::RowGroupMeta>, Box<dyn std::error::Error>> {
    let select_list = select_list_for_plans(plans);
    let order_by = order_by_clause(cluster_keys);
    let select_sql = format!("SELECT {select_list} FROM {qualified}{order_by}");

    let chunks = Spi::connect(
        |client| -> Result<Vec<rvbbit_storage::metadata::RowGroupMeta>, pgrx::spi::Error> {
            // Bug fix (Phase 2 slice 2): without this, compact() reads the
            // rvbbit table through the rewriter, which routes authoritative
            // parquet to the custom scan — so the SECOND compact re-reads
            // the existing parquet contents and writes them as a new row
            // group, silently losing any new INSERTs since the previous
            // compact. force_heap_scan=on tells the planner+rewriter hooks
            // to leave the heap path intact for this SPI scan only.
            //
            // Save/restore prevents leaking the setting back into the
            // user's transaction if they had it explicitly off.
            let prev: String = client
                .select(
                    "SELECT coalesce(current_setting('rvbbit.force_heap_scan', true), 'off')",
                    Some(1),
                    &[],
                )?
                .first()
                .get::<String>(1)?
                .unwrap_or_else(|| "off".to_string());
            client.select(
                "SELECT pg_catalog.set_config('rvbbit.force_heap_scan', 'on', true)",
                Some(1),
                &[],
            )?;

            let table = client.select(&select_sql, None, &[])?;
            let mut chunks: Vec<rvbbit_storage::metadata::RowGroupMeta> = Vec::new();
            let mut builders: Vec<ColumnBuilder> = plans
                .iter()
                .map(|c| ColumnBuilder::for_type(&c.arrow_type))
                .collect();
            let mut chunk_count: usize = 0;
            let mut chunk_idx: i64 = 0;

            for row in table {
                for (i, plan) in plans.iter().enumerate() {
                    let idx = (i + 1) as usize;
                    match &mut builders[i] {
                        ColumnBuilder::Bool(b) => b.append_option(row.get::<bool>(idx)?),
                        ColumnBuilder::Int16(b) => b.append_option(row.get::<i16>(idx)?),
                        ColumnBuilder::Int32(b) => b.append_option(row.get::<i32>(idx)?),
                        ColumnBuilder::Int64(b) => b.append_option(row.get::<i64>(idx)?),
                        ColumnBuilder::Float32(b) => b.append_option(row.get::<f32>(idx)?),
                        ColumnBuilder::Float64(b) => b.append_option(row.get::<f64>(idx)?),
                        ColumnBuilder::Utf8(b) => b.append_option(row.get::<String>(idx)?),
                        ColumnBuilder::TsMicros(b) => b.append_option(row.get::<i64>(idx)?),
                        ColumnBuilder::JsonbBinary(b) => {
                            if plan.pg_type == 3802 {
                                match row.get::<String>(idx)? {
                                    Some(t) => {
                                        let body = unsafe { jsonb_text_to_binary_body(&t) };
                                        b.append_option(body.as_deref());
                                    }
                                    None => b.append_null(),
                                }
                            } else {
                                b.append_option(row.get::<Vec<u8>>(idx)?.as_deref());
                            }
                        }
                        ColumnBuilder::F32List(b) => {
                            // PG real[] arrives as Vec<Option<f32>>. Inner
                            // builder eats each element; outer builder
                            // commits the list (true) or marks NULL (false).
                            match row.get::<Vec<Option<f32>>>(idx)? {
                                Some(arr) => {
                                    let inner = b.values();
                                    for v in arr {
                                        inner.append_option(v);
                                    }
                                    b.append(true);
                                }
                                None => b.append(false),
                            }
                        }
                    }
                }
                chunk_count += 1;

                if chunk_count >= chunk_rows {
                    let rg_id = first_rg_id + chunk_idx;
                    let chunk_path = path_root.join(format!("{rg_id}.parquet"));
                    let meta = flush_chunk(schema, &mut builders, plans, &chunk_path, rg_id)
                        .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                    chunks.push(meta);
                    chunk_count = 0;
                    chunk_idx += 1;
                }
            }

            if chunk_count > 0 {
                let rg_id = first_rg_id + chunk_idx;
                let chunk_path = path_root.join(format!("{rg_id}.parquet"));
                let meta = flush_chunk(schema, &mut builders, plans, &chunk_path, rg_id)
                    .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                chunks.push(meta);
            }

            // Restore force_heap_scan to its prior value (see comment above).
            // Quoting prev defensively in case some future GUC value contains
            // a single quote.
            let prev_escaped = prev.replace('\'', "''");
            client.select(
                &format!(
                    "SELECT pg_catalog.set_config('rvbbit.force_heap_scan', '{prev_escaped}', true)"
                ),
                Some(1),
                &[],
            )?;

            Ok(chunks)
        },
    )?;

    Ok(chunks)
}

fn write_hive_layout_chunks(
    qualified: &str,
    plans: &[ColumnPlan],
    path_root: &PathBuf,
    first_rg_id: i64,
    chunk_rows: usize,
    hive_key: &str,
) -> Result<Vec<rvbbit_storage::metadata::RowGroupMeta>, Box<dyn std::error::Error>> {
    let Some(partition_plan) = plans.iter().find(|p| p.name == hive_key) else {
        return Err(format!("hive partition key '{hive_key}' is not a table column").into());
    };
    if !is_hive_partitionable_type(partition_plan.pg_type) {
        return Err(format!("hive partition key '{hive_key}' has unsupported type").into());
    }

    let file_plans: Vec<ColumnPlan> = plans
        .iter()
        .filter(|p| p.name != hive_key)
        .cloned()
        .collect();
    let file_schema = schema_for_plans(&file_plans);
    let select_list = select_list_for_plans(&file_plans);
    let partition_expr = format!(
        "({})::text AS {}",
        quote_ident(hive_key),
        quote_ident("__rvbbit_partition_key")
    );
    let select_sql = format!(
        "SELECT {select_list}, {partition_expr} FROM {qualified} ORDER BY {}",
        quote_ident(hive_key)
    );
    let partition_idx = file_plans.len() + 1;

    let chunks = Spi::connect(
        |client| -> Result<Vec<rvbbit_storage::metadata::RowGroupMeta>, pgrx::spi::Error> {
            // Same heap-scan-forcing fix as write_layout_chunks. See comment
            // there for why this is load-bearing.
            let prev: String = client
                .select(
                    "SELECT coalesce(current_setting('rvbbit.force_heap_scan', true), 'off')",
                    Some(1),
                    &[],
                )?
                .first()
                .get::<String>(1)?
                .unwrap_or_else(|| "off".to_string());
            client.select(
                "SELECT pg_catalog.set_config('rvbbit.force_heap_scan', 'on', true)",
                Some(1),
                &[],
            )?;

            let table = client.select(&select_sql, None, &[])?;
            let mut chunks: Vec<rvbbit_storage::metadata::RowGroupMeta> = Vec::new();
            let mut builders: Vec<ColumnBuilder> = file_plans
                .iter()
                .map(|c| ColumnBuilder::for_type(&c.arrow_type))
                .collect();
            let mut chunk_count: usize = 0;
            let mut chunk_idx: i64 = 0;
            let mut current_partition: Option<String> = None;

            for row in table {
                let raw_partition: Option<String> = row.get::<String>(partition_idx)?;
                let partition = encode_hive_partition_value(raw_partition.as_deref());
                if current_partition.as_deref() != Some(partition.as_str()) {
                    if chunk_count > 0 {
                        let rg_id = first_rg_id + chunk_idx;
                        let chunk_path = path_root
                            .join(format!("{}={partition}", layout_dir_name(hive_key)))
                            .join(format!("{rg_id}.parquet"));
                        let meta = flush_chunk(
                            &file_schema,
                            &mut builders,
                            &file_plans,
                            &chunk_path,
                            rg_id,
                        )
                        .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                        chunks.push(meta);
                        chunk_count = 0;
                        chunk_idx += 1;
                    }
                    current_partition = Some(partition.clone());
                }

                for (i, plan) in file_plans.iter().enumerate() {
                    let idx = i + 1;
                    match &mut builders[i] {
                        ColumnBuilder::Bool(b) => b.append_option(row.get::<bool>(idx)?),
                        ColumnBuilder::Int16(b) => b.append_option(row.get::<i16>(idx)?),
                        ColumnBuilder::Int32(b) => b.append_option(row.get::<i32>(idx)?),
                        ColumnBuilder::Int64(b) => b.append_option(row.get::<i64>(idx)?),
                        ColumnBuilder::Float32(b) => b.append_option(row.get::<f32>(idx)?),
                        ColumnBuilder::Float64(b) => b.append_option(row.get::<f64>(idx)?),
                        ColumnBuilder::Utf8(b) => b.append_option(row.get::<String>(idx)?),
                        ColumnBuilder::TsMicros(b) => b.append_option(row.get::<i64>(idx)?),
                        ColumnBuilder::JsonbBinary(b) => {
                            if plan.pg_type == 3802 {
                                match row.get::<String>(idx)? {
                                    Some(t) => {
                                        let body = unsafe { jsonb_text_to_binary_body(&t) };
                                        b.append_option(body.as_deref());
                                    }
                                    None => b.append_null(),
                                }
                            } else {
                                b.append_option(row.get::<Vec<u8>>(idx)?.as_deref());
                            }
                        }
                        ColumnBuilder::F32List(b) => {
                            // PG real[] arrives as Vec<Option<f32>>. Inner
                            // builder eats each element; outer builder
                            // commits the list (true) or marks NULL (false).
                            match row.get::<Vec<Option<f32>>>(idx)? {
                                Some(arr) => {
                                    let inner = b.values();
                                    for v in arr {
                                        inner.append_option(v);
                                    }
                                    b.append(true);
                                }
                                None => b.append(false),
                            }
                        }
                    }
                }
                chunk_count += 1;

                if chunk_count >= chunk_rows {
                    let rg_id = first_rg_id + chunk_idx;
                    let partition = current_partition.as_deref().unwrap_or("__NULL__");
                    let chunk_path = path_root
                        .join(format!("{}={partition}", layout_dir_name(hive_key)))
                        .join(format!("{rg_id}.parquet"));
                    let meta =
                        flush_chunk(&file_schema, &mut builders, &file_plans, &chunk_path, rg_id)
                            .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                    chunks.push(meta);
                    chunk_count = 0;
                    chunk_idx += 1;
                }
            }

            if chunk_count > 0 {
                let rg_id = first_rg_id + chunk_idx;
                let partition = current_partition.as_deref().unwrap_or("__NULL__");
                let chunk_path = path_root
                    .join(format!("{}={partition}", layout_dir_name(hive_key)))
                    .join(format!("{rg_id}.parquet"));
                let meta =
                    flush_chunk(&file_schema, &mut builders, &file_plans, &chunk_path, rg_id)
                        .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                chunks.push(meta);
            }

            let prev_escaped = prev.replace('\'', "''");
            client.select(
                &format!(
                    "SELECT pg_catalog.set_config('rvbbit.force_heap_scan', '{prev_escaped}', true)"
                ),
                Some(1),
                &[],
            )?;

            Ok(chunks)
        },
    )?;

    Ok(chunks)
}

fn encode_hive_partition_value(value: Option<&str>) -> String {
    let Some(value) = value else {
        return "__HIVE_DEFAULT_PARTITION__".to_string();
    };
    if value.is_empty() {
        return "__EMPTY__".to_string();
    }
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'-' | b'.' => out.push(byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

fn escaped_row_group_json(
    meta: &rvbbit_storage::metadata::RowGroupMeta,
) -> Result<(String, String), Box<dyn std::error::Error>> {
    let stats_json = serde_json::to_string(&meta.column_stats)?;
    let stats_escaped = stats_json.replace("$rvbbit$", "$rvbbit$$rvbbit$");
    let per_group_json = serde_json::to_string(&meta.per_group_stats)?;
    let per_group_escaped = per_group_json.replace("$rvbbit$", "$rvbbit$$rvbbit$");
    Ok((stats_escaped, per_group_escaped))
}

fn register_primary_chunks(
    rel_oid: u32,
    chunks: &[rvbbit_storage::metadata::RowGroupMeta],
    generation: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    for meta in chunks {
        let (stats_escaped, per_group_escaped) = escaped_row_group_json(meta)?;
        let path_str = meta.path.replace('\'', "''");
        Spi::run(&format!(
            "INSERT INTO rvbbit.row_groups \
             (table_oid, rg_id, path, n_rows, n_bytes, generation, stats, per_group_stats) \
             VALUES ({rel_oid}::oid, {rg_id}, '{path_str}', {n_rows_meta}, {n_bytes}, \
                     {generation}, \
                     $rvbbit${stats_escaped}$rvbbit$::jsonb, \
                     $rvbbit${per_group_escaped}$rvbbit$::jsonb)",
            rg_id = meta.rg_id,
            n_rows_meta = meta.n_rows,
            n_bytes = meta.n_bytes,
        ))?;
        register_group_stats(rel_oid, meta)?;
        register_column_bitmaps(rel_oid, meta)?;
        register_text_dictionaries(rel_oid, meta)?;
    }
    Ok(())
}

fn register_group_stats(
    rel_oid: u32,
    meta: &rvbbit_storage::metadata::RowGroupMeta,
) -> Result<(), Box<dyn std::error::Error>> {
    if meta.per_group_stats.is_empty() || !rvbbit_catalog_table_exists("group_stats") {
        return Ok(());
    }
    for block in &meta.per_group_stats {
        let group_col = block.group_column.replace('\'', "''");
        for bucket in &block.groups {
            let group_key = group_value_key(&bucket.value).replace('\'', "''");
            let group_value_text_sql = match group_value_to_text(&bucket.value) {
                Some(value) => format!("'{}'", value.replace('\'', "''")),
                None => "NULL".to_string(),
            };
            let agg_sql = if bucket.agg.is_empty() {
                "NULL::jsonb".to_string()
            } else {
                let agg_json = serde_json::to_string(&bucket.agg)?;
                let agg_escaped = agg_json.replace("$rvbbit$", "$rvbbit$$rvbbit$");
                format!("$rvbbit${agg_escaped}$rvbbit$::jsonb")
            };
            Spi::run(&format!(
                "INSERT INTO rvbbit.group_stats \
                 (table_oid, rg_id, group_col, group_key, group_value_text, count, agg) \
                 VALUES ({rel_oid}::oid, {rg_id}, '{group_col}', '{group_key}', \
                         {group_value_text_sql}, {count}, {agg_sql})",
                rg_id = meta.rg_id,
                count = bucket.count,
            ))?;
        }
    }
    Ok(())
}

fn group_value_key(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::Null => "z:null".to_string(),
        serde_json::Value::Bool(v) => format!("b:{v}"),
        serde_json::Value::Number(v) => format!("n:{v}"),
        serde_json::Value::String(v) => format!("s:{v}"),
        other => format!("j:{other}"),
    }
}

fn group_value_to_text(value: &serde_json::Value) -> Option<String> {
    match value {
        serde_json::Value::Null => None,
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Bool(v) => Some(v.to_string()),
        serde_json::Value::Number(v) => Some(v.to_string()),
        other => Some(other.to_string()),
    }
}

fn register_column_bitmaps(
    rel_oid: u32,
    meta: &rvbbit_storage::metadata::RowGroupMeta,
) -> Result<(), Box<dyn std::error::Error>> {
    if meta.column_bitmaps.is_empty() || !rvbbit_catalog_table_exists("column_bitmaps") {
        return Ok(());
    }
    for block in &meta.column_bitmaps {
        let column_name = block.column.replace('\'', "''");
        let bitmap_kind = block.kind.replace('\'', "''");
        for entry in &block.entries {
            let value_text = entry.value_text.replace('\'', "''");
            let value_json = serde_json::to_string(&entry.value)?;
            let value_json = value_json.replace("$rvbbit$", "$rvbbit$$rvbbit$");
            let bitmap_b64 = entry.bitmap_b64.replace('\'', "''");
            Spi::run(&format!(
                "INSERT INTO rvbbit.column_bitmaps \
                 (table_oid, rg_id, column_name, bitmap_kind, value_text, value_json, \
                  bitmap, n_set, n_total) \
                 VALUES ({rel_oid}::oid, {rg_id}, '{column_name}', '{bitmap_kind}', \
                         '{value_text}', $rvbbit${value_json}$rvbbit$::jsonb, \
                         decode('{bitmap_b64}', 'base64'), {n_set}, {n_total})",
                rg_id = meta.rg_id,
                n_set = entry.n_set,
                n_total = meta.n_rows,
            ))?;
        }
    }
    Ok(())
}

fn register_text_dictionaries(
    rel_oid: u32,
    meta: &rvbbit_storage::metadata::RowGroupMeta,
) -> Result<(), Box<dyn std::error::Error>> {
    if meta.text_dictionaries.is_empty() || !rvbbit_catalog_table_exists("text_dictionaries") {
        return Ok(());
    }
    for block in &meta.text_dictionaries {
        let column_name = block.column.replace('\'', "''");
        let path = block.path.replace('\'', "''");
        Spi::run(&format!(
            "INSERT INTO rvbbit.text_dictionaries \
             (table_oid, rg_id, column_name, path, n_rows, n_values, n_nulls, n_empty, n_bytes) \
             VALUES ({rel_oid}::oid, {rg_id}, '{column_name}', '{path}', \
                     {n_rows}, {n_values}, {n_nulls}, {n_empty}, {n_bytes})",
            rg_id = meta.rg_id,
            n_rows = block.n_rows,
            n_values = block.n_values,
            n_nulls = block.n_nulls,
            n_empty = block.n_empty,
            n_bytes = block.n_bytes,
        ))?;
    }
    Ok(())
}

fn rvbbit_catalog_table_exists(table: &str) -> bool {
    let table = table.replace('\'', "''");
    Spi::get_one::<bool>(&format!("SELECT to_regclass('rvbbit.{table}') IS NOT NULL"))
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn register_variant_chunks(
    rel_oid: u32,
    layout: &str,
    chunks: &[rvbbit_storage::metadata::RowGroupMeta],
) -> Result<(), Box<dyn std::error::Error>> {
    let layout_escaped = layout.replace('\'', "''");
    for meta in chunks {
        let (stats_escaped, per_group_escaped) = escaped_row_group_json(meta)?;
        let path_str = meta.path.replace('\'', "''");
        Spi::run(&format!(
            "INSERT INTO rvbbit.row_group_variants \
             (table_oid, layout, rg_id, path, n_rows, n_bytes, stats, per_group_stats) \
             VALUES ({rel_oid}::oid, '{layout_escaped}', {rg_id}, '{path_str}', \
                     {n_rows_meta}, {n_bytes}, \
                     $rvbbit${stats_escaped}$rvbbit$::jsonb, \
                     $rvbbit${per_group_escaped}$rvbbit$::jsonb)",
            rg_id = meta.rg_id,
            n_rows_meta = meta.n_rows,
            n_bytes = meta.n_bytes,
        ))?;
    }
    Ok(())
}

/// Finalize the current Arrow builders into a parquet row group on disk,
/// reset the builders for the next chunk, and return the metadata so the
/// caller can register it in `rvbbit.row_groups`.
fn flush_chunk(
    schema: &Arc<Schema>,
    builders: &mut Vec<ColumnBuilder>,
    plans: &[ColumnPlan],
    path: &PathBuf,
    rg_id: i64,
) -> Result<rvbbit_storage::metadata::RowGroupMeta, Box<dyn std::error::Error>> {
    // Steal the current builders out, replace with fresh ones for the next chunk.
    let fresh: Vec<ColumnBuilder> = plans
        .iter()
        .map(|c| ColumnBuilder::for_type(&c.arrow_type))
        .collect();
    let old = std::mem::replace(builders, fresh);
    let arrays: Vec<ArrayRef> = old.into_iter().map(|b| b.finish()).collect();
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    let meta = RowGroupWriter::write(path, rg_id, &batch)?;
    Ok(meta)
}

/// If the relation looks like the legacy llm_events shape (has response +
/// metadata jsonb columns), add the known JSON shred columns as extra
/// projections so they're written into the parquet file. Generic tables
/// are unaffected; users register their own shreds explicitly.
fn extend_plans_with_legacy_shreds(plans: &mut Vec<ColumnPlan>) {
    let has_response = plans
        .iter()
        .any(|c| c.name == "response" && c.pg_type == 3802);
    let has_metadata = plans
        .iter()
        .any(|c| c.name == "metadata" && c.pg_type == 3802);
    if !(has_response && has_metadata) {
        return;
    }
    plans.push(ColumnPlan {
        name: "x_response_stop_reason".into(),
        pg_type: 25,
        not_null: false,
        select_expr: "response->>'stop_reason'".into(),
        arrow_type: DataType::Utf8,
        base_column: false,
    });
    plans.push(ColumnPlan {
        name: "x_response_model".into(),
        pg_type: 25,
        not_null: false,
        select_expr: "response->>'model'".into(),
        arrow_type: DataType::Utf8,
        base_column: false,
    });
    plans.push(ColumnPlan {
        name: "x_response_input_tokens".into(),
        pg_type: 23,
        not_null: false,
        select_expr: "(response->'usage'->>'input_tokens')::int".into(),
        arrow_type: DataType::Int32,
        base_column: false,
    });
    plans.push(ColumnPlan {
        name: "x_response_output_tokens".into(),
        pg_type: 23,
        not_null: false,
        select_expr: "(response->'usage'->>'output_tokens')::int".into(),
        arrow_type: DataType::Int32,
        base_column: false,
    });
    plans.push(ColumnPlan {
        name: "x_metadata_region".into(),
        pg_type: 25,
        not_null: false,
        select_expr: "metadata->>'region'".into(),
        arrow_type: DataType::Utf8,
        base_column: false,
    });
}

fn register_legacy_llm_shreds_if_present(
    rel_oid: u32,
    plans: &[ColumnPlan],
) -> Result<(), Box<dyn std::error::Error>> {
    // Detect llm_events-shaped tables by their source jsonb columns
    // (response + metadata). Skip otherwise.
    let has_response = plans
        .iter()
        .any(|c| c.name == "response" && c.pg_type == 3802);
    let has_metadata = plans
        .iter()
        .any(|c| c.name == "metadata" && c.pg_type == 3802);
    if !(has_response && has_metadata) {
        return Ok(());
    }
    Spi::run(&format!(
        "INSERT INTO rvbbit.shreds (table_oid, column_name, source_expr, src_column, path, data_type) VALUES
            ({rel_oid}::oid, 'x_response_stop_reason',
             $expr$response->>'stop_reason'$expr$,
             'response', ARRAY['stop_reason']::text[], 'text'),
            ({rel_oid}::oid, 'x_response_model',
             $expr$response->>'model'$expr$,
             'response', ARRAY['model']::text[], 'text'),
            ({rel_oid}::oid, 'x_response_input_tokens',
             $expr$(response->'usage'->>'input_tokens')::int$expr$,
             'response', ARRAY['usage','input_tokens']::text[], 'int4'),
            ({rel_oid}::oid, 'x_response_output_tokens',
             $expr$(response->'usage'->>'output_tokens')::int$expr$,
             'response', ARRAY['usage','output_tokens']::text[], 'int4'),
            ({rel_oid}::oid, 'x_metadata_region',
             $expr$metadata->>'region'$expr$,
             'metadata', ARRAY['region']::text[], 'text')
         ON CONFLICT DO NOTHING"
    ))?;
    Ok(())
}
