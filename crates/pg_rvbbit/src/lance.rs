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

use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow::array::{
    Array, FixedSizeListArray, Float32Array, Int64Array, RecordBatch, RecordBatchIterator,
    StringArray,
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
    let embedding_array =
        match FixedSizeListArray::try_new(item_field.clone(), dim, Arc::new(values_array), None) {
            Ok(a) => a,
            Err(e) => pgrx::error!("rvbbit.lance_create_demo: FixedSizeList: {e}"),
        };

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, false),
        Field::new("embedding", DataType::FixedSizeList(item_field, dim), false),
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
            if matches!(
                field.data_type(),
                DataType::FixedSizeList(_, _) | DataType::List(_)
            ) {
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
                _ => {
                    json!(arrow::array::cast::AsArray::as_string::<i32>(col.as_ref()).value(row_idx))
                }
            };
            obj.insert(field.name().clone(), val);
        }
        rows.push(Value::Object(obj));
    }
    Value::Array(rows)
}

#[pg_extern]
fn lance_enable_text(
    reloid: pg_sys::Oid,
    col: &str,
    lance_url: &str,
    specialist: default!(&str, "''"),
) -> JsonB {
    let rel_oid = reloid.to_u32();
    let spec = match crate::embeddings::resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("rvbbit.lance_enable_text: {e}"),
    };
    let specialist_name = spec.name.clone();
    let result = match refresh_lance_text_dataset(rel_oid, col, &specialist_name, lance_url) {
        Ok(r) => r,
        Err(e) => {
            let col_lit = sql_literal(col);
            let spec_lit = sql_literal(&specialist_name);
            let msg_lit = sql_literal(&e);
            let _ = Spi::run(&format!(
                "UPDATE rvbbit.lance_text_indexes \
                    SET status = 'failed', status_message = {msg_lit}, refreshed_at = clock_timestamp() \
                  WHERE table_oid = {rel_oid}::oid \
                    AND column_name = {col_lit} \
                    AND specialist = {spec_lit}"
            ));
            pgrx::error!("rvbbit.lance_enable_text: {e}");
        }
    };
    upsert_lance_text_index(
        rel_oid,
        col,
        &specialist_name,
        lance_url,
        result.dim,
        result.n_values,
    )
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_enable_text: catalog update: {e}"));
    JsonB(json!({
        "table_oid": rel_oid,
        "column": col,
        "specialist": specialist_name,
        "lance_url": lance_url,
        "dim": result.dim,
        "n_values": result.n_values,
        "status": "ready"
    }))
}

#[pg_extern]
fn lance_refresh_text(reloid: pg_sys::Oid, col: &str, specialist: default!(&str, "''")) -> JsonB {
    let rel_oid = reloid.to_u32();
    let spec = match crate::embeddings::resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("rvbbit.lance_refresh_text: {e}"),
    };
    let specialist_name = spec.name.clone();
    let col_lit = sql_literal(col);
    let spec_lit = sql_literal(&specialist_name);
    let lance_url: String = match Spi::get_one::<String>(&format!(
        "SELECT lance_url FROM rvbbit.lance_text_indexes \
         WHERE table_oid = {rel_oid}::oid \
           AND column_name = {col_lit} \
           AND specialist = {spec_lit} \
           AND status <> 'disabled'"
    )) {
        Ok(Some(path)) => path,
        Ok(None) => pgrx::error!(
            "rvbbit.lance_refresh_text: no Lance text index for table oid {rel_oid}, column {col}, specialist {specialist_name}"
        ),
        Err(e) => pgrx::error!("rvbbit.lance_refresh_text: catalog lookup: {e}"),
    };
    let result = match refresh_lance_text_dataset(rel_oid, col, &specialist_name, &lance_url) {
        Ok(r) => r,
        Err(e) => {
            let msg_lit = sql_literal(&e);
            let _ = Spi::run(&format!(
                "UPDATE rvbbit.lance_text_indexes \
                    SET status = 'failed', status_message = {msg_lit}, refreshed_at = clock_timestamp() \
                  WHERE table_oid = {rel_oid}::oid \
                    AND column_name = {col_lit} \
                    AND specialist = {spec_lit}"
            ));
            pgrx::error!("rvbbit.lance_refresh_text: {e}");
        }
    };
    upsert_lance_text_index(
        rel_oid,
        col,
        &specialist_name,
        &lance_url,
        result.dim,
        result.n_values,
    )
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_refresh_text: catalog update: {e}"));
    JsonB(json!({
        "table_oid": rel_oid,
        "column": col,
        "specialist": specialist_name,
        "lance_url": lance_url,
        "dim": result.dim,
        "n_values": result.n_values,
        "status": "ready"
    }))
}

