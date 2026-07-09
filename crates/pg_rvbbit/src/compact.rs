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

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::File;
use std::path::PathBuf;
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Instant;

use arrow::array::{
    make_array, Array, ArrayRef, BinaryBuilder, BooleanArray, BooleanBuilder, Date32Array,
    Float32Builder, Float64Builder, Int16Array, Int16Builder, Int32Array, Int32Builder, Int64Array,
    Int64Builder, LargeStringArray, ListBuilder, RecordBatch, StringArray, StringBuilder,
    StringViewArray, TimestampMicrosecondBuilder, UInt32Array,
};
use arrow::compute::{cast, take};
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use pgrx::prelude::*;
use pgrx::JsonB;
use pgrx::Spi;
use rvbbit_storage::row_group::{RowGroupWriteOptions, RowGroupWriter};
use tokio::runtime::Builder as TokioRuntimeBuilder;
use vortex::array::arrow::ArrowSessionExt;
use vortex::file::WriteOptionsSessionExt;
use vortex::io::session::RuntimeSessionExt;
use vortex::session::VortexSession;
use vortex::VortexSessionDefault;

const SCAN_LAYOUT_DIR: &str = "scan";
const CLUSTER_LAYOUT_PREFIX: &str = "cluster:";
const HIVE_LAYOUT_PREFIX: &str = "hive:";
const VORTEX_SCAN_LAYOUT: &str = "vortex_scan";
const EXPORT_CTID_COLUMN: &str = "__rvbbit_export_ctid";
const EXPORT_ROW_KEY_COLUMN: &str = "__rvbbit_row_key_json";

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

struct LayoutWriteResult {
    chunks: Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: LayoutWriteTimings,
}

struct ImportWriteResult {
    chunks: Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: LayoutWriteTimings,
}

#[derive(Default)]
struct LayoutWriteTimings {
    spi_select_seconds: f64,
    source_open_seconds: f64,
    source_read_seconds: f64,
    source_canonicalize_seconds: f64,
    row_build_seconds: f64,
    finish_batch_seconds: f64,
    writer_wait_seconds: f64,
    writer_join_seconds: f64,
    writer_seconds_sum: f64,
    writer_seconds_max: f64,
    identity_insert_seconds: f64,
}

impl LayoutWriteTimings {
    fn add_writer_seconds(&mut self, seconds: f64) {
        self.writer_seconds_sum += seconds;
        if seconds > self.writer_seconds_max {
            self.writer_seconds_max = seconds;
        }
    }
}

struct ChunkWriteResult {
    meta: rvbbit_storage::metadata::RowGroupMeta,
    write_seconds: f64,
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

/// PG types we store in parquet as their canonical TEXT (Arrow Utf8) and
/// reconstruct to the real type on read via the type's input function (see
/// custom_scan's ColumnReader::Utf8 reconstruction). Lossless and semantics-
/// preserving: the column stays its declared type to SQL, so uuid/numeric/inet/…
/// comparisons, joins, and ORDER BY behave exactly as on the heap.
///
/// An explicit allowlist (plus user enums, whose oids are dynamic) rather than a
/// blind catch-all, so genuinely lossy/unhandled types (ranges, composites,
/// geometry, arrays-of-composite) still fail loudly instead of degrading.
fn is_text_surrogate_type(pg_type: u32, typtype: &str) -> bool {
    typtype == "e" // user-defined enum (dynamic oid)
        || matches!(
            pg_type,
            2950   // uuid
            | 1700 // numeric
            | 869  // inet
            | 650  // cidr
            | 829  // macaddr
            | 774  // macaddr8
            | 1083 // time
            | 1266 // timetz
            | 1186 // interval
        )
}

/// Returns (arrow_type, select_expression). The select_expression is what
/// gets emitted in the SELECT list — for most types it's just the column
/// name; for timestamps we project to epoch microseconds, for jsonb and the
/// text-surrogate types (uuid/numeric/inet/enum/…) we project to ::text and
/// reconstruct on read.
fn plan_for_pg_type(
    pg_type: u32,
    typtype: &str,
    col_name: &str,
) -> Result<(DataType, String), String> {
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
        // Text-surrogate types (uuid/numeric/inet/…/enum): store canonical text,
        // reconstruct the real type on read via its input function.
        n if is_text_surrogate_type(n, typtype) => (DataType::Utf8, format!("{quoted}::text")),
        other => {
            return Err(format!(
                "rvbbit.export_to_parquet: unsupported PG type oid {other} \
                 for column '{col_name}' (natively supported: bool, int2, int4, \
                 int8, float4, float8, text, varchar, char, name, timestamp, \
                 timestamptz, date, jsonb, bytea, real[]; round-tripped as text: \
                 uuid, numeric, inet, cidr, macaddr, macaddr8, time, timetz, \
                 interval, enums)"
            ));
        }
    })
}

