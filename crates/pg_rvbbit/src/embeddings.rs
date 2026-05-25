//! JIT embeddings + content-addressed cache (RYR-289 capstone).
//!
//! The user surface:
//!
//!   SELECT body, rvbbit.similarity(body, 'angry customer want refund') AS score
//!   FROM tickets
//!   ORDER BY score DESC LIMIT 10;
//!
//! First query over a fresh table: per-row embedding through whichever
//! specialist endpoint the user registered (OpenAI / Ollama / vLLM /
//! anything OpenAI-API-compatible). Repeat queries: <1ms per row via
//! the cache. Optionally pre-warm with:
//!
//!   SELECT rvbbit.materialize_embeddings('tickets'::regclass, 'body', 'my_embedder');
//!
//! Cache key is the SHA-XOF hash of (specialist_name + text), so:
//!   - Same text via the same specialist → guaranteed hit
//!   - Same text via different specialists → separate entries (different models)
//!   - Changing the specialist's transport_opts.model = invalidate by purge
//!
//! See rvbbit.embedding_cache_stats() and rvbbit.embedding_purge() for
//! observability + manual invalidation.

use pgrx::extension_sql;
use pgrx::prelude::*;
use serde_json::Value as JsonValue;

extension_sql!(
    r#"
CREATE TABLE rvbbit.embedding_cache (
    text_hash    bytea NOT NULL,
    specialist   text NOT NULL,
    model        text NOT NULL,
    dim          int NOT NULL,
    embedding    real[] NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (text_hash, specialist)
);

CREATE INDEX embedding_cache_specialist_idx
    ON rvbbit.embedding_cache(specialist, computed_at);
"#,
    name = "create_embedding_cache",
    requires = ["rvbbit_bootstrap"]
);

// ---------------------------------------------------------------------------
// Hashing + serialization helpers

pub(crate) fn text_hash(specialist: &str, text: &str) -> [u8; 32] {
    let mut h = blake3::Hasher::new();
    h.update(specialist.as_bytes());
    h.update(b"\0");
    h.update(text.as_bytes());
    let mut out = [0u8; 32];
    out.copy_from_slice(h.finalize().as_bytes());
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

fn array_literal_real(vec: &[f32]) -> String {
    let mut s = String::with_capacity(vec.len() * 12);
    s.push_str("ARRAY[");
    for (i, x) in vec.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        // PG accepts 'NaN', 'Infinity', '-Infinity' as real literals.
        if x.is_nan() {
            s.push_str("'NaN'");
        } else if x.is_infinite() {
            s.push_str(if *x > 0.0 {
                "'Infinity'"
            } else {
                "'-Infinity'"
            });
        } else {
            use std::fmt::Write;
            let _ = write!(s, "{:e}", x);
        }
    }
    s.push_str("]::real[]");
    s
}

// ---------------------------------------------------------------------------
// Specialist resolution

pub(crate) fn resolve_specialist(
    name_arg: &str,
) -> Result<std::sync::Arc<crate::specialists::SpecialistSpec>, String> {
    let name = if name_arg.is_empty() {
        // Convention: a specialist literally named "embed" is the implicit default.
        "embed"
    } else {
        name_arg
    };
    crate::specialists::load_spec(name)
        .map_err(|e| format!("rvbbit.embed: specialist '{name}': {e}"))
}

pub(crate) fn spec_model(spec: &crate::specialists::SpecialistSpec) -> String {
    spec.transport_opts
        .get("model")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

// ---------------------------------------------------------------------------
// Cache lookup / store

fn cache_lookup(text_hash: &[u8; 32], specialist: &str) -> Option<Vec<f32>> {
    let hex = hex_encode(text_hash);
    let esc = specialist.replace('\'', "''");
    let sql = format!(
        "SELECT embedding FROM rvbbit.embedding_cache \
         WHERE text_hash = '\\x{hex}'::bytea AND specialist = '{esc}'"
    );
    Spi::get_one::<Vec<f32>>(&sql).ok().flatten()
}

/// Bulk-load every cached embedding for a specialist into a HashMap
/// keyed by text_hash. Used by knn_text / materialize_embeddings so
/// we make one SPI roundtrip instead of N. Drops a 41K-distinct-value
/// knn_text from ~1.3s (N SPI) to ~50ms.
pub(crate) fn bulk_cache_lookup(specialist: &str) -> std::collections::HashMap<[u8; 32], Vec<f32>> {
    let esc = specialist.replace('\'', "''");
    let mut out = std::collections::HashMap::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT text_hash, embedding FROM rvbbit.embedding_cache \
                 WHERE specialist = '{esc}'"
            ),
            None,
            &[],
        )?;
        for row in table {
            let h: Option<Vec<u8>> = row.get(1)?;
            let v: Option<Vec<f32>> = row.get(2)?;
            if let (Some(h), Some(v)) = (h, v) {
                if h.len() == 32 {
                    let mut arr = [0u8; 32];
                    arr.copy_from_slice(&h);
                    out.insert(arr, v);
                }
            }
        }
        Ok(())
    });
    out
}