struct LanceTextRefresh {
    n_values: i64,
    dim: i32,
}

#[pg_extern]
fn kg_lance_enable(
    node_kind: &str,
    graph: default!(&str, "''"),
    specialist: default!(&str, "''"),
    lance_url: default!(&str, "''"),
) -> JsonB {
    let norm_graph = match kg_normalize_graph_value(graph) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.kg_lance_enable: {e}"),
    };
    let norm_kind = match kg_normalize_kind_value(node_kind) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.kg_lance_enable: {e}"),
    };
    let spec = match crate::embeddings::resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("rvbbit.kg_lance_enable: {e}"),
    };
    let lance_path = match kg_lance_path(&norm_graph, &norm_kind, &spec.name, lance_url) {
        Ok(path) => path,
        Err(e) => pgrx::error!("rvbbit.kg_lance_enable: {e}"),
    };

    match refresh_kg_lance_nodes(&norm_graph, &norm_kind, &spec.name, &lance_path) {
        Ok(result) => {
            upsert_kg_lance_index(
                &norm_graph,
                &norm_kind,
                &spec.name,
                &lance_path,
                result.dim,
                result.n_values,
                "ready",
                None,
            )
            .unwrap_or_else(|e| pgrx::error!("rvbbit.kg_lance_enable: catalog update: {e}"));
            JsonB(json!({
                "graph_id": norm_graph,
                "kind": norm_kind,
                "target": "nodes",
                "specialist": spec.name,
                "lance_url": lance_path,
                "dim": result.dim,
                "n_values": result.n_values,
                "status": "ready"
            }))
        }
        Err(e) => {
            let _ = upsert_kg_lance_index(
                &norm_graph,
                &norm_kind,
                &spec.name,
                &lance_path,
                0,
                0,
                "failed",
                Some(&e),
            );
            pgrx::error!("rvbbit.kg_lance_enable: {e}");
        }
    }
}

#[pg_extern]
fn kg_lance_refresh(
    node_kind: &str,
    graph: default!(&str, "''"),
    specialist: default!(&str, "''"),
) -> JsonB {
    let norm_graph = match kg_normalize_graph_value(graph) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.kg_lance_refresh: {e}"),
    };
    let norm_kind = match kg_normalize_kind_value(node_kind) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.kg_lance_refresh: {e}"),
    };
    let spec = match crate::embeddings::resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("rvbbit.kg_lance_refresh: {e}"),
    };
    let path = match kg_lance_catalog_path(&norm_graph, &norm_kind, &spec.name) {
        Ok(Some(path)) => path,
        Ok(None) => pgrx::error!(
            "rvbbit.kg_lance_refresh: no KG Lance index for graph {norm_graph}, kind {norm_kind}, specialist {}",
            spec.name
        ),
        Err(e) => pgrx::error!("rvbbit.kg_lance_refresh: {e}"),
    };

    match refresh_kg_lance_nodes(&norm_graph, &norm_kind, &spec.name, &path) {
        Ok(result) => {
            upsert_kg_lance_index(
                &norm_graph,
                &norm_kind,
                &spec.name,
                &path,
                result.dim,
                result.n_values,
                "ready",
                None,
            )
            .unwrap_or_else(|e| pgrx::error!("rvbbit.kg_lance_refresh: catalog update: {e}"));
            JsonB(json!({
                "graph_id": norm_graph,
                "kind": norm_kind,
                "target": "nodes",
                "specialist": spec.name,
                "lance_url": path,
                "dim": result.dim,
                "n_values": result.n_values,
                "status": "ready"
            }))
        }
        Err(e) => {
            let _ = upsert_kg_lance_index(
                &norm_graph,
                &norm_kind,
                &spec.name,
                &path,
                0,
                0,
                "failed",
                Some(&e),
            );
            pgrx::error!("rvbbit.kg_lance_refresh: {e}");
        }
    }
}

#[pg_extern]
fn kg_lance_resolve_nodes(
    node_kind: &str,
    node_label: &str,
    specialist: default!(&str, "''"),
    match_threshold: default!(f64, "0.92"),
    graph: default!(&str, "''"),
    limit_count: default!(i32, "10"),
) -> JsonB {
    let value = match kg_lance_resolve_nodes_impl(
        node_kind,
        node_label,
        specialist,
        match_threshold,
        graph,
        limit_count,
    ) {
        Ok(v) => v,
        Err(_) => Value::Array(Vec::new()),
    };
    JsonB(value)
}