fn introspect_columns(rel_oid: u32) -> Result<Vec<ColumnPlan>, String> {
    // attname is the PG `name` type, not text — explicit ::text cast so
    // SPI's row.get::<String>() doesn't choke on the oid mismatch.
    let sql = format!(
        "SELECT a.attname::text, a.atttypid::oid::int, a.attnotnull, t.typtype::text \
         FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid \
         WHERE a.attrelid = {rel_oid}::oid AND a.attnum > 0 AND NOT a.attisdropped \
         ORDER BY a.attnum"
    );
    let mut plans: Vec<ColumnPlan> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: String = row.get::<String>(1)?.unwrap_or_default();
            let pg_type: i32 = row.get::<i32>(2)?.unwrap_or(0);
            let not_null: bool = row.get::<bool>(3)?.unwrap_or(false);
            let typtype: String = row.get::<String>(4)?.unwrap_or_default();
            let pg_type_u32 = pg_type as u32;
            let (arrow_type, select_expr) = plan_for_pg_type(pg_type_u32, &typtype, &name)
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

fn identity_mode_for_export(rel_oid: u32) -> Result<Option<String>, Box<dyn std::error::Error>> {
    if !row_identity_map_available() {
        return Ok(None);
    }
    let mode = Spi::get_one::<String>(&format!(
        "SELECT rvbbit.accel_identity_mode({rel_oid}::oid::regclass)"
    ))?;
    let Some(mode) = mode else {
        return Ok(None);
    };

    let policy = compact_setting("RVBBIT_ACCEL_IDENTITY_MAP", "rvbbit.accel_identity_map")
        .unwrap_or_else(|| "primary_key".to_string())
        .to_ascii_lowercase()
        .replace('-', "_");
    let enabled = match policy.as_str() {
        "0" | "off" | "false" | "no" | "none" | "disabled" => false,
        "primary_key" | "primary" | "pk" => mode == "primary_key",
        "ctid" => mode == "ctid",
        "1" | "on" | "true" | "yes" | "all" | "auto" => true,
        _ => true,
    };

    Ok(enabled.then_some(mode))
}

fn pk_identity_expr_for_export(rel_oid: u32) -> Result<Option<String>, Box<dyn std::error::Error>> {
    let expr = Spi::get_one::<String>(&format!(
        "SELECT rvbbit.accel_identity_expr({rel_oid}::oid::regclass, NULL)"
    ))?;
    Ok(expr.map(|expr| format!("{expr} AS {}", quote_ident(EXPORT_ROW_KEY_COLUMN))))
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
    compact_setting(
        "RVBBIT_COMPACT_SCAN_CHUNK_ROWS",
        "rvbbit.compact_scan_chunk_rows",
    )
    .and_then(|s| s.parse::<usize>().ok())
    .filter(|n| *n > 0)
    .unwrap_or(1_048_576)
}

fn writer_threads_setting() -> usize {
    compact_setting(
        "RVBBIT_COMPACT_WRITER_THREADS",
        "rvbbit.compact_writer_threads",
    )
    .and_then(|s| s.parse::<usize>().ok())
    .filter(|n| (1..=32).contains(n))
    .unwrap_or(1)
}

fn elapsed_seconds_since(start: Instant) -> f64 {
    start.elapsed().as_secs_f64()
}

fn dual_layout_enabled(rel_oid: u32) -> bool {
    if table_denies_layout(rel_oid, "cluster") {
        return false;
    }
    matches!(
        std::env::var("RVBBIT_COMPACT_DUAL_LAYOUT")
            .unwrap_or_else(|_| "off".to_string())
            .to_ascii_lowercase()
            .as_str(),
        "1" | "on" | "true" | "yes"
    )
}

fn sync_variant_layouts_enabled() -> bool {
    // Default ON so the legacy compact(rel, keep_heap) path also syncs layout
    // variants (matching refresh_acceleration / rebuild_acceleration, which call
    // the variant builders directly without this gate). Only the per-layout
    // gates that are ON actually build — by default that's vortex alone (hive +
    // cluster stay opt-in). Disable with RVBBIT_COMPACT_VARIANTS_SYNC=off or
    // rvbbit.compact_variants_sync=off.
    matches!(
        compact_setting(
            "RVBBIT_COMPACT_VARIANTS_SYNC",
            "rvbbit.compact_variants_sync"
        )
        .unwrap_or_else(|| "on".to_string())
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

fn parse_bool_setting(raw: &str) -> Option<bool> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "1" | "on" | "true" | "yes" => Some(true),
        "0" | "off" | "false" | "no" => Some(false),
        _ => None,
    }
}

fn compact_bool_setting(env_name: &str, guc_name: &str) -> Option<bool> {
    compact_setting(env_name, guc_name).and_then(|raw| parse_bool_setting(&raw))
}

fn cluster_layout_for_key(key: &str) -> String {
    format!("{CLUSTER_LAYOUT_PREFIX}{key}")
}

fn hive_layout_enabled(rel_oid: u32) -> bool {
    if table_denies_layout(rel_oid, "hive") {
        return false;
    }
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

/// True when this table has an explicit per-table deny for `layout`
/// (rvbbit.accel_policy.denied_layouts). Lets the UI/SQL stop the rebuilder from
/// materializing a layout for one table while the global default stays on.
fn table_denies_layout(rel_oid: u32, layout: &str) -> bool {
    Spi::get_one::<bool>(&format!(
        "SELECT coalesce('{layout}' = ANY(denied_layouts), false) \
         FROM rvbbit.accel_policy WHERE table_oid = {rel_oid}::oid"
    ))
    .ok()
    .flatten()
    .unwrap_or(false)
}

fn vortex_layout_enabled(rel_oid: u32) -> bool {
    if table_denies_layout(rel_oid, "vortex") {
        return false;
    }
    // Default ON. Vortex is generally the fastest scan layout on large tables,
    // and the router only *uses* it when vortex files are present + authoritative
    // (vortex_availability) — otherwise it transparently falls back to the
    // canonical parquet vector path. Building it by default means the
    // datafusion_vortex / duck_vortex candidates are actually selectable.
    // Disable with RVBBIT_COMPACT_VORTEX_LAYOUT=off or rvbbit.compact_vortex_layout=off.
    matches!(
        compact_setting(
            "RVBBIT_COMPACT_VORTEX_LAYOUT",
            "rvbbit.compact_vortex_layout"
        )
        .unwrap_or_else(|| "on".to_string())
        .to_ascii_lowercase()
        .as_str(),
        "1" | "on" | "true" | "yes"
    )
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LayoutVariantSource {
    Heap,
    CanonicalParquet,
}

impl LayoutVariantSource {
    fn as_str(self) -> &'static str {
        match self {
            LayoutVariantSource::Heap => "heap",
            LayoutVariantSource::CanonicalParquet => "canonical_parquet",
        }
    }
}

fn layout_variant_source_setting() -> LayoutVariantSource {
    match compact_setting(
        "RVBBIT_LAYOUT_VARIANT_SOURCE",
        "rvbbit.layout_variant_source",
    )
    .unwrap_or_else(|| "canonical_parquet".to_string())
    .to_ascii_lowercase()
    .replace('-', "_")
    .as_str()
    {
        "heap" | "spi" | "postgres" => LayoutVariantSource::Heap,
        "canonical" | "parquet" | "canonical_parquet" | "mono_parquet" => {
            LayoutVariantSource::CanonicalParquet
        }
        _ => LayoutVariantSource::CanonicalParquet,
    }
}

fn layout_variant_parquet_batch_rows_setting(default_rows: usize) -> usize {
    compact_setting(
        "RVBBIT_LAYOUT_VARIANT_PARQUET_BATCH_ROWS",
        "rvbbit.layout_variant_parquet_batch_rows",
    )
    .and_then(|s| s.parse::<usize>().ok())
    .filter(|n| *n > 0)
    .unwrap_or(default_rows)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum HiveVariantMetadataProfile {
    Minimal,
    Rich,
}

impl HiveVariantMetadataProfile {
    fn as_str(self) -> &'static str {
        match self {
            HiveVariantMetadataProfile::Minimal => "minimal",
            HiveVariantMetadataProfile::Rich => "rich",
        }
    }

    fn write_options(self) -> RowGroupWriteOptions {
        match self {
            HiveVariantMetadataProfile::Minimal => RowGroupWriteOptions::minimal(),
            HiveVariantMetadataProfile::Rich => RowGroupWriteOptions::from_env(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CanonicalMetadataProfile {
    Minimal,
    Rich,
}

impl CanonicalMetadataProfile {
    fn as_str(self) -> &'static str {
        match self {
            CanonicalMetadataProfile::Minimal => "minimal",
            CanonicalMetadataProfile::Rich => "rich",
        }
    }

    fn parse(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().replace('-', "_").as_str() {
            "minimal" | "min" | "thin" | "fast" | "bulk" | "bulk_load" => {
                CanonicalMetadataProfile::Minimal
            }
            _ => CanonicalMetadataProfile::Rich,
        }
    }
}

fn canonical_metadata_profile(direct_import: bool) -> CanonicalMetadataProfile {
    let direct_profile = direct_import
        .then(|| {
            compact_setting(
                "RVBBIT_DIRECT_ACCEL_METADATA_PROFILE",
                "rvbbit.direct_accel_metadata_profile",
            )
        })
        .flatten();
    let raw = direct_profile
        .or_else(|| {
            compact_setting(
                "RVBBIT_COMPACT_METADATA_PROFILE",
                "rvbbit.compact_metadata_profile",
            )
        })
        .unwrap_or_else(|| "rich".to_string());
    CanonicalMetadataProfile::parse(&raw)
}

fn canonical_write_options(
    direct_import: bool,
) -> (RowGroupWriteOptions, CanonicalMetadataProfile) {
    let profile = canonical_metadata_profile(direct_import);
    let mut options = match profile {
        CanonicalMetadataProfile::Minimal => RowGroupWriteOptions::minimal(),
        CanonicalMetadataProfile::Rich => RowGroupWriteOptions::from_env(),
    };

    if let Some(value) =
        compact_bool_setting("RVBBIT_COMPACT_TEXT_STATS", "rvbbit.compact_text_stats")
    {
        options.text_stats = value;
    }
    if let Some(value) = compact_bool_setting(
        "RVBBIT_COMPACT_PER_GROUP_STATS",
        "rvbbit.compact_per_group_stats",
    ) {
        options.per_group_stats = value;
    }
    if let Some(value) = compact_bool_setting(
        "RVBBIT_COMPACT_VALUE_BITMAPS",
        "rvbbit.compact_value_bitmaps",
    ) {
        options.value_bitmaps = value;
    }
    if let Some(value) = compact_bool_setting(
        "RVBBIT_COMPACT_TEXT_DICTIONARIES",
        "rvbbit.compact_text_dictionaries",
    ) {
        options.text_dictionaries = value;
    }
    if let Some(value) = compact_bool_setting("RVBBIT_PARQUET_BLOOM", "rvbbit.parquet_bloom") {
        options.parquet_bloom = Some(value);
    }

    (options, profile)
}

fn hive_variant_metadata_profile() -> HiveVariantMetadataProfile {
    match compact_setting(
        "RVBBIT_HIVE_VARIANT_METADATA",
        "rvbbit.hive_variant_metadata",
    )
    .unwrap_or_else(|| "minimal".to_string())
    .to_ascii_lowercase()
    .replace('-', "_")
    .as_str()
    {
        "rich" | "full" | "canonical" => HiveVariantMetadataProfile::Rich,
        "minimal" | "min" | "none" | "duck" | "duck_only" => HiveVariantMetadataProfile::Minimal,
        _ => HiveVariantMetadataProfile::Minimal,
    }
}

fn identity_batch_rows_setting() -> usize {
    compact_setting(
        "RVBBIT_ACCEL_IDENTITY_BATCH_ROWS",
        "rvbbit.accel_identity_batch_rows",
    )
    .and_then(|s| s.parse::<usize>().ok())
    .filter(|n| (1..=100_000).contains(n))
    .unwrap_or(10_000)
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

fn sql_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
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
    let raw = compact_setting("RVBBIT_COMPACT_CLUSTER_KEYS", "rvbbit.compact_cluster_keys")?;
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

fn accepted_workload_layout_keys(
    rel_oid: u32,
    layout_kind: &str,
    plans: &[ColumnPlan],
) -> Vec<String> {
    if table_denies_layout(rel_oid, layout_kind)
        || !rvbbit_catalog_table_exists("workload_layout_recommendations")
    {
        return Vec::new();
    }
    let valid = |name: &str| {
        plans.iter().any(|p| {
            p.base_column
                && p.name == name
                && match layout_kind {
                    "cluster" => is_clusterable_type(p.pg_type),
                    "hive" => is_hive_partitionable_type(p.pg_type),
                    _ => false,
                }
        })
    };
    let sql = format!(
        "SELECT column_name \
         FROM rvbbit.workload_layout_recommendations \
         WHERE table_oid = {rel_oid}::oid \
           AND layout_kind = {} \
           AND status = 'accepted' \
         ORDER BY score DESC, updated_at DESC, column_name",
        sql_literal(layout_kind)
    );
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let rows = client.select(&sql, None, &[])?;
        for row in rows {
            let Some(name) = row.get::<String>(1)? else {
                continue;
            };
            if valid(&name) && !out.iter().any(|seen| seen == &name) {
                out.push(name);
            }
        }
        Ok(())
    });
    out
}

fn accepted_workload_layout_exists(rel_oid: u32, layout_kind: &str) -> bool {
    if table_denies_layout(rel_oid, layout_kind)
        || !rvbbit_catalog_table_exists("workload_layout_recommendations")
    {
        return false;
    }
    Spi::get_one::<bool>(&format!(
        "SELECT EXISTS ( \
             SELECT 1 FROM rvbbit.workload_layout_recommendations \
             WHERE table_oid = {rel_oid}::oid \
               AND layout_kind = {} \
               AND status = 'accepted' \
         )",
        sql_literal(layout_kind)
    ))
    .ok()
    .flatten()
    .unwrap_or(false)
}

/// Pick a small physical clustering key list without user DDL. This is a
/// storage-layout hint, not a semantic index: queries remain normal SQL, and
/// row-group min/max pruning consumes the tighter ranges later.
fn auto_cluster_keys(rel_oid: u32, plans: &[ColumnPlan]) -> Vec<String> {
    if let Some(keys) = override_cluster_keys(plans) {
        return keys.into_iter().take(cluster_variant_limit()).collect();
    }
    let accepted = accepted_workload_layout_keys(rel_oid, "cluster", plans);
    if !accepted.is_empty() {
        return accepted.into_iter().take(cluster_variant_limit()).collect();
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
    let accepted = accepted_workload_layout_keys(rel_oid, "hive", plans);
    if !accepted.is_empty() {
        return accepted.into_iter().take(hive_variant_limit()).collect();
    }
    if !hive_layout_enabled(rel_oid) {
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
    export_to_parquet_impl(rel, None, true)
}

#[pg_extern]
fn export_to_parquet_full_scan(rel: pg_sys::Oid) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_impl(rel, None, false)
}

#[pg_extern]
fn export_to_parquet_snapshot_visible(
    rel: pg_sys::Oid,
    snapshot: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_snapshot_filter(rel, snapshot, true)
}

#[pg_extern]
fn export_to_parquet_snapshot_gap(
    rel: pg_sys::Oid,
    snapshot: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_snapshot_filter(rel, snapshot, false)
}

#[pg_extern]
fn export_to_parquet_snapshot_visible_at(
    rel: pg_sys::Oid,
    snapshot: &str,
    first_rg_id: i64,
    generation: i64,
) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_snapshot_filter_at(rel, snapshot, true, Some((first_rg_id, generation)))
}

#[pg_extern]
fn export_to_parquet_snapshot_gap_at(
    rel: pg_sys::Oid,
    snapshot: &str,
    first_rg_id: i64,
    generation: i64,
) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_snapshot_filter_at(rel, snapshot, false, Some((first_rg_id, generation)))
}

#[pg_extern]
fn export_to_parquet_xid_range(
    rel: pg_sys::Oid,
    min_xid: &str,
    max_xid: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    let min_xid = xid_numeric_literal(min_xid)?;
    let max_xid = xid_numeric_literal(max_xid)?;
    if max_xid.parse::<u128>()? <= min_xid.parse::<u128>()? {
        return Ok(0);
    }
    // mvcc-08: compare in full-xid8 space (rvbbit.xid_to_fxid reconstructs the
    // 32-bit heap xmin against the current epoch) so the delta filter stays
    // monotonic across XID wraparound — a bare 32-bit xmin comparison silently
    // drops every new row once the watermark passes 2^32.
    let filter = format!(
        "rvbbit.xid_to_fxid(xmin) > {min_xid}::numeric \
         AND rvbbit.xid_to_fxid(xmin) <= {max_xid}::numeric"
    );
    export_to_parquet_impl(rel, Some(filter), false)
}

#[pg_extern]
fn import_canonical_parquet_chunks(
    rel: pg_sys::Oid,
    source_paths: Vec<Option<String>>,
    refresh_variants: default!(bool, "true"),
) -> Result<JsonB, Box<dyn std::error::Error>> {
    let import_start = Instant::now();
    let rel_oid = rel.to_u32();
    let source_paths: Vec<String> = source_paths
        .into_iter()
        .flatten()
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .collect();
    if source_paths.is_empty() {
        return Err("source_paths cannot be empty".into());
    }
    if identity_mode_for_export(rel_oid)?.is_some() {
        return Err(
            "rvbbit.import_canonical_parquet_chunks does not support row identity maps; \
             use RVBBIT_ACCEL_IDENTITY_MAP=primary_key/off for source-aware bulk loads"
                .into(),
        );
    }

    let qualified: String = Spi::get_one(&format!("SELECT {rel_oid}::oid::regclass::text"))?
        .ok_or("relation does not exist")?;
    let is_rvbbit = Spi::get_one::<bool>(&format!(
        "SELECT rvbbit.is_rvbbit_table({rel_oid}::oid::regclass)"
    ))?
    .unwrap_or(false);
    if !is_rvbbit {
        return Err(format!("{qualified} is not an rvbbit table").into());
    }

    Spi::run(&format!("LOCK TABLE {qualified} IN SHARE MODE"))?;
    Spi::run(&format!(
        "INSERT INTO rvbbit.acceleration_state (table_oid) \
         VALUES ({rel_oid}::oid) ON CONFLICT (table_oid) DO NOTHING"
    ))?;

    let existing_rgs = Spi::get_one::<i64>(&format!(
        "SELECT count(*)::bigint FROM rvbbit.row_groups WHERE table_oid = {rel_oid}::oid"
    ))?
    .unwrap_or(0);
    if existing_rgs > 0 {
        return Err(
            "rvbbit.import_canonical_parquet_chunks only supports fresh tables with no row groups"
                .into(),
        );
    }

    let safe_upper_xid = Spi::get_one::<String>(
        "SELECT greatest(0::numeric, \
                (pg_snapshot_xmin(pg_current_snapshot())::text)::numeric - 1)::text",
    )?
    .unwrap_or_else(|| "0".to_string());

    let op_id = create_refresh_operation(
        rel_oid,
        &qualified,
        serde_json::json!({
            "mode": "source_aware_direct_import",
            "source": "external_parquet",
            "source_files": source_paths.len(),
            "refresh_variants": refresh_variants,
            "watermark": "heap copied by loader; accelerator files imported from same source",
        }),
    )?;
    Spi::run(&format!(
        "SELECT set_config('rvbbit.acceleration_operation_id', {}, true)",
        sql_literal(&op_id.to_string())
    ))?;

    let mut plans = introspect_columns(rel_oid)?;
    extend_plans_with_legacy_shreds(&mut plans);
    if plans.iter().any(|plan| !plan.base_column) {
        return Err(
            "rvbbit.import_canonical_parquet_chunks does not support synthetic shred columns"
                .into(),
        );
    }
    let schema = schema_for_plans(&plans);

    let data_dir: String =
        Spi::get_one("SHOW data_directory")?.ok_or("data_directory GUC is NULL")?;
    let mut path_root = PathBuf::from(data_dir);
    path_root.push("rvbbit");
    path_root.push(rel_oid.to_string());
    std::fs::create_dir_all(&path_root)?;
    let scan_root = path_root.join(SCAN_LAYOUT_DIR);

    let first_rg_id = Spi::get_one::<i64>(&format!(
        "SELECT coalesce(max(rg_id), -1) + 1 \
         FROM rvbbit.row_groups WHERE table_oid = {rel_oid}::oid"
    ))?
    .unwrap_or(0);
    let generation = Spi::get_one::<i64>(&format!(
        "SELECT rvbbit.allocate_generation({rel_oid}::oid::regclass)"
    ))?
    .unwrap_or(1);

    let phase_id = start_acceleration_phase(
        rel_oid,
        &qualified,
        "canonical_delta_import",
        Some(SCAN_LAYOUT_DIR),
        None,
        serde_json::json!({
            "source": "external_parquet",
            "mode": "source_aware_direct_import",
            "source_files": source_paths.len(),
            "generation": generation,
        }),
    )?;
    if let Some(phase_id) = phase_id {
        Spi::run(&format!(
            "SELECT set_config('rvbbit.acceleration_phase_id', {}, true)",
            sql_literal(&phase_id.to_string())
        ))?;
    }

    let scan_chunk_rows = scan_chunk_rows_setting();
    let writer_threads = writer_threads_setting();
    let (write_options, metadata_profile) = canonical_write_options(true);
    let row_limit = import_row_limit_setting();
    let import_result = import_source_parquet_chunks(
        &source_paths,
        &plans,
        &schema,
        &scan_root,
        first_rg_id,
        scan_chunk_rows,
        writer_threads,
        row_limit,
        write_options,
    );
    let import_write = match import_result {
        Ok(import_write) => import_write,
        Err(err) => {
            let _ = finish_acceleration_phase(
                phase_id,
                "failed",
                0,
                0,
                0,
                None,
                None,
                serde_json::json!({"source": "external_parquet"}),
                Some(&err.to_string()),
            );
            return Err(err);
        }
    };
    let chunks = import_write.chunks;
    let (rows_written, files_written, bytes_written) = variant_chunk_totals(&chunks);
    register_primary_chunks(rel_oid, &chunks, generation)?;
    if rows_written > 0 {
        Spi::run(&format!(
            "INSERT INTO rvbbit.generations (table_oid, generation, n_rows, n_row_groups) \
             VALUES ({rel_oid}::oid, {generation}, {rows_written}, {files_written})"
        ))?;
    }

    finish_acceleration_phase(
        phase_id,
        "ok",
        rows_written,
        files_written,
        bytes_written,
        Some(rows_written),
        Some(rows_written),
        serde_json::json!({
            "generation": generation,
            "source_files": source_paths.len(),
            "chunk_rows": scan_chunk_rows,
            "writer_threads": writer_threads,
            "metadata_profile": metadata_profile.as_str(),
            "row_limit": row_limit,
            "import_seconds": elapsed_seconds_since(import_start),
            "import_timing": {
                "source_open_seconds": import_write.timings.source_open_seconds,
                "source_read_seconds": import_write.timings.source_read_seconds,
                "source_canonicalize_seconds": import_write.timings.source_canonicalize_seconds,
                "writer_wait_seconds": import_write.timings.writer_wait_seconds,
                "writer_join_seconds": import_write.timings.writer_join_seconds,
                "writer_seconds_sum": import_write.timings.writer_seconds_sum,
                "writer_seconds_max": import_write.timings.writer_seconds_max
            },
        }),
        None,
    )?;

    let mut variants_rows = None;
    if refresh_variants && rows_written > 0 {
        let built = refresh_layout_variants_impl(rel_oid, &qualified, &plans, &schema, &path_root)?;
        variants_rows = Some(built);
    }

    Spi::run(&format!(
        "UPDATE rvbbit.tables \
            SET shadow_heap_retained = true, \
                shadow_heap_dirty = false, \
                dirty_has_insert = false, \
                dirty_has_update = false, \
                dirty_has_delete = false, \
                dirty_has_truncate = false, \
                next_generation = greatest(next_generation, {generation} + 1) \
          WHERE table_oid = {rel_oid}::oid"
    ))?;
    Spi::run(&format!(
        "SELECT rvbbit.clear_table_dirty_markers({rel_oid}::oid)"
    ))?;
    Spi::run(&format!(
        "SELECT rvbbit.install_shadow_heap_dirty_triggers({rel_oid}::oid::regclass)"
    ))?;
    Spi::run(&format!(
        "UPDATE rvbbit.acceleration_state \
            SET last_refresh_xid = {safe_upper_xid}, \
                last_refresh_generation = {generation}, \
                last_refresh_at = clock_timestamp(), \
                updated_at = clock_timestamp() \
          WHERE table_oid = {rel_oid}::oid"
    ))?;
    Spi::run(&format!(
        "UPDATE rvbbit.acceleration_operations \
            SET status = 'ok', \
                finished_at = clock_timestamp(), \
                rows_written = {rows_written}, \
                row_groups_written = {files_written}, \
                watermark_after = {safe_upper_xid}, \
                generation_after = {generation} \
          WHERE id = {op_id}"
    ))?;
    Spi::run("SELECT set_config('rvbbit.acceleration_phase_id', '', true)")?;
    Spi::run("SELECT set_config('rvbbit.acceleration_operation_id', '', true)")?;

    crate::custom_scan::invalidate_scan_metadata(rel_oid);
    crate::planner::invalidate_planner_aggregates(rel_oid);
    crate::columnar_cache::invalidate_table(rel_oid);
    crate::df::invalidate_registration();
    crate::live_counters::bump_scan_epoch_on_commit();

    Ok(JsonB(serde_json::json!({
        "status": "ok",
        "operation_id": op_id,
        "table": qualified,
        "source_files": source_paths.len(),
        "rows_written": rows_written,
        "row_groups_written": files_written,
        "bytes_written": bytes_written,
        "generation_after": generation,
        "watermark_after": safe_upper_xid,
        "variants_rows": variants_rows,
        "metadata_profile": metadata_profile.as_str(),
        "row_limit": row_limit,
        "timing": {
            "source_open_seconds": import_write.timings.source_open_seconds,
            "source_read_seconds": import_write.timings.source_read_seconds,
            "source_canonicalize_seconds": import_write.timings.source_canonicalize_seconds,
            "writer_wait_seconds": import_write.timings.writer_wait_seconds,
            "writer_join_seconds": import_write.timings.writer_join_seconds,
            "writer_seconds_sum": import_write.timings.writer_seconds_sum,
            "writer_seconds_max": import_write.timings.writer_seconds_max,
        },
        "seconds": elapsed_seconds_since(import_start),
    })))
}

fn export_to_parquet_snapshot_filter(
    rel: pg_sys::Oid,
    snapshot: &str,
    visible: bool,
) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_snapshot_filter_at(rel, snapshot, visible, None)
}

fn export_to_parquet_snapshot_filter_at(
    rel: pg_sys::Oid,
    snapshot: &str,
    visible: bool,
    fixed_ids: Option<(i64, i64)>,
) -> Result<i64, Box<dyn std::error::Error>> {
    let snapshot = snapshot.trim();
    if snapshot.is_empty() {
        return Err("snapshot cannot be empty".into());
    }
    // The rebuild/fold SQL owns the snapshot text. pg_visible_in_snapshot gives
    // us the exact logical split between the long baseline scan and the short
    // final catch-up scan without needing to hold a table lock for the baseline.
    let visible_expr = format!(
        "pg_visible_in_snapshot((rvbbit.xid_to_fxid(xmin)::text)::xid8, {}::pg_snapshot)",
        sql_literal(snapshot)
    );
    let filter = if visible {
        visible_expr
    } else {
        format!("NOT ({visible_expr})")
    };
    export_to_parquet_impl_at(rel, Some(filter), false, fixed_ids)
}

fn xid_numeric_literal(raw: &str) -> Result<String, Box<dyn std::error::Error>> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("xid watermark cannot be empty".into());
    }
    let parsed: u128 = trimmed.parse()?;
    Ok(parsed.to_string())
}

