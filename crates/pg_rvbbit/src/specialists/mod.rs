//! Specialist sidecar dispatch — sub-LLM models accessed via HTTP.
//!
//! A "specialist" is a small focused model (sentiment classifier, embedder,
//! reranker, zero-shot classifier, etc.) that an operator can call as a step
//! alongside or instead of an LLM call. Specialists live in their own
//! processes (typically containers) and rvbbit talks to them over HTTP.
//!
//! Three transports planned:
//!   - rvbbit — our minimal `POST /predict` batch contract (this file's `rvbbit` mod)
//!   - gradio — Gradio Spaces and self-hosted Gradio apps (Phase B)
//!   - openai — any OpenAI-compatible /v1 endpoint: vLLM, Ollama, TGI (Phase B)
//!
//! The Transport trait is intentionally narrow: send N inputs, get N outputs.
//! Whether that means one HTTP call (client-batched) or N concurrent HTTP
//! calls (server-batched) is a transport detail.
//!
//! Threading: dispatch is sync (reqwest::blocking) and Send+Sync, so the
//! same trait object services pool threads from prewarm without any wrapping.
//! Spec metadata is cached per-backend in a thread-safe registry; the cache
//! is populated lazily from the leader on first use (or eagerly via
//! rvbbit.reload_backends()). Workers MUST NOT trigger an SPI load.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock, RwLock};
use std::time::Duration;

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::providers::ProviderError;

pub mod anthropic;
pub mod gemini;
pub mod gradio;
pub mod local_embed;
pub mod native;
pub mod openai;
pub mod openai_chat;
pub mod stub;

/// Catalog metadata for one registered specialist. Resolved from
/// rvbbit.backends by name and cached in the per-backend registry.
#[derive(Debug, Clone)]
pub struct SpecialistSpec {
    pub name: String,
    pub transport: String,
    pub endpoint_url: String,
    pub batch_size: usize,
    pub max_concurrent: usize,
    pub timeout_ms: u64,
    /// Name of env var holding a bearer token (NOT the token itself).
    pub auth_header_env: Option<String>,
    /// Auth token resolved at spec-load time (leader, SPI-safe) from the env
    /// var OR the rvbbit.secrets table. Cached here so `auth_token()` on a
    /// pool thread never needs SPI. `None` = resolve lazily from env at call.
    pub resolved_token: Option<String>,
    /// Transport-specific knobs (gradio fn_index, openai model name, …).
    pub transport_opts: Value,
}

impl SpecialistSpec {
    /// Resolved Authorization header value, or None. Prefers the load-time
    /// resolution (env or secrets table); falls back to a call-time env read
    /// (no SPI, so pool-thread safe) for specs built outside the SPI path.
    pub fn auth_token(&self) -> Option<String> {
        if let Some(t) = self.resolved_token.as_ref().filter(|v| !v.is_empty()) {
            return Some(t.clone());
        }
        let var = self.auth_header_env.as_ref()?;
        std::env::var(var).ok().filter(|v| !v.is_empty())
    }
}

#[derive(Debug, Clone)]
pub struct SpecialistResponse {
    /// One output per input, same order. Type is whatever the sidecar emits
    /// (string for sentiment, array for embeddings, object for structured).
    pub outputs: Vec<Value>,
    /// Token + cost usage, one entry per output. Empty for transports that
    /// have no notion of usage (embedders, classifiers); chat transports
    /// fill it so `providers::chat` can build an accurate receipt.
    pub usage: Vec<Usage>,
    pub latency_ms: i32,
}

/// Token + cost accounting for one model call. Cost is `None` unless the
/// provider settles it inline (OpenRouter does); otherwise it is derived
/// from `rvbbit.model_rates` downstream.
#[derive(Debug, Clone, Default)]
pub struct Usage {
    pub tokens_in: i32,
    pub tokens_out: i32,
    pub cost_usd: Option<f64>,
    pub cost_source: Option<String>,
    pub provider_request_id: Option<String>,
    pub provider_generation_id: Option<String>,
    pub upstream_id: Option<String>,
    pub native_tokens_in: Option<i32>,
    pub native_tokens_out: Option<i32>,
    pub reasoning_tokens: Option<i32>,
    pub cached_tokens: Option<i32>,
    pub raw: Option<Value>,
}

pub trait Transport: Send + Sync {
    /// Send N inputs, get N outputs. Implementation chooses 1-HTTP-call
    /// (client-batched) vs N-parallel-calls (server-batched) based on
    /// what the protocol supports.
    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError>;