pub(crate) fn try_knn_text_lance(
    rel_oid: u32,
    col: &str,
    specialist: &str,
    query: &[f32],
    k: usize,
) -> Result<Option<Vec<(String, f64)>>, String> {
    let col_lit = sql_literal(col);
    let spec_lit = sql_literal(specialist);
    let Some(path) = Spi::get_one::<String>(&format!(
        "SELECT (SELECT lance_url FROM rvbbit.lance_text_indexes \
                 WHERE table_oid = {rel_oid}::oid \
                   AND column_name = {col_lit} \
                   AND specialist = {spec_lit} \
                   AND status = 'ready' \
                 LIMIT 1)"
    ))
    .map_err(|e| format!("catalog lookup: {e}"))?
    else {
        return Ok(None);
    };
    knn_text_dataset(&path, query, k).map(Some)
}

fn refresh_lance_text_dataset(
    rel_oid: u32,
    col: &str,
    specialist: &str,
    lance_path: &str,
) -> Result<LanceTextRefresh, String> {
    let spec = crate::embeddings::resolve_specialist(specialist)?;
    let model = crate::embeddings::spec_model(&spec);
    let texts = distinct_text_values(rel_oid, col)?;
    if texts.is_empty() {
        return Err("source column has no non-null text values".to_string());
    }

    let cache_map = crate::embeddings::bulk_cache_lookup(&spec.name);
    let mut pairs: Vec<(String, Vec<f32>)> = Vec::with_capacity(texts.len());
    let mut to_embed: Vec<String> = Vec::new();
    for text in texts {
        let h = crate::embeddings::text_hash(&spec.name, &text);
        match cache_map.get(&h) {
            Some(v) => pairs.push((text, v.clone())),
            None => to_embed.push(text),
        }
    }

    let batch_size = spec.batch_size.max(1);
    for chunk in to_embed.chunks(batch_size) {
        let inputs: Vec<Value> = chunk.iter().map(|t| json!({"text": t})).collect();
        let result = crate::specialists::predict_batch(&spec, &inputs)
            .map_err(|e| format!("predict_batch: {e}"))?;
        if result.outputs.len() != chunk.len() {
            return Err(format!(
                "specialist returned {} outputs for {} inputs",
                result.outputs.len(),
                chunk.len()
            ));
        }
        for (text, output) in chunk.iter().zip(result.outputs.iter()) {
            let vec = crate::embeddings::parse_embedding_value(output)?;
            let h = crate::embeddings::text_hash(&spec.name, text);
            let _ = crate::embeddings::cache_store(&h, &spec.name, &model, &vec);
            pairs.push((text.clone(), vec));
        }
    }

    write_lance_text_dataset(pairs, lance_path)
}

fn refresh_kg_lance_nodes(
    graph_id: &str,
    node_kind: &str,
    specialist: &str,
    lance_path: &str,
) -> Result<LanceTextRefresh, String> {
    let spec = crate::embeddings::resolve_specialist(specialist)?;
    let model = crate::embeddings::spec_model(&spec);
    let nodes = kg_node_labels(graph_id, node_kind)?;
    if nodes.is_empty() {
        return Err(format!(
            "KG graph {graph_id}, kind {node_kind} has no nodes to index"
        ));
    }

    let cache_map = crate::embeddings::bulk_cache_lookup(&spec.name);
    let mut pairs: Vec<(i64, String, Vec<f32>)> = Vec::with_capacity(nodes.len());
    let mut to_embed: Vec<(i64, String)> = Vec::new();
    for (node_id, label) in nodes {
        let h = crate::embeddings::text_hash(&spec.name, &label);
        match cache_map.get(&h) {
            Some(v) => pairs.push((node_id, label, v.clone())),
            None => to_embed.push((node_id, label)),
        }
    }

    let batch_size = spec.batch_size.max(1);
    for chunk in to_embed.chunks(batch_size) {
        let inputs: Vec<Value> = chunk
            .iter()
            .map(|(_, text)| json!({"text": text}))
            .collect();
        let result = crate::specialists::predict_batch(&spec, &inputs)
            .map_err(|e| format!("predict_batch: {e}"))?;
        if result.outputs.len() != chunk.len() {
            return Err(format!(
                "specialist returned {} outputs for {} inputs",
                result.outputs.len(),
                chunk.len()
            ));
        }
        for ((node_id, text), output) in chunk.iter().zip(result.outputs.iter()) {
            let vec = crate::embeddings::parse_embedding_value(output)?;
            let h = crate::embeddings::text_hash(&spec.name, text);
            let _ = crate::embeddings::cache_store(&h, &spec.name, &model, &vec);
            pairs.push((*node_id, text.clone(), vec));
        }
    }

    write_lance_text_dataset_with_ids(pairs, lance_path)
}

