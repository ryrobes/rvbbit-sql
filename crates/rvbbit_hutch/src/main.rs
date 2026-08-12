//! rvbbit-hutch — the managed-warren gateway (docs/HUTCH_PLAN.md).
//!
//! A smart reverse-proxy with auth: pg_rvbbit's specialist dispatch speaks
//! its native predict contract (POST {url}, Bearer key, {"inputs":[...]} →
//! {"outputs":[...]}) to routes here; the hutch authenticates the key to a
//! tenant, checks entitlements, enforces per-tenant lanes, forwards to the
//! model backend (the zoo) and meters the call. Client install is metadata
//! only: rvbbit.backends rows pointing at /b/{name}/predict with the key in
//! an env var — no extension changes, no heartbeats (external backends are
//! callable by definition in warren_backend_status).
//!
//!   rvbbit-hutch --config hutch.yaml         run the gateway
//!   rvbbit-hutch hash-key <raw-key>          print sha256 for tenants.yaml
//!
//! Middleware order is the trust spine: key → tenant → entitlement → lane →
//! forward → meter. Every non-200 is a stable code + human message because
//! those bodies land in customer receipts.

mod config;
mod error;
mod lanes;
mod meter;
mod polar;
mod proxy;
mod tenants;

use axum::extract::{DefaultBodyLimit, Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine as _;
use futures_util::StreamExt;
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use config::{Adapter, HutchConfig, Upstream};
use error::HutchError;
use lanes::LaneRegistry;
use meter::{Meter, MeterRow, ProviderAttempt, ProviderPriceSnapshot};
use proxy::{forward, ForwardErr};
use tenants::{TenantStatus, TenantStore};

const OPENROUTER_APP_URL: &str = "https://rvbbit.ai";
const OPENROUTER_APP_TITLE: &str = "Clover (RVBBIT)";
const MAX_SSE_OBSERVER_BUFFER: usize = 1024 * 1024;
static INVOCATION_SEQUENCE: AtomicU64 = AtomicU64::new(1);

struct AppState {
    cfg: HutchConfig,
    /// Fully resolved at startup (env override + config-relative rules).
    tenants_path: String,
    tenants: RwLock<TenantStore>,
    polar_sync: Option<polar::PolarSync>,
    lanes: LaneRegistry,
    meter: Meter,
    http: reqwest::Client,
}

fn new_invocation_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let sequence = INVOCATION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("hutch-{nanos:x}-{sequence:x}")
}

#[derive(Debug, Clone, Default, PartialEq)]
struct ProviderUsageObservation {
    request_id: Option<String>,
    upstream_model: Option<String>,
    prompt_tokens: Option<i64>,
    completion_tokens: Option<i64>,
    provider_cost_nanousd: Option<i64>,
}

fn numeric_value(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse::<f64>().ok())
        .filter(|number| number.is_finite())
}

fn usd_to_nanousd(value: &Value) -> Option<i64> {
    let usd = numeric_value(value)?;
    if usd < 0.0 {
        return None;
    }
    let nanos = (usd * 1_000_000_000.0).round();
    (nanos <= i64::MAX as f64).then_some(nanos as i64)
}

fn provider_usage_from_response(value: &Value) -> ProviderUsageObservation {
    ProviderUsageObservation {
        request_id: value.get("id").and_then(Value::as_str).map(str::to_string),
        upstream_model: value
            .get("model")
            .and_then(Value::as_str)
            .map(str::to_string),
        prompt_tokens: value
            .pointer("/usage/prompt_tokens")
            .and_then(Value::as_i64),
        completion_tokens: value
            .pointer("/usage/completion_tokens")
            .and_then(Value::as_i64),
        provider_cost_nanousd: value.pointer("/usage/cost").and_then(usd_to_nanousd),
    }
}

fn merge_provider_usage(target: &mut ProviderUsageObservation, update: ProviderUsageObservation) {
    if update.request_id.is_some() {
        target.request_id = update.request_id;
    }
    if update.upstream_model.is_some() {
        target.upstream_model = update.upstream_model;
    }
    if update.prompt_tokens.is_some() {
        target.prompt_tokens = update.prompt_tokens;
    }
    if update.completion_tokens.is_some() {
        target.completion_tokens = update.completion_tokens;
    }
    if update.provider_cost_nanousd.is_some() {
        target.provider_cost_nanousd = update.provider_cost_nanousd;
    }
}

fn llm_provider(llm: &config::LlmCfg) -> String {
    if let Some(provider) = llm
        .provider
        .as_deref()
        .map(str::trim)
        .filter(|provider| !provider.is_empty())
    {
        return provider.to_ascii_lowercase();
    }
    reqwest::Url::parse(&llm.upstream_base)
        .ok()
        .and_then(|url| url.host_str().map(str::to_ascii_lowercase))
        .map(|host| {
            if host == "openrouter.ai" || host.ends_with(".openrouter.ai") {
                "openrouter".to_string()
            } else {
                host
            }
        })
        .unwrap_or_else(|| "generic-openai".to_string())
}

fn reference_cost_nanousd(
    llm: &config::LlmCfg,
    prompt_tokens: Option<i64>,
    completion_tokens: Option<i64>,
) -> i64 {
    // A configured micro-USD / 1k-token rate is numerically identical to
    // nano-USD / token, so this multiplication stays exact and integer-only.
    prompt_tokens
        .unwrap_or_default()
        .max(0)
        .saturating_mul(llm.prompt_microusd_per_1k)
        .saturating_add(
            completion_tokens
                .unwrap_or_default()
                .max(0)
                .saturating_mul(llm.completion_microusd_per_1k),
        )
}

struct ProviderAttemptContext<'a> {
    tenant: &'a str,
    invocation_id: &'a str,
    attempt_no: usize,
    mode: &'a str,
    status: u16,
    cost_source: &'a str,
}

fn record_provider_attempt(
    state: &AppState,
    llm: &config::LlmCfg,
    observation: &ProviderUsageObservation,
    context: ProviderAttemptContext<'_>,
) {
    let provider = llm_provider(llm);
    state.meter.record_provider_attempt(ProviderAttempt {
        invocation_id: context.invocation_id,
        tenant: context.tenant,
        canonical_model: &llm.name,
        provider: &provider,
        upstream_model: observation
            .upstream_model
            .as_deref()
            .unwrap_or(&llm.upstream_model),
        provider_request_id: observation.request_id.as_deref(),
        attempt_no: context.attempt_no,
        mode: context.mode,
        status: context.status,
        prompt_tokens: observation.prompt_tokens,
        completion_tokens: observation.completion_tokens,
        provider_cost_nanousd: observation.provider_cost_nanousd,
        cost_source: context.cost_source,
        reconcile: is_openrouter_upstream(llm),
    });
}

#[derive(Default)]
struct OpenAiSseUsageObserver {
    buffer: Vec<u8>,
    usage: ProviderUsageObservation,
}

impl OpenAiSseUsageObserver {
    fn observe(&mut self, bytes: &[u8]) {
        self.buffer.extend_from_slice(bytes);
        while let Some(newline) = self.buffer.iter().position(|byte| *byte == b'\n') {
            let mut line: Vec<u8> = self.buffer.drain(..=newline).collect();
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            self.observe_line(&line);
        }
        if self.buffer.len() > MAX_SSE_OBSERVER_BUFFER {
            tracing::warn!(
                bytes = self.buffer.len(),
                "discarding oversized SSE accounting line"
            );
            self.buffer.clear();
        }
    }

    fn observe_line(&mut self, line: &[u8]) {
        let Some(data) = line.strip_prefix(b"data:") else {
            return;
        };
        let data = data.strip_prefix(b" ").unwrap_or(data);
        if data.is_empty() || data == b"[DONE]" {
            return;
        }
        if let Ok(value) = serde_json::from_slice::<Value>(data) {
            merge_provider_usage(&mut self.usage, provider_usage_from_response(&value));
        }
    }

    fn finish(&mut self) {
        if !self.buffer.is_empty() {
            let line = std::mem::take(&mut self.buffer);
            self.observe_line(&line);
        }
    }
}

