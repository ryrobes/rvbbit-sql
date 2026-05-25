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
use std::ops::Range;
use std::path::Path;

use anyhow::{anyhow, Context, Result};
use arrow::array::{
    Array, BooleanArray, Float32Array, Float64Array, Int16Array, Int32Array, Int64Array,
    RecordBatch, StringArray, TimestampMicrosecondArray,
};
use arrow::compute::kernels::aggregate::{
    max as arrow_max, max_boolean, min as arrow_min, min_boolean, sum as arrow_sum,
};
use arrow::datatypes::{DataType, SchemaRef};
use parquet::arrow::arrow_reader::{
    ParquetRecordBatchReader, ParquetRecordBatchReaderBuilder, RowSelection,
};
use parquet::arrow::ArrowWriter;
use parquet::arrow::ProjectionMask;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::properties::WriterProperties;

use crate::metadata::{
    ColumnStats, GroupBucket, NumericAgg, PerGroupBlock, RowGroupMeta, TextSketch,
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

pub struct RowGroupWriter;

impl RowGroupWriter {
    pub fn write(path: &Path, rg_id: i64, batch: &RecordBatch) -> Result<RowGroupMeta> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating parent dir {}", parent.display()))?;
        }

        let schema: SchemaRef = batch.schema();
        let props = WriterProperties::builder()
            .set_compression(Compression::ZSTD(
                ZstdLevel::try_new(DEFAULT_ZSTD_LEVEL).expect("valid zstd level"),
            ))
            .set_statistics_enabled(parquet::file::properties::EnabledStatistics::Page)
            .build();

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
        let column_stats = compute_arrow_stats(batch, compact_text_stats_enabled());
        let per_group_stats = if compact_per_group_stats_enabled() {
            compute_per_group_stats(batch)
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
        })
    }
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
    env_enabled("RVBBIT_COMPACT_TEXT_STATS", false)
}

fn compact_per_group_stats_enabled() -> bool {
    env_enabled("RVBBIT_COMPACT_PER_GROUP_STATS", false)
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
                        arrow_sum(a).map(|v| json!(v)),
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
