//! `rvbbit.topics(query_sql, k)` — SQL-native topic clustering via
//! k-means over cached embeddings.
//!
//!   SELECT cluster_id, count, exemplar
//!   FROM rvbbit.topics('SELECT body FROM tickets', 5);
//!
//!     cluster_id | count | exemplar
//!     -----------+-------+----------------------------------------
//!              0 |  4218 | 'shipping was late, package damaged'
//!              1 |  3127 | 'wrong item received, want exchange'
//!              2 |  2891 | 'app crashes when opening notifications'
//!              3 |  1402 | 'great service, will buy again'
//!              4 |   983 | 'billing question about my plan'
//!
//! Compose labeling via existing operators:
//!
//!   SELECT cluster_id, count, exemplar,
//!          rvbbit.about(exemplar, 'one-word topic label') AS label
//!   FROM rvbbit.topics(...);
//!
//! Determinism: a seeded RNG drives k-means++ init. Same input + same
//! seed = same output. Default seed is 0xAE; user can override.

use std::collections::HashMap;

use pgrx::prelude::*;

/// One distinct value seen in the query result. `count` lets the user
/// see "this exemplar represents N rows" without us inflating the
/// in-memory vector list to the row count.
struct Texted {
    text: String,
    count: i64,
    vec: Vec<f32>,
}

/// k-means over cached + JIT embeddings of `query_sql`'s output.
/// `query_sql` must be a SELECT returning exactly one text column.
/// `k` is hard-capped at 256 to keep the centroid update step bounded.
#[pg_extern(volatile)]
fn topics(
    query_sql: &str,
    k: i32,
    specialist: default!(&str, "''"),
    max_iter: default!(i32, 20),
    seed: default!(i64, 0xAE),
) -> TableIterator<
    'static,
    (
        name!(cluster_id, i32),
        name!(count, i64),
        name!(exemplar, String),
    ),
> {
    if k <= 0 || max_iter <= 0 {
        return TableIterator::new(Vec::<(i32, i64, String)>::new().into_iter());
    }
    let k = (k as usize).min(256);

    let spec = match crate::embeddings::resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("{e}"),
    };
    let model = crate::embeddings::spec_model(&spec);

    // 1. Collect distinct (text, count) from the user's query.
    let distinct = match collect_distinct_texts(query_sql) {
        Ok(d) => d,
        Err(e) => pgrx::error!("rvbbit.topics: query SPI: {e}"),
    };
    if distinct.is_empty() {
        return TableIterator::new(Vec::<(i32, i64, String)>::new().into_iter());
    }

    // 2. Bulk cache + embed misses (mirror knn_text path).
    let cache_map = crate::embeddings::bulk_cache_lookup(&spec.name);
    let mut to_embed: Vec<(String, i64)> = Vec::new();
    let mut texted: Vec<Texted> = Vec::with_capacity(distinct.len());
    for (text, count) in &distinct {
        let h = crate::embeddings::text_hash(&spec.name, text);
        match cache_map.get(&h) {
            Some(v) => texted.push(Texted {
                text: text.clone(),
                count: *count,
                vec: v.clone(),
            }),
            None => to_embed.push((text.clone(), *count)),
        }
    }

    if !to_embed.is_empty() {
        let batch_size = spec.batch_size.max(1);
        for chunk in to_embed.chunks(batch_size) {
            let inputs: Vec<serde_json::Value> = chunk
                .iter()
                .map(|(t, _)| serde_json::json!({"text": t}))
                .collect();
            let result = match crate::specialists::predict_batch(&spec, &inputs) {
                Ok(r) => r,
                Err(e) => pgrx::error!("rvbbit.topics: batch embed: {e}"),
            };
            if result.outputs.len() != chunk.len() {
                pgrx::error!(
                    "rvbbit.topics: got {} outputs for {} inputs",
                    result.outputs.len(),
                    chunk.len()
                );
            }
            for ((text, count), output) in chunk.iter().zip(result.outputs.iter()) {
                let v = match crate::embeddings::parse_embedding_value(output) {
                    Ok(v) => v,
                    Err(e) => pgrx::error!("rvbbit.topics: {e}"),
                };
                let h = crate::embeddings::text_hash(&spec.name, text);
                let _ = crate::embeddings::cache_store(&h, &spec.name, &model, &v);
                texted.push(Texted {
                    text: text.clone(),
                    count: *count,
                    vec: v,
                });
            }
        }
    }

    // Clamp k to the number of distinct rows.
    let k_eff = k.min(texted.len());
    if k_eff == 0 {
        return TableIterator::new(Vec::<(i32, i64, String)>::new().into_iter());
    }

    // 3. k-means++ init + Lloyd iterations.
    let assignments = run_kmeans(&texted, k_eff, max_iter as usize, seed as u64);

    // 4. Roll up: per cluster, sum counts + pick exemplar (closest to
    //    the cluster's mean centroid by cosine).
    let mut sums: Vec<(i64, Vec<f32>, usize)> = (0..k_eff)
        .map(|_| (0, vec![0.0; texted[0].vec.len()], 0))
        .collect();
    for (i, &c) in assignments.iter().enumerate() {
        sums[c].0 += texted[i].count;
        for (j, &x) in texted[i].vec.iter().enumerate() {
            sums[c].1[j] += x;
        }
        sums[c].2 += 1;
    }
    // Normalize mean centroid.
    let centroids: Vec<Vec<f32>> = sums
        .iter()
        .map(|(_, sum, n)| {
            if *n == 0 {
                sum.clone()
            } else {
                sum.iter().map(|x| x / *n as f32).collect()
            }
        })
        .collect();

    // For each cluster: find best-cosine exemplar.
    let mut best: Vec<Option<(f64, usize)>> = vec![None; k_eff];
    for (i, &c) in assignments.iter().enumerate() {
        let score = cosine_f32(&centroids[c], &texted[i].vec);
        match best[c] {
            None => best[c] = Some((score, i)),
            Some((s, _)) if score > s => best[c] = Some((score, i)),
            _ => {}
        }
    }

    let mut out: Vec<(i32, i64, String)> = Vec::with_capacity(k_eff);
    for (cid, slot) in best.iter().enumerate() {
        if let Some((_, idx)) = slot {
            out.push((cid as i32, sums[cid].0, texted[*idx].text.clone()));
        }
    }
    // Sort by count descending so the biggest topic comes first — that's
    // what users want to see at the top.
    out.sort_by(|a, b| b.1.cmp(&a.1));
    // Renumber cluster_id so output is 0..N stable.
    let renumbered: Vec<(i32, i64, String)> = out
        .into_iter()
        .enumerate()
        .map(|(i, (_, c, e))| (i as i32, c, e))
        .collect();

    TableIterator::new(renumbered.into_iter())
}