fn create_refresh_operation(
    rel_oid: u32,
    qualified: &str,
    settings: serde_json::Value,
) -> Result<i64, Box<dyn std::error::Error>> {
    let settings_json = serde_json::to_string(&settings)?;
    let op_id = Spi::get_one::<i64>(&format!(
        "INSERT INTO rvbbit.acceleration_operations \
             (table_oid, table_name, operation, status, watermark_before, settings) \
         VALUES ({rel_oid}::oid, {}, 'refresh_acceleration', 'running', 0, {}::jsonb) \
         RETURNING id",
        sql_literal(qualified),
        sql_literal(&settings_json)
    ))?
    .ok_or("failed to create acceleration operation")?;
    Ok(op_id)
}

fn import_source_parquet_chunks(
    source_paths: &[String],
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
    scan_root: &PathBuf,
    first_rg_id: i64,
    batch_size: usize,
    writer_threads: usize,
    row_limit: Option<usize>,
    write_options: RowGroupWriteOptions,
) -> Result<ImportWriteResult, Box<dyn std::error::Error>> {
    let mut chunks = Vec::new();
    let mut rg_id = first_rg_id;
    let mut timings = LayoutWriteTimings::default();
    let mut writer_handles = ChunkWriterQueue::default();
    let mut rows_remaining = row_limit;
    'sources: for source_path in source_paths {
        let open_start = Instant::now();
        let file = File::open(source_path)
            .map_err(|e| format!("opening source parquet {source_path}: {e}"))?;
        let mut reader = ParquetRecordBatchReaderBuilder::try_new(file)
            .map_err(|e| format!("opening source parquet reader {source_path}: {e}"))?
            .with_batch_size(batch_size)
            .build()
            .map_err(|e| format!("building source parquet reader {source_path}: {e}"))?;
        timings.source_open_seconds += elapsed_seconds_since(open_start);
        loop {
            let read_start = Instant::now();
            let Some(batch) = reader.next() else {
                timings.source_read_seconds += elapsed_seconds_since(read_start);
                break;
            };
            timings.source_read_seconds += elapsed_seconds_since(read_start);
            let batch = batch.map_err(|e| format!("reading source parquet {source_path}: {e}"))?;
            if batch.num_rows() == 0 {
                continue;
            }
            let batch = if let Some(remaining) = rows_remaining {
                if remaining == 0 {
                    break 'sources;
                }
                let take_rows = remaining.min(batch.num_rows());
                rows_remaining = Some(remaining - take_rows);
                batch.slice(0, take_rows)
            } else {
                batch
            };
            let canonicalize_start = Instant::now();
            let canonical = canonicalize_import_batch(&batch, plans, schema)?;
            timings.source_canonicalize_seconds += elapsed_seconds_since(canonicalize_start);
            let chunk_path = scan_root.join(format!("{rg_id}.parquet"));
            enqueue_chunk_writer(
                &mut writer_handles,
                &mut chunks,
                &mut timings,
                writer_threads,
                chunk_path,
                rg_id,
                canonical,
                write_options,
            )?;
            rg_id += 1;
        }
    }
    drain_chunk_writers(&mut writer_handles, &mut chunks, &mut timings)?;
    chunks.sort_by_key(|meta| meta.rg_id);
    Ok(ImportWriteResult { chunks, timings })
}

fn import_row_limit_setting() -> Option<usize> {
    compact_setting("RVBBIT_IMPORT_ROW_LIMIT", "rvbbit.import_row_limit")
        .and_then(|raw| raw.trim().parse::<usize>().ok())
}

fn canonicalize_import_batch(
    batch: &RecordBatch,
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
) -> Result<RecordBatch, Box<dyn std::error::Error>> {
    let source_schema = batch.schema();
    let epoch_seconds_columns = import_epoch_seconds_columns();
    let mut arrays = Vec::with_capacity(plans.len());
    for plan in plans {
        let source_idx = source_schema.index_of(&plan.name).map_err(|_| {
            format!(
                "source parquet is missing column '{}' required by target table",
                plan.name
            )
        })?;
        arrays.push(canonicalize_import_array(
            batch.column(source_idx).as_ref(),
            &plan.arrow_type,
            plan.pg_type,
            &plan.name,
            &epoch_seconds_columns,
        )?);
    }
    Ok(RecordBatch::try_new(schema.clone(), arrays)?)
}

fn import_epoch_seconds_columns() -> HashSet<String> {
    let Some(raw) = compact_setting(
        "RVBBIT_IMPORT_EPOCH_SECONDS_COLUMNS",
        "rvbbit.import_epoch_seconds_columns",
    ) else {
        return HashSet::new();
    };
    if parse_bool_setting(&raw).is_some_and(|enabled| !enabled) {
        return HashSet::new();
    }
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

fn canonicalize_import_array(
    array: &dyn Array,
    target_type: &DataType,
    pg_type: u32,
    column_name: &str,
    epoch_seconds_columns: &HashSet<String>,
) -> Result<ArrayRef, Box<dyn std::error::Error>> {
    if array.data_type() == target_type {
        return Ok(make_array(array.to_data()));
    }
    if matches!(target_type, DataType::Timestamp(TimeUnit::Microsecond, _))
        && epoch_seconds_columns.contains(column_name)
    {
        return epoch_seconds_array_to_timestamp_micros(array, target_type, column_name);
    }
    if matches!(target_type, DataType::Binary) && pg_type == 3802 {
        return jsonb_text_array_to_binary_body(array, column_name);
    }
    if matches!(target_type, DataType::Int32) && matches!(array.data_type(), DataType::Date32) {
        return date32_array_to_int32(array, column_name);
    }
    let source_array = make_array(array.to_data());
    cast(&source_array, target_type).map_err(|e| {
        format!(
            "cannot cast source column '{}' from {:?} to canonical {:?}: {e}",
            column_name,
            array.data_type(),
            target_type
        )
        .into()
    })
}

fn epoch_seconds_array_to_timestamp_micros(
    array: &dyn Array,
    target_type: &DataType,
    column_name: &str,
) -> Result<ArrayRef, Box<dyn std::error::Error>> {
    let source_array = make_array(array.to_data());
    let casted = cast(&source_array, &DataType::Int64).map_err(|e| {
        format!(
            "cannot cast epoch-second timestamp source column '{}' from {:?} to int64: {e}",
            column_name,
            array.data_type()
        )
    })?;
    let seconds = casted
        .as_any()
        .downcast_ref::<Int64Array>()
        .ok_or_else(|| format!("column '{column_name}' epoch-second cast did not produce Int64"))?;
    let mut builder = match target_type {
        DataType::Timestamp(TimeUnit::Microsecond, Some(tz)) => {
            TimestampMicrosecondBuilder::new().with_timezone(tz.to_string())
        }
        _ => TimestampMicrosecondBuilder::new(),
    };
    for row_idx in 0..seconds.len() {
        if seconds.is_null(row_idx) {
            builder.append_null();
        } else {
            let micros = seconds
                .value(row_idx)
                .checked_mul(1_000_000)
                .ok_or_else(|| {
                    format!("epoch-second timestamp column '{column_name}' overflows micros")
                })?;
            builder.append_value(micros);
        }
    }
    Ok(Arc::new(builder.finish()))
}

fn date32_array_to_int32(
    array: &dyn Array,
    column_name: &str,
) -> Result<ArrayRef, Box<dyn std::error::Error>> {
    let Some(date_array) = array.as_any().downcast_ref::<Date32Array>() else {
        return Err(format!("column '{column_name}' is not a Date32Array").into());
    };
    let mut builder = Int32Builder::new();
    for row_idx in 0..date_array.len() {
        if date_array.is_null(row_idx) {
            builder.append_null();
        } else {
            builder.append_value(date_array.value(row_idx));
        }
    }
    Ok(Arc::new(builder.finish()))
}

fn jsonb_text_array_to_binary_body(
    array: &dyn Array,
    column_name: &str,
) -> Result<ArrayRef, Box<dyn std::error::Error>> {
    let string_array = if matches!(array.data_type(), DataType::Utf8) {
        array
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| format!("column '{column_name}' is not a StringArray"))?
            .clone()
    } else {
        let source_array = make_array(array.to_data());
        let casted = cast(&source_array, &DataType::Utf8).map_err(|e| {
            format!(
                "cannot cast jsonb source column '{}' from {:?} to text: {e}",
                column_name,
                array.data_type()
            )
        })?;
        casted
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| format!("column '{column_name}' cast did not produce StringArray"))?
            .clone()
    };
    let mut builder = BinaryBuilder::new();
    for row_idx in 0..string_array.len() {
        if string_array.is_null(row_idx) {
            builder.append_null();
        } else {
            let Some(body) = (unsafe { jsonb_text_to_binary_body(string_array.value(row_idx)) })
            else {
                return Err(format!("could not canonicalize jsonb column '{column_name}'").into());
            };
            builder.append_value(body);
        }
    }
    Ok(Arc::new(builder.finish()))
}