    fn name(&self) -> &'static str;

    /// True if this transport supports client-side batching (one HTTP call
    /// per batch of N). False for transports that must dispatch one call
    /// per input (e.g. Gradio, which batches server-side).
    ///
    /// prewarm uses this to decide whether to chunk by batch_size or
    /// dispatch one pool task per input.
    fn client_batches(&self) -> bool;

    /// Tool-calling chat for the `agent` step kind: a multi-message transcript
    /// plus tool specs, returning the model's `tool_calls` (or a final answer).
    /// Default-unsupported — only OpenAI-compatible transports override it.
    fn chat_with_tools(
        &self,
        _spec: &SpecialistSpec,
        _model: &str,
        _messages: &[crate::providers::ChatMessage],
        _tools: &[crate::providers::ToolSpec],
    ) -> Result<crate::providers::ChatToolsResponse, ProviderError> {
        Err(ProviderError::NotImplemented(format!(
            "transport '{}' does not support tool-calling chat",
            self.name()
        )))
    }
}

// ---------------------------------------------------------------------------
// Registry — Transport implementations, keyed by transport name
// ---------------------------------------------------------------------------

static TRANSPORTS: OnceLock<HashMap<&'static str, Box<dyn Transport>>> = OnceLock::new();

fn build_transports() -> HashMap<&'static str, Box<dyn Transport>> {
    let mut m: HashMap<&'static str, Box<dyn Transport>> = HashMap::new();
    m.insert("rvbbit", Box::new(native::RvbbitTransport::new()));
    m.insert("anthropic", Box::new(anthropic::AnthropicTransport::new()));
    m.insert("gemini", Box::new(gemini::GeminiTransport::new()));
    m.insert("gradio", Box::new(gradio::GradioTransport::new()));
    m.insert(
        "local_embed",
        Box::new(local_embed::LocalEmbedTransport::new()),
    );
    m.insert("openai", Box::new(openai::OpenAiEmbeddingsTransport::new()));
    m.insert(
        "openai_chat",
        Box::new(openai_chat::OpenAiChatTransport::new()),
    );
    m.insert("stub", Box::new(stub::StubTransport::new()));
    m
}

/// Concurrency cap on a single LLM provider's in-flight calls, from
/// `RVBBIT_PROVIDER_MAX_CONCURRENT` (default 8). Each chat transport holds
/// one semaphore sized by this so a bulk query cannot exceed a provider's
/// rate limit.
pub(crate) fn provider_max_concurrent() -> usize {
    std::env::var("RVBBIT_PROVIDER_MAX_CONCURRENT")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(8)
        .max(1)
}

static BACKEND_SEMAPHORES: OnceLock<Mutex<HashMap<String, (usize, crate::flow::Semaphore)>>> =
    OnceLock::new();

fn backend_semaphores() -> &'static Mutex<HashMap<String, (usize, crate::flow::Semaphore)>> {
    BACKEND_SEMAPHORES.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Acquire this backend's catalog-level concurrency permit. This is separate
/// from a transport's process-wide cap: `max_concurrent` protects one
/// backend/model endpoint, while `RVBBIT_PROVIDER_MAX_CONCURRENT` protects
/// a provider class from total fan-out.
pub(crate) fn acquire_backend_permit(spec: &SpecialistSpec) -> crate::flow::Permit {
    let max = spec.max_concurrent.max(1);
    let sem = {
        let mut map = backend_semaphores().lock();
        match map.get(&spec.name) {
            Some((existing_max, existing)) if *existing_max == max => existing.clone(),
            _ => {
                let sem = crate::flow::Semaphore::new(max);
                map.insert(spec.name.clone(), (max, sem.clone()));
                sem
            }
        }
    };
    sem.acquire()
}

pub fn transport_for(name: &str) -> Result<&'static dyn Transport, ProviderError> {
    let map = TRANSPORTS.get_or_init(build_transports);
    map.get(name)
        .map(|b| b.as_ref())
        .ok_or_else(|| ProviderError::NotImplemented(format!("transport '{}'", name)))
}

// ---------------------------------------------------------------------------
// Spec cache — populated from rvbbit.backends via SPI (leader only)
// ---------------------------------------------------------------------------

static SPEC_CACHE: OnceLock<RwLock<HashMap<String, Arc<SpecialistSpec>>>> = OnceLock::new();

fn cache() -> &'static RwLock<HashMap<String, Arc<SpecialistSpec>>> {
    SPEC_CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Get a cached spec. SAFE FROM ANY THREAD — no SPI. Returns None on miss.
