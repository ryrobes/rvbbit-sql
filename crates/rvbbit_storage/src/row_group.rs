//! Row group reader/writer.
//!
//! Each row group is a single Parquet file inside a per-table directory:
//!
//!   <pg_data>/rvbbit/<table_oid>/<rg_id>.parquet
//!
//! Phase 2a: RowGroupWriter::write writes a RecordBatch as a parquet file
//! with ZSTD compression and returns per-column statistics.
//!
//! Phase 2c: RowGroupReader provides open / open_projected for selective
//! column reads. The projection path is the whole point of columnar — when
//! a query touches 2 columns out of 50, we should read only those 2 column
//! blocks off disk.

use std::fs::File;
use std::io::{Read, Write};
use std::ops::Range;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use arrow::array::{
    Array, BooleanArray, Date32Array, Float32Array, Float64Array, Int16Array, Int32Array,
    Int64Array, RecordBatch, StringArray, TimestampMicrosecondArray,
};
use arrow::compute::kernels::aggregate::{
    max as arrow_max, max_boolean, min as arrow_min, min_boolean, sum as arrow_sum,
};
use arrow::datatypes::{DataType, SchemaRef};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use parquet::arrow::arrow_reader::{
    ParquetRecordBatchReader, ParquetRecordBatchReaderBuilder, RowSelection,
};
use parquet::arrow::ArrowWriter;
use parquet::arrow::ProjectionMask;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::properties::{WriterProperties, WriterVersion};
use parquet::schema::types::ColumnPath;
use roaring::RoaringBitmap;

use crate::metadata::{
    ColumnBitmapBlock, ColumnBitmapEntry, ColumnStats, GroupBucket, NumericAgg, PerGroupBlock,
    RowGroupMeta, TextDictionaryBlock, TextSketch,
};
use std::collections::HashMap;

/// ZSTD level 3 — heavily compressed but cheap to decompress.
const DEFAULT_ZSTD_LEVEL: i32 = 3;

/// Arrow batch size when reading. Parquet defaults to 1024 rows, which
/// makes the per-batch reader-rebuild overhead (downcast + closure setup
/// in custom_scan) dominate at high row counts. 65536 is large enough to
/// amortize that overhead almost completely while keeping per-batch
/// memory under ~512KB for typical narrow projections.
const READ_BATCH_SIZE: usize = 65_536;
const TEXT_DICTIONARY_MAGIC: &[u8; 8] = b"RVBTD001";

#[derive(Clone, Debug)]
pub struct TextDictionary {
    pub values: Vec<String>,
    pub codes: Vec<u32>,
    pub n_nulls: i64,
    pub n_empty: i64,
}

impl TextDictionary {
    pub fn read(path: &Path) -> Result<Self> {
        let mut file = File::open(path)
            .with_context(|| format!("opening text dictionary {}", path.display()))?;
        let mut magic = [0u8; 8];
        file.read_exact(&mut magic)
            .with_context(|| format!("reading text dictionary magic {}", path.display()))?;
        if &magic != TEXT_DICTIONARY_MAGIC {
            return Err(anyhow!(
                "invalid text dictionary magic for {}",
                path.display()
            ));
        }

        let n_rows = read_u64(&mut file, path)?;
        let n_values = read_u64(&mut file, path)?;
        let n_nulls = read_u64(&mut file, path)?;
        let n_empty = read_u64(&mut file, path)?;
        let n_rows_usize = usize::try_from(n_rows)
            .map_err(|_| anyhow!("text dictionary row count too large: {n_rows}"))?;
        let n_values_usize = usize::try_from(n_values)
            .map_err(|_| anyhow!("text dictionary value count too large: {n_values}"))?;

        let mut values = Vec::with_capacity(n_values_usize);
        for _ in 0..n_values_usize {
            let len = read_u32(&mut file, path)? as usize;
            let mut bytes = vec![0u8; len];
            file.read_exact(&mut bytes)
                .with_context(|| format!("reading text dictionary value {}", path.display()))?;
            values.push(String::from_utf8(bytes).with_context(|| {
                format!(
                    "text dictionary value is not valid UTF-8 in {}",
                    path.display()
                )
            })?);
        }

        let mut codes = Vec::with_capacity(n_rows_usize);
        for _ in 0..n_rows_usize {
            let code = read_u32(&mut file, path)?;
            if code as usize > values.len() {
                return Err(anyhow!(
                    "text dictionary code {} out of range for {} values in {}",
                    code,
                    values.len(),
                    path.display()
                ));
            }
            codes.push(code);
        }

        Ok(Self {
            values,
            codes,
            n_nulls: n_nulls.min(i64::MAX as u64) as i64,
            n_empty: n_empty.min(i64::MAX as u64) as i64,
        })
    }

    pub fn value_for_code(&self, code: u32) -> Option<&str> {
        if code == 0 {
            return None;
        }
        self.values.get((code - 1) as usize).map(String::as_str)
    }

    pub fn memory_size(&self) -> usize {
        self.codes
            .len()
            .saturating_mul(std::mem::size_of::<u32>())
            .saturating_add(
                self.values
                    .iter()
                    .map(|value| value.len() + 24)
                    .sum::<usize>(),
            )
            .saturating_add(64)
    }
}

pub struct RowGroupWriter;

#[derive(Clone, Copy, Debug)]
pub struct RowGroupWriteOptions {
    pub column_stats: bool,
    pub text_stats: bool,
    pub per_group_stats: bool,
    pub value_bitmaps: bool,
    pub text_dictionaries: bool,
    pub parquet_bloom: Option<bool>,
}

