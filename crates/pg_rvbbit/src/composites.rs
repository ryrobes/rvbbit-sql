//! Composite semantic operators (Lars-inspired Tier B; RYR-303).
//!
//! These compose on top of the existing embedding cache: nothing here
//! makes new HTTP calls if the inputs are already embedded. They are
//! pure-Rust algorithms over the cached vector space, which is what
//! makes them fast + deterministic given fixed inputs + specialist.
//!
//! Surface:
//!   - rvbbit.outliers(query_sql, n [, criterion] [, specialist])
//!       -> SETOF (text, score)
//!     Without criterion: top-n most ISOLATED rows (large NN distance).
//!     With criterion:    top-n LEAST RELEVANT rows vs. criterion.
//!
//!   - rvbbit.dedupe_groups(query_sql [, threshold] [, specialist])
//!       -> SETOF (group_id, representative, size, members)
//!     Union-find over a similarity threshold graph; one row per group.
//!
//!   - rvbbit.semantic_case(text, conditions[], results[], default, specialist)
//!       -> text
//!     Argmax over condition embeddings; returns the matching result
//!     (or default if no condition clears the threshold).

use std::collections::HashMap;

use pgrx::prelude::*;
use serde_json::Value as JsonValue;

use crate::embeddings::{
    bulk_cache_lookup, cache_store, parse_embedding_value, resolve_specialist, spec_model,
    text_hash,
};

// ---------------------------------------------------------------------------
// Shared: ensure every distinct text is embedded, return (text, count, vec).

struct Texted {
    text: String,
    count: i64,
    vec: Vec<f32>,
}

