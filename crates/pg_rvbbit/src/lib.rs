//! pg_rvbbit — columnar storage extension for Postgres 18.
//!
//! Phase 0: extension loads, exposes a version function. No storage engine
//! wired up yet. Phase 1 will register the Table Access Method and route
//! INSERT/SELECT through the catcher heap table.

use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::{json, Map, Value};
use std::time::Instant;

::pgrx::pg_module_magic!();

mod bitmap;
mod cache;
mod catalog;
mod catcher;
mod cluster;
mod code_steps;
mod columnar_cache;
mod compact;
mod composites;
mod costs;
mod custom_scan;
mod delete_log;
mod df;
mod duck_backend;
mod duck_telemetry;
mod embeddings;
mod evidence;
mod explain;
mod fast_hash;
mod flow;
mod kg;
mod lance;
mod mv;
mod operators;
mod planner;
mod prewarm;
mod probe;
mod provider_catalog;
mod providers;
mod python_runtime;
mod rewriter;
#[cfg(not(test))]
mod route_log;
#[cfg(test)]
mod route_log {
    pub unsafe fn register_hooks() {}

    pub(crate) fn enqueue_decision(
        _query_sql: &str,
        _route_doc: &serde_json::Value,
        _cache_hit: bool,
        _rewritten: bool,
    ) {
    }

    pub(crate) fn record_pending_execution(
        _query_sql: &str,
        _route_doc: &serde_json::Value,
        _cache_hit: bool,
        _rewritten: bool,
    ) {
    }
}
mod mcp;
mod router;
mod scan;
mod sketches;
mod specialists;
mod takes;
mod tam;
mod telemetry;
mod time_travel;
mod tokens;
mod triples;
mod unit_of_work;
mod validator;
mod vector;

/// Force the backend registry to reload from rvbbit.backends. Call after
/// registering new backends in a long-lived session so the thread-safe
/// cache picks them up. Returns the number of backends loaded.
#[pg_extern]
fn reload_backends() -> i32 {
    match specialists::reload_all() {
        Ok(n) => n as i32,
        Err(e) => pgrx::error!("rvbbit.reload_backends: {}", e),
    }
}

/// True when an environment variable is visible to the Postgres extension
/// process and non-empty. Does not expose the value.
#[pg_extern(stable)]
fn env_present(env_name: &str) -> bool {
    let trimmed = env_name.trim();
    !trimmed.is_empty()
        && std::env::var(trimmed)
            .map(|v| !v.trim().is_empty())
            .unwrap_or(false)
}

/// Active probe for one registered backend. This exercises the same transport
/// machinery used by specialist operator nodes, with a generic sample payload
/// that works for the common text/query/label-oriented sidecars.
#[pg_extern(volatile)]
fn backend_probe(backend_name: &str) -> JsonB {
    let sample = json!({
        "text": "Rvbbit backend probe",
        "query": "backend health",
        "labels": ["entity", "topic"],
        "categories": "entity,topic"
    });
    backend_probe_with_input(backend_name, JsonB(sample))
}

#[pg_extern(volatile)]
fn backend_probe_with_input(backend_name: &str, sample: JsonB) -> JsonB {
    let start = Instant::now();
    match specialists::load_spec(backend_name) {
        Ok(spec) => {
            let sample = normalize_probe_sample(&spec, sample.0);
            match specialists::predict_one(&spec, &sample) {
                Ok(output) => JsonB(json!({
                    "ok": true,
                    "backend": backend_name,
                    "transport": spec.transport,
                    "endpoint": spec.endpoint_url,
                    "latency_ms": start.elapsed().as_secs_f64() * 1000.0,
                    "output": output
                })),
                Err(err) => JsonB(json!({
                    "ok": false,
                    "backend": backend_name,
                    "latency_ms": start.elapsed().as_secs_f64() * 1000.0,
                    "error": err.to_string()
                })),
            }
        }
        Err(err) => JsonB(json!({
            "ok": false,
            "backend": backend_name,
            "latency_ms": start.elapsed().as_secs_f64() * 1000.0,
            "error": err.to_string()
        })),
    }
}

fn normalize_probe_sample(spec: &specialists::SpecialistSpec, sample: Value) -> Value {
    if !matches!(
        spec.transport.as_str(),
        "openai_chat" | "anthropic" | "gemini"
    ) {
        return sample;
    }

    let mut obj: Map<String, Value> = sample.as_object().cloned().unwrap_or_default();
    if !obj.contains_key("user") {
        let user = obj
            .get("text")
            .or_else(|| obj.get("query"))
            .and_then(|v| v.as_str())
            .unwrap_or("Rvbbit backend probe");
        obj.insert("user".into(), json!(user));
    }
    if !obj.contains_key("model") {
        if let Some(model) = spec.transport_opts.get("model").and_then(|v| v.as_str()) {
            obj.insert("model".into(), json!(model));
        }
    }
    obj.entry("max_tokens").or_insert_with(|| json!(16));
    Value::Object(obj)
}

/// Called once per backend at startup when the extension is in
/// shared_preload_libraries. Registers the planner / executor hooks that
/// implement transparent reads from parquet (Phase 2c) and — eventually —
/// the SQL-rewriting layer that consumes rvbbit.shreds (Phase 5).
#[pgrx::pg_guard]
#[no_mangle]
pub extern "C-unwind" fn _PG_init() {
    unsafe {
        planner::register_hooks();
        rewriter::register_hooks();
        route_log::register_hooks();
        time_travel::register_hooks();
        explain::register_explain_semantic();
    }
}

#[pg_extern]
fn rvbbit_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Build-time info — useful for benchmark logs to know exactly what's running.
#[pg_extern]
fn rvbbit_build_info() -> String {
    format!(
        "pg_rvbbit {} (target {}, profile {})",
        env!("CARGO_PKG_VERSION"),
        std::env::consts::ARCH,
        if cfg!(debug_assertions) {
            "debug"
        } else {
            "release"
        },
    )
}

#[cfg(any(test, feature = "pg_test"))]
#[pg_schema]
mod tests {
    use pgrx::prelude::*;

    #[pg_test]
    fn test_version_returns_something() {
        let v: Option<&str> = Spi::get_one("SELECT rvbbit.rvbbit_version()").unwrap();
        assert!(v.is_some());
        assert!(!v.unwrap().is_empty());
    }
}

#[cfg(test)]
pub mod pg_test {
    pub fn setup(_options: Vec<&str>) {}
    pub fn postgresql_conf_options() -> Vec<&'static str> {
        vec![]
    }
}