impl RowGroupWriteOptions {
    pub fn from_env() -> Self {
        Self {
            column_stats: true,
            text_stats: compact_text_stats_enabled(),
            per_group_stats: compact_per_group_stats_enabled(),
            value_bitmaps: compact_value_bitmaps_enabled(),
            text_dictionaries: compact_text_dictionaries_enabled(),
            parquet_bloom: None,
        }
    }

    pub fn minimal() -> Self {
        Self {
            column_stats: false,
            text_stats: false,
            per_group_stats: false,
            value_bitmaps: false,
            text_dictionaries: false,
            parquet_bloom: Some(false),
        }
    }
}

impl RowGroupWriter {
    pub fn write(path: &Path, rg_id: i64, batch: &RecordBatch) -> Result<RowGroupMeta> {
        Self::write_with_options(path, rg_id, batch, RowGroupWriteOptions::from_env())
    }

    pub fn write_with_options(
        path: &Path,
        rg_id: i64,
        batch: &RecordBatch,
        options: RowGroupWriteOptions,
    ) -> Result<RowGroupMeta> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating parent dir {}", parent.display()))?;
        }

        let schema: SchemaRef = batch.schema();
        let props = build_writer_properties(&schema, options);

        let file = File::create(path).with_context(|| format!("creating {}", path.display()))?;
        let mut writer = ArrowWriter::try_new(file, schema.clone(), Some(props))
            .context("opening parquet writer")?;
        writer.write(batch).context("writing record batch")?;
        let parquet_metadata = writer.close().context("closing parquet writer")?;

        let n_bytes = std::fs::metadata(path)
            .with_context(|| format!("stat {}", path.display()))?
            .len() as i64;

        // Compute min/max/sum/null_count from the Arrow batch ourselves —
        // parquet only stores byte-encoded min/max, easier to extract from
        // the typed Arrow arrays we just wrote.
        let column_stats = if options.column_stats {
            compute_arrow_stats(batch, options.text_stats)
        } else {
            Vec::new()
        };
        let per_group_stats = if options.per_group_stats {
            compute_per_group_stats(batch)
        } else {
            Vec::new()
        };
        let column_bitmaps = if options.value_bitmaps {
            compute_column_bitmaps(batch)?
        } else {
            Vec::new()
        };
        let text_dictionaries = if options.text_dictionaries {
            compute_text_dictionaries(path, batch)?
        } else {
            Vec::new()
        };
        let _ = parquet_metadata; // kept in case we want page-level stats later

        Ok(RowGroupMeta {
            rg_id,
            path: path.to_string_lossy().into_owned(),
            n_rows: batch.num_rows() as i64,
            n_bytes,
            min_xid: None,
            max_xid: None,
            column_stats,
            per_group_stats,
            column_bitmaps,
            text_dictionaries,
        })
    }
}

/// Default Parquet data-page row limit when `RVBBIT_PARQUET_PAGE_ROWS`
/// isn't set. Smaller pages give finer-grained skip via column indexes
/// at the cost of slightly bigger files. We default to 5k rows because
/// rvbbit already keeps parquet alongside the PG heap, so disk footprint
/// is already a known cost (with cold-tier storage as the mitigation);
/// the better page-level skipping is the higher-leverage tradeoff.
const DEFAULT_PAGE_ROW_COUNT_LIMIT: usize = 5_000;

/// Default bloom-filter false-positive rate. Lower → fewer wasted page
/// reads on miss, higher → smaller bloom filters on disk.
const DEFAULT_BLOOM_FPP: f64 = 0.01;

/// Default truncate length for column index min/max prefixes. Keeps the
/// per-page index compact even when string columns are long.
const DEFAULT_COLUMN_INDEX_TRUNCATE: usize = 64;