fn embed_distinct_from_query(
    query_sql: &str,
    specialist_arg: &str,
) -> Result<(Vec<Texted>, String), String> {
    let spec = resolve_specialist(specialist_arg)?;
    let model = spec_model(&spec);
    let distinct = collect_distinct_texts(query_sql)?;
    if distinct.is_empty() {
        return Ok((Vec::new(), spec.name.clone()));
    }
    let cache_map = bulk_cache_lookup(&spec.name);
    let mut out: Vec<Texted> = Vec::with_capacity(distinct.len());
    let mut to_embed: Vec<(String, i64)> = Vec::new();
    for (text, count) in &distinct {
        let h = text_hash(&spec.name, text);
        match cache_map.get(&h) {
            Some(v) => out.push(Texted {
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
            let inputs: Vec<JsonValue> = chunk
                .iter()
                .map(|(t, _)| serde_json::json!({"text": t}))
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
            for ((text, count), output) in chunk.iter().zip(result.outputs.iter()) {
                let v = parse_embedding_value(output)?;
                let h = text_hash(&spec.name, text);
                let _ = cache_store(&h, &spec.name, &model, &v);
                out.push(Texted {
                    text: text.clone(),
                    count: *count,
                    vec: v,
                });
            }
        }
    }
    Ok((out, spec.name.clone()))
}

fn collect_distinct_texts(query_sql: &str) -> Result<Vec<(String, i64)>, String> {
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

fn embed_single(text: &str, specialist_arg: &str) -> Result<(Vec<f32>, String), String> {
    let spec = resolve_specialist(specialist_arg)?;
    let model = spec_model(&spec);
    let h = text_hash(&spec.name, text);
    let map = bulk_cache_lookup(&spec.name);
    if let Some(v) = map.get(&h) {
        return Ok((v.clone(), spec.name.clone()));
    }
    let input = serde_json::json!({"text": text});
    let result =
        crate::specialists::predict_one(&spec, &input).map_err(|e| format!("predict_one: {e}"))?;
    let v = parse_embedding_value(&result)?;
    let _ = cache_store(&h, &spec.name, &model, &v);
    Ok((v, spec.name.clone()))
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
    let denom = na.sqrt() * nb.sqrt();
    if denom == 0.0 {
        0.0
    } else {
        dot / denom
    }
}

// ---------------------------------------------------------------------------
// outliers

/// Find the N most-unusual texts from `query_sql`.
///
/// Modes:
///   - `criterion = ''` (isolation): per-row score = 1 - cosine(row, nearest_other_row).
///     Rows that are far from any other row in embedding space rank highest.
///   - `criterion = 'something'` (relevance): per-row score = 1 - cosine(row, criterion).
///     Rows that don't match the criterion rank highest.
///
/// Returns SETOF (text, score) sorted by score DESC.
#[pg_extern(volatile)]
fn outliers(
    query_sql: &str,
    n: i32,
    criterion: default!(&str, "''"),
    specialist: default!(&str, "''"),
) -> TableIterator<'static, (name!(text, String), name!(score, f64))> {
    if n <= 0 {
        return TableIterator::new(Vec::<(String, f64)>::new().into_iter());
    }
    let n = n as usize;

    let (texted, spec_name) = match embed_distinct_from_query(query_sql, specialist) {
        Ok(t) => t,
        Err(e) => pgrx::error!("rvbbit.outliers: {e}"),
    };
    if texted.is_empty() {
        return TableIterator::new(Vec::<(String, f64)>::new().into_iter());
    }

    let scored: Vec<(String, f64)> = if criterion.trim().is_empty() {
        // Isolation: score = 1 - max_other_cosine.
        // O(N²) for the pairwise comparison. Document the limit.
        let mut out = Vec::with_capacity(texted.len());
        for (i, ti) in texted.iter().enumerate() {
            let mut max_sim = -1.0_f64;
            for (j, tj) in texted.iter().enumerate() {
                if i == j {
                    continue;
                }
                let s = cosine(&ti.vec, &tj.vec);
                if s > max_sim {
                    max_sim = s;
                }
            }
            // If singleton, max_sim stays -1 → distance 2; clamp to 1 for stability.
            let dist = if max_sim < -1.0 + 1e-9 {
                1.0
            } else {
                (1.0 - max_sim).clamp(0.0, 2.0)
            };
            out.push((ti.text.clone(), dist));
        }
        out
    } else {
        // Criterion: score = 1 - cosine(text, criterion).
        let (crit_vec, _) = match embed_single(criterion, &spec_name) {
            Ok(v) => v,
            Err(e) => pgrx::error!("rvbbit.outliers: embed criterion: {e}"),
        };
        texted
            .iter()
            .map(|t| {
                let s = cosine(&t.vec, &crit_vec);
                (t.text.clone(), (1.0 - s).clamp(0.0, 2.0))
            })
            .collect()
    };

    let mut sorted = scored;
    sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    sorted.truncate(n);
    TableIterator::new(sorted.into_iter())
}

// ---------------------------------------------------------------------------
// dedupe_groups

/// Group near-duplicate texts via a similarity threshold + union-find.
/// Returns one row per group: (group_id, representative, size, members).
///
/// Representative = longest text in the group (most informative); ties
/// broken lexicographically. Sorted by size DESC then representative ASC.
#[pg_extern(volatile)]
fn dedupe_groups(
    query_sql: &str,
    threshold: default!(f64, "0.7"),
    specialist: default!(&str, "''"),
) -> TableIterator<
    'static,
    (
        name!(group_id, i32),
        name!(representative, String),
        name!(size, i64),
        name!(members, Vec<String>),
    ),
> {
    let (texted, _) = match embed_distinct_from_query(query_sql, specialist) {
        Ok(t) => t,
        Err(e) => pgrx::error!("rvbbit.dedupe_groups: {e}"),
    };
    if texted.is_empty() {
        return TableIterator::new(Vec::<(i32, String, i64, Vec<String>)>::new().into_iter());
    }

    let n = texted.len();
    let mut parent: Vec<usize> = (0..n).collect();

    fn find(parent: &mut [usize], mut x: usize) -> usize {
        while parent[x] != x {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        x
    }
    fn union(parent: &mut [usize], a: usize, b: usize) {
        let ra = find(parent, a);
        let rb = find(parent, b);
        if ra != rb {
            parent[ra] = rb;
        }
    }

    // Pairwise threshold graph. O(N²) — document the limit.
    for i in 0..n {
        for j in (i + 1)..n {
            let s = cosine(&texted[i].vec, &texted[j].vec);
            if s >= threshold {
                union(&mut parent, i, j);
            }
        }
    }

    // Group by root.
    let mut groups: HashMap<usize, Vec<usize>> = HashMap::new();
    for i in 0..n {
        let r = find(&mut parent, i);
        groups.entry(r).or_default().push(i);
    }

    let mut rows: Vec<(i64, String, Vec<String>)> = Vec::new();
    for (_, members) in groups {
        let texts: Vec<String> = members.iter().map(|&i| texted[i].text.clone()).collect();
        let mut sorted = texts.clone();
        sorted.sort();
        // Representative: longest, tie -> lexicographically smallest.
        let mut rep_idx = 0usize;
        for (k, t) in texts.iter().enumerate() {
            let cur = &texts[rep_idx];
            if t.len() > cur.len() || (t.len() == cur.len() && t < cur) {
                rep_idx = k;
            }
        }
        let size: i64 = members.iter().map(|&i| texted[i].count).sum();
        rows.push((size, texts[rep_idx].clone(), sorted));
    }

    rows.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));

    let out: Vec<(i32, String, i64, Vec<String>)> = rows
        .into_iter()
        .enumerate()
        .map(|(i, (size, rep, members))| (i as i32, rep, size, members))
        .collect();
    TableIterator::new(out.into_iter())
}

// ---------------------------------------------------------------------------
// diff (semantic set difference / novelty detection)

/// Find the N rows from `query_a` that are most-semantically-novel
/// relative to `query_b`. For each distinct text in A, computes
/// distance = 1 - max cosine(a_row, b_row over all b_rows).
/// Returns SETOF (text, novelty) sorted by novelty DESC.
///
///   -- "What's new in yesterday's tickets vs last week's?"
///   SELECT * FROM rvbbit.diff(
///     'SELECT body FROM tickets WHERE created_at > now() - interval ''1 day''',
///     'SELECT body FROM tickets WHERE created_at <= now() - interval ''1 day''
///                              AND created_at > now() - interval ''7 days''',
///     k => 10);
///
/// Use cases: anomaly / novelty detection, change-log briefs, daily
/// "what's new" digests for support / community / news streams. The
/// alternative ("write a Python script") is what every team does today.
#[pg_extern(volatile)]
fn diff(
    query_a: &str,
    query_b: &str,
    k: i32,
    specialist: default!(&str, "''"),
) -> TableIterator<'static, (name!(text, String), name!(novelty, f64))> {
    if k <= 0 {
        return TableIterator::new(Vec::<(String, f64)>::new().into_iter());
    }
    let k = k as usize;

    let (a, _) = match embed_distinct_from_query(query_a, specialist) {
        Ok(t) => t,
        Err(e) => pgrx::error!("rvbbit.diff: query_a: {e}"),
    };
    if a.is_empty() {
        return TableIterator::new(Vec::<(String, f64)>::new().into_iter());
    }
    let (b, _) = match embed_distinct_from_query(query_b, specialist) {
        Ok(t) => t,
        Err(e) => pgrx::error!("rvbbit.diff: query_b: {e}"),
    };

    let mut scored: Vec<(String, f64)> = Vec::with_capacity(a.len());
    for ta in &a {
        // Distance to nearest B (or 1.0 if B is empty — everything novel).
        let mut max_sim = -1.0_f64;
        for tb in &b {
            // Skip identical-text matches that happen to appear in both
            // queries; they're not "novel". Distinguish by text equality.
            if ta.text == tb.text {
                max_sim = 1.0;
                break;
            }
            let s = cosine(&ta.vec, &tb.vec);
            if s > max_sim {
                max_sim = s;
            }
        }
        let novelty = if b.is_empty() {
            1.0
        } else {
            (1.0 - max_sim).clamp(0.0, 2.0)
        };
        scored.push((ta.text.clone(), novelty));
    }

    scored.sort_by(|x, y| y.1.partial_cmp(&x.1).unwrap_or(std::cmp::Ordering::Equal));
    scored.truncate(k);
    TableIterator::new(scored.into_iter())
}