fn export_to_parquet_impl(
    rel: pg_sys::Oid,
    heap_filter: Option<String>,
    refresh_sync_variants: bool,
) -> Result<i64, Box<dyn std::error::Error>> {
    export_to_parquet_impl_at(rel, heap_filter, refresh_sync_variants, None)
}

fn export_to_parquet_impl_at(
    rel: pg_sys::Oid,
    heap_filter: Option<String>,
    refresh_sync_variants: bool,
    fixed_ids: Option<(i64, i64)>,
) -> Result<i64, Box<dyn std::error::Error>> {
    let export_start = Instant::now();
    let rel_oid = rel.to_u32();
    let scan_chunk_rows = scan_chunk_rows_setting();
    let writer_threads = writer_threads_setting();
    let setup_start = Instant::now();

    let qualified: String = Spi::get_one(&format!("SELECT {rel_oid}::oid::regclass::text"))?
        .ok_or("relation does not exist")?;
    let identity_mode = identity_mode_for_export(rel_oid)?;
    let heap_filter_present = heap_filter.is_some();
    let scan_source = match (heap_filter, identity_mode.as_deref()) {
        (Some(filter), Some("ctid")) => format!(
            "(SELECT *, ctid::text AS {} FROM {qualified} WHERE {filter}) AS rvbbit_delta",
            quote_ident(EXPORT_CTID_COLUMN)
        ),
        (None, Some("ctid")) => format!(
            "(SELECT *, ctid::text AS {} FROM {qualified}) AS rvbbit_full",
            quote_ident(EXPORT_CTID_COLUMN)
        ),
        (Some(filter), _) => format!("(SELECT * FROM {qualified} WHERE {filter}) AS rvbbit_delta"),
        (None, _) => qualified.clone(),
    };

    let (first_rg_id, generation): (i64, i64) = if let Some((first_rg_id, generation)) = fixed_ids {
        if first_rg_id < 0 {
            return Err("first_rg_id must be non-negative".into());
        }
        if generation <= 0 {
            return Err("generation must be positive".into());
        }
        (first_rg_id, generation)
    } else {
        // Phase 2 generation allocation. The advisory_xact_lock is per-table:
        // class id 0x52564254 (ASCII "RVBT") + table_oid packed into one
        // bigint, so two concurrent compacts on the SAME table serialize but
        // two on DIFFERENT tables proceed in parallel. UPDATE...RETURNING gives
        // us the value BEFORE the increment, which is the generation we stamp
        // on this compaction's row groups.
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

        // concurrency-03/mvcc-09: choose first_rg_id only AFTER the per-table
        // advisory lock above is held. The lock serializes concurrent compacts
        // on the same table, so a second compaction blocks until the first
        // commits and then reads its row groups — otherwise both could pick the
        // same rg_id and overwrite each other's parquet files.
        let first_rg_id: i64 = Spi::get_one(&format!(
            "SELECT coalesce(max(rg_id), -1) + 1 \
                 FROM rvbbit.row_groups WHERE table_oid = {rel_oid}::oid"
        ))?
        .unwrap_or(0);
        (first_rg_id, generation)
    };

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
    let setup_seconds = elapsed_seconds_since(setup_start);
    let write_start = Instant::now();
    let write_result = write_layout_chunks(
        rel_oid,
        &scan_source,
        &plans,
        &schema,
        &scan_root,
        first_rg_id,
        scan_chunk_rows,
        generation,
        identity_mode.as_deref(),
        &[],
    )?;
    let write_layout_seconds = elapsed_seconds_since(write_start);
    let register_start = Instant::now();
    register_primary_chunks(rel_oid, &write_result.chunks, generation)?;
    let register_seconds = elapsed_seconds_since(register_start);

    let mut total_rows: i64 = 0;
    for meta in &write_result.chunks {
        total_rows += meta.n_rows;
    }

    // Phase 2 slice 7: record the generation timeline so AS OF TIMESTAMP
    // queries (rvbbit.set_as_of) can resolve a wall-clock time to the right
    // generation. We only INSERT when this compaction actually wrote rows;
    // a no-op compact (empty heap) doesn't extend the timeline.
    let generation_insert_start = Instant::now();
    if total_rows > 0 {
        let n_groups = write_result.chunks.len() as i32;
        Spi::run(&format!(
            "INSERT INTO rvbbit.generations (table_oid, generation, n_rows, n_row_groups) \
             VALUES ({rel_oid}::oid, {generation}, {total_rows}, {n_groups})"
        ))?;
    }
    let generation_insert_seconds = elapsed_seconds_since(generation_insert_start);

    let sync_variants_start = Instant::now();
    if refresh_sync_variants && sync_variant_layouts_enabled() && total_rows > 0 {
        refresh_layout_variants_impl(rel_oid, &qualified, &plans, &schema, &path_root)?;
    }
    let sync_variants_seconds = elapsed_seconds_since(sync_variants_start);

    let post_export_start = Instant::now();
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

    // Row groups (and possibly the snapshot visibility floor) just changed.
    // Drop backend-local caches so the same session re-plans from the new
    // state instead of serving a stale file list / registration. The variant
    // refresh paths invalidate too, but a plain compact (e.g. snapshot_load's
    // two-arg compact, no variant rebuild) ends here — and a rapid
    // snapshot_load loop in one session would otherwise read stale.
    crate::custom_scan::invalidate_scan_metadata(rel_oid);
    crate::planner::invalidate_planner_aggregates(rel_oid);
    crate::columnar_cache::invalidate_table(rel_oid);
    crate::df::invalidate_registration();
    // Cross-backend: tell OTHER backends (e.g. pooled UI connections) their scan
    // caches are stale so they don't keep serving the pre-compact row groups.
    crate::live_counters::bump_scan_epoch_on_commit();

    // Keep-cold: if this table is pinned to a cold tier, re-upload the freshly
    // written local row groups so it stays on object storage automatically.
    crate::storage::maybe_reoffload_cold(rel_oid);
    // Read fleet: publish the new generation to the shared store (dual-presence
    // — local files keep serving this node; the published copies serve warrens).
    crate::storage::maybe_publish(rel_oid);
    let post_export_seconds = elapsed_seconds_since(post_export_start);

    update_current_acceleration_phase_details(serde_json::json!({
        "canonical_timing": {
            "export_total_seconds": elapsed_seconds_since(export_start),
            "setup_seconds": setup_seconds,
            "write_layout_wall_seconds": write_layout_seconds,
            "spi_select_seconds": write_result.timings.spi_select_seconds,
            "row_build_seconds": write_result.timings.row_build_seconds,
            "finish_batch_seconds": write_result.timings.finish_batch_seconds,
            "writer_wait_seconds": write_result.timings.writer_wait_seconds,
            "writer_join_seconds": write_result.timings.writer_join_seconds,
            "writer_seconds_sum": write_result.timings.writer_seconds_sum,
            "writer_seconds_max": write_result.timings.writer_seconds_max,
            "identity_insert_seconds": write_result.timings.identity_insert_seconds,
            "register_seconds": register_seconds,
            "generation_insert_seconds": generation_insert_seconds,
            "sync_variants_seconds": sync_variants_seconds,
            "post_export_seconds": post_export_seconds
        },
        "chunk_rows": scan_chunk_rows,
        "writer_threads": writer_threads,
        "identity_mode": identity_mode,
        "heap_filter": heap_filter_present,
        "row_groups_written": write_result.chunks.len(),
    }))?;

    Ok(total_rows)
}