/// Build WriterProperties for a row-group write. Knobs are env-gated so
/// benchmark runs can A/B-compare without recompiling:
///
///   RVBBIT_PARQUET_V2          (default: on)  — Parquet 2.0 writer + V2 pages
///   RVBBIT_PARQUET_BLOOM       (default: on)  — bloom filters on text/binary cols
///   RVBBIT_PARQUET_BLOOM_FPP   (default: 0.01)
///   RVBBIT_PARQUET_PAGE_ROWS   (default: 5000) — data-page row count limit
///
/// Numeric columns get bloom filters disabled because column min/max
/// already cover the equality-pruning case for those types.
fn build_writer_properties(schema: &SchemaRef, options: RowGroupWriteOptions) -> WriterProperties {
    let writer_version = if env_enabled("RVBBIT_PARQUET_V2", true) {
        WriterVersion::PARQUET_2_0
    } else {
        WriterVersion::PARQUET_1_0
    };

    let page_rows = env_usize("RVBBIT_PARQUET_PAGE_ROWS", DEFAULT_PAGE_ROW_COUNT_LIMIT);
    let bloom_enabled = options
        .parquet_bloom
        .unwrap_or_else(|| env_enabled("RVBBIT_PARQUET_BLOOM", true));
    let bloom_fpp = env_f64("RVBBIT_PARQUET_BLOOM_FPP", DEFAULT_BLOOM_FPP);

    let mut builder = WriterProperties::builder()
        .set_writer_version(writer_version)
        .set_compression(Compression::ZSTD(
            ZstdLevel::try_new(DEFAULT_ZSTD_LEVEL).expect("valid zstd level"),
        ))
        .set_statistics_enabled(parquet::file::properties::EnabledStatistics::Page)
        .set_column_index_truncate_length(Some(DEFAULT_COLUMN_INDEX_TRUNCATE))
        .set_data_page_row_count_limit(page_rows);

    if bloom_enabled {
        builder = builder
            .set_bloom_filter_enabled(true)
            .set_bloom_filter_fpp(bloom_fpp);

        // Numeric / temporal columns: bloom only useful for equality
        // pushdown ON SPECIFIC VALUES that fall inside min/max but aren't
        // actually present. ClickBench-style `WHERE id = literal` queries
        // are the canonical case. Skipped by default (saves disk + write
        // time on the column-stats-only workload) and turned on per
        // workload via RVBBIT_PARQUET_BLOOM_NUMERIC=on.
        let bloom_numeric = env_enabled("RVBBIT_PARQUET_BLOOM_NUMERIC", false);
        if !bloom_numeric {
            for field in schema.fields() {
                let is_numeric_or_temporal = matches!(
                    field.data_type(),
                    DataType::Int16
                        | DataType::Int32
                        | DataType::Int64
                        | DataType::Float32
                        | DataType::Float64
                        | DataType::Boolean
                        | DataType::Date32
                        | DataType::Timestamp(_, _)
                );
                if is_numeric_or_temporal {
                    builder = builder.set_column_bloom_filter_enabled(
                        ColumnPath::from(field.name().as_str()),
                        false,
                    );
                }
            }
        }
    }

    builder.build()
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_enabled(name: &str, default: bool) -> bool {
    let raw = std::env::var(name).unwrap_or_else(|_| {
        if default {
            "on".to_string()
        } else {
            "off".to_string()
        }
    });
    matches!(
        raw.to_ascii_lowercase().as_str(),
        "1" | "on" | "true" | "yes"
    )
}

fn compact_text_stats_enabled() -> bool {
    env_enabled("RVBBIT_COMPACT_TEXT_STATS", true)
}

fn compact_per_group_stats_enabled() -> bool {
    env_enabled("RVBBIT_COMPACT_PER_GROUP_STATS", true)
}

fn compact_value_bitmaps_enabled() -> bool {
    env_enabled("RVBBIT_COMPACT_VALUE_BITMAPS", true)
}

fn compact_text_dictionaries_enabled() -> bool {
    env_enabled("RVBBIT_COMPACT_TEXT_DICTIONARIES", true)
}

fn compact_value_bitmap_max_distinct() -> usize {
    env_usize("RVBBIT_COMPACT_VALUE_BITMAP_MAX_DISTINCT", 4096)
}

fn compact_text_dictionary_max_bytes() -> usize {
    env_usize(
        "RVBBIT_COMPACT_TEXT_DICTIONARY_MAX_BYTES",
        128 * 1024 * 1024,
    )
}

fn compute_arrow_stats(batch: &RecordBatch, text_stats_enabled: bool) -> Vec<ColumnStats> {
    use serde_json::json;
    let schema = batch.schema();
    schema
        .fields()
        .iter()
        .enumerate()
        .map(|(i, f)| {
            let arr = batch.column(i);
            let null_count = arr.null_count() as i64;
            let (min, max, sum) = match arr.data_type() {
                DataType::Int16 => {
                    let a = arr.as_any().downcast_ref::<Int16Array>().unwrap();
                    (
                        arrow_min(a).map(|v| json!(v)),
                        arrow_max(a).map(|v| json!(v)),
                        Some(json!(sum_int16(a))),
                    )
                }
                DataType::Int32 => {
                    let a = arr.as_any().downcast_ref::<Int32Array>().unwrap();
                    (
                        arrow_min(a).map(|v| json!(v)),
                        arrow_max(a).map(|v| json!(v)),
                        Some(json!(sum_int32(a))),
                    )
                }
                DataType::Int64 => {
                    let a = arr.as_any().downcast_ref::<Int64Array>().unwrap();
                    (
                        arrow_min(a).map(|v| json!(v)),
                        arrow_max(a).map(|v| json!(v)),
                        // Store i64 sums as a decimal string so compact-time
                        // metadata can answer PG's SUM(bigint)/AVG(bigint)
                        // exactly even when the total exceeds i64.
                        Some(json!(sum_int64(a).to_string())),
                    )
                }
                DataType::Float32 => {
                    let a = arr.as_any().downcast_ref::<Float32Array>().unwrap();
                    (
                        arrow_min(a).map(|v| json!(v as f64)),
                        arrow_max(a).map(|v| json!(v as f64)),
                        arrow_sum(a).map(|v| json!(v as f64)),
                    )
                }
                DataType::Float64 => {
                    let a = arr.as_any().downcast_ref::<Float64Array>().unwrap();
                    (
                        arrow_min(a).map(|v| json!(v)),
                        arrow_max(a).map(|v| json!(v)),
                        arrow_sum(a).map(|v| json!(v)),
                    )
                }
                DataType::Boolean => {
                    let a = arr.as_any().downcast_ref::<BooleanArray>().unwrap();
                    (
                        min_boolean(a).map(|v| json!(v)),
                        max_boolean(a).map(|v| json!(v)),
                        None,
                    )
                }
                DataType::Timestamp(_, _) => {
                    let a = arr
                        .as_any()
                        .downcast_ref::<TimestampMicrosecondArray>()
                        .unwrap();
                    (
                        arrow_min(a).map(|v| json!(v)),
                        arrow_max(a).map(|v| json!(v)),
                        None,
                    )
                }
                DataType::Utf8 => {
                    let a = arr.as_any().downcast_ref::<StringArray>().unwrap();
                    let (min, max) = string_min_max(a);
                    (min.map(|v| json!(v)), max.map(|v| json!(v)), None)
                }
                // Min/max for binary skipped for now — bytea/jsonb ordering
                // does not map cleanly to normal SQL predicates.
                _ => (None, None, None),
            };
            // RYR-291: HyperLogLog++ for text columns only. Numeric
            // distinct counts are rarely the user's question; for text
            // columns this powers rvbbit.approx_distinct (cross-group
            // union) and feeds EXPLAIN SEMANTIC selectivity.
            let (hll_b64, distinct_estimate, text_sketch_b64) =
                if text_stats_enabled && arr.data_type() == &DataType::Utf8 {
                    let s = arr.as_any().downcast_ref::<StringArray>().unwrap();
                    let mut hll = crate::hll::Hll::new();
                    let mut sketch = TextSketch::new();
                    for row in 0..s.len() {
                        if !s.is_null(row) {
                            let value = s.value(row);
                            hll.insert(value);
                            sketch.insert_value(value);
                        }
                    }
                    let count = hll.count() as i64;
                    (Some(hll.to_b64()), Some(count), Some(sketch.to_b64()))
                } else {
                    (None, None, None)
                };
            ColumnStats {
                name: f.name().clone(),
                null_count,
                distinct_estimate,
                min,
                max,
                sum,
                hll_b64,
                text_sketch_b64,
            }
        })
        .collect()
}

fn string_min_max(a: &StringArray) -> (Option<String>, Option<String>) {
    let mut min: Option<&str> = None;
    let mut max: Option<&str> = None;
    for row in 0..a.len() {
        if a.is_null(row) {
            continue;
        }
        let value = a.value(row);
        if min.is_none_or(|current| value < current) {
            min = Some(value);
        }
        if max.is_none_or(|current| value > current) {
            max = Some(value);
        }
    }
    (min.map(str::to_string), max.map(str::to_string))
}

fn sum_int16(a: &Int16Array) -> i64 {
    let mut total = 0i64;
    for row in 0..a.len() {
        if !a.is_null(row) {
            total += a.value(row) as i64;
        }
    }
    total
}

fn sum_int32(a: &Int32Array) -> i64 {
    let mut total = 0i64;
    for row in 0..a.len() {
        if !a.is_null(row) {
            total += a.value(row) as i64;
        }
    }
    total
}

fn sum_int64(a: &Int64Array) -> i128 {
    let mut total = 0i128;
    for row in 0..a.len() {
        if !a.is_null(row) {
            total += a.value(row) as i128;
        }
    }
    total
}

/// Build per-group aggregate blocks for every column that's a viable
/// GROUP BY target (low cardinality). For each such column we walk
/// every numeric column once and bucket sum + count_nonnull by the
/// group column's value.
///
/// Cardinality cap: 256 distinct values per group column. Above that
/// we skip the column (the metadata grows linearly and the rewriter
/// can't substitute that many constants anyway).
const MAX_GROUP_CARDINALITY: usize = 256;

fn compute_per_group_stats(batch: &RecordBatch) -> Vec<PerGroupBlock> {
    use serde_json::json;
    let schema = batch.schema();
    let n_rows = batch.num_rows();
    let mut out = Vec::new();

    for (group_idx, group_field) in schema.fields().iter().enumerate() {
        // Candidate group columns: small ints / bools / dates. Floats
        // and timestamps are usually too unique. Text could be a group
        // column but skip for v0 to keep allocator pressure down.
        let group_arr = batch.column(group_idx);
        let dt = group_arr.data_type();
        let is_candidate = matches!(
            dt,
            DataType::Boolean | DataType::Int16 | DataType::Int32 | DataType::Int64
        );
        if !is_candidate {
            continue;
        }

        // Pass 1: bucket row indices by group value (as JSON for type-agnostic
        // serialization). Bail out if cardinality exceeds the cap.
        let mut buckets: HashMap<String, Vec<u32>> = HashMap::new();
        let mut bucket_value: HashMap<String, serde_json::Value> = HashMap::new();
        for row in 0..n_rows {
            let key = group_key_for(group_arr, row);
            let entry = buckets.entry(key.clone()).or_insert_with(Vec::new);
            entry.push(row as u32);
            if !bucket_value.contains_key(&key) {
                bucket_value.insert(key, value_for(group_arr, row));
            }
            if buckets.len() > MAX_GROUP_CARDINALITY {
                break;
            }
        }
        if buckets.len() > MAX_GROUP_CARDINALITY {
            continue;
        }

        // Pass 2: for each bucket, aggregate every numeric "other" column.
        let mut group_list: Vec<GroupBucket> = Vec::with_capacity(buckets.len());
        for (key, rows) in &buckets {
            let group_value = bucket_value.get(key).cloned().unwrap_or(json!(null));
            let count = rows.len() as i64;
            let mut agg: HashMap<String, NumericAgg> = HashMap::new();

            for (other_idx, other_field) in schema.fields().iter().enumerate() {
                if other_idx == group_idx {
                    continue;
                }
                let other_arr = batch.column(other_idx);
                let na = numeric_agg_for_bucket(other_arr, rows);
                if let Some(a) = na {
                    agg.insert(other_field.name().clone(), a);
                }
            }
            group_list.push(GroupBucket {
                value: group_value,
                count,
                agg,
            });
        }

        out.push(PerGroupBlock {
            group_column: group_field.name().clone(),
            groups: group_list,
        });
    }
    out
}

/// Stable string key for a group value — used to bucket rows in the
/// per-group HashMap without needing the value type at compile time.
fn group_key_for(arr: &dyn Array, row: usize) -> String {
    if arr.is_null(row) {
        return "__NULL__".into();
    }
    match arr.data_type() {
        DataType::Boolean => format!(
            "b:{}",
            arr.as_any()
                .downcast_ref::<BooleanArray>()
                .unwrap()
                .value(row)
        ),
        DataType::Int16 => format!(
            "i:{}",
            arr.as_any()
                .downcast_ref::<Int16Array>()
                .unwrap()
                .value(row)
        ),
        DataType::Int32 => format!(
            "i:{}",
            arr.as_any()
                .downcast_ref::<Int32Array>()
                .unwrap()
                .value(row)
        ),
        DataType::Int64 => format!(
            "i:{}",
            arr.as_any()
                .downcast_ref::<Int64Array>()
                .unwrap()
                .value(row)
        ),
        _ => "__OTHER__".into(),
    }
}

fn value_for(arr: &dyn Array, row: usize) -> serde_json::Value {
    use serde_json::json;
    if arr.is_null(row) {
        return json!(null);
    }
    match arr.data_type() {
        DataType::Boolean => json!(arr
            .as_any()
            .downcast_ref::<BooleanArray>()
            .unwrap()
            .value(row)),
        DataType::Int16 => json!(arr
            .as_any()
            .downcast_ref::<Int16Array>()
            .unwrap()
            .value(row)),
        DataType::Int32 => json!(arr
            .as_any()
            .downcast_ref::<Int32Array>()
            .unwrap()
            .value(row)),
        DataType::Int64 => json!(arr
            .as_any()
            .downcast_ref::<Int64Array>()
            .unwrap()
            .value(row)),
        _ => json!(null),
    }
}

/// Sum + non-null count of `arr` restricted to the given row indices.
/// Returns None for non-numeric columns.
fn numeric_agg_for_bucket(arr: &dyn Array, rows: &[u32]) -> Option<NumericAgg> {
    let mut sum: f64 = 0.0;
    let mut count_nonnull: i64 = 0;
    match arr.data_type() {
        DataType::Int16 => {
            let a = arr.as_any().downcast_ref::<Int16Array>().unwrap();
            for &r in rows {
                let i = r as usize;
                if !a.is_null(i) {
                    sum += a.value(i) as f64;
                    count_nonnull += 1;
                }
            }
        }
        DataType::Int32 => {
            let a = arr.as_any().downcast_ref::<Int32Array>().unwrap();
            for &r in rows {
                let i = r as usize;
                if !a.is_null(i) {
                    sum += a.value(i) as f64;
                    count_nonnull += 1;
                }
            }
        }
        DataType::Int64 => {
            let a = arr.as_any().downcast_ref::<Int64Array>().unwrap();
            for &r in rows {
                let i = r as usize;
                if !a.is_null(i) {
                    sum += a.value(i) as f64;
                    count_nonnull += 1;
                }
            }
        }
        DataType::Float32 => {
            let a = arr.as_any().downcast_ref::<Float32Array>().unwrap();
            for &r in rows {
                let i = r as usize;
                if !a.is_null(i) {
                    sum += a.value(i) as f64;
                    count_nonnull += 1;
                }
            }
        }
        DataType::Float64 => {
            let a = arr.as_any().downcast_ref::<Float64Array>().unwrap();
            for &r in rows {
                let i = r as usize;
                if !a.is_null(i) {
                    sum += a.value(i);
                    count_nonnull += 1;
                }
            }
        }
        _ => return None,
    }
    Some(NumericAgg { sum, count_nonnull })
}

fn compute_column_bitmaps(batch: &RecordBatch) -> Result<Vec<ColumnBitmapBlock>> {
    use serde_json::json;

    let schema = batch.schema();
    let max_distinct = compact_value_bitmap_max_distinct();
    let mut out = Vec::new();

    for (idx, field) in schema.fields().iter().enumerate() {
        let array = batch.column(idx);
        match array.data_type() {
            DataType::Boolean => {
                let a = array.as_any().downcast_ref::<BooleanArray>().unwrap();
                let entries = bitmap_entries_for_rows(a.len(), max_distinct, |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        Some((a.value(row).to_string(), json!(a.value(row))))
                    }
                })?;
                if !entries.is_empty() {
                    out.push(ColumnBitmapBlock {
                        column: field.name().clone(),
                        kind: "value".to_string(),
                        entries,
                    });
                }
            }
            DataType::Int16 => {
                let a = array.as_any().downcast_ref::<Int16Array>().unwrap();
                let entries = bitmap_entries_for_rows(a.len(), max_distinct, |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        let value = a.value(row) as i64;
                        Some((value.to_string(), json!(value)))
                    }
                })?;
                if !entries.is_empty() {
                    out.push(ColumnBitmapBlock {
                        column: field.name().clone(),
                        kind: "value".to_string(),
                        entries,
                    });
                }
            }
            DataType::Int32 => {
                let a = array.as_any().downcast_ref::<Int32Array>().unwrap();
                let entries = bitmap_entries_for_rows(a.len(), max_distinct, |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        let value = a.value(row) as i64;
                        Some((value.to_string(), json!(value)))
                    }
                })?;
                if !entries.is_empty() {
                    out.push(ColumnBitmapBlock {
                        column: field.name().clone(),
                        kind: "value".to_string(),
                        entries,
                    });
                }
            }
            DataType::Date32 => {
                let a = array.as_any().downcast_ref::<Date32Array>().unwrap();
                let entries = bitmap_entries_for_rows(a.len(), max_distinct, |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        let value = a.value(row) as i64;
                        Some((value.to_string(), json!(value)))
                    }
                })?;
                if !entries.is_empty() {
                    out.push(ColumnBitmapBlock {
                        column: field.name().clone(),
                        kind: "value".to_string(),
                        entries,
                    });
                }
            }
            DataType::Int64 => {
                let a = array.as_any().downcast_ref::<Int64Array>().unwrap();
                let entries = bitmap_entries_for_rows(a.len(), max_distinct, |row| {
                    if a.is_null(row) {
                        None
                    } else {
                        let value = a.value(row);
                        Some((value.to_string(), json!(value)))
                    }
                })?;
                if !entries.is_empty() {
                    out.push(ColumnBitmapBlock {
                        column: field.name().clone(),
                        kind: "value".to_string(),
                        entries,
                    });
                }
            }
            DataType::Utf8 => {
                let a = array.as_any().downcast_ref::<StringArray>().unwrap();
                let mut bitmap = RoaringBitmap::new();
                for row in 0..a.len() {
                    if !a.is_null(row) && !a.value(row).is_empty() {
                        bitmap.insert(row as u32);
                    }
                }
                if !bitmap.is_empty() {
                    out.push(ColumnBitmapBlock {
                        column: field.name().clone(),
                        kind: "not_empty".to_string(),
                        entries: vec![bitmap_entry(
                            "__not_empty__",
                            json!("__not_empty__"),
                            bitmap,
                        )?],
                    });
                }
            }
            _ => {}
        }
    }

    Ok(out)
}

