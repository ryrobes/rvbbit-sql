//! Stub transport — deterministic in-process responses that need no
//! network or model files. Two modes, picked per input:
//!
//!   chat-shaped (input has a `user` key) → echoes the `user` text back.
//!     A `stub` backend thus doubles as a deterministic LLM provider for
//!     tests: register one, point an `llm` node's `provider` at it.
//!   otherwise → a hash-derived "embedding" vector. Same text → same
//!     vector; vector content is hash-derived, so semantic relationships
//!     are NOT preserved (use real openai/rvbbit transports for that).
//!
//! Intended for tests + sanity checks of the cache / dispatch plumbing.
//!
//! Registered like any other backend:
//!   SELECT rvbbit.register_backend(
//!       backend_name => 'stub_embedder',
//!       backend_endpoint => 'stub://384',         -- numeric suffix = dim
//!       backend_transport => 'stub'
//!   );

use super::{SpecialistResponse, SpecialistSpec, Transport};
use crate::providers::ProviderError;
use serde_json::Value;

pub struct StubTransport;

impl StubTransport {
    pub fn new() -> Self {
        Self
    }
}

impl Transport for StubTransport {
    fn name(&self) -> &'static str {
        "stub"
    }

    fn client_batches(&self) -> bool {
        true
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        let dim = parse_dim(&spec.endpoint_url).unwrap_or(384);
        let t0 = std::time::Instant::now();
        let outputs: Vec<Value> = inputs
            .iter()
            .map(|input| {
                // Chat-shaped input → echo the prompt, so a stub backend is
                // a deterministic LLM provider. Otherwise → embedding vector.
                if let Some(user) = input.get("user").and_then(|u| u.as_str()) {
                    return Value::String(user.to_string());
                }
                let text = input.get("text").and_then(|t| t.as_str()).unwrap_or("");
                let vec = hash_embed(text, dim);
                Value::Array(
                    vec.into_iter()
                        .map(|f| {
                            serde_json::Number::from_f64(f as f64)
                                .map(Value::Number)
                                .unwrap_or(Value::Null)
                        })
                        .collect(),
                )
            })
            .collect();
        let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;
        Ok(SpecialistResponse {
            outputs,
            usage: Vec::new(),
            latency_ms,
        })
    }
}

fn parse_dim(endpoint: &str) -> Option<usize> {
    // Accept either "stub://384" or "stub://anything/384" — take the last
    // segment if it's numeric.
    endpoint
        .rsplit('/')
        .find_map(|seg| seg.parse::<usize>().ok())
        .filter(|n| *n > 0 && *n <= 4096)
}

fn hash_embed(text: &str, dim: usize) -> Vec<f32> {
    // Strategy: blake3-XOF expands to `dim * 4` bytes, reinterpreted as
    // f32. The vector is then L2-normalized so cosine == dot product
    // matches the convention used by real embedders.
    let mut hasher = blake3::Hasher::new();
    hasher.update(text.as_bytes());
    let mut out = vec![0u8; dim * 4];
    hasher.finalize_xof().fill(&mut out);

    let mut v: Vec<f32> = out
        .chunks_exact(4)
        .map(|c| {
            let bits = u32::from_le_bytes([c[0], c[1], c[2], c[3]]);
            // Map u32 to [-1.0, 1.0] roughly uniformly.
            (bits as f32 / u32::MAX as f32) * 2.0 - 1.0
        })
        .collect();

    let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm > 0.0 {
        for x in &mut v {
            *x /= norm;
        }
    }
    v
}