fn distinct_text_values(rel_oid: u32, col: &str) -> Result<Vec<String>, String> {
    let qualified: String =
        Spi::get_one::<String>(&format!("SELECT {rel_oid}::oid::regclass::text"))
            .map_err(|e| format!("resolve relation: {e}"))?
            .ok_or_else(|| format!("relation oid {rel_oid} does not exist"))?;
    let col_ident = quote_ident(col);
    let sql =
        format!("SELECT DISTINCT {col_ident}::text FROM {qualified} WHERE {col_ident} IS NOT NULL");
    let mut out = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            if let Some(value) = row.get::<String>(1)? {
                out.push(value);
            }
        }
        Ok(())
    })
    .map_err(|e| format!("distinct text scan: {e}"))?;
    Ok(out)
}

fn kg_node_labels(graph_id: &str, node_kind: &str) -> Result<Vec<(i64, String)>, String> {
    let graph_lit = sql_literal(graph_id);
    let kind_lit = sql_literal(node_kind);
    let sql = format!(
        "SELECT node_id::bigint, label::text \
         FROM rvbbit.kg_nodes \
         WHERE graph_id = {graph_lit} \
           AND kind = {kind_lit} \
         ORDER BY node_id"
    );
    let mut out = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let node_id: Option<i64> = row.get(1)?;
            let label: Option<String> = row.get(2)?;
            if let (Some(node_id), Some(label)) = (node_id, label) {
                out.push((node_id, label));
            }
        }
        Ok(())
    })
    .map_err(|e| format!("KG node scan: {e}"))?;
    Ok(out)
}

fn write_lance_text_dataset(
    pairs: Vec<(String, Vec<f32>)>,
    lance_path: &str,
) -> Result<LanceTextRefresh, String> {
    let pairs_with_ids = pairs
        .into_iter()
        .enumerate()
        .map(|(idx, (text, embedding))| (idx as i64, text, embedding))
        .collect();
    write_lance_text_dataset_with_ids(pairs_with_ids, lance_path)
}

fn write_lance_text_dataset_with_ids(
    pairs: Vec<(i64, String, Vec<f32>)>,
    lance_path: &str,
) -> Result<LanceTextRefresh, String> {
    let n_values = pairs.len() as i64;
    let dim = pairs
        .first()
        .map(|(_, _, v)| v.len())
        .filter(|d| *d > 0)
        .ok_or_else(|| "no embeddings to write".to_string())?;
    let mut values = Vec::with_capacity(pairs.len() * dim);
    let mut texts = Vec::with_capacity(pairs.len());
    let mut ids = Vec::with_capacity(pairs.len());
    for (id, text, embedding) in pairs {
        if embedding.len() != dim {
            return Err(format!(
                "embedding dimension changed for value {:?}: got {}, expected {dim}",
                text,
                embedding.len()
            ));
        }
        ids.push(id);
        texts.push(text);
        values.extend_from_slice(&embedding);
    }

    let id_array = Int64Array::from(ids);
    let value_array = StringArray::from(texts);
    let values_array = Float32Array::from(values);
    let item_field = Arc::new(Field::new("item", DataType::Float32, false));
    let embedding_array =
        FixedSizeListArray::try_new(item_field.clone(), dim as i32, Arc::new(values_array), None)
            .map_err(|e| format!("FixedSizeList: {e}"))?;

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, false),
        Field::new("value", DataType::Utf8, false),
        Field::new(
            "embedding",
            DataType::FixedSizeList(item_field, dim as i32),
            false,
        ),
    ]));
    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![
            Arc::new(id_array),
            Arc::new(value_array),
            Arc::new(embedding_array),
        ],
    )
    .map_err(|e| format!("RecordBatch: {e}"))?;
    let batches = RecordBatchIterator::new(vec![Ok(batch)], schema);
    let lance_path = lance_path.to_string();
    ensure_lance_parent(&lance_path)?;
    with_lance_runtime(|rt| {
        rt.block_on(async {
            let mut params = WriteParams::default();
            params.mode = lance::dataset::WriteMode::Overwrite;
            Dataset::write(batches, &lance_path, Some(params))
                .await
                .map_err(|e| format!("Dataset::write: {e}"))?;
            Ok::<(), String>(())
        })
    })?;

    Ok(LanceTextRefresh {
        n_values,
        dim: dim as i32,
    })
}