/// Worker threads from the prewarm pool MUST use this (not load_spec).
pub fn get_cached_spec(name: &str) -> Option<Arc<SpecialistSpec>> {
    cache().read().ok()?.get(name).cloned()
}

/// Load a spec via SPI and cache it. LEADER CONTEXT ONLY (pgrx Spi).
/// Idempotent — returns the cached spec if already present.
pub fn load_spec(name: &str) -> Result<Arc<SpecialistSpec>, ProviderError> {
    if let Some(s) = get_cached_spec(name) {
        return Ok(s);
    }
    let spec = load_spec_from_spi(name)?;
    let arc = Arc::new(spec);
    if let Ok(mut w) = cache().write() {
        w.insert(name.to_string(), arc.clone());
    }
    Ok(arc)
}

/// Pre-load into the per-backend spec cache every model backend an operator
/// touches — `specialist` nodes, the LLM `provider` of each `llm` node, and
/// the default LLM provider (which backs single-LLM operators and `llm`
/// nodes with no explicit `provider`). LEADER / backend context only (the
/// SPI load). Must run before any pool thread executes the operator: a
/// cache miss falls back to an SPI load, and SPI is illegal on a pool
/// worker thread. Errors are ignored — a missing backend surfaces cleanly
/// later as a per-node error.
pub fn warm_operator_specs(steps: Option<&Value>, takes: Option<&Value>) {
    let mut names: Vec<String> = Vec::new();
    collect_specialist_names(steps, &mut names);
    collect_specialist_names(takes.and_then(|t| t.get("nodes")), &mut names);
    collect_provider_names(steps, &mut names);
    collect_provider_names(takes.and_then(|t| t.get("nodes")), &mut names);
    names.push(crate::providers::default_provider_name());
    for name in names {
        let _ = load_spec(&name);
    }
}

fn collect_specialist_names(nodes: Option<&Value>, out: &mut Vec<String>) {
    let Some(arr) = nodes.and_then(|n| n.as_array()) else {
        return;
    };
    for node in arr {
        if node.get("kind").and_then(|k| k.as_str()) == Some("specialist") {
            if let Some(name) = node.get("specialist").and_then(|s| s.as_str()) {
                out.push(name.to_string());
            }
        }
    }
}

/// Collect the LLM `provider` backend named by each `kind:llm` node. A node
/// with no `provider` falls back to the default, which `warm_operator_specs`
/// always pre-loads anyway.
fn collect_provider_names(nodes: Option<&Value>, out: &mut Vec<String>) {
    let Some(arr) = nodes.and_then(|n| n.as_array()) else {
        return;
    };
    for node in arr {
        if node.get("kind").and_then(|k| k.as_str()) == Some("llm") {
            if let Some(p) = node.get("provider").and_then(|s| s.as_str()) {
                out.push(p.to_string());
            }
        }
    }
}

/// Load (or refresh) all specs from rvbbit.backends into the cache.
/// LEADER CONTEXT ONLY. Called by rvbbit.reload_backends() and by
/// prewarm before it dispatches work.
pub fn reload_all() -> Result<usize, ProviderError> {
    let _ = crate::providers::reload_default_provider_from_spi();
    let names = load_all_names()?;
    let n = names.len();
    if let Ok(mut w) = cache().write() {
        w.clear();
    }
    for name in &names {
        // Force fresh load (cache just cleared).
        let spec = Arc::new(load_spec_from_spi(name)?);
        if let Ok(mut w) = cache().write() {
            w.insert(name.clone(), spec);
        }
    }
    Ok(n)
}

fn load_all_names() -> Result<Vec<String>, ProviderError> {
    use pgrx::Spi;
    let mut names = Vec::new();
    let _: Result<(), pgrx::spi::Error> = Spi::connect(|client| {
        let table = client.select(
            "SELECT name FROM rvbbit.warren_backend_status WHERE callable ORDER BY name",
            None,
            &[],
        )?;
        for row in table {
            if let Some(n) = row.get::<String>(1)? {
                names.push(n);
            }
        }
        Ok(())
    });
    Ok(names)
}