/// Unlink a batch of row-group files from disk. Used by rvbbit.reap_generations
/// AFTER the corresponding catalog rows are deleted, so the unlink is
/// orphan-safe (nothing references the files). Missing files are ignored;
/// returns the count actually removed.
#[pgrx::pg_extern]
fn reap_unlink_files(paths: Option<Vec<Option<String>>>) -> i32 {
    let mut n = 0i32;
    if let Some(ps) = paths {
        for p in ps.into_iter().flatten() {
            if std::fs::remove_file(&p).is_ok() {
                n += 1;
            }
        }
    }
    n
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

#[pg_extern]
fn refresh_layout_variants_xid_range(
    rel: pg_sys::Oid,
    min_xid: &str,
    max_xid: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    let rel_oid = rel.to_u32();
    let min_xid = xid_numeric_literal(min_xid)?;
    let max_xid = xid_numeric_literal(max_xid)?;
    if max_xid.parse::<u128>()? <= min_xid.parse::<u128>()? {
        return Ok(0);
    }
    let qualified: String = Spi::get_one(&format!("SELECT {rel_oid}::oid::regclass::text"))?
        .ok_or("relation does not exist")?;
    let delta_source = format!(
        "(SELECT * FROM {qualified} \
          WHERE (xmin::text)::numeric > {min_xid}::numeric \
            AND (xmin::text)::numeric <= {max_xid}::numeric) AS rvbbit_variant_delta"
    );

    let mut plans = introspect_columns(rel_oid)?;
    extend_plans_with_legacy_shreds(&mut plans);
    let schema = schema_for_plans(&plans);

    let data_dir: String =
        Spi::get_one("SHOW data_directory")?.ok_or("data_directory GUC is NULL")?;
    let mut path_root = PathBuf::from(data_dir);
    path_root.push("rvbbit");
    path_root.push(rel_oid.to_string());
    std::fs::create_dir_all(&path_root)?;

    refresh_layout_variants_delta_impl(
        rel_oid,
        &qualified,
        &delta_source,
        &plans,
        &schema,
        &path_root,
    )
}

fn refresh_layout_variants_impl(
    rel_oid: u32,
    qualified: &str,
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
    path_root: &PathBuf,
) -> Result<i64, Box<dyn std::error::Error>> {
    let cluster_chunk_rows = chunk_rows_setting();
    let scan_chunk_rows = scan_chunk_rows_setting();
    let cluster_keys = auto_cluster_keys(rel_oid, plans);
    let hive_keys = auto_hive_partition_keys(rel_oid, plans);
    let workload_cluster_enabled = accepted_workload_layout_exists(rel_oid, "cluster");
    let workload_hive_enabled = accepted_workload_layout_exists(rel_oid, "hive");
    let hive_source = layout_variant_source_setting();
    let hive_parquet_batch_rows =
        layout_variant_parquet_batch_rows_setting(scan_chunk_rows.max(cluster_chunk_rows));
    let hive_metadata_profile = hive_variant_metadata_profile();
    let mut rows_written = 0_i64;

    if (dual_layout_enabled(rel_oid) || workload_cluster_enabled) && !cluster_keys.is_empty() {
        for cluster_key in cluster_keys.iter().take(cluster_variant_limit()) {
            let layout = cluster_layout_for_key(cluster_key);
            let phase_id = start_acceleration_phase(
                rel_oid,
                qualified,
                "layout_variant_rebuild",
                Some(&layout),
                Some(cluster_key),
                serde_json::json!({
                    "layout_kind": "cluster",
                    "build_mode": "full_rebuild",
                    "source": "heap",
                    "cluster_key": cluster_key,
                    "selected_cluster_keys": &cluster_keys,
                    "chunk_rows": cluster_chunk_rows,
                }),
            )?;
            let build_result = (|| -> Result<_, Box<dyn std::error::Error>> {
                clear_variant_layout(rel_oid, &layout, path_root)?;
                let cluster_root = path_root.join(layout_dir_name(&layout));
                let variant_chunks = write_layout_chunks(
                    rel_oid,
                    qualified,
                    plans,
                    schema,
                    &cluster_root,
                    0,
                    cluster_chunk_rows,
                    0,
                    None,
                    std::slice::from_ref(cluster_key),
                )?
                .chunks;
                Ok(variant_chunks)
            })();
            let variant_chunks = match build_result {
                Ok(chunks) => chunks,
                Err(err) => {
                    let _ = finish_acceleration_phase(
                        phase_id,
                        "failed",
                        0,
                        0,
                        0,
                        None,
                        None,
                        serde_json::json!({"layout_kind": "cluster"}),
                        Some(&err.to_string()),
                    );
                    return Err(err);
                }
            };
            let (variant_rows, variant_files, variant_bytes) =
                variant_chunk_totals(&variant_chunks);
            let expected_rows = canonical_row_count(rel_oid)?;
            let valid = validate_variant_chunks(rel_oid, &layout, &variant_chunks)?;
            if valid {
                rows_written += variant_rows;
                register_variant_chunks(rel_oid, &layout, &variant_chunks)?;
                mark_variant_status_ready(rel_oid, &layout, &variant_chunks)?;
            }
            finish_acceleration_phase(
                phase_id,
                if valid { "ok" } else { "invalid" },
                variant_rows,
                variant_files,
                variant_bytes,
                Some(expected_rows),
                Some(variant_rows),
                serde_json::json!({"validated": valid}),
                None,
            )?;
        }
    }

    if (hive_layout_enabled(rel_oid) || workload_hive_enabled) && !hive_keys.is_empty() {
        for hive_key in hive_keys.iter().take(hive_variant_limit()) {
            let layout = hive_layout_for_key(hive_key);
            let phase_id = start_acceleration_phase(
                rel_oid,
                qualified,
                "layout_variant_rebuild",
                Some(&layout),
                Some(hive_key),
                serde_json::json!({
                    "layout_kind": "hive",
                    "build_mode": "full_rebuild",
                    "source": hive_source.as_str(),
                    "hive_key": hive_key,
                    "selected_hive_keys": &hive_keys,
                    "chunk_rows": cluster_chunk_rows,
                    "parquet_batch_rows": hive_parquet_batch_rows,
                    "metadata_profile": hive_metadata_profile.as_str(),
                }),
            )?;
            let build_result = (|| -> Result<_, Box<dyn std::error::Error>> {
                clear_variant_layout(rel_oid, &layout, path_root)?;
                let hive_root = path_root.join(layout_dir_name(&layout));
                let variant_chunks = match hive_source {
                    LayoutVariantSource::Heap => write_hive_layout_chunks(
                        qualified,
                        plans,
                        &hive_root,
                        0,
                        cluster_chunk_rows,
                        hive_key,
                        hive_metadata_profile.write_options(),
                    )?,
                    LayoutVariantSource::CanonicalParquet => {
                        write_hive_layout_chunks_from_canonical_parquet(
                            rel_oid,
                            plans,
                            &hive_root,
                            0,
                            cluster_chunk_rows,
                            hive_parquet_batch_rows,
                            hive_key,
                            hive_metadata_profile.write_options(),
                        )?
                    }
                };
                Ok(variant_chunks)
            })();
            let variant_chunks = match build_result {
                Ok(chunks) => chunks,
                Err(err) => {
                    let _ = finish_acceleration_phase(
                        phase_id,
                        "failed",
                        0,
                        0,
                        0,
                        None,
                        None,
                        serde_json::json!({"layout_kind": "hive"}),
                        Some(&err.to_string()),
                    );
                    return Err(err);
                }
            };
            let (variant_rows, variant_files, variant_bytes) =
                variant_chunk_totals(&variant_chunks);
            let expected_rows = canonical_row_count(rel_oid)?;
            let valid = validate_variant_chunks(rel_oid, &layout, &variant_chunks)?;
            if valid {
                rows_written += variant_rows;
                register_variant_chunks(rel_oid, &layout, &variant_chunks)?;
                mark_variant_status_ready(rel_oid, &layout, &variant_chunks)?;
            }
            finish_acceleration_phase(
                phase_id,
                if valid { "ok" } else { "invalid" },
                variant_rows,
                variant_files,
                variant_bytes,
                Some(expected_rows),
                Some(variant_rows),
                serde_json::json!({
                    "validated": valid,
                    "source": hive_source.as_str(),
                    "parquet_batch_rows": hive_parquet_batch_rows,
                    "metadata_profile": hive_metadata_profile.as_str(),
                }),
                None,
            )?;
        }
    }

    if vortex_layout_enabled(rel_oid) {
        let layout = VORTEX_SCAN_LAYOUT;
        let phase_id = start_acceleration_phase(
            rel_oid,
            qualified,
            "format_variant_rebuild",
            Some(layout),
            None,
            serde_json::json!({
                "layout_kind": "vortex",
                "build_mode": "full_rebuild",
                "source": "canonical_parquet",
                "file_extension": "vortex",
            }),
        )?;
        let build_result = (|| -> Result<_, Box<dyn std::error::Error>> {
            clear_variant_layout(rel_oid, layout, path_root)?;
            let vortex_root = path_root.join(layout_dir_name(layout));
            write_vortex_scan_chunks_from_canonical_parquet(
                rel_oid,
                plans,
                &vortex_root,
                layout,
                false,
            )
        })();
        let variant_chunks = match build_result {
            Ok(chunks) => chunks,
            Err(err) => {
                let _ = finish_acceleration_phase(
                    phase_id,
                    "failed",
                    0,
                    0,
                    0,
                    None,
                    None,
                    serde_json::json!({"layout_kind": "vortex"}),
                    Some(&err.to_string()),
                );
                return Err(err);
            }
        };
        let (variant_rows, variant_files, variant_bytes) = variant_chunk_totals(&variant_chunks);
        let expected_rows = canonical_row_count(rel_oid)?;
        let valid = validate_variant_chunks(rel_oid, layout, &variant_chunks)?;
        if valid {
            rows_written += variant_rows;
            register_variant_chunks(rel_oid, layout, &variant_chunks)?;
            mark_variant_status_ready(rel_oid, layout, &variant_chunks)?;
        }
        finish_acceleration_phase(
            phase_id,
            if valid { "ok" } else { "invalid" },
            variant_rows,
            variant_files,
            variant_bytes,
            Some(expected_rows),
            Some(variant_rows),
            serde_json::json!({
                "validated": valid,
                "source": "canonical_parquet",
                "file_extension": "vortex",
            }),
            None,
        )?;
    }

    // Drop backend-local caches that depend on rvbbit.row_groups state.
    // Without this, the same session would keep planning + scanning from
    // the pre-compact metadata snapshot.
    crate::planner::invalidate_planner_aggregates(rel_oid);
    crate::custom_scan::invalidate_scan_metadata(rel_oid);
    crate::columnar_cache::invalidate_table(rel_oid);
    crate::live_counters::bump_scan_epoch_on_commit();

    Ok(rows_written)
}

fn refresh_layout_variants_delta_impl(
    rel_oid: u32,
    qualified: &str,
    delta_source: &str,
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
    path_root: &PathBuf,
) -> Result<i64, Box<dyn std::error::Error>> {
    if dual_layout_enabled(rel_oid) || accepted_workload_layout_exists(rel_oid, "cluster") {
        return refresh_layout_variants_impl(rel_oid, qualified, plans, schema, path_root);
    }

    let chunk_rows = chunk_rows_setting();
    let hive_keys = auto_hive_partition_keys(rel_oid, plans);
    let hive_metadata_profile = hive_variant_metadata_profile();
    if !(hive_layout_enabled(rel_oid) || accepted_workload_layout_exists(rel_oid, "hive"))
        || hive_keys.is_empty()
    {
        if vortex_layout_enabled(rel_oid) {
            return refresh_vortex_scan_delta_impl(rel_oid, qualified, path_root);
        }
        return Ok(0);
    }

    let selected_hive_keys: Vec<String> =
        hive_keys.into_iter().take(hive_variant_limit()).collect();
    let all_ready = selected_hive_keys
        .iter()
        .map(|key| hive_layout_for_key(key))
        .all(|layout| variant_layout_ready(rel_oid, &layout));
    if !all_ready {
        let phase_id = start_acceleration_phase(
            rel_oid,
            qualified,
            "layout_variant_delta_append",
            None,
            None,
            serde_json::json!({
                "layout_kind": "hive",
                "build_mode": "delta_append",
                "source": "heap_delta",
                "selected_hive_keys": &selected_hive_keys,
                "fallback": "full_rebuild",
                "reason": "one or more selected hive layouts are not ready",
            }),
        )?;
        finish_acceleration_phase(
            phase_id,
            "skipped",
            0,
            0,
            0,
            Some(canonical_row_count(rel_oid)?),
            None,
            serde_json::json!({"fallback": "full_rebuild"}),
            None,
        )?;
        return refresh_layout_variants_impl(rel_oid, qualified, plans, schema, path_root);
    }

    let mut rows_written = 0_i64;
    for hive_key in &selected_hive_keys {
        let layout = hive_layout_for_key(hive_key);
        let first_rg_id: i64 = Spi::get_one(&format!(
            "SELECT coalesce(max(rg_id), -1) + 1 \
             FROM rvbbit.row_group_variants \
             WHERE table_oid = {rel_oid}::oid AND layout = {}",
            sql_literal(&layout)
        ))?
        .unwrap_or(0);
        let phase_id = start_acceleration_phase(
            rel_oid,
            qualified,
            "layout_variant_delta_append",
            Some(&layout),
            Some(hive_key),
            serde_json::json!({
                "layout_kind": "hive",
                "build_mode": "delta_append",
                "source": "heap_delta",
                "hive_key": hive_key,
                "selected_hive_keys": &selected_hive_keys,
                "first_rg_id": first_rg_id,
                "chunk_rows": chunk_rows,
                "metadata_profile": hive_metadata_profile.as_str(),
            }),
        )?;
        let hive_root = path_root.join(layout_dir_name(&layout));
        let build_result = write_hive_layout_chunks(
            delta_source,
            plans,
            &hive_root,
            first_rg_id,
            chunk_rows,
            hive_key,
            hive_metadata_profile.write_options(),
        );
        let variant_chunks = match build_result {
            Ok(chunks) => chunks,
            Err(err) => {
                let _ = finish_acceleration_phase(
                    phase_id,
                    "failed",
                    0,
                    0,
                    0,
                    None,
                    None,
                    serde_json::json!({"layout_kind": "hive"}),
                    Some(&err.to_string()),
                );
                return Err(err);
            }
        };
        let (variant_rows, variant_files, variant_bytes) = variant_chunk_totals(&variant_chunks);
        if variant_rows > 0 {
            register_variant_chunks(rel_oid, &layout, &variant_chunks)?;
        }
        let expected_rows = canonical_row_count(rel_oid)?;
        let valid = validate_registered_variant_layout(rel_oid, &layout)?;
        if valid {
            rows_written += variant_rows;
        }
        finish_acceleration_phase(
            phase_id,
            if valid { "ok" } else { "invalid" },
            variant_rows,
            variant_files,
            variant_bytes,
            Some(expected_rows),
            None,
            serde_json::json!({"validated": valid}),
            None,
        )?;
    }

    if vortex_layout_enabled(rel_oid) {
        rows_written += refresh_vortex_scan_delta_impl(rel_oid, qualified, path_root)?;
    }

    crate::planner::invalidate_planner_aggregates(rel_oid);
    crate::custom_scan::invalidate_scan_metadata(rel_oid);
    crate::columnar_cache::invalidate_table(rel_oid);
    crate::live_counters::bump_scan_epoch_on_commit();

    Ok(rows_written)
}

fn refresh_vortex_scan_delta_impl(
    rel_oid: u32,
    qualified: &str,
    path_root: &PathBuf,
) -> Result<i64, Box<dyn std::error::Error>> {
    let layout = VORTEX_SCAN_LAYOUT;
    let first_missing = missing_vortex_row_group_count(rel_oid, layout)?;
    let phase_id = start_acceleration_phase(
        rel_oid,
        qualified,
        "format_variant_delta_append",
        Some(layout),
        None,
        serde_json::json!({
            "layout_kind": "vortex",
            "build_mode": "delta_append",
            "source": "canonical_parquet",
            "file_extension": "vortex",
            "missing_canonical_row_groups": first_missing,
        }),
    )?;
    let vortex_root = path_root.join(layout_dir_name(layout));
    let mut plans = introspect_columns(rel_oid)?;
    extend_plans_with_legacy_shreds(&mut plans);
    let build_result = write_vortex_scan_chunks_from_canonical_parquet(
        rel_oid,
        &plans,
        &vortex_root,
        layout,
        true,
    );
    let variant_chunks = match build_result {
        Ok(chunks) => chunks,
        Err(err) => {
            let _ = finish_acceleration_phase(
                phase_id,
                "failed",
                0,
                0,
                0,
                None,
                None,
                serde_json::json!({"layout_kind": "vortex"}),
                Some(&err.to_string()),
            );
            return Err(err);
        }
    };
    let (variant_rows, variant_files, variant_bytes) = variant_chunk_totals(&variant_chunks);
    if variant_rows > 0 {
        register_variant_chunks(rel_oid, layout, &variant_chunks)?;
    }
    let expected_rows = canonical_row_count(rel_oid)?;
    let valid = validate_registered_variant_layout(rel_oid, layout)?;
    finish_acceleration_phase(
        phase_id,
        if valid { "ok" } else { "invalid" },
        variant_rows,
        variant_files,
        variant_bytes,
        Some(expected_rows),
        None,
        serde_json::json!({
            "validated": valid,
            "source": "canonical_parquet",
            "file_extension": "vortex",
        }),
        None,
    )?;
    Ok(if valid { variant_rows } else { 0 })
}

fn clear_variant_layout(
    rel_oid: u32,
    layout: &str,
    path_root: &PathBuf,
) -> Result<(), Box<dyn std::error::Error>> {
    let layout_escaped = layout.replace('\'', "''");
    Spi::run(&format!(
        "DELETE FROM rvbbit.layout_variant_status \
         WHERE table_oid = {rel_oid}::oid AND layout = '{layout_escaped}'"
    ))?;
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

fn canonical_row_count(rel_oid: u32) -> Result<i64, Box<dyn std::error::Error>> {
    Ok(Spi::get_one::<i64>(&format!(
        "SELECT coalesce(sum(n_rows), 0)::bigint \
         FROM rvbbit.row_groups \
         WHERE table_oid = {rel_oid}::oid"
    ))?
    .unwrap_or(0))
}

fn validate_variant_chunks(
    rel_oid: u32,
    layout: &str,
    chunks: &[rvbbit_storage::metadata::RowGroupMeta],
) -> Result<bool, Box<dyn std::error::Error>> {
    let expected_rows = canonical_row_count(rel_oid)?;
    let actual_rows = chunks.iter().map(|c| c.n_rows).sum::<i64>();
    let file_count = chunks.len() as i32;
    let missing_files = chunks
        .iter()
        .filter(|c| !std::path::Path::new(&c.path).exists())
        .count();

    if expected_rows > 0 && actual_rows == expected_rows && file_count > 0 && missing_files == 0 {
        return Ok(true);
    }

    let message = if expected_rows != actual_rows {
        format!("row count mismatch: expected {expected_rows}, wrote {actual_rows}")
    } else if file_count == 0 {
        "no variant files were written".to_string()
    } else {
        format!("{missing_files} variant parquet file(s) are missing")
    };
    mark_variant_status(
        rel_oid,
        layout,
        "invalid",
        expected_rows,
        actual_rows,
        file_count,
        Some(&message),
    )?;
    Ok(false)
}

fn variant_chunk_totals(chunks: &[rvbbit_storage::metadata::RowGroupMeta]) -> (i64, i32, i64) {
    (
        chunks.iter().map(|c| c.n_rows).sum::<i64>(),
        chunks.len() as i32,
        chunks.iter().map(|c| c.n_bytes).sum::<i64>(),
    )
}

fn variant_layout_ready(rel_oid: u32, layout: &str) -> bool {
    let layout_lit = sql_literal(layout);
    Spi::get_one::<bool>(&format!(
        "SELECT EXISTS (
             SELECT 1
             FROM rvbbit.layout_variant_status
             WHERE table_oid = {rel_oid}::oid
               AND layout = {layout_lit}
               AND status = 'ready'
               AND actual_rows > 0
         )"
    ))
    .ok()
    .flatten()
    .unwrap_or(false)
}

fn missing_vortex_row_group_count(
    rel_oid: u32,
    layout: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    let layout_lit = sql_literal(layout);
    Ok(Spi::get_one::<i64>(&format!(
        "SELECT count(*)::bigint
         FROM rvbbit.row_groups rg
         WHERE rg.table_oid = {rel_oid}::oid
           AND NOT EXISTS (
               SELECT 1
               FROM rvbbit.row_group_variants v
               WHERE v.table_oid = rg.table_oid
                 AND v.layout = {layout_lit}
                 AND v.rg_id = rg.rg_id
           )"
    ))?
    .unwrap_or(0))
}

fn validate_registered_variant_layout(
    rel_oid: u32,
    layout: &str,
) -> Result<bool, Box<dyn std::error::Error>> {
    let layout_lit = sql_literal(layout);
    let expected_rows = canonical_row_count(rel_oid)?;
    let mut actual_rows = 0_i64;
    let mut file_count = 0_i32;
    let mut n_bytes = 0_i64;
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT coalesce(sum(n_rows), 0)::bigint,
                        count(*)::int,
                        coalesce(sum(n_bytes), 0)::bigint
                 FROM rvbbit.row_group_variants
                 WHERE table_oid = {rel_oid}::oid AND layout = {layout_lit}"
            ),
            Some(1),
            &[],
        )?;
        let row = table.first();
        actual_rows = row.get::<i64>(1)?.unwrap_or(0);
        file_count = row.get::<i32>(2)?.unwrap_or(0);
        n_bytes = row.get::<i64>(3)?.unwrap_or(0);
        Ok(())
    })?;

    if expected_rows > 0 && actual_rows == expected_rows && file_count > 0 {
        mark_variant_status(
            rel_oid,
            layout,
            "ready",
            expected_rows,
            actual_rows,
            file_count,
            None,
        )?;
        return Ok(true);
    }

    let message = if expected_rows != actual_rows {
        format!("row count mismatch: expected {expected_rows}, catalog has {actual_rows}")
    } else {
        "no variant files are registered".to_string()
    };
    mark_variant_status(
        rel_oid,
        layout,
        "invalid",
        expected_rows,
        actual_rows,
        file_count,
        Some(&message),
    )?;
    pgrx::warning!(
        "rvbbit.refresh_layout_variants: layout {layout} for table oid {rel_oid} is invalid after write ({message}; bytes={n_bytes})"
    );
    Ok(false)
}