fn knn_text_dataset(path: &str, query: &[f32], k: usize) -> Result<Vec<(String, f64)>, String> {
    knn_text_dataset_with_ids(path, query, k).map(|rows| {
        rows.into_iter()
            .map(|(_, value, score)| (value, score))
            .collect()
    })
}

fn knn_text_dataset_with_ids(
    path: &str,
    query: &[f32],
    k: usize,
) -> Result<Vec<(i64, String, f64)>, String> {
    if query.is_empty() {
        return Err("query embedding is empty".to_string());
    }
    let path = path.to_string();
    let query = query.to_vec();
    with_lance_runtime(|rt| {
        rt.block_on(async {
            let dataset = Dataset::open(&path)
                .await
                .map_err(|e| format!("Dataset::open({path}): {e}"))?;
            let q = Float32Array::from(query);
            let mut scanner = dataset.scan();
            scanner
                .nearest("embedding", &q, k.max(1))
                .map_err(|e| format!("scanner.nearest: {e}"))?
                .distance_metric(MetricType::Cosine);
            let batch = scanner
                .try_into_batch()
                .await
                .map_err(|e| format!("scanner.try_into_batch: {e}"))?;
            lance_text_rows_with_ids_from_batch(&batch)
        })
    })
}

fn lance_text_rows_with_ids_from_batch(
    batch: &RecordBatch,
) -> Result<Vec<(i64, String, f64)>, String> {
    let schema = batch.schema();
    let id_idx = schema
        .index_of("id")
        .map_err(|e| format!("missing id column: {e}"))?;
    let value_idx = schema
        .index_of("value")
        .map_err(|e| format!("missing value column: {e}"))?;
    let distance_idx = schema
        .index_of("_distance")
        .map_err(|e| format!("missing _distance column: {e}"))?;
    let values = batch
        .column(value_idx)
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| "value column is not Utf8".to_string())?;
    let ids = batch
        .column(id_idx)
        .as_any()
        .downcast_ref::<Int64Array>()
        .ok_or_else(|| "id column is not Int64".to_string())?;
    let distances = batch.column(distance_idx);
    let mut out = Vec::with_capacity(batch.num_rows());
    for row_idx in 0..batch.num_rows() {
        let distance = if let Some(a) = distances.as_any().downcast_ref::<Float32Array>() {
            a.value(row_idx) as f64
        } else if let Some(a) = distances
            .as_any()
            .downcast_ref::<arrow::array::Float64Array>()
        {
            a.value(row_idx)
        } else {
            return Err(format!(
                "_distance column has unsupported type {:?}",
                distances.data_type()
            ));
        };
        out.push((
            ids.value(row_idx),
            values.value(row_idx).to_string(),
            1.0 - distance,
        ));
    }
    out.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));
    Ok(out)
}

fn upsert_lance_text_index(
    rel_oid: u32,
    col: &str,
    specialist: &str,
    lance_url: &str,
    dim: i32,
    n_values: i64,
) -> Result<(), String> {
    let col_lit = sql_literal(col);
    let spec_lit = sql_literal(specialist);
    let url_lit = sql_literal(lance_url);
    Spi::run(&format!(
        "INSERT INTO rvbbit.lance_text_indexes \
             (table_oid, column_name, specialist, lance_url, dim, n_values, status, status_message, refreshed_at) \
         VALUES ({rel_oid}::oid, {col_lit}, {spec_lit}, {url_lit}, {dim}, {n_values}, 'ready', NULL, clock_timestamp()) \
         ON CONFLICT (table_oid, column_name, specialist) DO UPDATE SET \
             lance_url = EXCLUDED.lance_url, \
             dim = EXCLUDED.dim, \
             n_values = EXCLUDED.n_values, \
             status = 'ready', \
             status_message = NULL, \
             refreshed_at = clock_timestamp()"
    ))
    .map_err(|e| e.to_string())
}

