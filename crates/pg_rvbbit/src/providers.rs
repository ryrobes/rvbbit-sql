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

/// Max characters of an upstream provider body to surface in a PG-visible error.
const PROVIDER_ERR_BODY_MAX: usize = 500;

/// Advance over a credential-value run starting at ASCII byte `from`, stopping
/// at whitespace, common JSON/quote delimiters, or ANY non-ASCII byte (so every
/// returned index lands on a char boundary — safe to slice with).
fn secret_token_end(s: &str, from: usize) -> usize {
    let b = s.as_bytes();
    let mut j = from;
    while j < b.len() {
        match b[j] {
            0x80..=0xFF => break, // non-ASCII lead/continuation: stop on boundary
            b' ' | b'\t' | b'\r' | b'\n' | b'"' | b'\'' | b',' | b';' | b'(' | b')' | b'{'
            | b'}' | b'[' | b']' | b'<' | b'>' | b'&' | b'`' => break,
            _ => j += 1,
        }
    }
    j
}

/// Redact credential-shaped tokens (`sk-…`, `Bearer …`, `api_key=…`/`:` …) and
/// truncate an upstream provider response body before it is interpolated into a
/// Postgres-visible error. Upstream error bodies can echo the prompt (PII) and
/// leak credentials; they must never be surfaced verbatim. The HTTP status is
/// kept separately by the caller and is unaffected.
pub(crate) fn redact_body(body: &str) -> String {
    // Bound redaction cost even for pathologically large bodies.
    let window: String = body.chars().take(PROVIDER_ERR_BODY_MAX * 4).collect();
    let lower = window.to_ascii_lowercase(); // same byte layout: ASCII-only change
    let mut secrets: Vec<String> = Vec::new();

    // sk-… (OpenAI / Anthropic keys, incl. sk-ant-…, sk-proj-…)
    let mut i = 0usize;
    while let Some(rel) = lower[i..].find("sk-") {
        let start = i + rel;
        let end = secret_token_end(&window, start + 3);
        secrets.push(window[start..end].to_string());
        i = end.max(start + 3);
    }

    // Bearer <token>
    let mut i = 0usize;
    while let Some(rel) = lower[i..].find("bearer ") {
        let mut start = i + rel + "bearer ".len();
        while window.as_bytes().get(start) == Some(&b' ') {
            start += 1;
        }
        let end = secret_token_end(&window, start);
        if end > start {
            secrets.push(window[start..end].to_string());
        }
        i = end.max(start);
    }

    // api_key / api-key / apikey / access_token / authorization  (= | :)  <token>
    for marker in ["api_key", "api-key", "apikey", "access_token", "authorization"] {
        let mut i = 0usize;
        while let Some(rel) = lower[i..].find(marker) {
            let after = i + rel + marker.len();
            let b = window.as_bytes();
            let mut p = after;
            // Skip whitespace and a closing quote before the delimiter so a JSON
            // key like "api_key": … is matched, not just api_key=….
            while matches!(b.get(p), Some(&b' ') | Some(&b'"') | Some(&b'\'')) {
                p += 1;
            }
            if matches!(b.get(p), Some(&b'=') | Some(&b':')) {
                p += 1;
                while matches!(b.get(p), Some(&b' ') | Some(&b'"') | Some(&b'\'')) {
                    p += 1;
                }
                let end = secret_token_end(&window, p);
                if end > p {
                    secrets.push(window[p..end].to_string());
                }
                i = end.max(after);
            } else {
                i = after;
            }
        }
    }

    let mut redacted = window;
    for tok in secrets {
        if tok.len() >= 4 {
            redacted = redacted.replace(&tok, "***REDACTED***");
        }
    }

    // Final char-safe length cap.
    let count = redacted.chars().count();
    let mut out: String = redacted.chars().take(PROVIDER_ERR_BODY_MAX).collect();
    if count > PROVIDER_ERR_BODY_MAX {
        out.push_str("…[+truncated]");
    }
    out
}

#[derive(Debug, thiserror::Error)]
pub enum ProviderError {
    #[error("provider config missing: {0}")]
    Config(String),
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("API returned status {status}: {}", redact_body(.body))]
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

// ---------------------------------------------------------------------------
// Tool-calling chat (the agent step kind). `chat` above is single-turn; an agent
// loop needs a multi-message transcript + tool specs + the model's `tool_calls`
// back. Only OpenAI-compatible transports implement it (others return an error).
// ---------------------------------------------------------------------------

