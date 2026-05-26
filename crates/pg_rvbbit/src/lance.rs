//! Phase 4 spike: Lance dataset support inside pg_rvbbit.
//!
//! Lance is a DataFusion-native columnar format with first-class vector
//! search (IVF-PQ / HNSW indices) and ObjectStore-native IO. The big
//! reason to integrate is rvbbit's vector workloads — `knn_text`,
//! embeddings, semantic-bitmap predicate caches — which today brute-
//! force scan a parquet column. Lance is purpose-built for that.
//!
//! This module ships the in-process integration substrate: three SQL
//! functions (create_demo, count, knn) backed by the upstream `lance`
//! crate, running on the same per-backend tokio Runtime that df.rs uses.
//! Once the spike validates the embed, downstream Phase 4 slices wire
//! Lance files into the rvbbit catalog and compact() pipeline.

use std::sync::Arc;

use arrow::array::{
    Array, FixedSizeListArray, Float32Array, Int64Array, RecordBatch, RecordBatchIterator,
};
use arrow::datatypes::{DataType, Field, Schema};
use lance::dataset::{Dataset, WriteParams};
use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::{json, Value};

use crate::df::with_lance_runtime;

/// rvbbit.lance_create_demo(path text, n_rows bigint, dim int) — write
/// a tiny synthetic Lance dataset with (id, embedding) columns so we can
/// exercise the read path without depending on Python or a separate
/// generator. embedding is a FixedSizeList<Float32; dim>. Values are
/// deterministic-pseudo-random so two runs produce identical content.
#[pg_extern]
fn lance_create_demo(path: &str, n_rows: i64, dim: i32) -> i64 {
    let dim_usize = dim.max(1) as usize;
    let n_rows_usize = n_rows.max(0) as usize;

    let id_array = Int64Array::from_iter_values(0..n_rows);

    let mut values: Vec<f32> = Vec::with_capacity(n_rows_usize * dim_usize);
    for i in 0..n_rows_usize {
        for d in 0..dim_usize {
            // Deterministic-pseudo-random in [-1, 1].
            values.push(((i as f32) * 0.137 + (d as f32) * 0.231).sin());
        }
    }
    let values_array = Float32Array::from(values);
    let item_field = Arc::new(Field::new("item", DataType::Float32, false));
    let embedding_array = match FixedSizeListArray::try_new(
        item_field.clone(),
        dim,
        Arc::new(values_array),
        None,
    ) {
        Ok(a) => a,
        Err(e) => pgrx::error!("rvbbit.lance_create_demo: FixedSizeList: {e}"),
    };

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, false),
        Field::new(
            "embedding",
            DataType::FixedSizeList(item_field, dim),
            false,
        ),
    ]));

    let batch = match RecordBatch::try_new(
        schema.clone(),
        vec![Arc::new(id_array), Arc::new(embedding_array)],
    ) {
        Ok(b) => b,
        Err(e) => pgrx::error!("rvbbit.lance_create_demo: RecordBatch: {e}"),
    };

    let batches = RecordBatchIterator::new(vec![Ok(batch)], schema);
    let path = path.to_string();
    let rows = n_rows;

    with_lance_runtime(|rt| {
        rt.block_on(async {
            // Lance writes a fresh dataset on overwrite; we recreate
            // every time so the demo is idempotent across re-runs.
            let mut params = WriteParams::default();
            params.mode = lance::dataset::WriteMode::Overwrite;
            Dataset::write(batches, &path, Some(params))
                .await
                .map_err(|e| format!("Dataset::write: {e}"))
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_create_demo: {e}"));

    rows
}

/// rvbbit.lance_count(path text) — open a Lance dataset and return its
/// row count. Smallest possible round-trip through the read path; if
/// this works the integration is fundamentally sound.
#[pg_extern]
fn lance_count(path: &str) -> i64 {
    let path = path.to_string();
    with_lance_runtime(|rt| {
        rt.block_on(async {
            let dataset = Dataset::open(&path)
                .await
                .map_err(|e| format!("Dataset::open({path}): {e}"))?;
            let count = dataset
                .count_rows(None)
                .await
                .map_err(|e| format!("count_rows: {e}"))?;
            Ok::<i64, String>(count as i64)
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_count: {e}"))
}

/// rvbbit.lance_knn(path text, query real[], k int) — k-nearest-
/// neighbor search against the `embedding` column of a Lance dataset.
/// Returns a JSON array of {id, _distance} objects ordered nearest
/// first. Uses Lance's built-in vector search (which auto-selects
/// brute-force or IVF-PQ depending on dataset size + index state).
#[pg_extern]
fn lance_knn(path: &str, query: Vec<f32>, k: i32) -> JsonB {
    let path = path.to_string();
    let k = k.max(1) as usize;
    let value = with_lance_runtime(|rt| {
        rt.block_on(async {
            let dataset = Dataset::open(&path)
                .await
                .map_err(|e| format!("Dataset::open({path}): {e}"))?;
            let q = Float32Array::from(query);
            let mut scanner = dataset.scan();
            scanner
                .nearest("embedding", &q, k)
                .map_err(|e| format!("scanner.nearest: {e}"))?;
            let batch = scanner
                .try_into_batch()
                .await
                .map_err(|e| format!("scanner.try_into_batch: {e}"))?;
            Ok::<Value, String>(batch_to_json_rows(&batch))
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_knn: {e}"));
    JsonB(value)
}

/// Render a Lance KNN result batch (id, _distance, [embedding]) as a
/// JSON array. We strip the embedding vector itself out to keep the
/// response shape small — the caller usually wants ids + distances,
/// not 384-dim vectors round-tripped through PG.
fn batch_to_json_rows(batch: &RecordBatch) -> Value {
    let mut rows = Vec::with_capacity(batch.num_rows());
    let schema = batch.schema();
    let fields: Vec<&Field> = schema.fields().iter().map(|f| f.as_ref()).collect();
    for row_idx in 0..batch.num_rows() {
        let mut obj = serde_json::Map::with_capacity(batch.num_columns());
        for (col_idx, field) in fields.iter().enumerate() {
            if matches!(field.data_type(), DataType::FixedSizeList(_, _) | DataType::List(_)) {
                // Skip large vector columns to keep the JSON small.
                continue;
            }
            let col = batch.column(col_idx);
            let val = match field.data_type() {
                DataType::Int64 => {
                    let a = col.as_any().downcast_ref::<Int64Array>().unwrap();
                    json!(a.value(row_idx))
                }
                DataType::Float32 => {
                    let a = col.as_any().downcast_ref::<Float32Array>().unwrap();
                    json!(a.value(row_idx))
                }
                _ => json!(arrow::array::cast::AsArray::as_string::<i32>(col.as_ref())
                    .value(row_idx)),
            };
            obj.insert(field.name().clone(), val);
        }
        rows.push(Value::Object(obj));
    }
    Value::Array(rows)
}