fn bitmap_entries_for_rows<F>(
    len: usize,
    max_distinct: usize,
    mut value_for_row: F,
) -> Result<Vec<ColumnBitmapEntry>>
where
    F: FnMut(usize) -> Option<(String, serde_json::Value)>,
{
    let mut buckets: HashMap<String, (serde_json::Value, RoaringBitmap)> = HashMap::new();
    for row in 0..len {
        let Some((key, value)) = value_for_row(row) else {
            continue;
        };
        if !buckets.contains_key(&key) && buckets.len() >= max_distinct {
            return Ok(Vec::new());
        }
        buckets
            .entry(key)
            .or_insert_with(|| (value, RoaringBitmap::new()))
            .1
            .insert(row as u32);
    }

    let mut entries = Vec::with_capacity(buckets.len());
    for (value_text, (value, bitmap)) in buckets {
        entries.push(bitmap_entry(&value_text, value, bitmap)?);
    }
    entries.sort_unstable_by(|a, b| a.value_text.cmp(&b.value_text));
    Ok(entries)
}

fn bitmap_entry(
    value_text: &str,
    value: serde_json::Value,
    bitmap: RoaringBitmap,
) -> Result<ColumnBitmapEntry> {
    let mut buf = Vec::with_capacity(bitmap.serialized_size());
    bitmap
        .serialize_into(&mut buf)
        .context("serializing compact value bitmap")?;
    Ok(ColumnBitmapEntry {
        value,
        value_text: value_text.to_string(),
        bitmap_b64: B64.encode(buf),
        n_set: bitmap.len() as i64,
    })
}