fn kg_lance_resolve_nodes_impl(
    node_kind: &str,
    node_label: &str,
    specialist: &str,
    match_threshold: f64,
    graph: &str,
    limit_count: i32,
) -> Result<Value, String> {
    if node_label.trim().is_empty() || match_threshold <= 0.0 {
        return Ok(Value::Array(Vec::new()));
    }
    let norm_graph = kg_normalize_graph_value(graph)?;
    let norm_kind = kg_normalize_kind_value(node_kind)?;
    let spec = crate::embeddings::resolve_specialist(specialist)?;
    let Some(path) = kg_lance_catalog_path(&norm_graph, &norm_kind, &spec.name)? else {
        return Ok(Value::Array(Vec::new()));
    };

    // mode="" preserves prior behavior for the KG node-label search (this path's
    // stored embeddings are bare); it can adopt "query" alongside a doc-side bump.
    let query = crate::embeddings::embed_one(node_label, &spec.name, "")?;
    let candidates = knn_text_dataset_with_ids(&path, &query, limit_count.max(1) as usize)?;
    if candidates.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }

    let ids: Vec<i64> = candidates.iter().map(|(node_id, _, _)| *node_id).collect();
    let mut score_by_id = std::collections::HashMap::with_capacity(candidates.len());
    for (node_id, _, score) in candidates {
        if score >= match_threshold {
            score_by_id.insert(node_id, score);
        }
    }
    if score_by_id.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }

    let id_list = ids
        .into_iter()
        .filter(|id| score_by_id.contains_key(id))
        .map(|id| id.to_string())
        .collect::<Vec<_>>()
        .join(",");
    if id_list.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let graph_lit = sql_literal(&norm_graph);
    let kind_lit = sql_literal(&norm_kind);
    let sql = format!(
        "SELECT node_id::bigint, kind::text, label::text \
         FROM rvbbit.kg_nodes \
         WHERE graph_id = {graph_lit} \
           AND kind = {kind_lit} \
           AND node_id = ANY(ARRAY[{id_list}]::bigint[])"
    );

    let mut rows = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let node_id: Option<i64> = row.get(1)?;
            let kind: Option<String> = row.get(2)?;
            let label: Option<String> = row.get(3)?;
            if let (Some(node_id), Some(kind), Some(label)) = (node_id, kind, label) {
                if let Some(score) = score_by_id.get(&node_id) {
                    rows.push(json!({
                        "node_id": node_id,
                        "kind": kind,
                        "label": label,
                        "score": score,
                        "match_method": "lance"
                    }));
                }
            }
        }
        Ok(())
    })
    .map_err(|e| format!("KG node lookup: {e}"))?;

    rows.sort_by(|a, b| {
        let sa = a.get("score").and_then(Value::as_f64).unwrap_or(0.0);
        let sb = b.get("score").and_then(Value::as_f64).unwrap_or(0.0);
        sb.partial_cmp(&sa)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                let ia = a.get("node_id").and_then(Value::as_i64).unwrap_or(i64::MAX);
                let ib = b.get("node_id").and_then(Value::as_i64).unwrap_or(i64::MAX);
                ia.cmp(&ib)
            })
    });
    Ok(Value::Array(rows))
}

fn kg_lance_catalog_path(
    graph_id: &str,
    node_kind: &str,
    specialist: &str,
) -> Result<Option<String>, String> {
    let graph_lit = sql_literal(graph_id);
    let kind_lit = sql_literal(node_kind);
    let spec_lit = sql_literal(specialist);
    Spi::get_one::<String>(&format!(
        "SELECT lance_url \
         FROM rvbbit.kg_lance_indexes \
         WHERE graph_id = {graph_lit} \
           AND kind = {kind_lit} \
           AND target = 'nodes' \
           AND specialist = {spec_lit} \
           AND status = 'ready' \
         LIMIT 1"
    ))
    .map_err(|e| format!("KG Lance catalog lookup: {e}"))
}

fn upsert_kg_lance_index(
    graph_id: &str,
    node_kind: &str,
    specialist: &str,
    lance_url: &str,
    dim: i32,
    n_values: i64,
    status: &str,
    status_message: Option<&str>,
) -> Result<(), String> {
    let graph_lit = sql_literal(graph_id);
    let kind_lit = sql_literal(node_kind);
    let spec_lit = sql_literal(specialist);
    let url_lit = sql_literal(lance_url);
    let status_lit = sql_literal(status);
    let msg_sql = status_message
        .map(sql_literal)
        .unwrap_or_else(|| "NULL".to_string());
    Spi::run(&format!(
        "INSERT INTO rvbbit.kg_lance_indexes \
             (graph_id, kind, target, specialist, lance_url, dim, n_values, status, status_message, refreshed_at) \
         VALUES ({graph_lit}, {kind_lit}, 'nodes', {spec_lit}, {url_lit}, {dim}, {n_values}, {status_lit}, {msg_sql}, clock_timestamp()) \
         ON CONFLICT (graph_id, kind, target, specialist) DO UPDATE SET \
             lance_url = EXCLUDED.lance_url, \
             dim = EXCLUDED.dim, \
             n_values = EXCLUDED.n_values, \
             status = EXCLUDED.status, \
             status_message = EXCLUDED.status_message, \
             refreshed_at = clock_timestamp()"
    ))
    .map_err(|e| e.to_string())
}