struct StreamingMeterGuard {
    state: Arc<AppState>,
    llm: config::LlmCfg,
    tenant: String,
    invocation_id: String,
    label: String,
    started: Instant,
    observer: OpenAiSseUsageObserver,
    error_code: Option<&'static str>,
    recorded: bool,
}

impl StreamingMeterGuard {
    fn observe(&mut self, bytes: &[u8]) {
        self.observer.observe(bytes);
    }

    fn mark_transport_error(&mut self) {
        self.error_code = Some("stream_error");
    }

    fn finish(&mut self) {
        self.observer.finish();
        self.record(None);
    }

    fn record(&mut self, fallback_error: Option<&'static str>) {
        if self.recorded {
            return;
        }
        self.recorded = true;
        let error_code = self.error_code.or(fallback_error);
        let duration_ms = self.started.elapsed().as_secs_f64() * 1000.0;
        let cost_source = if self.observer.usage.provider_cost_nanousd.is_some() {
            "inline_stream"
        } else {
            "pending"
        };
        record_provider_attempt(
            &self.state,
            &self.llm,
            &self.observer.usage,
            ProviderAttemptContext {
                tenant: &self.tenant,
                invocation_id: &self.invocation_id,
                attempt_no: 1,
                mode: "stream",
                status: if error_code.is_some() { 499 } else { 200 },
                cost_source,
            },
        );
        self.state.meter.record(MeterRow {
            invocation_id: Some(&self.invocation_id),
            tenant: &self.tenant,
            backend: &self.label,
            stream: true,
            n_inputs: 1,
            status: 200,
            error_code,
            duration_ms,
            upstream_ms: Some(duration_ms),
            model_version: &self.llm.model_version,
            reference_cost_nanousd: reference_cost_nanousd(
                &self.llm,
                self.observer.usage.prompt_tokens,
                self.observer.usage.completion_tokens,
            ),
            prompt_tokens: self.observer.usage.prompt_tokens,
            completion_tokens: self.observer.usage.completion_tokens,
        });
    }
}

impl Drop for StreamingMeterGuard {
    fn drop(&mut self) {
        self.record(Some("stream_interrupted"));
    }
}

/// HUTCH_TENANTS env wins; otherwise a relative tenants_file resolves
/// against the config file's own directory.
fn resolve_tenants_path(config_path: &str, tenants_file: &str) -> String {
    if let Ok(env_path) = std::env::var("HUTCH_TENANTS") {
        return env_path;
    }
    let p = std::path::Path::new(tenants_file);
    if p.is_absolute() {
        return tenants_file.to_string();
    }
    std::path::Path::new(config_path)
        .parent()
        .unwrap_or_else(|| std::path::Path::new("."))
        .join(p)
        .to_string_lossy()
        .into_owned()
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "rvbbit_hutch=info,info".into()),
        )
        .init();

    let mut args = std::env::args().skip(1);
    let mut config_path =
        std::env::var("HUTCH_CONFIG").unwrap_or_else(|_| "hutch.yaml".to_string());
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "hash-key" => {
                let raw = args.next().unwrap_or_else(|| {
                    eprintln!("usage: rvbbit-hutch hash-key <raw-key>");
                    std::process::exit(2);
                });
                println!("{}", tenants::hash_key(&raw));
                return;
            }
            "--config" => {
                config_path = args.next().unwrap_or_else(|| {
                    eprintln!("--config requires a path");
                    std::process::exit(2);
                });
            }
            other => {
                eprintln!("unknown arg: {other}");
                std::process::exit(2);
            }
        }
    }

    let cfg = HutchConfig::load(&config_path).unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(1);
    });
    // tenants_file is config, so a relative path resolves against the config
    // file's directory (meter_db is data and stays relative to the workdir —
    // that's where the volume mounts).
    let tenants_path = resolve_tenants_path(&config_path, &cfg.tenants_file);
    let store = TenantStore::load(&tenants_path).unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(1);
    });
    let meter = Meter::open(&cfg.meter_db).unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(1);
    });
    tracing::info!(
        backends = cfg.backends.len(),
        tenants = store.len(),
        upstream = match &cfg.upstream {
            Upstream::Mock => "mock".to_string(),
            Upstream::Proxy { base_url } => base_url.clone(),
        },
        "hutch starting on {}",
        cfg.bind
    );

    let max_body = cfg.max_body_bytes;
    let bind = cfg.bind.clone();
    let polar_sync = cfg.polar.clone().map(polar::PolarSync::new);
    if polar_sync.is_some() {
        tracing::info!("polar billing sync enabled");
    }
    let state = Arc::new(AppState {
        cfg,
        tenants_path,
        tenants: RwLock::new(store),
        polar_sync,
        lanes: LaneRegistry::default(),
        meter,
        http: reqwest::Client::new(),
    });
    tokio::spawn(provider_maintenance_loop(state.clone()));

    let app = Router::new()
        .route("/", get(health))
        .route("/healthz", get(health))
        .route("/metrics", get(metrics))
        .route("/admin/reload-tenants", post(reload_tenants))
        .route("/b/{backend}/predict", post(predict))
        .route("/v1/models", get(list_models))
        .route("/v1/embeddings", post(embeddings))
        .route("/v1/chat/completions", post(chat_completions))
        .layer(DefaultBodyLimit::max(max_body))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&bind)
        .await
        .unwrap_or_else(|e| {
            eprintln!("cannot bind {bind}: {e}");
            std::process::exit(1);
        });
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutting down");
        })
        .await
        .expect("server error");
}

async fn health(State(state): State<Arc<AppState>>) -> Json<Value> {
    Json(json!({
        "ok": true,
        "service": "rvbbit-hutch",
        "version": env!("CARGO_PKG_VERSION"),
        "backends": state.cfg.backends.iter().map(|b| b.name.clone()).collect::<Vec<_>>(),
    }))
}

/// Bearer key from Authorization or X-Rvbbit-Token (the hare lesson: some
/// front doors eat Authorization, so both are first-class).
fn extract_key(headers: &HeaderMap) -> Option<String> {
    if let Some(auth) = headers.get("authorization").and_then(|v| v.to_str().ok()) {
        if let Some(k) = auth
            .strip_prefix("Bearer ")
            .or_else(|| auth.strip_prefix("bearer "))
        {
            if !k.trim().is_empty() {
                return Some(k.trim().to_string());
            }
        }
    }
    headers
        .get("x-rvbbit-token")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// key → tenant: static store first (dev fixtures, manual grants), then the
/// Polar validate-on-first-sight cache. A NotGranted polar tenant is
/// returned as-is (status Expired) so route handlers refuse it with the
/// metered subscription_expired path.
async fn authenticate(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<tenants::Tenant, HutchError> {
    let key = extract_key(headers).ok_or_else(HutchError::invalid_key)?;
    {
        let store = state.tenants.read().expect("tenant store poisoned");
        if let Some(t) = store.lookup(&key) {
            return Ok(t.clone());
        }
    }
    if let Some(sync) = &state.polar_sync {
        match sync.lookup(&state.http, &key).await {
            polar::PolarLookup::Tenant(t) | polar::PolarLookup::NotGranted(t) => return Ok(t),
            polar::PolarLookup::Unknown => {}
            polar::PolarLookup::Unavailable(e) => {
                tracing::error!("polar unavailable during first-sight validation: {e}");
                return Err(HutchError::upstream(
                    "billing",
                    "key validation temporarily unavailable — retry shortly".into(),
                ));
            }
        }
    }
    Err(HutchError::invalid_key())
}

#[allow(clippy::result_large_err)]
fn admin_gate(state: &AppState, headers: &HeaderMap) -> Result<(), Response> {
    match &state.cfg.admin_token {
        None => Ok(()),
        Some(want) => match extract_key(headers) {
            Some(got) if &got == want => Ok(()),
            _ => Err((StatusCode::UNAUTHORIZED, "admin token required\n").into_response()),
        },
    }
}

async fn metrics(State(state): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    if let Err(resp) = admin_gate(&state, &headers) {
        return resp;
    }
    let body = state.meter.render_prometheus(&state.lanes.snapshot());
    ([("content-type", "text/plain; version=0.0.4")], body).into_response()
}

async fn reload_tenants(State(state): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    if let Err(resp) = admin_gate(&state, &headers) {
        return resp;
    }
    match TenantStore::load(&state.tenants_path) {
        Ok(store) => {
            let n = store.len();
            *state.tenants.write().expect("tenant store poisoned") = store;
            tracing::info!(tenants = n, "tenants reloaded");
            Json(json!({"ok": true, "tenants": n})).into_response()
        }
        Err(e) => {
            tracing::error!("tenants reload failed, keeping previous store: {e}");
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"ok": false, "error": e})),
            )
                .into_response()
        }
    }
}