fn compute_text_dictionaries(
    row_group_path: &Path,
    batch: &RecordBatch,
) -> Result<Vec<TextDictionaryBlock>> {
    let schema = batch.schema();
    let max_bytes = compact_text_dictionary_max_bytes();
    let mut out = Vec::new();

    for (idx, field) in schema.fields().iter().enumerate() {
        let array = batch.column(idx);
        if array.data_type() != &DataType::Utf8 {
            continue;
        }
        let strings = array.as_any().downcast_ref::<StringArray>().unwrap();
        let path = text_dictionary_path(row_group_path, idx);
        if let Some(block) = write_text_dictionary(&path, field.name(), strings, max_bytes)? {
            out.push(block);
        }
    }

    Ok(out)
}

fn write_text_dictionary(
    path: &Path,
    column: &str,
    array: &StringArray,
    max_bytes: usize,
) -> Result<Option<TextDictionaryBlock>> {
    let mut ids = HashMap::<String, u32>::new();
    let mut values = Vec::<String>::new();
    let mut codes = Vec::<u32>::with_capacity(array.len());
    let mut n_nulls = 0i64;
    let mut n_empty = 0i64;
    let mut value_bytes = 0usize;

    for row in 0..array.len() {
        if array.is_null(row) {
            codes.push(0);
            n_nulls += 1;
            continue;
        }
        let value = array.value(row);
        if value.is_empty() {
            n_empty += 1;
        }
        let code = if let Some(code) = ids.get(value) {
            *code
        } else {
            if values.len() >= u32::MAX as usize {
                return Ok(None);
            }
            let owned = value.to_string();
            let code = (values.len() + 1) as u32;
            value_bytes = value_bytes.saturating_add(owned.len());
            values.push(owned.clone());
            ids.insert(owned, code);
            code
        };
        codes.push(code);
    }

    let estimated_bytes = TEXT_DICTIONARY_MAGIC
        .len()
        .saturating_add(8 * 4)
        .saturating_add(values.len().saturating_mul(4))
        .saturating_add(value_bytes)
        .saturating_add(codes.len().saturating_mul(std::mem::size_of::<u32>()));
    if estimated_bytes > max_bytes {
        return Ok(None);
    }

    let mut file = File::create(path)
        .with_context(|| format!("creating text dictionary {}", path.display()))?;
    file.write_all(TEXT_DICTIONARY_MAGIC)
        .with_context(|| format!("writing text dictionary magic {}", path.display()))?;
    file.write_all(&(codes.len() as u64).to_le_bytes())?;
    file.write_all(&(values.len() as u64).to_le_bytes())?;
    file.write_all(&(n_nulls as u64).to_le_bytes())?;
    file.write_all(&(n_empty as u64).to_le_bytes())?;
    for value in &values {
        let bytes = value.as_bytes();
        if bytes.len() > u32::MAX as usize {
            return Ok(None);
        }
        file.write_all(&(bytes.len() as u32).to_le_bytes())?;
        file.write_all(bytes)?;
    }
    for code in &codes {
        file.write_all(&code.to_le_bytes())?;
    }
    file.flush()?;

    let n_bytes = std::fs::metadata(path)
        .with_context(|| format!("stat text dictionary {}", path.display()))?
        .len() as i64;
    Ok(Some(TextDictionaryBlock {
        column: column.to_string(),
        path: path.to_string_lossy().into_owned(),
        n_rows: codes.len() as i64,
        n_values: values.len() as i64,
        n_nulls,
        n_empty,
        n_bytes,
    }))
}

