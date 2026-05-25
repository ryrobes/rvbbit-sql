//! Token-count + tokenize UDFs over OpenAI BPE encodings.
//!
//! First slice of the local model tier (RYR-289). Cheapest layer:
//! no model downloads, no network, deterministic, used by EXPLAIN
//! SEMANTIC (RYR-290) for prompt-token cost estimation.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};

use parking_lot::RwLock;
use pgrx::prelude::*;
use tiktoken_rs::{cl100k_base, o200k_base, p50k_base, r50k_base, CoreBPE};

static ENCODER_CACHE: OnceLock<RwLock<HashMap<String, Arc<CoreBPE>>>> = OnceLock::new();

fn cache() -> &'static RwLock<HashMap<String, Arc<CoreBPE>>> {
    ENCODER_CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

fn get_encoder(name: &str) -> Result<Arc<CoreBPE>, String> {
    if let Some(enc) = cache().read().get(name) {
        return Ok(enc.clone());
    }
    let bpe = match name {
        "cl100k_base" => cl100k_base(),
        "o200k_base" => o200k_base(),
        "p50k_base" => p50k_base(),
        "r50k_base" => r50k_base(),
        other => {
            return Err(format!(
                "unknown encoding {other:?} (known: cl100k_base, o200k_base, p50k_base, r50k_base)"
            ));
        }
    }
    .map_err(|e| e.to_string())?;
    let arc = Arc::new(bpe);
    cache().write().insert(name.to_string(), arc.clone());
    Ok(arc)
}

#[pg_extern(immutable, parallel_safe)]
fn token_count(text: &str, encoding: default!(&str, "'cl100k_base'")) -> i32 {
    match get_encoder(encoding) {
        Ok(enc) => enc.encode_with_special_tokens(text).len() as i32,
        Err(e) => pgrx::error!("rvbbit.token_count: {e}"),
    }
}

#[pg_extern(immutable, parallel_safe)]
fn tokenize(text: &str, encoding: default!(&str, "'cl100k_base'")) -> Vec<i32> {
    match get_encoder(encoding) {
        Ok(enc) => enc
            .encode_with_special_tokens(text)
            .into_iter()
            .map(|t| t as i32)
            .collect(),
        Err(e) => pgrx::error!("rvbbit.tokenize: {e}"),
    }
}

/// List the supported encodings so users discover the surface from SQL.
#[pg_extern(immutable, parallel_safe)]
fn token_encodings() -> Vec<String> {
    vec![
        "cl100k_base".into(),
        "o200k_base".into(),
        "p50k_base".into(),
        "r50k_base".into(),
    ]
}