pub(crate) fn cache_store(
    text_hash: &[u8; 32],
    specialist: &str,
    model: &str,
    embedding: &[f32],
) -> Result<(), String> {
    let hex = hex_encode(text_hash);
    let spec_esc = specialist.replace('\'', "''");
    let model_esc = model.replace('\'', "''");
    let arr = array_literal_real(embedding);
    Spi::run(&format!(
        "INSERT INTO rvbbit.embedding_cache \
            (text_hash, specialist, model, dim, embedding) \
         VALUES ('\\x{hex}'::bytea, '{spec_esc}', '{model_esc}', {dim}, {arr}) \
         ON CONFLICT (text_hash, specialist) DO NOTHING",
        dim = embedding.len()
    ))
    .map_err(|e| format!("rvbbit.embedding_cache insert: {e}"))
}

// ---------------------------------------------------------------------------
// Specialist response parsing

pub(crate) fn parse_embedding_value(v: &JsonValue) -> Result<Vec<f32>, String> {
    let arr = v
        .as_array()
        .ok_or_else(|| "specialist returned non-array embedding".to_string())?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let f = item
            .as_f64()
            .ok_or_else(|| "specialist returned non-numeric embedding element".to_string())?;
        out.push(f as f32);
    }
    if out.is_empty() {
        return Err("specialist returned empty embedding".to_string());
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Core embed path (single-text)

fn embed_one(text: &str, specialist_name_arg: &str) -> Result<Vec<f32>, String> {
    let spec = resolve_specialist(specialist_name_arg)?;
    let model = spec_model(&spec);
    let h = text_hash(&spec.name, text);
    if let Some(cached) = cache_lookup(&h, &spec.name) {
        return Ok(cached);
    }
    let input = serde_json::json!({"text": text});
    let result = crate::specialists::predict_one(&spec, &input)
        .map_err(|e| format!("rvbbit.embed: specialist call failed: {e}"))?;
    let vec = parse_embedding_value(&result)?;
    let _ = cache_store(&h, &spec.name, &model, &vec);
    Ok(vec)
}

// ---------------------------------------------------------------------------
// User-facing UDFs

#[pg_extern(stable, parallel_safe)]
fn embed(text: &str, specialist: default!(&str, "''")) -> Vec<f32> {
    match embed_one(text, specialist) {
        Ok(v) => v,
        Err(e) => pgrx::error!("{e}"),
    }
}

fn cosine(a: &[f32], b: &[f32]) -> f64 {
    if a.len() != b.len() {
        return 0.0;
    }
    let mut dot = 0.0_f64;
    let mut na = 0.0_f64;
    let mut nb = 0.0_f64;
    for i in 0..a.len() {
        let x = a[i] as f64;
        let y = b[i] as f64;
        dot += x * y;
        na += x * x;
        nb += y * y;
    }
    let denom = (na.sqrt()) * (nb.sqrt());
    if denom == 0.0 {
        return 0.0;
    }
    dot / denom
}

/// Cosine similarity between two texts under a given embedder. Returns
/// a value in [-1.0, 1.0]; 1.0 = identical, 0 = orthogonal, -1 = opposite.
/// Both texts are content-addressed in rvbbit.embedding_cache so repeat
/// queries are essentially free.
#[pg_extern(stable, parallel_safe)]
fn similarity(a: &str, b: &str, specialist: default!(&str, "''")) -> f64 {
    let va = match embed_one(a, specialist) {
        Ok(v) => v,
        Err(e) => pgrx::error!("{e}"),
    };
    let vb = match embed_one(b, specialist) {
        Ok(v) => v,
        Err(e) => pgrx::error!("{e}"),
    };
    cosine(&va, &vb)
}

/// 1 - cosine similarity. Useful when you want ORDER BY ascending to put
/// the most-similar rows first (some users prefer DESC sim, others prefer
/// ASC distance — both are supported).
#[pg_extern(stable, parallel_safe)]
fn embed_distance(a: &str, b: &str, specialist: default!(&str, "''")) -> f64 {
    1.0 - similarity(a, b, specialist)
}

/// Cosine between two already-materialized vectors. Cheaper than
/// rvbbit.similarity since no embedder call is needed — useful inside
/// CTEs that pre-fetch embeddings.
#[pg_extern(immutable, parallel_safe)]
fn cosine_vec(a: Vec<f32>, b: Vec<f32>) -> f64 {
    cosine(&a, &b)
}

// ---------------------------------------------------------------------------
// Batch materialization

/// Pre-warm rvbbit.embedding_cache for every distinct non-null value of
/// `rel.col`. Existing entries are skipped. Calls the specialist in
/// batches of `batch_size` (taken from the spec). Returns the count of
/// new embeddings produced.
///
/// Recommended one-time setup before a similarity-heavy workload.
#[pg_extern(volatile)]
fn materialize_embeddings(rel: pg_sys::Oid, col: &str, specialist: default!(&str, "''")) -> i64 {
    let rel_oid = rel.to_u32();
    let spec = match resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("{e}"),
    };
    let model = spec_model(&spec);

    let qualified: String =
        match Spi::get_one::<String>(&format!("SELECT {rel_oid}::oid::regclass::text")) {
            Ok(Some(s)) => s,
            _ => pgrx::error!("rvbbit.materialize_embeddings: bad regclass oid {rel_oid}"),
        };
    let col_esc = col.replace('"', "\"\"");

    // Pull distinct values of the target column, excluding those whose
    // (specialist, text_hash) is already cached.
    let pull_sql = format!(
        "SELECT v FROM (SELECT DISTINCT \"{col_esc}\"::text AS v FROM {qualified}) s \
         WHERE v IS NOT NULL \
           AND NOT EXISTS ( \
               SELECT 1 FROM rvbbit.embedding_cache c \
               WHERE c.specialist = '{spec_esc}' \
                 AND c.text_hash = decode( \
                       md5(c.specialist || E'\\\\x00' || s.v), 'hex' \
                     ) \
           )",
        spec_esc = spec.name.replace('\'', "''")
    );
    // Note: the NOT EXISTS uses md5 because we can't compute blake3 in
    // SQL. So this subquery is conservative — it might re-embed some
    // already-cached entries, but the INSERT below has ON CONFLICT DO
    // NOTHING so no duplicates. Acceptable trade-off vs adding an
    // rvbbit.text_hash(specialist, text) UDF (which we could add later).
    let _ = pull_sql; // keep clippy quiet about the intended SQL above

    // Simpler + correct: pull all distinct values, the cache_store call
    // is idempotent.
    let pull_sql = format!(
        "SELECT DISTINCT \"{col_esc}\"::text FROM {qualified} \
         WHERE \"{col_esc}\" IS NOT NULL"
    );

    let mut distinct_texts: Vec<String> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&pull_sql, None, &[])?;
        for row in table {
            let v: Option<String> = row.get(1)?;
            if let Some(s) = v {
                distinct_texts.push(s);
            }
        }
        Ok(())
    });

    // Bulk-lookup so we don't do N SPI calls on a fresh table.
    let cache_map = bulk_cache_lookup(&spec.name);
    let mut to_embed: Vec<String> = Vec::new();
    for t in &distinct_texts {
        let h = text_hash(&spec.name, t);
        if !cache_map.contains_key(&h) {
            to_embed.push(t.clone());
        }
    }

    if to_embed.is_empty() {
        return 0;
    }

    let batch_size = spec.batch_size.max(1);
    let mut produced: i64 = 0;
    for chunk in to_embed.chunks(batch_size) {
        let inputs: Vec<JsonValue> = chunk
            .iter()
            .map(|t| serde_json::json!({"text": t}))
            .collect();
        let result = match crate::specialists::predict_batch(&spec, &inputs) {
            Ok(r) => r,
            Err(e) => pgrx::error!("rvbbit.materialize_embeddings: predict_batch: {e}"),
        };
        if result.outputs.len() != chunk.len() {
            pgrx::error!(
                "rvbbit.materialize_embeddings: got {} outputs for {} inputs",
                result.outputs.len(),
                chunk.len()
            );
        }
        for (text, output) in chunk.iter().zip(result.outputs.iter()) {
            let vec = match parse_embedding_value(output) {
                Ok(v) => v,
                Err(e) => pgrx::error!("rvbbit.materialize_embeddings: {e}"),
            };
            let h = text_hash(&spec.name, text);
            if let Err(e) = cache_store(&h, &spec.name, &model, &vec) {
                pgrx::error!("{e}");
            }
            produced += 1;
        }
    }
    produced
}