fn text_dictionary_path(row_group_path: &Path, column_idx: usize) -> PathBuf {
    let file_name = row_group_path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "row_group".into());
    row_group_path.with_file_name(format!("{file_name}.textdict.{column_idx}.rvbbit"))
}

fn read_u64(file: &mut File, path: &Path) -> Result<u64> {
    let mut buf = [0u8; 8];
    file.read_exact(&mut buf)
        .with_context(|| format!("reading u64 from text dictionary {}", path.display()))?;
    Ok(u64::from_le_bytes(buf))
}

fn read_u32(file: &mut File, path: &Path) -> Result<u32> {
    let mut buf = [0u8; 4];
    file.read_exact(&mut buf)
        .with_context(|| format!("reading u32 from text dictionary {}", path.display()))?;
    Ok(u32::from_le_bytes(buf))
}

// ---------------------------------------------------------------------------
// Reader
// ---------------------------------------------------------------------------

pub struct RowGroupReader;

pub struct RowGroupInfo {
    pub n_rows: i64,
    pub n_bytes: i64,
    pub n_columns: i32,
    pub schema: SchemaRef,
}

impl RowGroupReader {
    /// Cheap metadata-only read — no column data is touched.
    pub fn info(path: &Path) -> Result<RowGroupInfo> {
        let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
        let n_bytes = file.metadata()?.len() as i64;
        let builder = ParquetRecordBatchReaderBuilder::try_new(file)
            .context("opening parquet reader (info)")?;
        let schema = builder.schema().clone();
        let n_rows = builder.metadata().file_metadata().num_rows();
        let n_columns = schema.fields().len() as i32;
        Ok(RowGroupInfo {
            n_rows,
            n_bytes,
            n_columns,
            schema,
        })
    }