#[derive(Deserialize)]
struct PredictIn {
    inputs: Vec<Value>,
}

#[derive(Deserialize)]
struct EmbeddingsIn {
    #[serde(default)]
    model: Option<String>,
    input: Value,
    #[serde(default)]
    encoding_format: Option<String>,
}

#[derive(Debug, Default, PartialEq)]
struct SpecialistUsage {
    seen: bool,
    prompt_tokens: i64,
    completion_tokens: i64,
    cost_microusd: i64,
}

/// Predict-shaped specialist responses may carry their own nested model
/// usage. This lets composed backends such as web_research preserve the
/// actual token/provider-cost receipt while ordinary fixed-cost specialists
/// continue using unit_microusd unchanged.
fn specialist_usage(outputs: &[Value]) -> SpecialistUsage {
    let mut total = SpecialistUsage::default();
    for output in outputs {
        let Some(usage) = output.get("usage").and_then(Value::as_object) else {
            continue;
        };
        total.seen = true;
        total.prompt_tokens = total.prompt_tokens.saturating_add(
            usage
                .get("prompt_tokens")
                .and_then(Value::as_i64)
                .unwrap_or_default(),
        );
        total.completion_tokens = total.completion_tokens.saturating_add(
            usage
                .get("completion_tokens")
                .and_then(Value::as_i64)
                .unwrap_or_default(),
        );
        total.cost_microusd = total.cost_microusd.saturating_add(
            usage
                .get("cost_microusd")
                .and_then(Value::as_i64)
                .unwrap_or_default(),
        );
    }
    total
}

async fn predict(
    State(state): State<Arc<AppState>>,
    Path(backend_name): Path<String>,
    headers: HeaderMap,
    body: Result<Json<PredictIn>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let t0 = Instant::now();

    // 1. key → tenant
    let tenant = match authenticate(&state, &headers).await {
        Ok(t) => t,
        Err(e) => return e.into_response(),
    };

    // 2. backend + entitlement — checked before touching the body so the
    //    error story is stable regardless of payload.
    let backend = match state.cfg.backend(&backend_name) {
        Some(b) => b.clone(),
        None => return HutchError::unknown_backend(&backend_name).into_response(),
    };
    let refuse = |err: HutchError, code: &'static str, state: &AppState| {
        state.meter.record(MeterRow {
            invocation_id: None,
            tenant: &tenant.id,
            backend: &backend_name,
            stream: false,
            n_inputs: 0,
            status: err.status.as_u16(),
            error_code: Some(code),
            duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
            upstream_ms: None,
            model_version: &backend.model_version,
            reference_cost_nanousd: 0,
            prompt_tokens: None,
            completion_tokens: None,
        });
        err.into_response()
    };
    if tenant.status == TenantStatus::Expired {
        return refuse(
            HutchError::subscription_expired(&tenant.id),
            "subscription_expired",
            &state,
        );
    }
    if !tenant
        .entitlements
        .iter()
        .any(|e| e == &backend.entitlement)
    {
        return refuse(
            HutchError::not_entitled(&tenant.id, &backend.name, &backend.entitlement),
            "not_entitled",
            &state,
        );
    }

    // 3. body
    let inputs = match body {
        Ok(Json(p)) => p.inputs,
        Err(e) => return HutchError::bad_request(e.body_text()).into_response(),
    };
    if inputs.is_empty() {
        return Json(json!({"outputs": []})).into_response();
    }
    let n = inputs.len();

    // 4. lane — skipped for unlaned backends (cheap encoders: batching is
    // already the throttle; lanes price generation, not classification).
    let _permit = if backend.unlaned {
        None
    } else {
        match state
            .lanes
            .acquire(&tenant.id, tenant.lanes, state.cfg.lane_grace_ms)
            .await
        {
            Some(p) => Some(p),
            None => {
                return refuse(
                    HutchError::lanes_saturated(&tenant.id, tenant.lanes),
                    "lanes_saturated",
                    &state,
                )
            }
        }
    };

    // 5. forward
    let tenant_scope = tenants::hash_key(&format!("clover-research:{}", tenant.id));
    let fwd = forward(
        &state.http,
        &state.cfg.upstream,
        &backend,
        &inputs,
        Some(&tenant_scope),
    )
    .await;
    let duration_ms = t0.elapsed().as_secs_f64() * 1000.0;
    match fwd {
        Ok(ok) => {
            let usage = specialist_usage(&ok.outputs);
            state.meter.record(MeterRow {
                invocation_id: None,
                tenant: &tenant.id,
                backend: &backend.name,
                stream: false,
                n_inputs: n,
                status: 200,
                error_code: None,
                duration_ms,
                upstream_ms: Some(ok.upstream_ms),
                model_version: &backend.model_version,
                reference_cost_nanousd: if usage.seen {
                    usage.cost_microusd.saturating_mul(1000)
                } else {
                    backend
                        .unit_microusd
                        .saturating_mul(n as i64)
                        .saturating_mul(1000)
                },
                prompt_tokens: usage.seen.then_some(usage.prompt_tokens),
                completion_tokens: usage.seen.then_some(usage.completion_tokens),
            });
            // Extra fields are ignored by pg_rvbbit's PredictResponse parse;
            // humans with curl get the provenance breadcrumb in-band.
            let mut resp = Json(json!({
                "outputs": ok.outputs,
                "hutch": {"backend": backend.name, "model_version": backend.model_version, "n": n},
            }))
            .into_response();
            if let Ok(v) = axum::http::HeaderValue::from_str(&backend.model_version) {
                resp.headers_mut().insert("x-hutch-model-version", v);
            }
            resp
        }
        Err(e) => {
            let (err, code) = match e {
                ForwardErr::Status { status, body_head } => (
                    HutchError::upstream(&backend.name, format!("HTTP {status}: {body_head}")),
                    "upstream_error",
                ),
                ForwardErr::Transport(detail) => (
                    HutchError::upstream(&backend.name, detail),
                    "upstream_error",
                ),
            };
            state.meter.record(MeterRow {
                invocation_id: None,
                tenant: &tenant.id,
                backend: &backend.name,
                stream: false,
                n_inputs: n,
                status: err.status.as_u16(),
                error_code: Some(code),
                duration_ms,
                upstream_ms: None,
                model_version: &backend.model_version,
                reference_cost_nanousd: 0,
                prompt_tokens: None,
                completion_tokens: None,
            });
            err.into_response()
        }
    }
}

