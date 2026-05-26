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
use lance::index::vector::VectorIndexParams;
use lance::index::DatasetIndexExt;
use lance_index::IndexType;
use lance_linalg::distance::MetricType;
use pgrx::prelude::*;
use pgrx::{JsonB, Spi};
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

/// rvbbit.lance_import_column(reloid, pk_col, vec_col, dim, lance_path) —
/// export (pk, vec) from an existing rvbbit table into a fresh Lance
/// dataset, so operators can later build a vector index over it and run
/// fast KNN.
///
/// pk_col must be a bigint-compatible column (we cast via PG); vec_col
/// must be `real[]` (PG float4 array) with exactly `dim` elements per
/// row. Reads through SPI so it works on cold-tier tables too (the
/// custom_scan fall-through to df.rs handles ObjectStore reads).
///
/// Overwrites any existing dataset at `lance_path`. Returns the number
/// of rows written.
#[pg_extern]
fn lance_import_column(
    reloid: pg_sys::Oid,
    pk_col: &str,
    vec_col: &str,
    dim: i32,
    lance_path: &str,
) -> i64 {
    let dim_usize = dim.max(1) as usize;
    let rel_oid = reloid.to_u32();

    // Resolve qualified name so the SELECT is unambiguous.
    let qualified: String = match Spi::get_one::<String>(&format!(
        "SELECT n.nspname::text || '.' || c.relname::text \
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
         WHERE c.oid = {rel_oid}::oid"
    )) {
        Ok(Some(q)) => q,
        Ok(None) => pgrx::error!("rvbbit.lance_import_column: oid {rel_oid} does not exist"),
        Err(e) => pgrx::error!("rvbbit.lance_import_column: resolve oid: {e}"),
    };
    let select_sql = format!(
        "SELECT {pk_col}::bigint, {vec_col}::real[] FROM {qualified}"
    );

    // Read rows via SPI into Rust Vecs. SPI is sync, so we materialize
    // before entering the tokio runtime block.
    let mut pks: Vec<i64> = Vec::new();
    let mut values: Vec<f32> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&select_sql, None, &[])?;
        for row in table {
            let pk: i64 = row.get::<i64>(1)?.unwrap_or(0);
            let vec_row: Vec<Option<f32>> = row
                .get::<Vec<Option<f32>>>(2)?
                .unwrap_or_default();
            if vec_row.len() != dim_usize {
                pgrx::error!(
                    "rvbbit.lance_import_column: row pk={pk} has {} dims, expected {dim_usize}",
                    vec_row.len()
                );
            }
            pks.push(pk);
            for v in vec_row {
                values.push(v.unwrap_or(0.0));
            }
        }
        Ok(())
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_import_column: SPI: {e}"));

    let n_rows = pks.len() as i64;
    if n_rows == 0 {
        pgrx::warning!("rvbbit.lance_import_column: source query returned 0 rows");
        return 0;
    }

    let pk_array = Int64Array::from(pks);
    let values_array = Float32Array::from(values);
    let item_field = Arc::new(Field::new("item", DataType::Float32, false));
    let embedding_array = match FixedSizeListArray::try_new(
        item_field.clone(),
        dim,
        Arc::new(values_array),
        None,
    ) {
        Ok(a) => a,
        Err(e) => pgrx::error!("rvbbit.lance_import_column: FixedSizeList: {e}"),
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
        vec![Arc::new(pk_array), Arc::new(embedding_array)],
    ) {
        Ok(b) => b,
        Err(e) => pgrx::error!("rvbbit.lance_import_column: RecordBatch: {e}"),
    };
    let batches = RecordBatchIterator::new(vec![Ok(batch)], schema);

    let lance_path = lance_path.to_string();
    with_lance_runtime(|rt| {
        rt.block_on(async {
            let mut params = WriteParams::default();
            params.mode = lance::dataset::WriteMode::Overwrite;
            Dataset::write(batches, &lance_path, Some(params))
                .await
                .map_err(|e| format!("Dataset::write: {e}"))
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_import_column: {e}"));

    n_rows
}

/// rvbbit.lance_build_index(path, column, num_partitions, num_sub_vectors)
/// — create an IVF-PQ vector index on a Lance dataset's embedding column.
///
/// num_partitions controls the inverted-file granularity; rule of thumb
/// is sqrt(n_rows). num_sub_vectors must divide the embedding dimension;
/// 8-bit codes mean each subvector compresses to 1 byte. Metric is L2.
/// Subsequent rvbbit.lance_knn() calls automatically use the index when
/// present.
#[pg_extern]
fn lance_build_index(
    path: &str,
    column: &str,
    num_partitions: i32,
    num_sub_vectors: i32,
) -> i64 {
    let path = path.to_string();
    let column = column.to_string();
    let num_partitions = num_partitions.max(1) as usize;
    let num_sub_vectors = num_sub_vectors.max(1) as usize;

    with_lance_runtime(|rt| {
        rt.block_on(async {
            let mut dataset = Dataset::open(&path)
                .await
                .map_err(|e| format!("Dataset::open({path}): {e}"))?;

            // IVF-PQ params: 8-bit PQ codes, L2 metric, 50 k-means iterations.
            let params = VectorIndexParams::ivf_pq(
                num_partitions,
                8,
                num_sub_vectors,
                MetricType::L2,
                50,
            );

            dataset
                .create_index(
                    &[column.as_str()],
                    IndexType::IvfPq,
                    Some(format!("rvbbit_ivf_pq_{column}")),
                    &params,
                    /* replace */ true,
                )
                .await
                .map_err(|e| format!("create_index: {e}"))?;

            let row_count = dataset
                .count_rows(None)
                .await
                .map_err(|e| format!("count_rows: {e}"))?;
            Ok::<i64, String>(row_count as i64)
        })
    })
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_build_index: {e}"))
}