// ---------------------------------------------------------------------------
// semantic_case

/// Multi-branch semantic classification: returns `results[i]` where i =
/// argmax(cosine(text, conditions[i])). Falls back to `default_val` if
/// the best condition's similarity is below `min_score` (default 0.0,
/// meaning always pick the argmax).
///
/// Conditions and results must be the same length. Use this as the
/// SQL-native alternative to a long CASE WHEN with regex / LIKE.
#[pg_extern(volatile)]
fn semantic_case(
    text: &str,
    conditions: Vec<String>,
    results: Vec<String>,
    default_val: default!(&str, "''"),
    min_score: default!(f64, "0.0"),
    specialist: default!(&str, "''"),
) -> String {
    if conditions.len() != results.len() {
        pgrx::error!(
            "rvbbit.semantic_case: conditions ({}) and results ({}) lengths differ",
            conditions.len(),
            results.len()
        );
    }
    if conditions.is_empty() {
        return default_val.to_string();
    }
    if text.is_empty() {
        return default_val.to_string();
    }

    let spec = match resolve_specialist(specialist) {
        Ok(s) => s,
        Err(e) => pgrx::error!("rvbbit.semantic_case: {e}"),
    };
    let model = spec_model(&spec);
    let cache_map = bulk_cache_lookup(&spec.name);

    // Ensure text + every condition is embedded (single batch for missing).
    let mut needed: Vec<&str> = Vec::with_capacity(conditions.len() + 1);
    needed.push(text);
    for c in &conditions {
        needed.push(c.as_str());
    }
    let mut to_embed: Vec<String> = Vec::new();
    for &t in &needed {
        let h = text_hash(&spec.name, t);
        if !cache_map.contains_key(&h) {
            to_embed.push(t.to_string());
        }
    }
    if !to_embed.is_empty() {
        let inputs: Vec<JsonValue> = to_embed
            .iter()
            .map(|t| serde_json::json!({"text": t}))
            .collect();
        let result = match crate::specialists::predict_batch(&spec, &inputs) {
            Ok(r) => r,
            Err(e) => pgrx::error!("rvbbit.semantic_case: predict_batch: {e}"),
        };
        if result.outputs.len() != to_embed.len() {
            pgrx::error!(
                "rvbbit.semantic_case: got {} outputs for {} inputs",
                result.outputs.len(),
                to_embed.len()
            );
        }
        for (t, out) in to_embed.iter().zip(result.outputs.iter()) {
            let v = match parse_embedding_value(out) {
                Ok(v) => v,
                Err(e) => pgrx::error!("rvbbit.semantic_case: {e}"),
            };
            let h = text_hash(&spec.name, t);
            let _ = cache_store(&h, &spec.name, &model, &v);
        }
    }

    // Re-load after potential inserts.
    let cache_map = bulk_cache_lookup(&spec.name);
    let text_vec = match cache_map.get(&text_hash(&spec.name, text)) {
        Some(v) => v.clone(),
        None => return default_val.to_string(),
    };

    let mut best: (f64, usize) = (f64::MIN, 0);
    for (i, c) in conditions.iter().enumerate() {
        let h = text_hash(&spec.name, c);
        if let Some(cv) = cache_map.get(&h) {
            let s = cosine(&text_vec, cv);
            if s > best.0 {
                best = (s, i);
            }
        }
    }

    if best.0 < min_score {
        default_val.to_string()
    } else {
        results[best.1].clone()
    }
}