// ---------------------------------------------------------------------------
// SPI: distinct text harvest

fn collect_distinct_texts(query_sql: &str) -> Result<Vec<(String, i64)>, String> {
    // Wrap user query to count occurrences. Trust the user gave us a
    // single-column SELECT; PG will error clearly if not.
    let wrapped = format!(
        "SELECT t::text AS v, count(*)::bigint AS c \
         FROM ({query_sql}) AS u(t) \
         WHERE t IS NOT NULL \
         GROUP BY t"
    );
    let mut out = Vec::new();
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&wrapped, None, &[])?;
        for row in table {
            let v: Option<String> = row.get(1)?;
            let c: Option<i64> = row.get(2)?;
            if let (Some(v), Some(c)) = (v, c) {
                out.push((v, c));
            }
        }
        Ok(())
    })
    .map_err(|e| e.to_string())?;
    Ok(out)
}

// ---------------------------------------------------------------------------
// k-means core

fn run_kmeans(texted: &[Texted], k: usize, max_iter: usize, seed: u64) -> Vec<usize> {
    let n = texted.len();
    if n == 0 || k == 0 {
        return Vec::new();
    }
    if k >= n {
        // Trivial: every point its own cluster.
        return (0..n).collect();
    }

    let dim = texted[0].vec.len();
    let mut rng = SplitMix64::new(seed);

    // k-means++ init.
    let mut centroids: Vec<Vec<f32>> = Vec::with_capacity(k);
    centroids.push(texted[rng.next_usize(n)].vec.clone());
    let mut d2 = vec![f32::MAX; n];
    while centroids.len() < k {
        for (i, t) in texted.iter().enumerate() {
            let last = centroids.last().unwrap();
            let d = sq_dist(last, &t.vec);
            if d < d2[i] {
                d2[i] = d;
            }
        }
        let total: f64 = d2.iter().map(|&x| x as f64).sum();
        if total == 0.0 {
            // All remaining points coincide with chosen centroids; pad.
            centroids.push(texted[rng.next_usize(n)].vec.clone());
            continue;
        }
        let mut r = rng.next_f64() * total;
        let mut pick = 0usize;
        for (i, &x) in d2.iter().enumerate() {
            r -= x as f64;
            if r <= 0.0 {
                pick = i;
                break;
            }
        }
        centroids.push(texted[pick].vec.clone());
    }

    // Lloyd loop.
    let mut assign = vec![0usize; n];
    for _ in 0..max_iter {
        let mut changed = 0usize;
        for (i, t) in texted.iter().enumerate() {
            let mut best = 0usize;
            let mut best_d = f32::MAX;
            for (c, ctr) in centroids.iter().enumerate() {
                let d = sq_dist(ctr, &t.vec);
                if d < best_d {
                    best_d = d;
                    best = c;
                }
            }
            if assign[i] != best {
                assign[i] = best;
                changed += 1;
            }
        }
        if changed == 0 {
            break;
        }
        // Recompute centroids as means.
        let mut sums: Vec<(Vec<f32>, usize)> = (0..k).map(|_| (vec![0.0; dim], 0)).collect();
        for (i, &c) in assign.iter().enumerate() {
            sums[c].1 += 1;
            for (j, &x) in texted[i].vec.iter().enumerate() {
                sums[c].0[j] += x;
            }
        }
        for (c, (sum, n)) in sums.iter().enumerate() {
            if *n > 0 {
                centroids[c] = sum.iter().map(|x| x / *n as f32).collect();
            }
            // Empty cluster: keep its old centroid (rare on stub vectors).
        }
        let _ = changed;
    }

    let _ = HashMap::<usize, usize>::new(); // silence clippy on dead imports
    assign
}

fn sq_dist(a: &[f32], b: &[f32]) -> f32 {
    let mut s = 0.0_f32;
    for i in 0..a.len() {
        let d = a[i] - b[i];
        s += d * d;
    }
    s
}

fn cosine_f32(a: &[f32], b: &[f32]) -> f64 {
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
    let denom = na.sqrt() * nb.sqrt();
    if denom == 0.0 {
        0.0
    } else {
        dot / denom
    }
}

/// Small deterministic PRNG — no external crate. Stable across builds.
struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self {
            state: seed.wrapping_add(0x9E3779B97F4A7C15),
        }
    }
    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B7);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }
    fn next_usize(&mut self, n: usize) -> usize {
        if n == 0 {
            0
        } else {
            (self.next_u64() % n as u64) as usize
        }
    }
    fn next_f64(&mut self) -> f64 {
        // Map u64 to [0, 1).
        ((self.next_u64() >> 11) as f64) / ((1u64 << 53) as f64)
    }
}