    /// Open a reader that materializes ALL columns.
    pub fn open(path: &Path) -> Result<ParquetRecordBatchReader> {
        let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
        ParquetRecordBatchReaderBuilder::try_new(file)
            .context("opening parquet reader")?
            .with_batch_size(READ_BATCH_SIZE)
            .build()
            .context("building parquet reader")
    }

    /// Open a reader that materializes all columns but only the requested row
    /// offsets. Offsets are relative to this row-group file and may be sparse.
    pub fn open_selected_rows(path: &Path, rows: &[usize]) -> Result<ParquetRecordBatchReader> {
        if rows.is_empty() {
            return Err(anyhow!("open_selected_rows requires at least one row"));
        }
        let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
        let builder =
            ParquetRecordBatchReaderBuilder::try_new(file).context("opening parquet reader")?;
        let total_rows = builder.metadata().file_metadata().num_rows() as usize;
        let ranges = selected_row_ranges(rows, total_rows)?;
        let selection = RowSelection::from_consecutive_ranges(ranges.into_iter(), total_rows);
        builder
            .with_row_selection(selection)
            .with_batch_size(READ_BATCH_SIZE)
            .build()
            .context("building selected-row parquet reader")
    }

    /// Open a reader that materializes only the named columns and only the
    /// requested row offsets. Offsets are relative to this row-group file.
    pub fn open_projected_selected_rows(
        path: &Path,
        columns: &[&str],
        rows: &[usize],
    ) -> Result<ParquetRecordBatchReader> {
        if rows.is_empty() {
            return Err(anyhow!(
                "open_projected_selected_rows requires at least one row"
            ));
        }
        let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
        let builder =
            ParquetRecordBatchReaderBuilder::try_new(file).context("opening parquet reader")?;
        let schema = builder.schema();
        let mut indices = Vec::with_capacity(columns.len());
        for name in columns {
            let idx = schema.index_of(name).map_err(|_| {
                anyhow!(
                    "column '{}' not found in parquet schema (has: {:?})",
                    name,
                    schema
                        .fields()
                        .iter()
                        .map(|f| f.name().as_str())
                        .collect::<Vec<_>>()
                )
            })?;
            indices.push(idx);
        }
        let total_rows = builder.metadata().file_metadata().num_rows() as usize;
        let ranges = selected_row_ranges(rows, total_rows)?;
        let selection = RowSelection::from_consecutive_ranges(ranges.into_iter(), total_rows);
        let mask = ProjectionMask::roots(builder.parquet_schema(), indices);
        builder
            .with_projection(mask)
            .with_row_selection(selection)
            .with_batch_size(READ_BATCH_SIZE)
            .build()
            .context("building projected selected-row parquet reader")
    }