fn kg_normalize_graph_value(graph: &str) -> Result<String, String> {
    Spi::get_one::<String>(&format!(
        "SELECT rvbbit.kg_normalize_graph({})",
        sql_literal(graph)
    ))
    .map_err(|e| format!("normalize graph: {e}"))?
    .ok_or_else(|| "normalize graph returned NULL".to_string())
}

fn kg_normalize_kind_value(node_kind: &str) -> Result<String, String> {
    if node_kind.trim().is_empty() {
        return Err("node_kind must be non-empty".to_string());
    }
    let kind = Spi::get_one::<String>(&format!(
        "SELECT rvbbit.kg_normalize_label({})",
        sql_literal(node_kind)
    ))
    .map_err(|e| format!("normalize kind: {e}"))?
    .ok_or_else(|| "normalize kind returned NULL".to_string())?;
    if kind.is_empty() {
        return Err("node_kind must be non-empty".to_string());
    }
    Ok(kind)
}

fn kg_lance_path(
    graph_id: &str,
    node_kind: &str,
    specialist: &str,
    requested: &str,
) -> Result<String, String> {
    let trimmed = requested.trim();
    if !trimmed.is_empty() {
        return Ok(trimmed.to_string());
    }
    let data_dir = Spi::get_one::<String>("SELECT current_setting('data_directory')")
        .map_err(|e| format!("resolve data_directory: {e}"))?
        .ok_or_else(|| "data_directory is not available".to_string())?;
    let path = PathBuf::from(data_dir)
        .join("rvbbit")
        .join("kg_lance")
        .join(path_segment(graph_id))
        .join(path_segment(node_kind))
        .join(path_segment(specialist))
        .join("nodes.lance");
    Ok(path.to_string_lossy().to_string())
}

fn ensure_lance_parent(lance_path: &str) -> Result<(), String> {
    if lance_path.starts_with("s3://") || lance_path.starts_with("gs://") {
        return Ok(());
    }
    if let Some(parent) = Path::new(lance_path).parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create Lance parent {}: {e}", parent.display()))?;
    }
    Ok(())
}

fn path_segment(value: &str) -> String {
    let mut out = String::with_capacity(value.len().max(1));
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    if out.is_empty() {
        "default".to_string()
    } else {
        out
    }
}

fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn sql_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
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
    let select_sql = format!("SELECT {pk_col}::bigint, {vec_col}::real[] FROM {qualified}");

    // Read rows via SPI into Rust Vecs. SPI is sync, so we materialize
    // before entering the tokio runtime block.
    let mut pks: Vec<i64> = Vec::new();
    let mut values: Vec<f32> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&select_sql, None, &[])?;
        for row in table {
            let pk: i64 = row.get::<i64>(1)?.unwrap_or(0);
            let vec_row: Vec<Option<f32>> = row.get::<Vec<Option<f32>>>(2)?.unwrap_or_default();
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
    let embedding_array =
        match FixedSizeListArray::try_new(item_field.clone(), dim, Arc::new(values_array), None) {
            Ok(a) => a,
            Err(e) => pgrx::error!("rvbbit.lance_import_column: FixedSizeList: {e}"),
        };

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, false),
        Field::new("embedding", DataType::FixedSizeList(item_field, dim), false),
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