fn acceleration_phase_table_exists() -> bool {
    Spi::get_one::<bool>("SELECT to_regclass('rvbbit.acceleration_operation_phases') IS NOT NULL")
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn current_acceleration_operation_id() -> Option<i64> {
    Spi::get_one::<i64>(
        "SELECT nullif(current_setting('rvbbit.acceleration_operation_id', true), '')::bigint",
    )
    .ok()
    .flatten()
}

fn current_acceleration_phase_id() -> Option<i64> {
    Spi::get_one::<i64>(
        "SELECT nullif(current_setting('rvbbit.acceleration_phase_id', true), '')::bigint",
    )
    .ok()
    .flatten()
}

fn update_current_acceleration_phase_details(
    details: serde_json::Value,
) -> Result<(), Box<dyn std::error::Error>> {
    let Some(phase_id) = current_acceleration_phase_id() else {
        return Ok(());
    };
    if !acceleration_phase_table_exists() {
        return Ok(());
    }
    let details_json = serde_json::to_string(&details)?;
    Spi::run(&format!(
        "UPDATE rvbbit.acceleration_operation_phases \
            SET details = details || {}::jsonb \
          WHERE id = {phase_id}",
        sql_literal(&details_json)
    ))?;
    Ok(())
}

fn start_acceleration_phase(
    rel_oid: u32,
    qualified: &str,
    phase: &str,
    layout: Option<&str>,
    partition_key: Option<&str>,
    details: serde_json::Value,
) -> Result<Option<i64>, Box<dyn std::error::Error>> {
    if !acceleration_phase_table_exists() {
        return Ok(None);
    }
    let operation_id_sql = current_acceleration_operation_id()
        .map(|id| id.to_string())
        .unwrap_or_else(|| "NULL::bigint".to_string());
    let layout_sql = layout
        .map(|v| format!("{}::text", sql_literal(v)))
        .unwrap_or_else(|| "NULL::text".to_string());
    let key_sql = partition_key
        .map(|v| format!("{}::text", sql_literal(v)))
        .unwrap_or_else(|| "NULL::text".to_string());
    let details_json = serde_json::to_string(&details)?;
    let phase_id = Spi::get_one::<i64>(&format!(
        "INSERT INTO rvbbit.acceleration_operation_phases \
             (operation_id, table_oid, table_name, phase, layout, partition_key, status, details) \
         VALUES ({operation_id_sql}, {rel_oid}::oid, {}, {}, {layout_sql}, {key_sql}, 'running', {}::jsonb) \
         RETURNING id",
        sql_literal(qualified),
        sql_literal(phase),
        sql_literal(&details_json),
    ))?;
    Ok(phase_id)
}

#[allow(clippy::too_many_arguments)]
fn finish_acceleration_phase(
    phase_id: Option<i64>,
    status: &str,
    rows_written: i64,
    files_written: i32,
    bytes_written: i64,
    expected_rows: Option<i64>,
    actual_rows: Option<i64>,
    details: serde_json::Value,
    error: Option<&str>,
) -> Result<(), Box<dyn std::error::Error>> {
    let Some(phase_id) = phase_id else {
        return Ok(());
    };
    let details_json = serde_json::to_string(&details)?;
    let expected_sql = expected_rows
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NULL::bigint".to_string());
    let actual_sql = actual_rows
        .map(|v| v.to_string())
        .unwrap_or_else(|| "NULL::bigint".to_string());
    let error_sql = error
        .map(|v| format!("{}::text", sql_literal(v)))
        .unwrap_or_else(|| "NULL::text".to_string());
    Spi::run(&format!(
        "UPDATE rvbbit.acceleration_operation_phases \
            SET status = {}, \
                finished_at = clock_timestamp(), \
                rows_written = {rows_written}, \
                row_groups_written = {files_written}, \
                files_written = {files_written}, \
                bytes_written = {bytes_written}, \
                expected_rows = {expected_sql}, \
                actual_rows = {actual_sql}, \
                details = details || {}::jsonb, \
                error = {error_sql} \
          WHERE id = {phase_id}",
        sql_literal(status),
        sql_literal(&details_json),
    ))?;
    Ok(())
}

fn mark_variant_status_ready(
    rel_oid: u32,
    layout: &str,
    chunks: &[rvbbit_storage::metadata::RowGroupMeta],
) -> Result<(), Box<dyn std::error::Error>> {
    let expected_rows = canonical_row_count(rel_oid)?;
    let actual_rows = chunks.iter().map(|c| c.n_rows).sum::<i64>();
    mark_variant_status(
        rel_oid,
        layout,
        "ready",
        expected_rows,
        actual_rows,
        chunks.len() as i32,
        None,
    )
}

fn mark_variant_status(
    rel_oid: u32,
    layout: &str,
    status: &str,
    expected_rows: i64,
    actual_rows: i64,
    file_count: i32,
    message: Option<&str>,
) -> Result<(), Box<dyn std::error::Error>> {
    let layout_lit = sql_literal(layout);
    let status_lit = sql_literal(status);
    let message_sql = message
        .map(|m| format!("{}::text", sql_literal(m)))
        .unwrap_or_else(|| "NULL::text".to_string());
    Spi::run(&format!(
        "INSERT INTO rvbbit.layout_variant_status \
             (table_oid, layout, status, expected_rows, actual_rows, file_count, status_message) \
         VALUES ({rel_oid}::oid, {layout_lit}, {status_lit}, {expected_rows}, {actual_rows}, {file_count}, {message_sql}) \
         ON CONFLICT (table_oid, layout) DO UPDATE SET \
             status = EXCLUDED.status, \
             expected_rows = EXCLUDED.expected_rows, \
             actual_rows = EXCLUDED.actual_rows, \
             file_count = EXCLUDED.file_count, \
             status_message = EXCLUDED.status_message, \
             refreshed_at = now()"
    ))?;
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

fn row_identity_map_available() -> bool {
    rvbbit_catalog_table_exists("row_identity_map")
}

fn insert_row_identity_batch(
    rel_oid: u32,
    generation: i64,
    entries: &mut Vec<(i64, i32, String)>,
) -> Result<(), Box<dyn std::error::Error>> {
    if entries.is_empty() {
        entries.clear();
        return Ok(());
    }

    let values = entries
        .iter()
        .map(|(rg_id, ordinal, key_json)| {
            format!(
                "({rel_oid}::oid, {}, {rg_id}, {ordinal}, {generation})",
                sql_literal(key_json)
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    Spi::run(&format!(
        "INSERT INTO rvbbit.row_identity_map \
             (table_oid, key_json, rg_id, ordinal, generation) \
         VALUES {values} \
         ON CONFLICT (table_oid, key_json, rg_id, ordinal) DO NOTHING"
    ))?;
    entries.clear();
    Ok(())
}

fn write_layout_chunks(
    rel_oid: u32,
    qualified: &str,
    plans: &[ColumnPlan],
    schema: &Arc<Schema>,
    path_root: &PathBuf,
    first_rg_id: i64,
    chunk_rows: usize,
    generation: i64,
    identity_mode: Option<&str>,
    cluster_keys: &[String],
) -> Result<LayoutWriteResult, Box<dyn std::error::Error>> {
    let select_list = select_list_for_plans(plans);
    let row_identity_expr = match (cluster_keys.is_empty(), identity_mode) {
        (true, Some("primary_key")) => pk_identity_expr_for_export(rel_oid)?,
        (true, Some("ctid")) => Some(format!(
            "jsonb_build_array({}::text)::text AS {}",
            quote_ident(EXPORT_CTID_COLUMN),
            quote_ident(EXPORT_ROW_KEY_COLUMN)
        )),
        _ => None,
    };
    let select_list = match &row_identity_expr {
        Some(expr) => format!("{select_list}, {expr}"),
        None => select_list,
    };
    let row_identity_idx = row_identity_expr.as_ref().map(|_| plans.len() + 1);
    let identity_batch_rows = identity_batch_rows_setting();
    let writer_threads = writer_threads_setting();
    let order_by = order_by_clause(cluster_keys);
    let select_sql = format!("SELECT {select_list} FROM {qualified}{order_by}");
    let (write_options, _) = canonical_write_options(false);

    let write_result = Spi::connect(|client| -> Result<LayoutWriteResult, pgrx::spi::Error> {
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

        let mut timings = LayoutWriteTimings::default();
        let select_start = Instant::now();
        let table = client.select(&select_sql, None, &[])?;
        timings.spi_select_seconds = elapsed_seconds_since(select_start);
        let mut chunks: Vec<rvbbit_storage::metadata::RowGroupMeta> = Vec::new();
        let mut builders: Vec<ColumnBuilder> = plans
            .iter()
            .map(|c| ColumnBuilder::for_type(&c.arrow_type))
            .collect();
        let mut chunk_count: usize = 0;
        let mut chunk_idx: i64 = 0;
        let mut writer_handles = ChunkWriterQueue::default();
        let mut identity_batch: Vec<(i64, i32, String)> =
            Vec::with_capacity(identity_batch_rows.min(chunk_rows).max(1));

        for row in table {
            let rg_id = first_rg_id + chunk_idx;
            let ordinal = chunk_count as i32;
            let row_build_start = Instant::now();
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
            if let Some(key_idx) = row_identity_idx {
                if let Some(key_json) = row.get::<String>(key_idx)? {
                    identity_batch.push((rg_id, ordinal, key_json));
                    if identity_batch.len() >= identity_batch_rows {
                        let identity_start = Instant::now();
                        insert_row_identity_batch(rel_oid, generation, &mut identity_batch)
                            .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                        timings.identity_insert_seconds += elapsed_seconds_since(identity_start);
                    }
                }
            }
            timings.row_build_seconds += elapsed_seconds_since(row_build_start);
            chunk_count += 1;

            if chunk_count >= chunk_rows {
                let chunk_path = path_root.join(format!("{rg_id}.parquet"));
                let finish_start = Instant::now();
                let batch = finish_chunk_batch(schema, &mut builders, plans)
                    .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                timings.finish_batch_seconds += elapsed_seconds_since(finish_start);
                enqueue_chunk_writer(
                    &mut writer_handles,
                    &mut chunks,
                    &mut timings,
                    writer_threads,
                    chunk_path,
                    rg_id,
                    batch,
                    write_options,
                )
                .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                chunk_count = 0;
                chunk_idx += 1;
            }
        }

        if chunk_count > 0 {
            let rg_id = first_rg_id + chunk_idx;
            let chunk_path = path_root.join(format!("{rg_id}.parquet"));
            let finish_start = Instant::now();
            let batch = finish_chunk_batch(schema, &mut builders, plans)
                .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
            timings.finish_batch_seconds += elapsed_seconds_since(finish_start);
            enqueue_chunk_writer(
                &mut writer_handles,
                &mut chunks,
                &mut timings,
                writer_threads,
                chunk_path,
                rg_id,
                batch,
                write_options,
            )
            .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
        }
        drain_chunk_writers(&mut writer_handles, &mut chunks, &mut timings)
            .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
        chunks.sort_by_key(|meta| meta.rg_id);
        let identity_start = Instant::now();
        insert_row_identity_batch(rel_oid, generation, &mut identity_batch)
            .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
        timings.identity_insert_seconds += elapsed_seconds_since(identity_start);

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

        Ok(LayoutWriteResult { chunks, timings })
    })?;

    Ok(write_result)
}

type ChunkWriterHandle = JoinHandle<Result<ChunkWriteResult, String>>;

#[derive(Default)]
struct ChunkWriterQueue {
    handles: Vec<ChunkWriterHandle>,
}

impl Drop for ChunkWriterQueue {
    fn drop(&mut self) {
        while let Some(handle) = self.handles.pop() {
            let _ = handle.join();
        }
    }
}

fn enqueue_chunk_writer(
    queue: &mut ChunkWriterQueue,
    chunks: &mut Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: &mut LayoutWriteTimings,
    writer_threads: usize,
    chunk_path: PathBuf,
    rg_id: i64,
    batch: RecordBatch,
    write_options: RowGroupWriteOptions,
) -> Result<(), Box<dyn std::error::Error>> {
    if writer_threads <= 1 {
        let result = write_record_batch_chunk(&chunk_path, rg_id, batch, write_options)?;
        timings.add_writer_seconds(result.write_seconds);
        chunks.push(result.meta);
        return Ok(());
    }

    join_finished_chunk_writers(queue, chunks, timings)?;
    while queue.handles.len() >= writer_threads {
        let wait_start = Instant::now();
        join_oldest_chunk_writer(queue, chunks, timings)?;
        timings.writer_wait_seconds += elapsed_seconds_since(wait_start);
        join_finished_chunk_writers(queue, chunks, timings)?;
    }
    queue.handles.push(thread::spawn(move || {
        write_record_batch_chunk(&chunk_path, rg_id, batch, write_options)
            .map_err(|e| e.to_string())
    }));
    Ok(())
}

fn drain_chunk_writers(
    queue: &mut ChunkWriterQueue,
    chunks: &mut Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: &mut LayoutWriteTimings,
) -> Result<(), Box<dyn std::error::Error>> {
    while !queue.handles.is_empty() {
        let join_start = Instant::now();
        join_oldest_chunk_writer(queue, chunks, timings)?;
        timings.writer_join_seconds += elapsed_seconds_since(join_start);
    }
    Ok(())
}

fn join_finished_chunk_writers(
    queue: &mut ChunkWriterQueue,
    chunks: &mut Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: &mut LayoutWriteTimings,
) -> Result<(), Box<dyn std::error::Error>> {
    while let Some(idx) = queue.handles.iter().position(|handle| handle.is_finished()) {
        join_chunk_writer_at(queue, chunks, timings, idx)?;
    }
    Ok(())
}

fn join_oldest_chunk_writer(
    queue: &mut ChunkWriterQueue,
    chunks: &mut Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: &mut LayoutWriteTimings,
) -> Result<(), Box<dyn std::error::Error>> {
    join_chunk_writer_at(queue, chunks, timings, 0)
}

fn join_chunk_writer_at(
    queue: &mut ChunkWriterQueue,
    chunks: &mut Vec<rvbbit_storage::metadata::RowGroupMeta>,
    timings: &mut LayoutWriteTimings,
    idx: usize,
) -> Result<(), Box<dyn std::error::Error>> {
    let handle = queue.handles.remove(idx);
    let result = handle
        .join()
        .map_err(|_| "rvbbit parquet writer thread panicked")?
        .map_err(|e| format!("rvbbit parquet writer failed: {e}"))?;
    timings.add_writer_seconds(result.write_seconds);
    chunks.push(result.meta);
    Ok(())
}

fn write_hive_layout_chunks(
    qualified: &str,
    plans: &[ColumnPlan],
    path_root: &PathBuf,
    first_rg_id: i64,
    chunk_rows: usize,
    hive_key: &str,
    write_options: RowGroupWriteOptions,
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
                if let Some(flush_partition) = hive_partition_to_flush_on_transition(
                    current_partition.as_deref(),
                    partition.as_str(),
                    chunk_count,
                )
                .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?
                {
                    let rg_id = first_rg_id + chunk_idx;
                    let chunk_path = path_root
                        .join(format!("{}={flush_partition}", layout_dir_name(hive_key)))
                        .join(format!("{rg_id}.parquet"));
                    let meta = flush_chunk_with_options(
                        &file_schema,
                        &mut builders,
                        &file_plans,
                        &chunk_path,
                        rg_id,
                        write_options,
                    )
                    .map_err(|e| pgrx::spi::Error::CursorNotFound(e.to_string()))?;
                    chunks.push(meta);
                    chunk_count = 0;
                    chunk_idx += 1;
                    current_partition = Some(partition.clone());
                } else if current_partition.as_deref() != Some(partition.as_str()) {
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
                    let meta = flush_chunk_with_options(
                        &file_schema,
                        &mut builders,
                        &file_plans,
                        &chunk_path,
                        rg_id,
                        write_options,
                    )
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
                let meta = flush_chunk_with_options(
                    &file_schema,
                    &mut builders,
                    &file_plans,
                    &chunk_path,
                    rg_id,
                    write_options,
                )
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

fn write_hive_layout_chunks_from_canonical_parquet(
    rel_oid: u32,
    plans: &[ColumnPlan],
    path_root: &PathBuf,
    first_rg_id: i64,
    chunk_rows: usize,
    batch_rows: usize,
    hive_key: &str,
    write_options: RowGroupWriteOptions,
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
    let row_groups = canonical_row_group_paths(rel_oid)?;
    if row_groups.is_empty() {
        return Err(format!("table oid {rel_oid} has no canonical parquet row groups").into());
    }

    let mut chunks: Vec<rvbbit_storage::metadata::RowGroupMeta> = Vec::new();
    let mut chunk_idx = 0_i64;
    for (_source_rg_id, source_path) in row_groups {
        let file = File::open(&source_path)
            .map_err(|e| format!("opening canonical parquet {source_path}: {e}"))?;
        let reader = ParquetRecordBatchReaderBuilder::try_new(file)
            .map_err(|e| format!("opening canonical parquet reader {source_path}: {e}"))?
            .with_batch_size(batch_rows)
            .build()
            .map_err(|e| format!("building canonical parquet reader {source_path}: {e}"))?;

        for batch in reader {
            let batch =
                batch.map_err(|e| format!("reading canonical parquet {source_path}: {e}"))?;
            if batch.num_rows() == 0 {
                continue;
            }
            let partition_idx = batch.schema().index_of(hive_key).map_err(|_| {
                format!(
                    "hive partition key '{hive_key}' not found in canonical parquet schema for {source_path}"
                )
            })?;
            let partition_array = batch.column(partition_idx);
            let mut rows_by_partition: BTreeMap<String, Vec<u32>> = BTreeMap::new();
            for row_idx in 0..batch.num_rows() {
                let partition =
                    encoded_hive_partition_from_arrow(partition_array, row_idx, partition_plan)?;
                rows_by_partition
                    .entry(partition)
                    .or_default()
                    .push(row_idx as u32);
            }

            for (partition, row_indices) in rows_by_partition {
                if row_indices.is_empty() {
                    continue;
                }
                for row_chunk in row_indices.chunks(chunk_rows) {
                    let rg_id = first_rg_id + chunk_idx;
                    let chunk_path = path_root
                        .join(format!("{}={partition}", layout_dir_name(hive_key)))
                        .join(format!("{rg_id}.parquet"));
                    let partition_batch =
                        take_hive_file_batch(&batch, &file_plans, &file_schema, row_chunk)?;
                    let meta = RowGroupWriter::write_with_options(
                        &chunk_path,
                        rg_id,
                        &partition_batch,
                        write_options,
                    )?;
                    chunks.push(meta);
                    chunk_idx += 1;
                }
            }
        }
    }

    Ok(chunks)
}

fn write_vortex_scan_chunks_from_canonical_parquet(
    rel_oid: u32,
    plans: &[ColumnPlan],
    path_root: &PathBuf,
    layout: &str,
    only_missing: bool,
) -> Result<Vec<rvbbit_storage::metadata::RowGroupMeta>, Box<dyn std::error::Error>> {
    let row_groups = canonical_row_group_paths(rel_oid)?;
    if row_groups.is_empty() {
        return Err(format!("table oid {rel_oid} has no canonical parquet row groups").into());
    }

    std::fs::create_dir_all(path_root)?;
    let existing = if only_missing {
        registered_variant_rg_ids(rel_oid, layout)?
    } else {
        HashSet::new()
    };
    let rt = TokioRuntimeBuilder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("creating Vortex writer runtime: {e}"))?;
    let session = VortexSession::default().with_tokio();

    let mut chunks: Vec<rvbbit_storage::metadata::RowGroupMeta> = Vec::new();
    for (source_rg_id, source_path) in row_groups {
        if existing.contains(&source_rg_id) {
            continue;
        }
        let file = File::open(&source_path)
            .map_err(|e| format!("opening canonical parquet {source_path}: {e}"))?;
        let reader = ParquetRecordBatchReaderBuilder::try_new(file)
            .map_err(|e| format!("opening canonical parquet reader {source_path}: {e}"))?
            .with_batch_size(scan_chunk_rows_setting())
            .build()
            .map_err(|e| format!("building canonical parquet reader {source_path}: {e}"))?;

        for (batch_idx, batch) in reader.enumerate() {
            let batch =
                batch.map_err(|e| format!("reading canonical parquet {source_path}: {e}"))?;
            if batch.num_rows() == 0 {
                continue;
            }
            let batch = vortex_record_batch_for_plans(plans, batch)?;
            let rg_id = if batch_idx == 0 {
                source_rg_id
            } else {
                source_rg_id
                    .saturating_mul(1_000_000)
                    .saturating_add(batch_idx as i64)
            };
            let chunk_path = path_root.join(format!("{rg_id}.vortex"));
            let meta = write_vortex_record_batch(&rt, &session, &chunk_path, rg_id, batch)?;
            chunks.push(meta);
        }
    }

    Ok(chunks)
}

fn vortex_record_batch_for_plans(
    plans: &[ColumnPlan],
    batch: RecordBatch,
) -> Result<RecordBatch, Box<dyn std::error::Error>> {
    if plans.is_empty() {
        return Ok(batch);
    }
    let mut fields = Vec::with_capacity(batch.num_columns());
    let mut columns = Vec::with_capacity(batch.num_columns());
    for (field, column) in batch.schema().fields().iter().zip(batch.columns()) {
        let plan = plans.iter().find(|plan| plan.name == field.name().as_str());
        if plan.is_some_and(|plan| matches!(plan.pg_type, 1114 | 1184)) {
            let as_i64 = cast(column, &DataType::Int64).map_err(|e| {
                format!(
                    "casting Vortex timestamp column {} to epoch micros: {e}",
                    field.name()
                )
            })?;
            fields.push(Field::new(
                field.name(),
                DataType::Int64,
                field.is_nullable(),
            ));
            columns.push(as_i64);
        } else {
            fields.push((**field).clone());
            columns.push(column.clone());
        }
    }
    RecordBatch::try_new(Arc::new(Schema::new(fields)), columns)
        .map_err(|e| format!("building Vortex-safe record batch: {e}").into())
}

fn registered_variant_rg_ids(
    rel_oid: u32,
    layout: &str,
) -> Result<HashSet<i64>, Box<dyn std::error::Error>> {
    let mut out = HashSet::new();
    let layout_lit = sql_literal(layout);
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT rg_id
                 FROM rvbbit.row_group_variants
                 WHERE table_oid = {rel_oid}::oid AND layout = {layout_lit}"
            ),
            None,
            &[],
        )?;
        for row in table {
            if let Some(rg_id) = row.get::<i64>(1)? {
                out.insert(rg_id);
            }
        }
        Ok(())
    })?;
    Ok(out)
}

fn write_vortex_record_batch(
    rt: &tokio::runtime::Runtime,
    session: &VortexSession,
    path: &PathBuf,
    rg_id: i64,
    batch: RecordBatch,
) -> Result<rvbbit_storage::metadata::RowGroupMeta, Box<dyn std::error::Error>> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let n_rows = batch.num_rows() as i64;
    let schema = batch.schema();
    // Phase 1 (NATIVE_VORTEX_PLAN): compute per-column min/max/null-count from the
    // batch BEFORE it's moved into the Vortex writer, reusing the parquet stats fn so
    // the values are byte-identical to the canonical parquet stats. Timestamp columns
    // arrive here as Int64 unix-epoch micros (post `vortex_record_batch_for_plans`
    // cast) → the Int64 arm yields the same unix-epoch micros parquet stores; the
    // stats-pruning path applies NO epoch offset, so no adjustment is needed. Text
    // sketches are skipped (false) — irrelevant to vortex pruning + expensive.
    let column_stats = rvbbit_storage::row_group::compute_arrow_stats(&batch, false);
    rt.block_on(async {
        let array = session
            .arrow()
            .from_arrow_record_batch(batch, &schema)
            .map_err(|e| format!("converting Arrow batch to Vortex: {e}"))?;
        let mut file = tokio::fs::File::create(path)
            .await
            .map_err(|e| format!("creating Vortex file {}: {e}", path.display()))?;
        session
            .write_options()
            .write(&mut file, array.to_array_stream())
            .await
            .map_err(|e| format!("writing Vortex file {}: {e}", path.display()))?;
        Ok::<(), Box<dyn std::error::Error>>(())
    })?;
    let n_bytes = std::fs::metadata(path)
        .map_err(|e| format!("stat Vortex file {}: {e}", path.display()))?
        .len() as i64;
    Ok(rvbbit_storage::metadata::RowGroupMeta {
        rg_id,
        path: path.to_string_lossy().into_owned(),
        n_rows,
        n_bytes,
        min_xid: None,
        max_xid: None,
        column_stats,
        per_group_stats: Vec::new(),
        column_bitmaps: Vec::new(),
        text_dictionaries: Vec::new(),
    })
}

fn canonical_row_group_paths(
    rel_oid: u32,
) -> Result<Vec<(i64, String)>, Box<dyn std::error::Error>> {
    let mut row_groups = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT rg_id, path \
                 FROM rvbbit.row_groups \
                 WHERE table_oid = {rel_oid}::oid \
                 ORDER BY rg_id"
            ),
            None,
            &[],
        )?;
        for row in table {
            let rg_id = row.get::<i64>(1)?.unwrap_or(0);
            let path = row.get::<String>(2)?.unwrap_or_default();
            if !path.is_empty() {
                row_groups.push((rg_id, path));
            }
        }
        Ok(())
    })?;
    Ok(row_groups)
}

