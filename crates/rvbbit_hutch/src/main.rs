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
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::{Arc, RwLock};
use std::time::Instant;

use config::{HutchConfig, Upstream};
use error::HutchError;
use lanes::LaneRegistry;
use meter::{Meter, MeterRow};
use proxy::{forward, ForwardErr};
use tenants::{TenantStatus, TenantStore};

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

    let app = Router::new()
        .route("/", get(health))
        .route("/healthz", get(health))
        .route("/metrics", get(metrics))
        .route("/admin/reload-tenants", post(reload_tenants))
        .route("/b/{backend}/predict", post(predict))
        .route("/v1/models", get(list_models))
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
            tenant: &tenant.id,
            backend: &backend_name,
            n_inputs: 0,
            status: err.status.as_u16(),
            error_code: Some(code),
            duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
            upstream_ms: None,
            model_version: &backend.model_version,
            would_be_cost_microusd: 0,
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
                tenant: &tenant.id,
                backend: &backend.name,
                n_inputs: n,
                status: 200,
                error_code: None,
                duration_ms,
                upstream_ms: Some(ok.upstream_ms),
                model_version: &backend.model_version,
                would_be_cost_microusd: if usage.seen {
                    usage.cost_microusd
                } else {
                    backend.unit_microusd * n as i64
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
                tenant: &tenant.id,
                backend: &backend.name,
                n_inputs: n,
                status: err.status.as_u16(),
                error_code: Some(code),
                duration_ms,
                upstream_ms: None,
                model_version: &backend.model_version,
                would_be_cost_microusd: 0,
                prompt_tokens: None,
                completion_tokens: None,
            });
            err.into_response()
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
    let data: Vec<Value> = state
        .cfg
        .llms
        .iter()
        .filter(|l| tenant.entitlements.iter().any(|e| e == &l.entitlement))
        .map(|l| {
            json!({
                "id": l.name,
                "object": "model",
                "owned_by": "rvbbit-hutch",
                "meta": {"model_version": l.model_version},
            })
        })
        .collect();
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
    let label = format!("llm:{}", llm.name);

    let refuse = |err: HutchError, code: &'static str| {
        state.meter.record(MeterRow {
            tenant: &tenant.id,
            backend: &label,
            n_inputs: 1,
            status: err.status.as_u16(),
            error_code: Some(code),
            duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
            upstream_ms: None,
            model_version: &llm.model_version,
            would_be_cost_microusd: 0,
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

    route_llm_payload(&mut payload, &llm);
    let is_stream = payload
        .get("stream")
        .and_then(|s| s.as_bool())
        .unwrap_or(false);
    let url = format!(
        "{}/v1/chat/completions",
        llm.upstream_base.trim_end_matches('/')
    );
    let mut request = state
        .http
        .post(&url)
        .timeout(std::time::Duration::from_millis(llm.timeout_ms));
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
            return refuse(
                HutchError::upstream(&llm.name, e.to_string()),
                "upstream_error",
            )
        }
    };
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let head: String = resp
            .text()
            .await
            .unwrap_or_default()
            .chars()
            .take(300)
            .collect();
        return refuse(
            HutchError::upstream(&llm.name, format!("HTTP {status}: {head}")),
            "upstream_error",
        );
    }

    if is_stream {
        // Tokens are unknowable without teeing the SSE stream — meter the
        // call itself now, pass bytes through untouched. (v2: inject
        // stream_options.include_usage and parse the tail frame.)
        state.meter.record(MeterRow {
            tenant: &tenant.id,
            backend: &label,
            n_inputs: 1,
            status: 200,
            error_code: None,
            duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
            upstream_ms: None,
            model_version: &llm.model_version,
            would_be_cost_microusd: 0,
            prompt_tokens: None,
            completion_tokens: None,
        });
        let content_type = resp
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("text/event-stream")
            .to_string();
        let mut builder = axum::http::Response::builder()
            .status(StatusCode::OK)
            .header("content-type", content_type);
        if let Ok(v) = axum::http::HeaderValue::from_str(&llm.model_version) {
            builder = builder.header("x-hutch-model-version", v);
        }
        return builder
            .body(axum::body::Body::from_stream(resp.bytes_stream()))
            .expect("stream response build");
    }

    let mut out: Value = match resp.json().await {
        Ok(v) => v,
        Err(e) => {
            return refuse(
                HutchError::upstream(&llm.name, format!("bad upstream JSON: {e}")),
                "upstream_error",
            )
        }
    };
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
                return refuse(
                    HutchError::upstream(&llm.name, format!("retry failed: {e}")),
                    "upstream_error",
                )
            }
        };
        if !retried.status().is_success() {
            let status = retried.status().as_u16();
            let head: String = retried
                .text()
                .await
                .unwrap_or_default()
                .chars()
                .take(300)
                .collect();
            return refuse(
                HutchError::upstream(&llm.name, format!("retry HTTP {status}: {head}")),
                "upstream_error",
            );
        }
        out = match retried.json().await {
            Ok(v) => v,
            Err(e) => {
                return refuse(
                    HutchError::upstream(&llm.name, format!("bad retry JSON: {e}")),
                    "upstream_error",
                )
            }
        };
        if !llm_response_has_answer(&out) {
            return refuse(
                HutchError::upstream(&llm.name, "upstream returned no answer after retry".into()),
                "upstream_error",
            );
        }
    }
    let duration_ms = t0.elapsed().as_secs_f64() * 1000.0;
    let pt = out.pointer("/usage/prompt_tokens").and_then(|v| v.as_i64());
    let ct = out
        .pointer("/usage/completion_tokens")
        .and_then(|v| v.as_i64());
    let cost = pt.unwrap_or(0) * llm.prompt_microusd_per_1k / 1000
        + ct.unwrap_or(0) * llm.completion_microusd_per_1k / 1000;
    state.meter.record(MeterRow {
        tenant: &tenant.id,
        backend: &label,
        n_inputs: 1,
        status: 200,
        error_code: None,
        duration_ms,
        upstream_ms: Some(duration_ms),
        model_version: &llm.model_version,
        would_be_cost_microusd: cost,
        prompt_tokens: pt,
        completion_tokens: ct,
    });
    let mut resp_out = Json(out).into_response();
    if let Ok(v) = axum::http::HeaderValue::from_str(&llm.model_version) {
        resp_out.headers_mut().insert("x-hutch-model-version", v);
    }
    resp_out
}

fn route_llm_payload(payload: &mut Value, llm: &config::LlmCfg) {
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
    }
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
            name: "gemma4".into(),
            entitlement: "clover_llm".into(),
            upstream_base: "https://openrouter.ai/api".into(),
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
            "model": "gemma4",
            "reasoning": {"enabled": true},
            "temperature": 0.7,
            "provider": {"order": ["DeepInfra"], "allow_fallbacks": false},
            "messages": [{"role": "user", "content": "hello"}]
        });

        route_llm_payload(&mut payload, &cfg);

        assert_eq!(payload["model"], "~deepseek/deepseek-v4-flash-latest");
        assert_eq!(payload["reasoning"]["enabled"], false);
        assert_eq!(payload["temperature"], 0.7);
        assert_eq!(payload["seed"], 0);
        assert_eq!(payload["provider"]["order"], json!(["DeepInfra"]));
        assert_eq!(payload["provider"]["allow_fallbacks"], false);
        assert_eq!(payload["provider"]["sort"], "price");
        assert_eq!(payload["provider"]["zdr"], true);
        assert_eq!(payload["provider"]["data_collection"], "deny");
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
}