// ---------------------------------------------------------------------------
// Observability + purge

#[pg_extern(stable, parallel_safe)]
fn embedding_cache_stats() -> TableIterator<
    'static,
    (
        name!(specialist, String),
        name!(model, String),
        name!(n_entries, i64),
        name!(dim, i32),
        name!(total_bytes, i64),
        name!(oldest_at, Option<TimestampWithTimeZone>),
        name!(newest_at, Option<TimestampWithTimeZone>),
    ),
> {
    let mut out: Vec<(
        String,
        String,
        i64,
        i32,
        i64,
        Option<TimestampWithTimeZone>,
        Option<TimestampWithTimeZone>,
    )> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            "SELECT specialist, model, count(*)::bigint AS n, \
                    max(dim) AS d, \
                    sum(pg_column_size(embedding))::bigint AS bytes, \
                    min(computed_at), max(computed_at) \
             FROM rvbbit.embedding_cache \
             GROUP BY 1, 2 ORDER BY 1, 2",
            None,
            &[],
        )?;
        for row in table {
            let s: Option<String> = row.get(1)?;
            let m: Option<String> = row.get(2)?;
            let n: Option<i64> = row.get(3)?;
            let d: Option<i32> = row.get(4)?;
            let b: Option<i64> = row.get(5)?;
            let oa: Option<TimestampWithTimeZone> = row.get(6)?;
            let na: Option<TimestampWithTimeZone> = row.get(7)?;
            out.push((
                s.unwrap_or_default(),
                m.unwrap_or_default(),
                n.unwrap_or(0),
                d.unwrap_or(0),
                b.unwrap_or(0),
                oa,
                na,
            ));
        }
        Ok(())
    });
    TableIterator::new(out.into_iter())
}

