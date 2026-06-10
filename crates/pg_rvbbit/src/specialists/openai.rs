//! OpenAI-compatible embeddings transport.
//!
//! Talks to anything that implements OpenAI's POST /v1/embeddings:
//! Ollama, vLLM, TGI, LM Studio, LiteLLM, OpenAI proper. The endpoint_url
//! in the catalog is the full URL (typically ending in /v1/embeddings).
//!
//! Wire format (request):
//!   POST {endpoint_url}
//!   {"input": [text1, text2, ...], "model": "<from transport_opts>"}
//!
//! Wire format (response):
//!   200 OK
//!   {"data": [{"embedding": [...], "index": 0}, ...],
//!    "model": "...", "usage": {...}}
//!
//! Client-side batched — `input` is an array; one HTTP call covers N items.
//!
//! User catalog wiring expects `inputs.text` (per row); the transport packs
//! the per-row texts into the OpenAI `input` array. Other input keys are
//! ignored. The model name lives in `transport_opts.model`.
//!
//! Chat completions are intentionally NOT covered by this transport —
//! that overlaps with the LLM provider abstraction in providers.rs.
//! Add an Ollama/vLLM option there if you want chat-style calls.

use super::{http_client, SpecialistResponse, SpecialistSpec, Transport};
use crate::providers::ProviderError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;

pub struct OpenAiEmbeddingsTransport;

impl OpenAiEmbeddingsTransport {
    pub fn new() -> Self {
        Self
    }
}

#[derive(Serialize)]
struct OaiEmbedRequest<'a> {
    input: Vec<&'a str>,
    model: &'a str,
}

#[derive(Deserialize)]
struct OaiEmbedResponse {
    #[serde(default)]
    data: Vec<OaiEmbedItem>,
}

#[derive(Deserialize)]
struct OaiEmbedItem {
    #[serde(default)]
    embedding: Vec<f64>,
    #[serde(default)]
    index: usize,
}

impl Transport for OpenAiEmbeddingsTransport {
    fn name(&self) -> &'static str {
        "openai"
    }

    fn client_batches(&self) -> bool {
        true
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        let model = spec
            .transport_opts
            .get("model")
            .and_then(|v| v.as_str())
            .ok_or_else(|| {
                ProviderError::Config(format!(
                    "specialist '{}': transport_opts.model is required for openai transport",
                    spec.name
                ))
            })?;

        // Pull text out of each input. Convention: `inputs.text` is the
        // string to embed. Any other input fields are ignored.
        let texts: Vec<&str> = inputs
            .iter()
            .map(|v| v.get("text").and_then(|t| t.as_str()).unwrap_or(""))
            .collect();

        let body = OaiEmbedRequest {
            input: texts,
            model,
        };
        let mut req = http_client()
            .post(&spec.endpoint_url)
            .timeout(Duration::from_millis(spec.timeout_ms))
            .json(&body);
        if let Some(token) = spec.auth_token() {
            req = req.bearer_auth(token);
        }

        let t0 = std::time::Instant::now();
        let resp = req.send()?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().unwrap_or_default();
            return Err(ProviderError::ApiStatus {
                status: status.as_u16(),
                body,
            });
        }
        let mut parsed: OaiEmbedResponse = resp.json()?;
        let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

        // OpenAI guarantees data is returned ordered by index, but Ollama
        // and others have shipped bugs around this. Sort defensively.
        parsed.data.sort_by_key(|i| i.index);

        if parsed.data.len() != inputs.len() {
            return Err(ProviderError::BadResponse(format!(
                "openai embeddings: got {} embeddings for {} inputs",
                parsed.data.len(),
                inputs.len()
            )));
        }
        // security-10: a matching count is not enough — a non-conforming endpoint
        // can return duplicate or gappy indices (e.g. [0,0,2] for 3 inputs), which
        // would silently map the wrong vector to a row and corrupt the embedding
        // cache / Lance / KNN. After the sort the indices must be exactly 0..n-1.
        for (expected, item) in parsed.data.iter().enumerate() {
            if item.index as usize != expected {
                return Err(ProviderError::BadResponse(format!(
                    "openai embeddings: non-contiguous response index {} at position {} \
                     (expected 0..{}); cannot safely map embeddings back to inputs",
                    item.index,
                    expected,
                    inputs.len().saturating_sub(1)
                )));
            }
        }

        let outputs: Vec<Value> = parsed
            .data
            .into_iter()
            .map(|item| {
                Value::Array(
                    item.embedding
                        .into_iter()
                        .map(|f| {
                            serde_json::Number::from_f64(f)
                                .map(Value::Number)
                                .unwrap_or(Value::Null)
                        })
                        .collect(),
                )
            })
            .collect();

        Ok(SpecialistResponse {
            outputs,
            usage: Vec::new(),
            latency_ms,
        })
    }
}