    /// Open a reader that materializes only the named columns. Names not
    /// found are an error so the caller doesn't silently get empty results.
    pub fn open_projected(path: &Path, columns: &[&str]) -> Result<ParquetRecordBatchReader> {
        let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
        let builder = ParquetRecordBatchReaderBuilder::try_new(file)
            .context("opening parquet reader (projected)")?;
        let schema = builder.schema();
        let mut indices = Vec::with_capacity(columns.len());
        for name in columns {
            let idx = schema.index_of(name).map_err(|_| {
                anyhow!(
                    "column '{}' not found in parquet schema (has: {:?})",
                    name,
                    schema
                        .fields()
                        .iter()
                        .map(|f| f.name().as_str())
                        .collect::<Vec<_>>()
                )
            })?;
            indices.push(idx);
        }
        let mask = ProjectionMask::roots(builder.parquet_schema(), indices);
        builder
            .with_projection(mask)
            .with_batch_size(READ_BATCH_SIZE)
            .build()
            .context("building projected parquet reader")
    }
}

fn selected_row_ranges(rows: &[usize], total_rows: usize) -> Result<Vec<Range<usize>>> {
    let mut sorted = rows.to_vec();
    sorted.sort_unstable();
    sorted.dedup();

    let mut ranges: Vec<Range<usize>> = Vec::with_capacity(sorted.len());
    for row in sorted {
        if row >= total_rows {
            return Err(anyhow!(
                "selected row {} is outside row-group row count {}",
                row,
                total_rows
            ));
        }
        match ranges.last_mut() {
            Some(last) if last.end == row => last.end += 1,
            _ => ranges.push(row..row + 1),
        }
    }
    Ok(ranges)
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow::array::{Int32Array, Int64Array, StringArray};
    use arrow::datatypes::{DataType, Field, Schema};
    use std::sync::Arc;
    use tempfile::tempdir;

    fn sample_batch() -> RecordBatch {
        let schema = Arc::new(Schema::new(vec![
            Field::new("id", DataType::Int64, false),
            Field::new("name", DataType::Utf8, false),
            Field::new("score", DataType::Int32, false),
        ]));
        RecordBatch::try_new(
            schema,
            vec![
                Arc::new(Int64Array::from(vec![1, 2, 3, 4])),
                Arc::new(StringArray::from(vec!["a", "bb", "ccc", "dddd"])),
                Arc::new(Int32Array::from(vec![10, 20, 30, 40])),
            ],
        )
        .unwrap()
    }

    #[test]
    fn writes_then_reads_back() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("rg.parquet");
        let batch = sample_batch();
        let meta = RowGroupWriter::write(&path, 0, &batch).unwrap();
        assert_eq!(meta.n_rows, 4);

        let info = RowGroupReader::info(&path).unwrap();
        assert_eq!(info.n_rows, 4);
        assert_eq!(info.n_columns, 3);

        let reader = RowGroupReader::open(&path).unwrap();
        let mut total = 0;
        for batch in reader {
            total += batch.unwrap().num_rows();
        }
        assert_eq!(total, 4);
    }

    #[test]
    fn projected_read_returns_only_requested_columns() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("rg.parquet");
        RowGroupWriter::write(&path, 0, &sample_batch()).unwrap();

        let reader = RowGroupReader::open_projected(&path, &["id", "score"]).unwrap();
        let mut total_cols_seen = 0;
        for batch in reader {
            let b = batch.unwrap();
            assert_eq!(b.num_columns(), 2);
            assert_eq!(b.schema().field(0).name(), "id");
            assert_eq!(b.schema().field(1).name(), "score");
            total_cols_seen = b.num_columns();
        }
        assert_eq!(total_cols_seen, 2);
    }

    #[test]
    fn selected_row_read_returns_only_requested_rows() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("rg.parquet");
        RowGroupWriter::write(&path, 0, &sample_batch()).unwrap();

        let reader = RowGroupReader::open_selected_rows(&path, &[3, 1]).unwrap();
        let mut ids = Vec::new();
        let mut names = Vec::new();
        for batch in reader {
            let b = batch.unwrap();
            let id = b.column(0).as_any().downcast_ref::<Int64Array>().unwrap();
            let name = b.column(1).as_any().downcast_ref::<StringArray>().unwrap();
            for row in 0..b.num_rows() {
                ids.push(id.value(row));
                names.push(name.value(row).to_string());
            }
        }

        assert_eq!(ids, vec![2, 4]);
        assert_eq!(names, vec!["bb", "dddd"]);
    }

    #[test]
    fn text_dictionary_sidecar_round_trips_codes() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("rg.parquet");
        let batch = sample_batch();
        let meta = RowGroupWriter::write(&path, 0, &batch).unwrap();
        assert_eq!(meta.text_dictionaries.len(), 1);

        let dict = TextDictionary::read(Path::new(&meta.text_dictionaries[0].path)).unwrap();
        assert_eq!(dict.values, vec!["a", "bb", "ccc", "dddd"]);
        assert_eq!(dict.codes, vec![1, 2, 3, 4]);
        assert_eq!(dict.value_for_code(2), Some("bb"));
    }

    #[test]
    fn unknown_column_in_projection_errors() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("rg.parquet");
        RowGroupWriter::write(&path, 0, &sample_batch()).unwrap();
        let result = RowGroupReader::open_projected(&path, &["id", "nope"]);
        let err = match result {
            Ok(_) => panic!("expected error for unknown column"),
            Err(e) => e,
        };
        assert!(err.to_string().contains("'nope'"));
    }
}
