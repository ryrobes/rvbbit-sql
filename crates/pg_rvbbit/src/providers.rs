//! LLM chat calls — provider-agnostic.
//!
//! An LLM provider is just a model backend: a row in `rvbbit.backends`
//! with a chat transport (`openai_chat` today; Phase 2 adds `anthropic` and
//! `gemini`). `chat()` resolves the provider backend by name — an `llm`
//! node's `provider` field, or the default (`RVBBIT_DEFAULT_PROVIDER`, else
//! `openrouter`) — and dispatches through the very same `Transport`
//! machinery that serves specialist nodes. The model is just a parameter.
//!
//! `ChatRequest` / `ChatResponse` remain the ergonomic call interface the
//! executor builds; the per-provider wire formats live in the transports
//! (see `specialists/openai_chat.rs`).
//!
//! All calls are SYNC by design — pgrx backends are synchronous Postgres
//! processes; the thin sync path keeps transports tiny and avoids a tokio
//! runtime per backend.

use serde::Serialize;
use serde_json::Value;
use std::sync::{OnceLock, RwLock};

#[derive(Debug, thiserror::Error)]
pub enum ProviderError {
    #[error("provider config missing: {0}")]
    Config(String),
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("API returned status {status}: {body}")]
    ApiStatus { status: u16, body: String },
    #[error("API response malformed: {0}")]
    BadResponse(String),
    #[error("provider not implemented: {0}")]
    NotImplemented(String),
}

// ---------------------------------------------------------------------------
// Chat call interface
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct ChatRequest {
    pub model: String,
    pub system: Option<String>,
    pub user: String,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
    /// LLM provider backend (a row in `rvbbit.backends`). `None` resolves
    /// to the default provider — `RVBBIT_DEFAULT_PROVIDER`, else `openrouter`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ChatResponse {
    pub content: String,
    pub model: String,
    pub provider: String,
    pub transport: String,
    pub prompt_tokens: i32,
    pub completion_tokens: i32,
    pub latency_ms: i32,
    pub cost_usd: Option<f64>,
    pub cost_source: Option<String>,
    pub provider_request_id: Option<String>,
    pub provider_generation_id: Option<String>,
    pub upstream_id: Option<String>,
    pub native_tokens_in: Option<i32>,
    pub native_tokens_out: Option<i32>,
    pub reasoning_tokens: Option<i32>,
    pub cached_tokens: Option<i32>,
    pub raw_usage: Option<Value>,
}

static DEFAULT_PROVIDER_CACHE: OnceLock<RwLock<Option<String>>> = OnceLock::new();

fn default_provider_cache() -> &'static RwLock<Option<String>> {
    DEFAULT_PROVIDER_CACHE.get_or_init(|| RwLock::new(None))
}

/// Name of the default LLM provider backend. Precedence:
/// 1. `RVBBIT_DEFAULT_PROVIDER` env var for container/operator-level override.
/// 2. `rvbbit.settings['default_provider']`, refreshed by reload_backends().
/// 3. `openrouter`, which the extension bootstrap pre-registers.
pub fn default_provider_name() -> String {
    if let Some(provider) = std::env::var("RVBBIT_DEFAULT_PROVIDER")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
    {
        return provider;
    }

    if let Some(provider) = default_provider_cache().read().ok().and_then(|g| g.clone()) {
        return provider;
    }

    if !crate::flow::in_pool_worker() {
        let _ = reload_default_provider_from_spi();
        if let Some(provider) = default_provider_cache().read().ok().and_then(|g| g.clone()) {
            return provider;
        }
    }

    "openrouter".to_string()
}

/// Refresh the backend-local default-provider cache from SQL. Leader context
/// only: this uses SPI and must never be called from flow pool workers.
pub fn reload_default_provider_from_spi() -> Result<(), ProviderError> {
    use pgrx::Spi;

    let provider = Spi::get_one::<String>(
        "SELECT coalesce( \
            (SELECT value #>> '{}' FROM rvbbit.settings WHERE key = 'default_provider'), \
            'openrouter' \
         )",
    )
    .map_err(|e| ProviderError::Config(format!("default provider lookup failed: {e}")))?
    .map(|s| s.trim().to_string())
    .filter(|s| !s.is_empty())
    .unwrap_or_else(|| "openrouter".to_string());

    if let Ok(mut w) = default_provider_cache().write() {
        *w = Some(provider);
    }
    Ok(())
}

/// Run a chat completion against the request's provider backend.
///
/// `get_cached_spec` is SPI-free and safe on a pool worker; `load_spec`
/// falls back to an SPI read and is leader-only. The warm path pre-loads
/// every provider via `specialists::warm_operator_specs`, so pool threads
/// always hit the cache here — a miss only ever happens on the leader.
pub fn chat(req: ChatRequest) -> Result<ChatResponse, ProviderError> {
    let provider = req.provider.clone().unwrap_or_else(default_provider_name);
    let spec = match crate::specialists::get_cached_spec(&provider) {
        Some(s) => s,
        None => crate::specialists::load_spec(&provider)?,
    };

    let model = req.model;
    let input = serde_json::json!({
        "model": &model,
        "system": req.system,
        "user": req.user,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
    });

    let resp = crate::specialists::transport_for(&spec.transport)?
        .predict(&spec, std::slice::from_ref(&input))?;

    let content = resp
        .outputs
        .into_iter()
        .next()
        .map(|v| match v {
            Value::String(s) => s,
            other => other.to_string(),
        })
        .ok_or_else(|| ProviderError::BadResponse("chat transport returned no output".into()))?;
    let usage = resp.usage.into_iter().next().unwrap_or_default();

    Ok(ChatResponse {
        content,
        model,
        provider,
        transport: spec.transport.clone(),
        prompt_tokens: usage.tokens_in,
        completion_tokens: usage.tokens_out,
        latency_ms: resp.latency_ms,
        cost_usd: usage.cost_usd,
        cost_source: usage.cost_source,
        provider_request_id: usage.provider_request_id,
        provider_generation_id: usage.provider_generation_id,
        upstream_id: usage.upstream_id,
        native_tokens_in: usage.native_tokens_in,
        native_tokens_out: usage.native_tokens_out,
        reasoning_tokens: usage.reasoning_tokens,
        cached_tokens: usage.cached_tokens,
        raw_usage: usage.raw,
    })
}