fn load_spec_from_spi(name: &str) -> Result<SpecialistSpec, ProviderError> {
    use pgrx::{JsonB, Spi};
    let escaped = name.replace('\'', "''");
    let sql = format!(
        "SELECT transport, endpoint_url, batch_size, max_concurrent, \
                timeout_ms, auth_header_env, transport_opts \
         FROM rvbbit.warren_backend_status WHERE name = '{escaped}' AND callable"
    );
    let mut result: Option<SpecialistSpec> = None;
    let _: Result<(), pgrx::spi::Error> = Spi::connect(|client| {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let transport: Option<String> = row.get(1)?;
            let endpoint_url: Option<String> = row.get(2)?;
            let batch_size: Option<i32> = row.get(3)?;
            let max_concurrent: Option<i32> = row.get(4)?;
            let timeout_ms: Option<i32> = row.get(5)?;
            let auth_env: Option<String> = row.get(6)?;
            let opts: Option<JsonB> = row.get(7)?;
            if let (Some(t), Some(url)) = (transport, endpoint_url) {
                result = Some(SpecialistSpec {
                    name: name.to_string(),
                    transport: t,
                    endpoint_url: url,
                    batch_size: batch_size.unwrap_or(32).max(1) as usize,
                    max_concurrent: max_concurrent.unwrap_or(4).max(1) as usize,
                    timeout_ms: timeout_ms.unwrap_or(30_000).max(100) as u64,
                    auth_header_env: auth_env,
                    // Resolved below, after the SPI closure returns.
                    resolved_token: None,
                    transport_opts: opts
                        .map(|j| j.0)
                        .unwrap_or_else(|| Value::Object(Default::default())),
                });
            }
        }
        Ok(())
    });
    // Resolve the auth token on the leader (SPI-legal): env var FIRST
    // (deploy-time wins), then the SQL-settable rvbbit.secrets table via the
    // SECURITY DEFINER resolver. Cached in the spec so pool threads never SPI.
    if let Some(spec) = result.as_mut() {
        if let Some(var) = spec.auth_header_env.clone().filter(|v| !v.is_empty()) {
            let from_env = std::env::var(&var).ok().filter(|v| !v.is_empty());
            spec.resolved_token = from_env.or_else(|| {
                let esc = var.replace('\'', "''");
                Spi::get_one::<String>(&format!("SELECT rvbbit.get_secret('{esc}')"))
                    .ok()
                    .flatten()
                    .filter(|v| !v.is_empty())
            });
        }
    }
    result.ok_or_else(|| {
        ProviderError::Config(format!(
            "specialist '{}' not registered or Warren deployment is not callable",
            name
        ))
    })
}

// ---------------------------------------------------------------------------
// Top-level dispatch — used by unit_of_work for kind=specialist steps
// ---------------------------------------------------------------------------

/// Single-input invocation. Convenience wrapper around predict_batch for
/// scalar operator paths that haven't been batched yet.
pub fn predict_one(spec: &SpecialistSpec, input: &Value) -> Result<Value, ProviderError> {
    let inputs = [input.clone()];
    let resp = transport_for(&spec.transport)?.predict(spec, &inputs)?;
    resp.outputs
        .into_iter()
        .next()
        .ok_or_else(|| ProviderError::BadResponse("specialist returned no outputs".into()))
}

/// Batch invocation. Returns one output per input, same order.
pub fn predict_batch(
    spec: &SpecialistSpec,
    inputs: &[Value],
) -> Result<SpecialistResponse, ProviderError> {
    let resp = transport_for(&spec.transport)?.predict(spec, inputs)?;
    if resp.outputs.len() != inputs.len() {
        return Err(ProviderError::BadResponse(format!(
            "specialist '{}' returned {} outputs for {} inputs",
            spec.name,
            resp.outputs.len(),
            inputs.len()
        )));
    }
    Ok(resp)
}

// ---------------------------------------------------------------------------
// Shared HTTP client — one per backend, reused across transports + specs
// ---------------------------------------------------------------------------

static HTTP_CLIENT: OnceLock<reqwest::blocking::Client> = OnceLock::new();

pub(crate) fn http_client() -> &'static reqwest::blocking::Client {
    HTTP_CLIENT.get_or_init(|| {
        reqwest::blocking::Client::builder()
            // Per-call timeout is set via RequestBuilder::timeout from the spec.
            // This is the floor in case spec timeout is missing.
            .timeout(Duration::from_secs(60))
            .pool_max_idle_per_host(32)
            .pool_idle_timeout(Some(Duration::from_secs(90)))
            .build()
            .expect("reqwest client build")
    })
}

// ---------------------------------------------------------------------------
// Shared wire types — most transports reuse these
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub(crate) struct PredictRequest<'a> {
    pub inputs: &'a [Value],
}

#[derive(Debug, Deserialize)]
pub(crate) struct PredictResponse {
    pub outputs: Vec<Value>,
}