fn take_hive_file_batch(
    batch: &RecordBatch,
    file_plans: &[ColumnPlan],
    file_schema: &Arc<Schema>,
    row_indices: &[u32],
) -> Result<RecordBatch, Box<dyn std::error::Error>> {
    let indices = UInt32Array::from(row_indices.to_vec());
    let mut arrays: Vec<ArrayRef> = Vec::with_capacity(file_plans.len());
    for plan in file_plans {
        let idx = batch.schema().index_of(&plan.name).map_err(|_| {
            format!(
                "column '{}' not found in canonical parquet batch",
                plan.name
            )
        })?;
        arrays.push(take(batch.column(idx).as_ref(), &indices, None)?);
    }
    Ok(RecordBatch::try_new(file_schema.clone(), arrays)?)
}

fn encoded_hive_partition_from_arrow(
    array: &ArrayRef,
    row_idx: usize,
    plan: &ColumnPlan,
) -> Result<String, Box<dyn std::error::Error>> {
    if array.is_null(row_idx) {
        return Ok(encode_hive_partition_value(None));
    }

    let value = match plan.pg_type {
        16 => {
            let Some(array) = array.as_any().downcast_ref::<BooleanArray>() else {
                return Err(format!(
                    "hive partition key '{}' expected boolean parquet array, found {:?}",
                    plan.name,
                    array.data_type()
                )
                .into());
            };
            array.value(row_idx).to_string()
        }
        21 | 23 | 20 => arrow_integer_partition_value(array, row_idx, &plan.name)?.to_string(),
        25 | 1042 | 1043 => arrow_string_partition_value(array, row_idx, &plan.name)?,
        _ => {
            return Err(format!(
                "hive partition key '{}' has unsupported pg type {}",
                plan.name, plan.pg_type
            )
            .into());
        }
    };

    Ok(encode_hive_partition_value(Some(&value)))
}