/// One message in an agent transcript. `tool_calls` echoes an assistant turn's
/// chosen calls back to the model verbatim; `tool_call_id` ties a tool-result
/// turn to the call it answers.
#[derive(Debug, Clone)]
pub struct ChatMessage {
    pub role: String, // "system" | "user" | "assistant" | "tool"
    pub content: Option<String>,
    pub tool_calls: Option<Value>,
    pub tool_call_id: Option<String>,
}

impl ChatMessage {
    pub fn system(s: impl Into<String>) -> Self {
        Self { role: "system".into(), content: Some(s.into()), tool_calls: None, tool_call_id: None }
    }
    pub fn user(s: impl Into<String>) -> Self {
        Self { role: "user".into(), content: Some(s.into()), tool_calls: None, tool_call_id: None }
    }
}

/// A tool advertised to the model (name + JSON-schema parameters).
#[derive(Debug, Clone)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}

/// A tool call the model chose to make.
#[derive(Debug, Clone)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Value, // parsed object ({} if the model emitted invalid JSON)
}

/// Response from one tool-calling chat turn.
#[derive(Debug)]
pub struct ChatToolsResponse {
    pub content: Option<String>,
    pub tool_calls: Vec<ToolCall>,
    pub raw_tool_calls: Option<Value>, // echoed verbatim into the next assistant message
    pub finish_reason: Option<String>,
    pub model: String,
    pub provider: String,
    pub prompt_tokens: i32,
    pub completion_tokens: i32,
    pub cost_usd: Option<f64>,
    /// Filled by the transport; consumed by the v0.1 per-turn cost reconciler
    /// (the v0 aggregate sums inline cost and labels itself "agent").
    #[allow(dead_code)]
    pub cost_source: Option<String>,
    pub provider_generation_id: Option<String>,
    /// As above — the provider's exact token/cost breakdown, kept for reconciliation.
    #[allow(dead_code)]
    pub raw_usage: Option<Value>,
    pub latency_ms: i32,
}

/// Tool-calling chat against the request's provider. Mirrors `chat`'s spec
/// resolution, then dispatches to the transport's `chat_with_tools`.
pub fn chat_with_tools(
    model: &str,
    provider: Option<&str>,
    messages: &[ChatMessage],
    tools: &[ToolSpec],
) -> Result<ChatToolsResponse, ProviderError> {
    let provider = provider.map(|s| s.to_string()).unwrap_or_else(default_provider_name);
    let spec = match crate::specialists::get_cached_spec(&provider) {
        Some(s) => s,
        None => crate::specialists::load_spec(&provider)?,
    };
    let mut resp = crate::specialists::transport_for(&spec.transport)?
        .chat_with_tools(&spec, model, messages, tools)?;
    if resp.model.is_empty() {
        resp.model = model.to_string();
    }
    if resp.provider.is_empty() {
        resp.provider = provider;
    }
    Ok(resp)
}

#[cfg(test)]
mod redact_tests {
    use super::redact_body;

    #[test]
    fn masks_credentials_and_truncates() {
        // sk- keys, Bearer tokens, and api_key=/: values are masked.
        let body = r#"{"error":"bad key sk-ant-abc123XYZ","auth":"Bearer tok_9f8e7d6c"}"#;
        let out = redact_body(body);
        assert!(!out.contains("sk-ant-abc123XYZ"), "sk- key leaked: {out}");
        assert!(!out.contains("tok_9f8e7d6c"), "bearer token leaked: {out}");
        assert!(out.contains("***REDACTED***"));

        let out2 = redact_body(r#"{"api_key": "supersecretvalue123", "x": 1}"#);
        assert!(!out2.contains("supersecretvalue123"), "api_key value leaked: {out2}");

        // Long bodies are truncated with a marker.
        let long = "x".repeat(5000);
        let out3 = redact_body(&long);
        assert!(out3.chars().count() <= super::PROVIDER_ERR_BODY_MAX + 16);
        assert!(out3.ends_with("…[+truncated]"));

        // Non-secret text is preserved; multibyte input never panics.
        let out4 = redact_body("rate limit exceeded for café ☕ — retry later");
        assert!(out4.contains("rate limit exceeded"));
        assert!(!out4.contains("***REDACTED***"));
    }
}