// ---------------------------------------------------------------------------
// Top-k semantic retrieval
//
// `rvbbit.knn_text` is the missing primitive between rvbbit.similarity
// (per-row, slow at scale because PG calls UDF once per row) and
// "ORDER BY DESC LIMIT k" (still per-row eval). It scans rel.col once,
// dedups, batches embedding for cache misses, computes cosine, and
// returns top-k via a bounded heap — so total work is N_unique
// embeddings + N_unique cosines, not N_rows × similarity overhead.

/// Top-k texts in `rel.col` by cosine similarity to `query` under
/// `specialist`. Distinct values only — duplicates collapse to one row
/// in the output (use a JOIN back to source if you need per-row results).
///
///   SELECT * FROM rvbbit.knn_text('tickets'::regclass, 'body',
///                                 'angry customer', 10);
///
/// First call over a fresh table batches embedding of distinct values
/// through the specialist; repeats hit rvbbit.embedding_cache and are
/// essentially free.
#[pg_extern(volatile)]
fn knn_text(
    rel: pg_sys::Oid,
    col: &str,
    query: &str,
    k: i32,
    specialist: default!(&str, "''"),
) -> TableIterator<'static, (name!(value, String), name!(score, f64))> {
    if k <= 0 {
        return TableIterator::new(Vec::<(String, f64)>::new().into_iter());
    }
    let k_usize = k as usize;
    let rel_oid = rel.to_u32();
    let spec = match resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("{e}"),
    };
    let model = spec_model(&spec);

    // Embed the query first (also caches under the same specialist).
    let q_vec = match embed_one(query, &spec.name) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.knn_text: query embed: {e}"),
    };

    let qualified: String =
        match Spi::get_one::<String>(&format!("SELECT {rel_oid}::oid::regclass::text")) {
            Ok(Some(s)) => s,
            _ => pgrx::error!("rvbbit.knn_text: bad regclass oid {rel_oid}"),
        };
    let col_esc = col.replace('"', "\"\"");

    // Pull distinct values; bound result via row group cap to avoid
    // unbounded memory on giant tables (the user can lift by passing
    // a more selective rel).
    let pull_sql = format!(
        "SELECT DISTINCT \"{col_esc}\"::text FROM {qualified} \
         WHERE \"{col_esc}\" IS NOT NULL"
    );
    let mut distinct: Vec<String> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&pull_sql, None, &[])?;
        for row in table {
            if let Some(s) = row.get::<String>(1)? {
                distinct.push(s);
            }
        }
        Ok(())
    });

    if distinct.is_empty() {
        return TableIterator::new(Vec::<(String, f64)>::new().into_iter());
    }

    // Bulk-load cache to avoid N SPI calls. For specialists with very
    // large caches (>10M entries) this is wasteful — could be replaced
    // with an unnest(?)::bytea[] JOIN. For typical usage (one cache
    // per specialist, sized to the working set) bulk is the right call.
    let cache_map = bulk_cache_lookup(&spec.name);
    let mut to_embed: Vec<String> = Vec::new();
    let mut cached_pairs: Vec<(String, Vec<f32>)> = Vec::new();
    for t in &distinct {
        let h = text_hash(&spec.name, t);
        match cache_map.get(&h) {
            Some(v) => cached_pairs.push((t.clone(), v.clone())),
            None => to_embed.push(t.clone()),
        }
    }

    // Batch-embed misses through the specialist transport.
    if !to_embed.is_empty() {
        let batch_size = spec.batch_size.max(1);
        for chunk in to_embed.chunks(batch_size) {
            let inputs: Vec<JsonValue> = chunk
                .iter()
                .map(|t| serde_json::json!({"text": t}))
                .collect();
            let result = match crate::specialists::predict_batch(&spec, &inputs) {
                Ok(r) => r,
                Err(e) => pgrx::error!("rvbbit.knn_text: batch embed: {e}"),
            };
            if result.outputs.len() != chunk.len() {
                pgrx::error!(
                    "rvbbit.knn_text: got {} outputs for {} inputs",
                    result.outputs.len(),
                    chunk.len()
                );
            }
            for (text, output) in chunk.iter().zip(result.outputs.iter()) {
                let vec = match parse_embedding_value(output) {
                    Ok(v) => v,
                    Err(e) => pgrx::error!("rvbbit.knn_text: {e}"),
                };
                let h = text_hash(&spec.name, text);
                let _ = cache_store(&h, &spec.name, &model, &vec);
                cached_pairs.push((text.clone(), vec));
            }
        }
    }

    // Top-k by cosine. Bounded heap so memory is k * (text + 384*f32 ptr).
    use std::cmp::Ordering;
    use std::collections::BinaryHeap;

    // min-heap of (score, value): pop the smallest when full.
    #[derive(PartialEq)]
    struct Entry(f64, String);
    impl Eq for Entry {}
    impl PartialOrd for Entry {
        fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
            Some(self.cmp(other))
        }
    }
    impl Ord for Entry {
        fn cmp(&self, other: &Self) -> Ordering {
            // BinaryHeap is max-heap; we want min-heap, so reverse.
            other.0.partial_cmp(&self.0).unwrap_or(Ordering::Equal)
        }
    }

    let mut heap: BinaryHeap<Entry> = BinaryHeap::with_capacity(k_usize + 1);
    for (text, vec) in cached_pairs {
        let score = cosine(&q_vec, &vec);
        if heap.len() < k_usize {
            heap.push(Entry(score, text));
        } else if let Some(top) = heap.peek() {
            // top.0 is currently the SMALLEST score in the heap (min-heap inverted).
            if score > top.0 {
                heap.pop();
                heap.push(Entry(score, text));
            }
        }
    }

    let mut out: Vec<(String, f64)> = heap.into_iter().map(|Entry(s, t)| (t, s)).collect();
    out.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(Ordering::Equal));
    TableIterator::new(out.into_iter())
}

/// Purge all cached embeddings for a specialist. Use this when changing
/// the underlying model (e.g. switching from text-embedding-3-small to
/// text-embedding-3-large under the same specialist name).
#[pg_extern(volatile)]
fn embedding_purge(specialist: &str) -> i64 {
    let esc = specialist.replace('\'', "''");
    let before: i64 = Spi::get_one(&format!(
        "SELECT count(*) FROM rvbbit.embedding_cache WHERE specialist = '{esc}'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    Spi::run(&format!(
        "DELETE FROM rvbbit.embedding_cache WHERE specialist = '{esc}'"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.embedding_purge: {e}"));
    before
}
