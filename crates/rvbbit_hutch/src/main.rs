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
        if let Some(k) = auth.strip_prefix("Bearer ").or_else(|| auth.strip_prefix("bearer ")) {
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
            (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"ok": false, "error": e})))
                .into_response()
        }
    }
}

#[derive(Deserialize)]
struct PredictIn {
    inputs: Vec<Value>,
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
    if !tenant.entitlements.iter().any(|e| e == &backend.entitlement) {
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
    let fwd = forward(&state.http, &state.cfg.upstream, &backend, &inputs).await;
    let duration_ms = t0.elapsed().as_secs_f64() * 1000.0;
    match fwd {
        Ok(ok) => {
            state.meter.record(MeterRow {
                tenant: &tenant.id,
                backend: &backend.name,
                n_inputs: n,
                status: 200,
                error_code: None,
                duration_ms,
                upstream_ms: Some(ok.upstream_ms),
                model_version: &backend.model_version,
                would_be_cost_microusd: backend.unit_microusd * n as i64,
                prompt_tokens: None,
                completion_tokens: None,
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
                ForwardErr::Transport(detail) => {
                    (HutchError::upstream(&backend.name, detail), "upstream_error")
                }
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

    payload["model"] = json!(llm.upstream_model);
    let is_stream = payload
        .get("stream")
        .and_then(|s| s.as_bool())
        .unwrap_or(false);
    let url = format!(
        "{}/v1/chat/completions",
        llm.upstream_base.trim_end_matches('/')
    );
    let upstream = state
        .http
        .post(&url)
        .timeout(std::time::Duration::from_millis(llm.timeout_ms))
        .json(&payload)
        .send()
        .await;
    let resp = match upstream {
        Ok(r) => r,
        Err(e) => return refuse(HutchError::upstream(&llm.name, e.to_string()), "upstream_error"),
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

    let out: Value = match resp.json().await {
        Ok(v) => v,
        Err(e) => {
            return refuse(
                HutchError::upstream(&llm.name, format!("bad upstream JSON: {e}")),
                "upstream_error",
            )
        }
    };
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