fn normalize_embedding_inputs(input: Value) -> Result<Vec<Value>, String> {
    match input {
        Value::String(_) => Ok(vec![input]),
        Value::Array(items) if items.iter().all(Value::is_string) => Ok(items),
        Value::Array(_) => Err(
            "embeddings input must be a string or an array of strings; token arrays are not supported"
                .into(),
        ),
        _ => Err("embeddings input must be a string or an array of strings".into()),
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum EmbeddingEncoding {
    Float,
    Base64,
}

fn embedding_encoding(value: Option<&str>) -> Result<EmbeddingEncoding, String> {
    match value.map(str::trim).filter(|value| !value.is_empty()) {
        None | Some("float") => Ok(EmbeddingEncoding::Float),
        Some("base64") => Ok(EmbeddingEncoding::Base64),
        Some(other) => Err(format!(
            "embeddings encoding_format must be 'float' or 'base64', got '{other}'"
        )),
    }
}

fn encode_embedding(value: Value, encoding: EmbeddingEncoding) -> Result<Value, String> {
    if encoding == EmbeddingEncoding::Float {
        return Ok(value);
    }
    let values = value
        .as_array()
        .ok_or_else(|| "embedding backend returned a non-array vector".to_string())?;
    let mut bytes = Vec::with_capacity(values.len() * std::mem::size_of::<f32>());
    for item in values {
        let number = item
            .as_f64()
            .filter(|number| number.is_finite())
            .ok_or_else(|| "embedding backend returned a non-numeric vector value".to_string())?;
        bytes.extend_from_slice(&(number as f32).to_le_bytes());
    }
    Ok(Value::String(BASE64_STANDARD.encode(bytes)))
}

fn embedding_data(outputs: Vec<Value>, encoding: EmbeddingEncoding) -> Result<Vec<Value>, String> {
    outputs
        .into_iter()
        .enumerate()
        .map(|(index, embedding)| {
            Ok(json!({
                "object": "embedding",
                "embedding": encode_embedding(embedding, encoding)?,
                "index": index,
            }))
        })
        .collect()
}

/// OpenAI-compatible embedding surface for managed consumers such as
/// Hindsight. It is the same authenticated, entitled and metered `embed`
/// backend used by semantic SQL; this is only a second wire shape.
async fn embeddings(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: Result<Json<EmbeddingsIn>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let t0 = Instant::now();
    let tenant = match authenticate(&state, &headers).await {
        Ok(t) => t,
        Err(e) => return e.into_response(),
    };
    let body = match body {
        Ok(Json(value)) => value,
        Err(e) => return HutchError::bad_request(e.body_text()).into_response(),
    };
    let backend_name = body
        .model
        .as_deref()
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .unwrap_or("embed")
        .to_string();
    let backend = match state.cfg.backend(&backend_name) {
        Some(backend) if backend.adapter == Adapter::OpenaiEmbeddings => backend.clone(),
        _ => return HutchError::unknown_model(&backend_name).into_response(),
    };
    let refuse = |err: HutchError, code: &'static str, state: &AppState| {
        state.meter.record(MeterRow {
            invocation_id: None,
            tenant: &tenant.id,
            backend: &backend.name,
            stream: false,
            n_inputs: 0,
            status: err.status.as_u16(),
            error_code: Some(code),
            duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
            upstream_ms: None,
            model_version: &backend.model_version,
            reference_cost_nanousd: 0,
            prompt_tokens: None,
            completion_tokens: None,
        });
        err.into_response()
    };
    if tenant.status == TenantStatus::Expired {
        return refuse(
            HutchError::subscription_expired(&tenant.id),
            "subscription_expired",
            &state,
        );
    }
    if !tenant
        .entitlements
        .iter()
        .any(|entitlement| entitlement == &backend.entitlement)
    {
        return refuse(
            HutchError::not_entitled(&tenant.id, &backend.name, &backend.entitlement),
            "not_entitled",
            &state,
        );
    }
    let encoding = match embedding_encoding(body.encoding_format.as_deref()) {
        Ok(encoding) => encoding,
        Err(detail) => return refuse(HutchError::bad_request(detail), "bad_request", &state),
    };
    let inputs = match normalize_embedding_inputs(body.input) {
        Ok(inputs) => inputs,
        Err(detail) => return refuse(HutchError::bad_request(detail), "bad_request", &state),
    };
    if inputs.is_empty() {
        return Json(json!({
            "object": "list",
            "data": [],
            "model": backend.name.clone(),
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }))
        .into_response();
    }
    let n = inputs.len();
    let _permit = if backend.unlaned {
        None
    } else {
        match state
            .lanes
            .acquire(&tenant.id, tenant.lanes, state.cfg.lane_grace_ms)
            .await
        {
            Some(permit) => Some(permit),
            None => {
                return refuse(
                    HutchError::lanes_saturated(&tenant.id, tenant.lanes),
                    "lanes_saturated",
                    &state,
                )
            }
        }
    };
    let forwarded = forward(&state.http, &state.cfg.upstream, &backend, &inputs, None).await;
    let duration_ms = t0.elapsed().as_secs_f64() * 1000.0;
    match forwarded {
        Ok(ok) => {
            let data = match embedding_data(ok.outputs, encoding) {
                Ok(data) => data,
                Err(detail) => {
                    return refuse(
                        HutchError::upstream(&backend.name, detail),
                        "upstream_error",
                        &state,
                    )
                }
            };
            state.meter.record(MeterRow {
                invocation_id: None,
                tenant: &tenant.id,
                backend: &backend.name,
                stream: false,
                n_inputs: n,
                status: 200,
                error_code: None,
                duration_ms,
                upstream_ms: Some(ok.upstream_ms),
                model_version: &backend.model_version,
                reference_cost_nanousd: backend
                    .unit_microusd
                    .saturating_mul(n as i64)
                    .saturating_mul(1000),
                prompt_tokens: None,
                completion_tokens: None,
            });
            let mut response = Json(json!({
                "object": "list",
                "data": data,
                "model": backend.name.clone(),
                "usage": ok.usage.unwrap_or_else(|| {
                    json!({"prompt_tokens": 0, "total_tokens": 0})
                }),
            }))
            .into_response();
            if let Ok(value) = axum::http::HeaderValue::from_str(&backend.model_version) {
                response
                    .headers_mut()
                    .insert("x-hutch-model-version", value);
            }
            response
        }
        Err(error) => {
            let detail = match error {
                ForwardErr::Status { status, body_head } => format!("HTTP {status}: {body_head}"),
                ForwardErr::Transport(detail) => detail,
            };
            refuse(
                HutchError::upstream(&backend.name, detail),
                "upstream_error",
                &state,
            )
        }
    }
}

// ---------------------------------------------------------------------------
// OpenAI-compatible LLM surface — one wire format serves pg_rvbbit's
// openai_chat transport, agent()/flow steps, AND raw OpenAI SDKs. Routing is
// by the request's `model` field; the hutch rewrites it to the upstream's
// served name on the way through.
// ---------------------------------------------------------------------------

async fn list_models(State(state): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let tenant = match authenticate(&state, &headers).await {
        Ok(t) => t,
        Err(e) => return e.into_response(),
    };
    let chat_models = state
        .cfg
        .llms
        .iter()
        .filter(|llm| {
            tenant
                .entitlements
                .iter()
                .any(|entitlement| entitlement == &llm.entitlement)
        })
        .map(|llm| {
            json!({
                "id": llm.name,
                "object": "model",
                "owned_by": "rvbbit-hutch",
                "meta": {"managed": true, "capability": "chat.completions"},
            })
        });
    let embedding_models = state
        .cfg
        .backends
        .iter()
        .filter(|backend| {
            backend.adapter == Adapter::OpenaiEmbeddings
                && tenant
                    .entitlements
                    .iter()
                    .any(|entitlement| entitlement == &backend.entitlement)
        })
        .map(|backend| {
            json!({
                "id": backend.name,
                "object": "model",
                "owned_by": "rvbbit-hutch",
                "meta": {
                    "managed": true,
                    "capability": "embeddings",
                    "model_version": backend.model_version,
                },
            })
        });
    let data: Vec<Value> = chat_models.chain(embedding_models).collect();
    Json(json!({"object": "list", "data": data})).into_response()
}

async fn chat_completions(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let t0 = Instant::now();
    let tenant = match authenticate(&state, &headers).await {
        Ok(t) => t,
        Err(e) => return e.into_response(),
    };
    let mut payload = match body {
        Ok(Json(v)) => v,
        Err(e) => return HutchError::bad_request(e.body_text()).into_response(),
    };
    let model = payload
        .get("model")
        .and_then(|m| m.as_str())
        .unwrap_or_default()
        .to_string();
    let llm = match state.cfg.llm(&model) {
        Some(l) => l.clone(),
        None => return HutchError::unknown_model(&model).into_response(),
    };
    let invocation_id = new_invocation_id();
    let label = format!("llm:{}", llm.name);
    // Apply server defaults before classifying the request mode so metrics
    // reflect the request Hutch actually forwards, not only caller input.
    route_llm_payload(&mut payload, &llm, &tenant.id);
    let is_stream = payload
        .get("stream")
        .and_then(|s| s.as_bool())
        .unwrap_or(false);

    let refuse = |err: HutchError, code: &'static str| {
        state.meter.record(MeterRow {
            invocation_id: Some(&invocation_id),
            tenant: &tenant.id,
            backend: &label,
            stream: is_stream,
            n_inputs: 1,
            status: err.status.as_u16(),
            error_code: Some(code),
            duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
            upstream_ms: None,
            model_version: &llm.model_version,
            reference_cost_nanousd: 0,
            prompt_tokens: None,
            completion_tokens: None,
        });
        err.into_response()
    };
    if tenant.status == TenantStatus::Expired {
        return refuse(
            HutchError::subscription_expired(&tenant.id),
            "subscription_expired",
        );
    }
    if !tenant.entitlements.iter().any(|e| e == &llm.entitlement) {
        return refuse(
            HutchError::not_entitled(&tenant.id, &llm.name, &llm.entitlement),
            "not_entitled",
        );
    }
    let permit = state
        .lanes
        .acquire(&tenant.id, tenant.lanes, state.cfg.lane_grace_ms)
        .await;
    let _permit = match permit {
        Some(p) => p,
        None => {
            return refuse(
                HutchError::lanes_saturated(&tenant.id, tenant.lanes),
                "lanes_saturated",
            )
        }
    };

    let url = format!(
        "{}/v1/chat/completions",
        llm.upstream_base.trim_end_matches('/')
    );
    let mut request = state
        .http
        .post(&url)
        .timeout(std::time::Duration::from_millis(llm.timeout_ms));
    if is_openrouter_upstream(&llm) {
        request = request
            .header("HTTP-Referer", OPENROUTER_APP_URL)
            .header("X-OpenRouter-Title", OPENROUTER_APP_TITLE)
            .header("X-Title", OPENROUTER_APP_TITLE);
    }
    match resolve_upstream_bearer_token(&llm, |name| std::env::var(name).ok()) {
        Ok(Some(token)) => request = request.bearer_auth(token),
        Ok(None) => {}
        Err(detail) => {
            return refuse(
                HutchError::upstream(&llm.name, detail),
                "upstream_not_configured",
            )
        }
    }
    let request = request.json(&payload);
    // OpenRouter occasionally returns HTTP 200 with an empty/malformed choice
    // and zero usage. Keep one clone for a bounded non-streaming retry; stream
    // bodies and valid tool-call responses are never retried.
    let retry_request = if is_stream { None } else { request.try_clone() };
    let upstream = request.send().await;
    let resp = match upstream {
        Ok(r) => r,
        Err(e) => {
            record_provider_attempt(
                &state,
                &llm,
                &ProviderUsageObservation::default(),
                ProviderAttemptContext {
                    tenant: &tenant.id,
                    invocation_id: &invocation_id,
                    attempt_no: 1,
                    mode: if is_stream { "stream" } else { "response" },
                    status: 0,
                    cost_source: "unavailable",
                },
            );
            return refuse(
                HutchError::upstream(&llm.name, e.to_string()),
                "upstream_error",
            );
        }
    };
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        let observation = serde_json::from_str::<Value>(&body)
            .ok()
            .map(|value| provider_usage_from_response(&value))
            .unwrap_or_default();
        record_provider_attempt(
            &state,
            &llm,
            &observation,
            ProviderAttemptContext {
                tenant: &tenant.id,
                invocation_id: &invocation_id,
                attempt_no: 1,
                mode: if is_stream { "stream" } else { "response" },
                status,
                cost_source: if observation.provider_cost_nanousd.is_some() {
                    "inline_error"
                } else {
                    "unavailable"
                },
            },
        );
        let head: String = body.chars().take(300).collect();
        return refuse(
            HutchError::upstream(&llm.name, format!("HTTP {status}: {head}")),
            "upstream_error",
        );
    }

    if is_stream {
        // OpenRouter includes usage and actual cost in the final SSE event.
        // Observe complete data lines while yielding every upstream byte
        // unchanged. The guard also persists a pending reconciliation row if
        // the browser disconnects before the final event arrives.
        let content_type = resp
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("text/event-stream")
            .to_string();
        let upstream_stream = resp.bytes_stream();
        let stream_state = state.clone();
        let stream_llm = llm.clone();
        let stream_tenant = tenant.id.clone();
        let stream_invocation_id = invocation_id.clone();
        let stream_label = label.clone();
        let lane_permit = _permit;
        let metered_stream = async_stream::stream! {
            let _lane_permit = lane_permit;
            let mut upstream_stream = Box::pin(upstream_stream);
            let mut guard = StreamingMeterGuard {
                state: stream_state,
                llm: stream_llm,
                tenant: stream_tenant,
                invocation_id: stream_invocation_id,
                label: stream_label,
                started: t0,
                observer: OpenAiSseUsageObserver::default(),
                error_code: None,
                recorded: false,
            };
            while let Some(item) = upstream_stream.next().await {
                match item {
                    Ok(bytes) => {
                        guard.observe(&bytes);
                        yield Ok::<_, std::io::Error>(bytes);
                    }
                    Err(error) => {
                        guard.mark_transport_error();
                        yield Err(std::io::Error::other(error));
                        break;
                    }
                }
            }
            guard.finish();
        };
        let mut builder = axum::http::Response::builder()
            .status(StatusCode::OK)
            .header("content-type", content_type);
        if let Ok(v) = axum::http::HeaderValue::from_str(&llm.model_version) {
            builder = builder.header("x-hutch-model-version", v);
        }
        return builder
            .body(axum::body::Body::from_stream(metered_stream))
            .expect("stream response build");
    }

    let mut out: Value = match resp.json().await {
        Ok(v) => v,
        Err(e) => {
            record_provider_attempt(
                &state,
                &llm,
                &ProviderUsageObservation::default(),
                ProviderAttemptContext {
                    tenant: &tenant.id,
                    invocation_id: &invocation_id,
                    attempt_no: 1,
                    mode: "response",
                    status: 502,
                    cost_source: "unavailable",
                },
            );
            return refuse(
                HutchError::upstream(&llm.name, format!("bad upstream JSON: {e}")),
                "upstream_error",
            );
        }
    };
    let first_observation = provider_usage_from_response(&out);
    record_provider_attempt(
        &state,
        &llm,
        &first_observation,
        ProviderAttemptContext {
            tenant: &tenant.id,
            invocation_id: &invocation_id,
            attempt_no: 1,
            mode: "response",
            status: 200,
            cost_source: if first_observation.provider_cost_nanousd.is_some() {
                "inline_response"
            } else {
                "pending"
            },
        },
    );
    let mut final_observation = first_observation;
    if !llm_response_has_answer(&out) {
        let Some(retry) = retry_request else {
            return refuse(
                HutchError::upstream(&llm.name, "upstream returned no answer".into()),
                "upstream_error",
            );
        };
        let retried = match retry.send().await {
            Ok(response) => response,
            Err(e) => {
                record_provider_attempt(
                    &state,
                    &llm,
                    &ProviderUsageObservation::default(),
                    ProviderAttemptContext {
                        tenant: &tenant.id,
                        invocation_id: &invocation_id,
                        attempt_no: 2,
                        mode: "response",
                        status: 0,
                        cost_source: "unavailable",
                    },
                );
                return refuse(
                    HutchError::upstream(&llm.name, format!("retry failed: {e}")),
                    "upstream_error",
                );
            }
        };
        if !retried.status().is_success() {
            let status = retried.status().as_u16();
            let body = retried.text().await.unwrap_or_default();
            let observation = serde_json::from_str::<Value>(&body)
                .ok()
                .map(|value| provider_usage_from_response(&value))
                .unwrap_or_default();
            record_provider_attempt(
                &state,
                &llm,
                &observation,
                ProviderAttemptContext {
                    tenant: &tenant.id,
                    invocation_id: &invocation_id,
                    attempt_no: 2,
                    mode: "response",
                    status,
                    cost_source: if observation.provider_cost_nanousd.is_some() {
                        "inline_error"
                    } else {
                        "unavailable"
                    },
                },
            );
            let head: String = body.chars().take(300).collect();
            return refuse(
                HutchError::upstream(&llm.name, format!("retry HTTP {status}: {head}")),
                "upstream_error",
            );
        }
        out = match retried.json().await {
            Ok(v) => v,
            Err(e) => {
                record_provider_attempt(
                    &state,
                    &llm,
                    &ProviderUsageObservation::default(),
                    ProviderAttemptContext {
                        tenant: &tenant.id,
                        invocation_id: &invocation_id,
                        attempt_no: 2,
                        mode: "response",
                        status: 502,
                        cost_source: "unavailable",
                    },
                );
                return refuse(
                    HutchError::upstream(&llm.name, format!("bad retry JSON: {e}")),
                    "upstream_error",
                );
            }
        };
        final_observation = provider_usage_from_response(&out);
        record_provider_attempt(
            &state,
            &llm,
            &final_observation,
            ProviderAttemptContext {
                tenant: &tenant.id,
                invocation_id: &invocation_id,
                attempt_no: 2,
                mode: "response",
                status: 200,
                cost_source: if final_observation.provider_cost_nanousd.is_some() {
                    "inline_response"
                } else {
                    "pending"
                },
            },
        );
        if !llm_response_has_answer(&out) {
            return refuse(
                HutchError::upstream(&llm.name, "upstream returned no answer after retry".into()),
                "upstream_error",
            );
        }
    }
    let duration_ms = t0.elapsed().as_secs_f64() * 1000.0;
    state.meter.record(MeterRow {
        invocation_id: Some(&invocation_id),
        tenant: &tenant.id,
        backend: &label,
        stream: false,
        n_inputs: 1,
        status: 200,
        error_code: None,
        duration_ms,
        upstream_ms: Some(duration_ms),
        model_version: &llm.model_version,
        reference_cost_nanousd: reference_cost_nanousd(
            &llm,
            final_observation.prompt_tokens,
            final_observation.completion_tokens,
        ),
        prompt_tokens: final_observation.prompt_tokens,
        completion_tokens: final_observation.completion_tokens,
    });
    // The OpenAI-compatible client contract names the managed Clover service,
    // not whichever upstream model currently implements it. Exact upstream
    // provenance remains in the private meter and x-hutch-model-version.
    if let Some(object) = out.as_object_mut() {
        object.insert("model".into(), Value::String(llm.name.clone()));
    }
    let mut resp_out = Json(out).into_response();
    if let Ok(v) = axum::http::HeaderValue::from_str(&llm.model_version) {
        resp_out.headers_mut().insert("x-hutch-model-version", v);
    }
    resp_out
}