/// Core import logic factored out of the SQL wrapper so compact() can call
/// it directly when auto-refresh is enabled for the table. Reads via SPI
/// (works on cold-tier through the custom_scan fall-through), writes a
/// fresh Lance dataset at `lance_path`. Returns the row count written.
pub(crate) fn refresh_lance_dataset(
    reloid: u32,
    pk_col: &str,
    vec_col: &str,
    dim: i32,
    lance_path: &str,
) -> Result<i64, String> {
    let dim_usize = dim.max(1) as usize;
    let qualified: String = Spi::get_one::<String>(&format!(
        "SELECT n.nspname::text || '.' || c.relname::text \
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
         WHERE c.oid = {reloid}::oid"
    ))
    .map_err(|e| format!("resolve oid: {e}"))?
    .ok_or_else(|| format!("oid {reloid} does not exist"))?;

    let select_sql = format!("SELECT {pk_col}::bigint, {vec_col}::real[] FROM {qualified}");

    let mut pks: Vec<i64> = Vec::new();
    let mut values: Vec<f32> = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&select_sql, None, &[])?;
        for row in table {
            let pk: i64 = row.get::<i64>(1)?.unwrap_or(0);
            let vec_row: Vec<Option<f32>> = row.get::<Vec<Option<f32>>>(2)?.unwrap_or_default();
            if vec_row.len() != dim_usize {
                return Err(pgrx::spi::Error::CursorNotFound(format!(
                    "row pk={pk} has {} dims, expected {dim_usize}",
                    vec_row.len()
                )));
            }
            pks.push(pk);
            for v in vec_row {
                values.push(v.unwrap_or(0.0));
            }
        }
        Ok(())
    })
    .map_err(|e| format!("SPI: {e}"))?;

    let n_rows = pks.len() as i64;
    if n_rows == 0 {
        // Empty source: nothing to write. Caller can decide whether that's
        // an error or expected (e.g. compact() of an empty table).
        return Ok(0);
    }

    let pk_array = Int64Array::from(pks);
    let values_array = Float32Array::from(values);
    let item_field = Arc::new(Field::new("item", DataType::Float32, false));
    let embedding_array =
        FixedSizeListArray::try_new(item_field.clone(), dim, Arc::new(values_array), None)
            .map_err(|e| format!("FixedSizeList: {e}"))?;

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, false),
        Field::new("embedding", DataType::FixedSizeList(item_field, dim), false),
    ]));
    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![Arc::new(pk_array), Arc::new(embedding_array)],
    )
    .map_err(|e| format!("RecordBatch: {e}"))?;
    let batches = RecordBatchIterator::new(vec![Ok(batch)], schema);

    let lance_path = lance_path.to_string();
    with_lance_runtime(|rt| {
        rt.block_on(async {
            let mut params = WriteParams::default();
            params.mode = lance::dataset::WriteMode::Overwrite;
            Dataset::write(batches, &lance_path, Some(params))
                .await
                .map_err(|e| format!("Dataset::write: {e}"))?;
            Ok::<(), String>(())
        })
    })?;

    Ok(n_rows)
}

/// rvbbit.lance_enable(reloid, vec_col, dim, lance_url) — opt a table
/// in to automatic Lance refresh. Sets the catalog flags AND does an
/// initial import so the Lance dataset reflects the current table
/// state. After this, every compact() call rebuilds the Lance dataset
/// to match the latest table contents.
///
/// To disable, pass NULL for lance_url (caller can use the catalog
/// directly: UPDATE rvbbit.tables SET lance_url = NULL).
#[pg_extern]
fn lance_enable(reloid: pg_sys::Oid, vec_col: &str, dim: i32, lance_url: &str) -> i64 {
    let rel_oid = reloid.to_u32();
    let vec_col_safe = vec_col.replace('\'', "''");
    let lance_url_safe = lance_url.replace('\'', "''");

    // Set the catalog flags BEFORE the initial write so any race with a
    // concurrent compact() picks them up.
    Spi::run(&format!(
        "UPDATE rvbbit.tables \
            SET lance_url = '{lance_url_safe}', \
                lance_vector_column = '{vec_col_safe}', \
                lance_dim = {dim} \
          WHERE table_oid = {rel_oid}::oid"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_enable: catalog update: {e}"));

    // Initial population. Uses 'id' as the implicit PK — convention that
    // makes rvbbit.knn() simpler, matches what rvbbit.compact's LLM-events
    // schema uses, and avoids exposing a fifth function parameter.
    refresh_lance_dataset(rel_oid, "id", vec_col, dim, lance_url)
        .unwrap_or_else(|e| pgrx::error!("rvbbit.lance_enable: initial refresh: {e}"))
}

/// rvbbit.knn(reloid, query, k) — catalog-driven KNN. Looks up
/// lance_url + lance_vector_column from rvbbit.tables and runs
/// vector search against the table's Lance dataset. No operator
/// has to remember the URL or path.
///
/// Returns JSON array of {id, _distance} ordered nearest first.
#[pg_extern]
fn knn(reloid: pg_sys::Oid, query: Vec<f32>, k: i32) -> JsonB {
    let rel_oid = reloid.to_u32();
    let lance_url: String = match Spi::get_one::<String>(&format!(
        "SELECT lance_url FROM rvbbit.tables \
         WHERE table_oid = {rel_oid}::oid AND lance_url IS NOT NULL"
    )) {
        Ok(Some(u)) => u,
        Ok(None) => pgrx::error!(
            "rvbbit.knn: table oid {rel_oid} has no Lance dataset; \
             call rvbbit.lance_enable() first"
        ),
        Err(e) => pgrx::error!("rvbbit.knn: catalog lookup: {e}"),
    };
    lance_knn(&lance_url, query, k)
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
fn lance_build_index(path: &str, column: &str, num_partitions: i32, num_sub_vectors: i32) -> i64 {
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
            let params =
                VectorIndexParams::ivf_pq(num_partitions, 8, num_sub_vectors, MetricType::L2, 50);

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