fn arrow_integer_partition_value(
    array: &ArrayRef,
    row_idx: usize,
    column_name: &str,
) -> Result<i64, Box<dyn std::error::Error>> {
    match array.data_type() {
        DataType::Int16 => Ok(array
            .as_any()
            .downcast_ref::<Int16Array>()
            .ok_or_else(|| format!("column '{column_name}' is not Int16"))?
            .value(row_idx) as i64),
        DataType::Int32 => Ok(array
            .as_any()
            .downcast_ref::<Int32Array>()
            .ok_or_else(|| format!("column '{column_name}' is not Int32"))?
            .value(row_idx) as i64),
        DataType::Int64 => Ok(array
            .as_any()
            .downcast_ref::<Int64Array>()
            .ok_or_else(|| format!("column '{column_name}' is not Int64"))?
            .value(row_idx)),
        other => Err(format!(
            "hive partition key '{column_name}' expected integer parquet array, found {other:?}"
        )
        .into()),
    }
}

fn arrow_string_partition_value(
    array: &ArrayRef,
    row_idx: usize,
    column_name: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    match array.data_type() {
        DataType::Utf8 => Ok(array
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| format!("column '{column_name}' is not Utf8"))?
            .value(row_idx)
            .to_string()),
        DataType::LargeUtf8 => Ok(array
            .as_any()
            .downcast_ref::<LargeStringArray>()
            .ok_or_else(|| format!("column '{column_name}' is not LargeUtf8"))?
            .value(row_idx)
            .to_string()),
        DataType::Utf8View => Ok(array
            .as_any()
            .downcast_ref::<StringViewArray>()
            .ok_or_else(|| format!("column '{column_name}' is not Utf8View"))?
            .value(row_idx)
            .to_string()),
        other => Err(format!(
            "hive partition key '{column_name}' expected string parquet array, found {other:?}"
        )
        .into()),
    }
}

fn hive_partition_to_flush_on_transition<'a>(
    current_partition: Option<&'a str>,
    incoming_partition: &str,
    chunk_count: usize,
) -> Result<Option<&'a str>, &'static str> {
    if current_partition == Some(incoming_partition) || chunk_count == 0 {
        return Ok(None);
    }
    current_partition
        .map(Some)
        .ok_or("hive writer has buffered rows without a current partition")
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

fn flush_chunk_with_options(
    schema: &Arc<Schema>,
    builders: &mut Vec<ColumnBuilder>,
    plans: &[ColumnPlan],
    path: &PathBuf,
    rg_id: i64,
    write_options: RowGroupWriteOptions,
) -> Result<rvbbit_storage::metadata::RowGroupMeta, Box<dyn std::error::Error>> {
    let batch = finish_chunk_batch(schema, builders, plans)?;
    Ok(write_record_batch_chunk(path, rg_id, batch, write_options)?.meta)
}

fn finish_chunk_batch(
    schema: &Arc<Schema>,
    builders: &mut Vec<ColumnBuilder>,
    plans: &[ColumnPlan],
) -> Result<RecordBatch, Box<dyn std::error::Error>> {
    // Steal the current builders out, replace with fresh ones for the next chunk.
    let fresh: Vec<ColumnBuilder> = plans
        .iter()
        .map(|c| ColumnBuilder::for_type(&c.arrow_type))
        .collect();
    let old = std::mem::replace(builders, fresh);
    let arrays: Vec<ArrayRef> = old.into_iter().map(|b| b.finish()).collect();
    Ok(RecordBatch::try_new(schema.clone(), arrays)?)
}

fn write_record_batch_chunk(
    path: &PathBuf,
    rg_id: i64,
    batch: RecordBatch,
    write_options: RowGroupWriteOptions,
) -> Result<ChunkWriteResult, Box<dyn std::error::Error>> {
    let write_start = Instant::now();
    let meta = RowGroupWriter::write_with_options(path, rg_id, &batch, write_options)?;
    Ok(ChunkWriteResult {
        meta,
        write_seconds: elapsed_seconds_since(write_start),
    })
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

#[cfg(test)]
mod tests {
    use super::hive_partition_to_flush_on_transition;

    #[test]
    fn hive_boundary_flushes_buffered_rows_to_previous_partition() {
        assert_eq!(
            hive_partition_to_flush_on_transition(Some("A"), "B", 12),
            Ok(Some("A"))
        );
    }

    #[test]
    fn hive_boundary_does_not_flush_empty_or_same_partition() {
        assert_eq!(
            hive_partition_to_flush_on_transition(Some("A"), "A", 12),
            Ok(None)
        );
        assert_eq!(
            hive_partition_to_flush_on_transition(None, "A", 0),
            Ok(None)
        );
    }

    #[test]
    fn hive_boundary_rejects_buffered_rows_without_partition() {
        assert_eq!(
            hive_partition_to_flush_on_transition(None, "A", 1),
            Err("hive writer has buffered rows without a current partition")
        );
    }

    // The text-surrogate allowlist: uuid/numeric/inet/…/enum plan as Utf8 via
    // ::text (reconstructed on read), native types are untouched, and genuinely
    // unsupported types still error loudly instead of silently degrading.
    #[test]
    fn text_surrogate_allowlist_maps_to_cast_text() {
        use super::{is_text_surrogate_type, plan_for_pg_type};
        // built-in surrogate oids — uuid(2950), numeric(1700), inet(869),
        // cidr(650), macaddr(829), macaddr8(774), time(1083), timetz(1266),
        // interval(1186)
        for &oid in &[2950u32, 1700, 869, 650, 829, 774, 1083, 1266, 1186] {
            assert!(
                is_text_surrogate_type(oid, "b"),
                "oid {oid} should be a text surrogate"
            );
            let (_, expr) = plan_for_pg_type(oid, "b", "c").expect("surrogate must plan ok");
            assert!(
                expr.contains("::text"),
                "surrogate oid {oid} must project ::text: {expr}"
            );
        }
        // user enums are caught by typtype 'e' regardless of (dynamic) oid
        assert!(is_text_surrogate_type(999_999, "e"));
        assert!(plan_for_pg_type(999_999, "e", "status")
            .unwrap()
            .1
            .contains("::text"));
        // native types are not surrogates and keep planning natively (int4=23)
        assert!(!is_text_surrogate_type(23, "b"));
        assert!(plan_for_pg_type(23, "b", "i").is_ok());
        // genuinely unsupported types still error (point = 600, not allowlisted)
        assert!(!is_text_surrogate_type(600, "b"));
        assert!(plan_for_pg_type(600, "b", "p").is_err());
    }
}