fn route_llm_payload(payload: &mut Value, llm: &config::LlmCfg, tenant_id: &str) {
    let supplied_user = payload
        .get("user")
        .and_then(Value::as_str)
        .and_then(bounded_tracking_user);
    if let Some(object) = payload.as_object_mut() {
        for (key, value) in &llm.request_defaults {
            if key != "model" {
                match object.get_mut(key) {
                    Some(current) => merge_json_defaults(current, value),
                    None => {
                        object.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        for (key, value) in &llm.request_overrides {
            if key != "model" {
                match object.get_mut(key) {
                    Some(current) => merge_json_overrides(current, value),
                    None => {
                        object.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        object.insert("model".into(), json!(llm.upstream_model));
        // Preserve pg_rvbbit's human caller when present; direct Hutch clients
        // still get a stable tenant identifier. This field is provider cost
        // telemetry only and is never an entitlement/auth input in Hutch.
        if is_openrouter_upstream(llm) {
            let tracking_user = supplied_user.unwrap_or_else(|| {
                bounded_tracking_user(&format!("clover-tenant:{tenant_id}"))
                    .unwrap_or_else(|| "clover-tenant".to_string())
            });
            object.insert("user".into(), Value::String(tracking_user));
        }
    }
}

fn bounded_tracking_user(value: &str) -> Option<String> {
    let value = value.trim();
    if value.is_empty() || value.chars().count() > 254 || value.chars().any(char::is_control) {
        return None;
    }
    Some(value.to_string())
}

fn is_openrouter_upstream(llm: &config::LlmCfg) -> bool {
    llm_provider(llm) == "openrouter"
}

async fn provider_maintenance_loop(state: Arc<AppState>) {
    if state.cfg.llms.is_empty() {
        return;
    }
    let reconcile_every =
        std::time::Duration::from_secs(state.cfg.provider_cost_reconcile_secs.max(1));
    let price_every = std::time::Duration::from_secs(state.cfg.provider_price_refresh_secs.max(60));
    let mut last_price_refresh: Option<Instant> = None;
    loop {
        reconcile_pending_provider_costs(&state).await;
        if last_price_refresh.is_none_or(|last| last.elapsed() >= price_every) {
            refresh_provider_prices(&state).await;
            last_price_refresh = Some(Instant::now());
        }
        tokio::time::sleep(reconcile_every).await;
    }
}

async fn reconcile_pending_provider_costs(state: &AppState) {
    for pending in state.meter.pending_provider_costs(25) {
        if pending.provider != "openrouter" {
            state.meter.defer_provider_cost(
                pending.row_id,
                "no reconciliation adapter is configured for this provider",
            );
            continue;
        }
        let Some(llm) = state.cfg.llm(&pending.canonical_model) else {
            state
                .meter
                .defer_provider_cost(pending.row_id, "canonical model is no longer configured");
            continue;
        };
        let token = match resolve_upstream_bearer_token(llm, |name| std::env::var(name).ok()) {
            Ok(Some(token)) => token,
            Ok(None) => {
                state
                    .meter
                    .defer_provider_cost(pending.row_id, "provider credential is not configured");
                continue;
            }
            Err(detail) => {
                state.meter.defer_provider_cost(pending.row_id, &detail);
                continue;
            }
        };
        let url = format!("{}/v1/generation", llm.upstream_base.trim_end_matches('/'));
        let response = state
            .http
            .get(url)
            .bearer_auth(token)
            .query(&[("id", pending.provider_request_id.as_str())])
            .timeout(std::time::Duration::from_secs(30))
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(error) => {
                state
                    .meter
                    .defer_provider_cost(pending.row_id, &error.to_string());
                continue;
            }
        };
        let status = response.status();
        let value: Value = match response.json().await {
            Ok(value) => value,
            Err(error) => {
                state.meter.defer_provider_cost(
                    pending.row_id,
                    &format!("generation metadata HTTP {status}: {error}"),
                );
                continue;
            }
        };
        if !status.is_success() {
            let detail = value
                .pointer("/error/message")
                .and_then(Value::as_str)
                .unwrap_or("generation metadata unavailable");
            state.meter.defer_provider_cost(
                pending.row_id,
                &format!("generation metadata HTTP {status}: {detail}"),
            );
            continue;
        }
        let data = value.get("data").unwrap_or(&value);
        let cost = data
            .get("total_cost")
            .or_else(|| data.get("usage"))
            .and_then(usd_to_nanousd);
        let Some(cost) = cost else {
            state
                .meter
                .defer_provider_cost(pending.row_id, "generation metadata omitted total cost");
            continue;
        };
        let prompt_tokens = data
            .get("native_tokens_prompt")
            .or_else(|| data.get("tokens_prompt"))
            .and_then(Value::as_i64);
        let completion_tokens = data
            .get("native_tokens_completion")
            .or_else(|| data.get("tokens_completion"))
            .and_then(Value::as_i64);
        state
            .meter
            .reconcile_provider_cost(&pending, prompt_tokens, completion_tokens, cost);
        tracing::debug!(
            provider = pending.provider,
            upstream_model = pending.upstream_model,
            "reconciled provider cost"
        );
    }
}

async fn refresh_provider_prices(state: &AppState) {
    let Some(anchor) = state
        .cfg
        .llms
        .iter()
        .find(|llm| is_openrouter_upstream(llm))
    else {
        return;
    };
    let token = match resolve_upstream_bearer_token(anchor, |name| std::env::var(name).ok()) {
        Ok(Some(token)) => token,
        Ok(None) => return,
        Err(detail) => {
            tracing::warn!("provider price refresh skipped: {detail}");
            return;
        }
    };
    let url = format!("{}/v1/models", anchor.upstream_base.trim_end_matches('/'));
    let response = state
        .http
        .get(url)
        .bearer_auth(token)
        .query(&[("limit", "500")])
        .timeout(std::time::Duration::from_secs(30))
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(error) => {
            tracing::warn!("provider price refresh failed: {error}");
            return;
        }
    };
    let status = response.status();
    let value: Value = match response.json().await {
        Ok(value) => value,
        Err(error) => {
            tracing::warn!("provider price refresh HTTP {status} returned bad JSON: {error}");
            return;
        }
    };
    if !status.is_success() {
        let detail = value
            .pointer("/error/message")
            .and_then(Value::as_str)
            .unwrap_or("catalog unavailable");
        tracing::warn!("provider price refresh HTTP {status}: {detail}");
        return;
    }
    let Some(models) = value.get("data").and_then(Value::as_array) else {
        tracing::warn!("provider price refresh omitted the model list");
        return;
    };
    for llm in state
        .cfg
        .llms
        .iter()
        .filter(|llm| is_openrouter_upstream(llm))
    {
        let public_upstream_model = llm.upstream_model.trim_start_matches('~');
        let Some((model, alias_resolved)) = openrouter_catalog_model(models, public_upstream_model)
        else {
            tracing::warn!(
                model = public_upstream_model,
                "provider price catalog has no exact model entry"
            );
            continue;
        };
        let prompt = model.pointer("/pricing/prompt").and_then(numeric_value);
        let completion = model.pointer("/pricing/completion").and_then(numeric_value);
        let (Some(prompt), Some(completion)) = (prompt, completion) else {
            tracing::warn!(
                model = public_upstream_model,
                "provider price catalog entry omitted token rates"
            );
            continue;
        };
        state.meter.record_provider_price(ProviderPriceSnapshot {
            provider: "openrouter",
            upstream_model: public_upstream_model,
            prompt_usd_per_token: prompt,
            completion_usd_per_token: completion,
            source: if alias_resolved {
                "openrouter-models-alias"
            } else {
                "openrouter-models"
            },
        });
        if alias_resolved {
            tracing::debug!(
                model = public_upstream_model,
                resolved_model = model
                    .get("id")
                    .and_then(|value| value.as_str())
                    .unwrap_or("unknown"),
                "resolved provider price alias"
            );
        }
    }
}

/// OpenRouter's convenience `-latest` aliases are accepted for generation but
/// are not separate `/models` records. Resolve those only when the catalog has
/// one unambiguous newest model in the same named family. The configured alias
/// remains the gauge key; this is advisory pricing, never invoice accounting.
fn openrouter_catalog_model<'a>(models: &'a [Value], requested: &str) -> Option<(&'a Value, bool)> {
    if let Some(exact) = models.iter().find(|model| {
        model.get("id").and_then(Value::as_str) == Some(requested)
            || model.get("canonical_slug").and_then(Value::as_str) == Some(requested)
    }) {
        return Some((exact, false));
    }
    let family_prefix = requested.strip_suffix("latest")?;
    models
        .iter()
        .filter(|model| {
            model
                .get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| id.starts_with(family_prefix))
        })
        .max_by_key(|model| model.get("created").and_then(Value::as_i64).unwrap_or(0))
        .map(|model| (model, true))
}

fn llm_response_has_answer(out: &Value) -> bool {
    let Some(message) = out.pointer("/choices/0/message") else {
        return false;
    };
    let has_content = match message.get("content") {
        Some(Value::String(_)) => true,
        Some(Value::Array(parts)) => !parts.is_empty(),
        _ => false,
    };
    let has_tool_calls = message
        .get("tool_calls")
        .and_then(Value::as_array)
        .is_some_and(|calls| !calls.is_empty());
    let has_refusal = message
        .get("refusal")
        .and_then(Value::as_str)
        .is_some_and(|refusal| !refusal.is_empty());
    has_content || has_tool_calls || has_refusal
}

fn merge_json_defaults(target: &mut Value, defaults: &Value) {
    let (Some(target), Some(defaults)) = (target.as_object_mut(), defaults.as_object()) else {
        return;
    };
    for (key, value) in defaults {
        match target.get_mut(key) {
            Some(current) => merge_json_defaults(current, value),
            None => {
                target.insert(key.clone(), value.clone());
            }
        }
    }
}

fn merge_json_overrides(target: &mut Value, overrides: &Value) {
    match (target.as_object_mut(), overrides.as_object()) {
        (Some(target), Some(overrides)) => {
            for (key, value) in overrides {
                match target.get_mut(key) {
                    Some(current) => merge_json_overrides(current, value),
                    None => {
                        target.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        _ => *target = overrides.clone(),
    }
}

/// Resolve a hosted LLM credential without ever putting its value in config,
/// logs, or error messages.  The injected lookup keeps the behavior directly
/// testable without mutating process-global environment variables.
fn resolve_upstream_bearer_token<F>(
    llm: &config::LlmCfg,
    lookup: F,
) -> Result<Option<String>, String>
where
    F: FnOnce(&str) -> Option<String>,
{
    let Some(raw_env_name) = llm.upstream_bearer_token_env.as_deref() else {
        return Ok(None);
    };
    let env_name = raw_env_name.trim();
    if env_name.is_empty() {
        return Err("upstream_bearer_token_env is configured but blank".into());
    }
    let token = lookup(env_name)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("credential environment variable '{env_name}' is unset or empty"))?;
    Ok(Some(token))
}

#[cfg(test)]
mod llm_upstream_tests {
    use super::*;

    fn llm(env_name: Option<&str>) -> config::LlmCfg {
        config::LlmCfg {
            name: "clover".into(),
            aliases: vec!["gemma4".into()],
            entitlement: "clover_llm".into(),
            upstream_base: "https://openrouter.ai/api".into(),
            provider: Some("openrouter".into()),
            upstream_bearer_token_env: env_name.map(str::to_string),
            upstream_model: "~deepseek/deepseek-v4-flash-latest".into(),
            model_version: "openrouter/deepseek-v4-flash-latest".into(),
            request_overrides: std::collections::BTreeMap::new(),
            request_defaults: std::collections::BTreeMap::new(),
            prompt_microusd_per_1k: 90,
            completion_microusd_per_1k: 180,
            timeout_ms: 120_000,
        }
    }

    #[test]
    fn local_llm_needs_no_upstream_credential() {
        let got = resolve_upstream_bearer_token(&llm(None), |_| None).unwrap();
        assert_eq!(got, None);
    }

    #[test]
    fn hosted_llm_resolves_and_trims_credential() {
        let got = resolve_upstream_bearer_token(&llm(Some("OPENROUTER_API_KEY")), |name| {
            assert_eq!(name, "OPENROUTER_API_KEY");
            Some("  secret-token  ".into())
        })
        .unwrap();
        assert_eq!(got.as_deref(), Some("secret-token"));
    }

    #[test]
    fn hosted_llm_fails_closed_when_credential_is_missing() {
        let err =
            resolve_upstream_bearer_token(&llm(Some("OPENROUTER_API_KEY")), |_| None).unwrap_err();
        assert!(err.contains("OPENROUTER_API_KEY"));
        assert!(!err.contains("secret-token"));
    }

    #[test]
    fn hosted_llm_fails_closed_when_credential_name_is_blank() {
        let err = resolve_upstream_bearer_token(&llm(Some("  ")), |_| {
            panic!("blank environment name must not be looked up")
        })
        .unwrap_err();
        assert!(err.contains("configured but blank"));
    }

    #[test]
    fn hosted_llm_request_policy_preserves_explicit_values_and_forces_overrides() {
        let mut cfg = llm(Some("OPENROUTER_API_KEY"));
        cfg.request_defaults.insert("temperature".into(), json!(0));
        cfg.request_defaults.insert("seed".into(), json!(0));
        cfg.request_defaults.insert(
            "provider".into(),
            json!({"allow_fallbacks": true, "sort": "price"}),
        );
        cfg.request_overrides
            .insert("reasoning".into(), json!({"enabled": false}));
        cfg.request_overrides.insert(
            "provider".into(),
            json!({"zdr": true, "data_collection": "deny"}),
        );
        cfg.request_overrides
            .insert("model".into(), json!("must-not-win"));
        let mut payload = json!({
            "model": "clover",
            "reasoning": {"enabled": true},
            "temperature": 0.7,
            "provider": {"order": ["DeepInfra"], "allow_fallbacks": false},
            "messages": [{"role": "user", "content": "hello"}]
        });

        route_llm_payload(&mut payload, &cfg, "pilot-company");

        assert_eq!(payload["model"], "~deepseek/deepseek-v4-flash-latest");
        assert_eq!(payload["reasoning"]["enabled"], false);
        assert_eq!(payload["temperature"], 0.7);
        assert_eq!(payload["seed"], 0);
        assert_eq!(payload["provider"]["order"], json!(["DeepInfra"]));
        assert_eq!(payload["provider"]["allow_fallbacks"], false);
        assert_eq!(payload["provider"]["sort"], "price");
        assert_eq!(payload["provider"]["zdr"], true);
        assert_eq!(payload["provider"]["data_collection"], "deny");
        assert_eq!(payload["user"], "clover-tenant:pilot-company");
    }

    #[test]
    fn hosted_llm_preserves_bounded_human_tracking_user() {
        let cfg = llm(Some("OPENROUTER_API_KEY"));
        let mut payload = json!({
            "model": "gemma4",
            "user": "person@example.com",
            "messages": [{"role": "user", "content": "hello"}]
        });

        route_llm_payload(&mut payload, &cfg, "pilot-company");

        assert_eq!(payload["user"], "person@example.com");
        assert!(is_openrouter_upstream(&cfg));
    }

    #[test]
    fn hosted_llm_answer_validation_distinguishes_empty_from_tool_calls() {
        assert!(llm_response_has_answer(
            &json!({"choices":[{"message":{"content":"ready"}}]})
        ));
        assert!(llm_response_has_answer(
            &json!({"choices":[{"message":{"content":null,"tool_calls":[{"id":"call_1"}]}}]})
        ));
        assert!(llm_response_has_answer(
            &json!({"choices":[{"message":{"content":null,"refusal":"cannot comply"}}]})
        ));
        assert!(!llm_response_has_answer(&json!({"choices":[]})));
        assert!(!llm_response_has_answer(
            &json!({"choices":[{"message":{"content":null}}]})
        ));
        assert!(!llm_response_has_answer(
            &json!({"error":{"message":"temporary"}})
        ));
    }

    #[test]
    fn aggregates_composed_specialist_usage() {
        let usage = specialist_usage(&[
            json!({"usage":{"prompt_tokens":100,"completion_tokens":20,"cost_microusd":1200}}),
            json!({"usage":{"prompt_tokens":50,"completion_tokens":10,"cost_microusd":700}}),
            json!({"ok":false}),
        ]);
        assert_eq!(
            usage,
            SpecialistUsage {
                seen: true,
                prompt_tokens: 150,
                completion_tokens: 30,
                cost_microusd: 1900,
            }
        );
    }

    #[test]
    fn ordinary_specialists_have_no_composed_usage() {
        assert_eq!(
            specialist_usage(&[json!({"score":0.9})]),
            SpecialistUsage::default()
        );
    }

    #[test]
    fn openai_embedding_input_normalizes_one_or_many_strings() {
        assert_eq!(
            normalize_embedding_inputs(json!("one")).unwrap(),
            vec![json!("one")]
        );
        assert_eq!(
            normalize_embedding_inputs(json!(["one", "two"])).unwrap(),
            vec![json!("one"), json!("two")]
        );
        assert!(normalize_embedding_inputs(json!([[1, 2, 3]])).is_err());
        assert!(normalize_embedding_inputs(json!({"text": "one"})).is_err());
    }

    #[test]
    fn openai_embedding_encoding_supports_float_and_sdk_base64() {
        assert_eq!(embedding_encoding(None).unwrap(), EmbeddingEncoding::Float);
        assert_eq!(
            embedding_encoding(Some("base64")).unwrap(),
            EmbeddingEncoding::Base64
        );
        assert!(embedding_encoding(Some("hex")).is_err());

        let encoded = encode_embedding(json!([1.0, -2.5]), EmbeddingEncoding::Base64).unwrap();
        let bytes = BASE64_STANDARD
            .decode(encoded.as_str().expect("base64 string"))
            .unwrap();
        assert_eq!(
            bytes,
            [1.0_f32.to_le_bytes(), (-2.5_f32).to_le_bytes()].concat()
        );
    }

    #[test]
    fn openai_embedding_data_preserves_batch_indices() {
        let data = embedding_data(
            vec![json!([0.1, 0.2]), json!([0.3, 0.4])],
            EmbeddingEncoding::Float,
        )
        .unwrap();
        assert_eq!(data[0]["object"], "embedding");
        assert_eq!(data[0]["index"], 0);
        assert_eq!(data[1]["index"], 1);
        assert_eq!(data[1]["embedding"], json!([0.3, 0.4]));
    }

    #[test]
    fn provider_usage_extracts_inline_actual_cost_without_float_drift() {
        let usage = provider_usage_from_response(&json!({
            "id": "gen-test",
            "model": "openai/gpt-5.6-luna",
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "cost": 0.001234567
            }
        }));
        assert_eq!(usage.request_id.as_deref(), Some("gen-test"));
        assert_eq!(usage.upstream_model.as_deref(), Some("openai/gpt-5.6-luna"));
        assert_eq!(usage.prompt_tokens, Some(120));
        assert_eq!(usage.completion_tokens, Some(45));
        assert_eq!(usage.provider_cost_nanousd, Some(1_234_567));
    }

    #[test]
    fn streaming_usage_observer_handles_sse_chunk_boundaries() {
        let mut observer = OpenAiSseUsageObserver::default();
        observer.observe(
            b"data: {\"id\":\"gen-stream\",\"model\":\"openai/gpt-5.6-luna\",\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n",
        );
        observer.observe(b"data: {\"id\":\"gen-stream\",\"choices\":[],\"usa");
        observer.observe(
            b"ge\":{\"prompt_tokens\":10,\"completion_tokens\":2,\"cost\":0.000031}}\n\ndata: [DONE]\n\n",
        );
        observer.finish();

        assert_eq!(observer.usage.request_id.as_deref(), Some("gen-stream"));
        assert_eq!(observer.usage.prompt_tokens, Some(10));
        assert_eq!(observer.usage.completion_tokens, Some(2));
        assert_eq!(observer.usage.provider_cost_nanousd, Some(31_000));
    }

    #[test]
    fn reference_tariff_uses_exact_nanodollars_per_token() {
        let cfg = llm(Some("OPENROUTER_API_KEY"));
        assert_eq!(reference_cost_nanousd(&cfg, Some(100), Some(20)), 12_600);
        assert_eq!(llm_provider(&cfg), "openrouter");
    }

    #[test]
    fn provider_catalog_resolves_latest_alias_to_newest_family_member() {
        let models = vec![
            json!({"id":"deepseek/deepseek-v4-flash-0630","created":100}),
            json!({"id":"deepseek/deepseek-v4-flash-0731","created":200}),
            json!({"id":"deepseek/deepseek-v4","created":300}),
        ];
        let (model, alias_resolved) =
            openrouter_catalog_model(&models, "deepseek/deepseek-v4-flash-latest")
                .expect("latest alias");
        assert!(alias_resolved);
        assert_eq!(model["id"], "deepseek/deepseek-v4-flash-0731");

        let (exact, alias_resolved) =
            openrouter_catalog_model(&models, "deepseek/deepseek-v4-flash-0630")
                .expect("exact model");
        assert!(!alias_resolved);
        assert_eq!(exact["created"], 100);
    }
}
