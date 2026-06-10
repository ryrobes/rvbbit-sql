//! MCP (Model Context Protocol) integration.
//!
//! rvbbit brings the MCP ecosystem into SQL: an MCP server is a registered
//! external process exposing a set of typed tools (functions). Once
//! registered, its tools are discoverable, callable from SQL via
//! `rvbbit.mcp_call(server, tool, args jsonb)`, and (Phase 2) composable
//! inside operator pipelines as a `kind:"mcp"` node.
//!
//! ARCHITECTURE
//! A `mcp-gateway` sidecar (Python; reuses Anthropic's `mcp` SDK) holds
//! the actual MCP client subprocesses. PG backends only talk HTTP to the
//! gateway; the gateway reads `rvbbit.mcp_servers` for configs. This keeps
//! subprocess lifecycle out of the PG backend (where it would be unsafe)
//! and lets us reuse a mature MCP client.
//!
//! AUDIT
//! Every successful or tool-error call lands in `rvbbit.mcp_invocations`
//! for the observability UI. Transport-level failures (gateway down,
//! network error) raise a SQL error and roll back; that row is lost.
//! Adding async/out-of-band logging is a future enhancement.

use std::sync::{OnceLock, RwLock};
use std::time::Duration;

use pgrx::{prelude::*, Spi};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::providers::ProviderError;
use crate::specialists::http_client;

/// HTTP base URL of the gateway sidecar. Overridable via env for tests /
/// non-default deployments; defaults to the docker-compose service name.
static MCP_GATEWAY_URL: OnceLock<RwLock<Option<String>>> = OnceLock::new();

/// security-01: the shared bearer token for the MCP gateway, read from the
/// pg-rvbbit backend's environment. When set, every gateway request carries it;
/// the gateway enforces it on the tool-call/subprocess routes. Unset (the
/// turnkey default) means the gateway runs open on the trusted internal network.
fn gateway_token() -> Option<String> {
    std::env::var("RVBBIT_GATEWAY_TOKEN")
        .ok()
        .filter(|t| !t.is_empty())
}

/// Attach the gateway bearer token to a request when one is configured.
fn with_gateway_auth(
    req: reqwest::blocking::RequestBuilder,
) -> reqwest::blocking::RequestBuilder {
    match gateway_token() {
        Some(token) => req.bearer_auth(token),
        None => req,
    }
}

pub fn gateway_url() -> String {
    if let Some(url) = std::env::var("RVBBIT_MCP_GATEWAY_URL")
        .ok()
        .and_then(|s| normalize_gateway_url(&s))
    {
        return url;
    }
    if let Some(url) = cached_gateway_url() {
        return url;
    }
    if let Some(url) = load_gateway_url_from_sql() {
        set_cached_gateway_url(Some(url.clone()));
        return url;
    }
    "http://mcp-gateway:9180".into()
}

#[pg_extern]
fn reload_mcp_gateway() -> bool {
    let url = load_gateway_url_from_sql();
    set_cached_gateway_url(url.clone());
    url.is_some()
}

fn normalize_gateway_url(raw: &str) -> Option<String> {
    let trimmed = raw.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn gateway_cache() -> &'static RwLock<Option<String>> {
    MCP_GATEWAY_URL.get_or_init(|| RwLock::new(None))
}

fn cached_gateway_url() -> Option<String> {
    gateway_cache()
        .read()
        .ok()
        .and_then(|guard| guard.as_ref().cloned())
}

fn set_cached_gateway_url(url: Option<String>) {
    if let Ok(mut guard) = gateway_cache().write() {
        *guard = url;
    }
}

fn load_gateway_url_from_sql() -> Option<String> {
    if crate::flow::in_pool_worker() {
        return None;
    }
    Spi::get_one::<String>("SELECT rvbbit.mcp_gateway_endpoint()")
        .ok()
        .flatten()
        .and_then(|url| normalize_gateway_url(&url))
}

// ---- wire types ----------------------------------------------------------

#[derive(Serialize)]
struct CallReq<'a> {
    server: &'a str,
    tool: &'a str,
    arguments: &'a Value,
}

#[derive(Deserialize)]
struct CallResp {
    #[serde(default)]
    content: Value,
    #[serde(default, rename = "isError")]
    is_error: bool,
}

#[derive(Debug, Deserialize)]
pub struct ToolDef {
    pub name: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default, rename = "input_schema")]
    pub input_schema: Value,
}

#[derive(Deserialize)]
struct ToolsResp {
    #[serde(default)]
    tools: Vec<ToolDef>,
    #[serde(default)]
    resources: Vec<ResourceDef>,
}

#[derive(Debug, Deserialize)]
pub struct ResourceDef {
    pub uri: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub mime_type: Option<String>,
}

#[derive(Serialize)]
struct ReadResourceReq<'a> {
    server: &'a str,
    uri: &'a str,
}

#[derive(Deserialize)]
struct ReadResourceResp {
    #[serde(default)]
    contents: Value,
}

#[derive(Deserialize)]
struct ProbeResp {
    #[serde(default)]
    reachable: bool,
    #[serde(default)]
    latency_ms: i32,
    #[serde(default)]
    n_tools: i32,
    #[serde(default)]
    error: Option<String>,
}

// ---- HTTP calls to the gateway ------------------------------------------

/// Dispatch a tool call to the gateway. Returns the full MCP result
/// envelope `{content:[...], isError:bool}` as a jsonb-shaped value.
pub fn call(server: &str, tool: &str, args: &Value) -> Result<Value, ProviderError> {
    let body = CallReq {
        server,
        tool,
        arguments: args,
    };
    let url = format!("{}/call", gateway_url());
    let resp = with_gateway_auth(http_client().post(&url))
        .timeout(Duration::from_secs(180))
        .json(&body)
        .send()?;
    let status = resp.status();
    if !status.is_success() {
        return Err(ProviderError::ApiStatus {
            status: status.as_u16(),
            body: resp.text().unwrap_or_default(),
        });
    }
    let parsed: CallResp = resp.json()?;
    Ok(serde_json::json!({
        "content": parsed.content,
        "isError": parsed.is_error,
    }))
}

/// Ask the gateway to re-introspect a server (drops the cached subprocess
/// and re-runs `tools/list`), returns the fresh tool list.
pub fn refresh(server: &str) -> Result<(Vec<ToolDef>, Vec<ResourceDef>), ProviderError> {
    let url = format!("{}/refresh/{}", gateway_url(), server);
    let resp = with_gateway_auth(http_client().post(&url))
        .timeout(Duration::from_secs(60))
        .send()?;
    let status = resp.status();
    if !status.is_success() {
        return Err(ProviderError::ApiStatus {
            status: status.as_u16(),
            body: resp.text().unwrap_or_default(),
        });
    }
    let parsed: ToolsResp = resp.json()?;
    Ok((parsed.tools, parsed.resources))
}

pub fn read_resource(server: &str, uri: &str) -> Result<Value, ProviderError> {
    let body = ReadResourceReq { server, uri };
    let url = format!("{}/resource", gateway_url());
    let resp = with_gateway_auth(http_client().post(&url))
        .timeout(Duration::from_secs(60))
        .json(&body)
        .send()?;
    let status = resp.status();
    if !status.is_success() {
        return Err(ProviderError::ApiStatus {
            status: status.as_u16(),
            body: resp.text().unwrap_or_default(),
        });
    }
    let parsed: ReadResourceResp = resp.json()?;
    Ok(serde_json::json!({ "contents": parsed.contents }))
}

pub fn probe_server(server: &str) -> Result<Value, ProviderError> {
    let url = format!("{}/probe/{}", gateway_url(), server);
    let resp = with_gateway_auth(http_client().post(&url))
        .timeout(Duration::from_secs(30))
        .send()?;
    let status = resp.status();
    if !status.is_success() {
        return Err(ProviderError::ApiStatus {
            status: status.as_u16(),
            body: resp.text().unwrap_or_default(),
        });
    }
    let p: ProbeResp = resp.json()?;
    Ok(serde_json::json!({
        "reachable": p.reachable,
        "latency_ms": p.latency_ms,
        "n_tools": p.n_tools,
        "error": p.error,
    }))
}

// ---- UDFs ----------------------------------------------------------------

/// Call an MCP tool. Returns the full result envelope `{content:[...],
/// isError:bool}`. A tool that returned `isError=true` still succeeds at
/// the SQL level (the caller decides what to do); transport-level failures
/// raise a SQL error. Every call is logged to `rvbbit.mcp_invocations`
/// (except transport failures — see module doc).
///
/// CACHING. If `rvbbit.mcp_tools.cacheable = true` for this (server, tool),
/// the call is keyed by `(server, tool, blake3(args))` and the result is
/// looked up / stored in `rvbbit.mcp_cache`. `ttl_seconds` (NULL = forever)
/// bounds entry lifetime. Cache hits log to `mcp_invocations` with
/// `cache_hit = true` and skip the gateway. Only successful (non-isError)
/// calls populate the cache.
#[pg_extern]
fn mcp_call(server: &str, tool: &str, args: pgrx::JsonB) -> pgrx::JsonB {
    let cache = lookup_cache_policy(server, tool);
    let args_hash = if cache.is_some() {
        Some(args_hash_hex(&args.0))
    } else {
        None
    };

    // Try cache
    if let (Some(policy), Some(h)) = (&cache, args_hash.as_deref()) {
        if let Some(cached) = cache_get(server, tool, h, policy.ttl_seconds) {
            log_invocation(server, tool, &args.0, &cached, None, 0, true);
            return pgrx::JsonB(cached);
        }
    }

    let t0 = std::time::Instant::now();
    let result = call(server, tool, &args.0);
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

    match result {
        Ok(envelope) => {
            let is_error = envelope
                .get("isError")
                .and_then(|b| b.as_bool())
                .unwrap_or(false);
            let error_text = if is_error {
                Some(first_text(&envelope).unwrap_or_else(|| "tool returned isError=true".into()))
            } else {
                None
            };
            // Only cache clean successes — never poison the cache with errors.
            if cache.is_some() && !is_error {
                if let Some(h) = args_hash.as_deref() {
                    cache_put(server, tool, h, &args.0, &envelope);
                }
            }
            log_invocation(
                server,
                tool,
                &args.0,
                &envelope,
                error_text.as_deref(),
                latency_ms,
                false,
            );
            pgrx::JsonB(envelope)
        }
        Err(e) => pgrx::error!("rvbbit.mcp_call('{}', '{}'): {}", server, tool, e),
    }
}

/// Re-introspect a registered MCP server: ask the gateway for the current
/// `tools/list` AND `resources/list`, then upsert into `rvbbit.mcp_tools`
/// and `rvbbit.mcp_resources`. Returns the number of tools discovered.
///
/// UPSERT (not DELETE+INSERT) so a tool's `cacheable` / `ttl_seconds`
/// flags survive a refresh. Tools that no longer exist on the server are
/// removed; resources are fully replaced.
#[pg_extern]
fn refresh_mcp_server(server: &str) -> i32 {
    let (tools, resources) = match refresh(server) {
        Ok(t) => t,
        Err(e) => pgrx::error!("rvbbit.refresh_mcp_server('{}'): {}", server, e),
    };
    let n = tools.len() as i32;
    let server_lit = sql_lit(server);

    // --- Tools: upsert preserving cacheable/ttl_seconds -----------------
    let names_array = if tools.is_empty() {
        "ARRAY[]::text[]".to_string()
    } else {
        let inner = tools
            .iter()
            .map(|t| sql_lit(&t.name))
            .collect::<Vec<_>>()
            .join(",");
        format!("ARRAY[{inner}]")
    };
    let _ = pgrx::Spi::run(&format!(
        "DELETE FROM rvbbit.mcp_tools WHERE server = {server_lit} \
         AND NOT (name = ANY({names_array}))"
    ));
    for tool in &tools {
        let name_lit = sql_lit(&tool.name);
        let desc_lit = tool
            .description
            .as_deref()
            .map(sql_lit)
            .unwrap_or_else(|| "NULL".into());
        let schema_lit =
            sql_lit(&serde_json::to_string(&tool.input_schema).unwrap_or_else(|_| "{}".into()));
        let _ = pgrx::Spi::run(&format!(
            "INSERT INTO rvbbit.mcp_tools (server, name, description, input_schema, discovered_at) \
             VALUES ({server_lit}, {name_lit}, {desc_lit}, {schema_lit}::jsonb, clock_timestamp()) \
             ON CONFLICT (server, name) DO UPDATE SET \
                 description    = EXCLUDED.description, \
                 input_schema   = EXCLUDED.input_schema, \
                 discovered_at  = clock_timestamp()"
        ));
    }

    // --- Resources: full replace (no per-row state worth preserving) ----
    let _ = pgrx::Spi::run(&format!(
        "DELETE FROM rvbbit.mcp_resources WHERE server = {server_lit}"
    ));
    for res in &resources {
        let uri_lit = sql_lit(&res.uri);
        let name_lit = res
            .name
            .as_deref()
            .map(sql_lit)
            .unwrap_or_else(|| "NULL".into());
        let desc_lit = res
            .description
            .as_deref()
            .map(sql_lit)
            .unwrap_or_else(|| "NULL".into());
        let mime_lit = res
            .mime_type
            .as_deref()
            .map(sql_lit)
            .unwrap_or_else(|| "NULL".into());
        let _ = pgrx::Spi::run(&format!(
            "INSERT INTO rvbbit.mcp_resources (server, uri, name, description, mime_type) \
             VALUES ({server_lit}, {uri_lit}, {name_lit}, {desc_lit}, {mime_lit}) \
             ON CONFLICT (server, uri) DO NOTHING"
        ));
    }
    n
}

/// Extract the text of the first `type:"text"` content block from an MCP
/// call envelope. Convenience for the common case where a tool returns
/// just text. Returns NULL if no text block is present.
#[pg_extern(immutable, strict)]
fn mcp_text(response: pgrx::JsonB) -> Option<String> {
    first_text(&response.0)
}

/// Call an MCP tool and surface its result as a SETOF jsonb — one row per
/// item — so it composes with SQL relational ops (JOIN/WHERE/GROUP BY).
///
/// Many MCP tools return JSON inside their text content. This unwraps:
/// the first text block is parsed as JSON, then
///   - top-level array         → one row per element
///   - object with a known
///     array-bearing key
///     (items, results, data,
///      entries, rows)         → one row per element of that array
///   - any other object/scalar → one row containing the whole thing
///   - text that isn't JSON    → one row of `"the text"`
///
/// For unusual shapes use `rvbbit.mcp_call(...)` and navigate the jsonb
/// yourself.
#[pg_extern]
fn mcp_rows(
    server: &str,
    tool: &str,
    args: pgrx::JsonB,
) -> pgrx::iter::SetOfIterator<'static, pgrx::JsonB> {
    let t0 = std::time::Instant::now();
    let envelope = match call(server, tool, &args.0) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.mcp_rows('{}', '{}'): {}", server, tool, e),
    };
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;
    let is_error = envelope
        .get("isError")
        .and_then(|b| b.as_bool())
        .unwrap_or(false);
    let error_text = if is_error {
        Some(first_text(&envelope).unwrap_or_else(|| "tool returned isError=true".into()))
    } else {
        None
    };
    log_invocation(
        server,
        tool,
        &args.0,
        &envelope,
        error_text.as_deref(),
        latency_ms,
        false,
    );

    let rows = extract_rows(&envelope);
    pgrx::iter::SetOfIterator::new(rows.into_iter().map(pgrx::JsonB))
}

fn extract_rows(envelope: &Value) -> Vec<Value> {
    // Pull every text content block (ignore images / audio / resources for
    // the row surface — those aren't relational).
    let texts: Vec<String> = envelope
        .get("content")
        .and_then(|c| c.as_array())
        .map(|blocks| {
            blocks
                .iter()
                .filter_map(|b| {
                    if b.get("type").and_then(|t| t.as_str()) == Some("text") {
                        b.get("text")
                            .and_then(|t| t.as_str())
                            .map(|s| s.to_string())
                    } else {
                        None
                    }
                })
                .collect()
        })
        .unwrap_or_default();

    // Multiple text blocks — FastMCP and friends emit one block per item
    // when the tool returns a list. One row per block, parsed if JSON.
    if texts.len() > 1 {
        return texts
            .into_iter()
            .map(|t| serde_json::from_str::<Value>(&t).unwrap_or(Value::String(t)))
            .collect();
    }

    // Single block — unwrap common shapes.
    let text = match texts.into_iter().next() {
        Some(t) if !t.is_empty() => t,
        _ => return Vec::new(),
    };
    let parsed = match serde_json::from_str::<Value>(&text) {
        Ok(v) => v,
        // Plain text — surface as a single row.
        Err(_) => return vec![Value::String(text)],
    };
    match parsed {
        Value::Array(a) => a,
        Value::Object(o) => {
            // Most "list" APIs nest the rows under one of these names.
            for key in &["items", "results", "data", "entries", "rows"] {
                if let Some(Value::Array(a)) = o.get(*key) {
                    return a.clone();
                }
            }
            vec![Value::Object(o)]
        }
        scalar => vec![scalar],
    }
}

// ---- helpers -------------------------------------------------------------

pub(crate) fn first_text(envelope: &Value) -> Option<String> {
    envelope
        .get("content")
        .and_then(|c| c.as_array())
        .and_then(|arr| {
            arr.iter().find_map(|block| {
                let is_text = block.get("type").and_then(|t| t.as_str()) == Some("text");
                if is_text {
                    block
                        .get("text")
                        .and_then(|t| t.as_str())
                        .map(|s| s.to_string())
                } else {
                    None
                }
            })
        })
}

pub(crate) fn log_invocation(
    server: &str,
    tool: &str,
    args: &Value,
    output: &Value,
    error: Option<&str>,
    latency_ms: i32,
    cache_hit: bool,
) {
    // SPI is illegal on a flow-pool worker thread. When an operator with
    // an `mcp` node runs in bulk (warm path), the per-row calls happen on
    // pool threads; we skip logging there. The operator's overall receipt
    // still captures the call in its sub_calls audit.
    if crate::flow::in_pool_worker() {
        return;
    }
    let server_lit = sql_lit(server);
    let tool_lit = sql_lit(tool);
    let args_lit = sql_lit(&serde_json::to_string(args).unwrap_or_else(|_| "null".into()));
    let output_lit = sql_lit(&serde_json::to_string(output).unwrap_or_else(|_| "null".into()));
    let error_lit = error.map(sql_lit).unwrap_or_else(|| "NULL".into());
    let cache_hit_lit = if cache_hit { "true" } else { "false" };
    let sql = format!(
        "INSERT INTO rvbbit.mcp_invocations \
         (server, tool, args, output, error, latency_ms, cache_hit, query_id) \
         VALUES ({server_lit}, {tool_lit}, {args_lit}::jsonb, \
                 {output_lit}::jsonb, {error_lit}, {latency_ms}, {cache_hit_lit}, \
                 rvbbit.current_query_id())"
    );
    let _ = pgrx::Spi::run(&sql);
    crate::costs::log_mcp_invocation_cost(server, tool, error, cache_hit, output);
}

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

// ---- Resources (read-by-URI surface) -------------------------------------

/// Read an MCP resource. Returns the read envelope as jsonb:
///   { "contents": [ {uri, mimeType, text|blob}, … ] }
/// For the common text-body case, use `rvbbit.mcp_resource_text` instead.
#[pg_extern]
fn mcp_resource(server: &str, uri: &str) -> pgrx::JsonB {
    match read_resource(server, uri) {
        Ok(v) => pgrx::JsonB(v),
        Err(e) => pgrx::error!("rvbbit.mcp_resource('{}', '{}'): {}", server, uri, e),
    }
}

/// Convenience: read an MCP resource and return the first text content.
/// Returns NULL if the resource has no text body.
#[pg_extern]
fn mcp_resource_text(server: &str, uri: &str) -> Option<String> {
    let env = match read_resource(server, uri) {
        Ok(v) => v,
        Err(e) => pgrx::error!("rvbbit.mcp_resource_text('{}', '{}'): {}", server, uri, e),
    };
    env.get("contents")
        .and_then(|c| c.as_array())
        .and_then(|arr| {
            arr.iter()
                .find_map(|block| block.get("text").and_then(|t| t.as_str()).map(String::from))
        })
}

// ---- Active health probe -------------------------------------------------

/// Active health probe — round-trips `tools/list` against the server via
/// the gateway and returns `{reachable, latency_ms, n_tools, error}`. The
/// gateway lazy-spawns a missing subprocess as part of the probe, so a
/// `reachable=true` answer means the server is genuinely callable right
/// now (not just configured). For a passive snapshot of past activity use
/// the `rvbbit.mcp_health` view instead.
#[pg_extern]
fn mcp_probe(server: &str) -> pgrx::JsonB {
    match probe_server(server) {
        Ok(v) => pgrx::JsonB(v),
        Err(e) => pgrx::JsonB(serde_json::json!({
            "reachable": false,
            "latency_ms": 0,
            "n_tools": 0,
            "error": format!("gateway error: {e}"),
        })),
    }
}

// ---- Selective result caching --------------------------------------------

struct CachePolicy {
    ttl_seconds: Option<i32>,
}

/// Look up whether a (server, tool) is marked cacheable in mcp_tools.
fn lookup_cache_policy(server: &str, tool: &str) -> Option<CachePolicy> {
    let server_lit = sql_lit(server);
    let tool_lit = sql_lit(tool);
    let sql = format!(
        "SELECT cacheable, ttl_seconds FROM rvbbit.mcp_tools \
         WHERE server = {server_lit} AND name = {tool_lit}"
    );
    let mut policy: Option<CachePolicy> = None;
    let _: Result<(), pgrx::spi::Error> = pgrx::Spi::connect(|client| {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let cacheable: Option<bool> = row.get(1)?;
            let ttl: Option<i32> = row.get(2)?;
            if cacheable == Some(true) {
                policy = Some(CachePolicy { ttl_seconds: ttl });
            }
        }
        Ok(())
    });
    policy
}

/// Canonical content hash of the args. serde_json's default (sorted-key)
/// Map gives us deterministic serialization, so the same logical args
/// always hash to the same value.
fn args_hash_hex(args: &Value) -> String {
    let s = serde_json::to_string(args).unwrap_or_else(|_| "null".into());
    let hash = blake3::hash(s.as_bytes());
    // 32-char (128-bit) prefix is plenty for cache keys.
    hex_encode(&hash.as_bytes()[..16])
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0xf) as usize] as char);
    }
    s
}

/// Cache lookup. Returns the stored output if a fresh entry exists.
fn cache_get(server: &str, tool: &str, args_hash: &str, ttl_seconds: Option<i32>) -> Option<Value> {
    let server_lit = sql_lit(server);
    let tool_lit = sql_lit(tool);
    let hash_lit = sql_lit(args_hash);
    let freshness = match ttl_seconds {
        Some(s) if s > 0 => format!(
            " AND cached_at + interval '{} seconds' > clock_timestamp()",
            s
        ),
        _ => String::new(),
    };
    let sql = format!(
        "SELECT output FROM rvbbit.mcp_cache \
         WHERE server = {server_lit} AND tool = {tool_lit} \
           AND args_hash = {hash_lit}{freshness}"
    );
    let mut found: Option<Value> = None;
    let _: Result<(), pgrx::spi::Error> = pgrx::Spi::connect(|client| {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let out: Option<pgrx::JsonB> = row.get(1)?;
            if let Some(j) = out {
                found = Some(j.0);
            }
        }
        Ok(())
    });
    found
}

fn cache_put(server: &str, tool: &str, args_hash: &str, args: &Value, output: &Value) {
    let server_lit = sql_lit(server);
    let tool_lit = sql_lit(tool);
    let hash_lit = sql_lit(args_hash);
    let args_lit = sql_lit(&serde_json::to_string(args).unwrap_or_else(|_| "null".into()));
    let output_lit = sql_lit(&serde_json::to_string(output).unwrap_or_else(|_| "null".into()));
    let sql = format!(
        "INSERT INTO rvbbit.mcp_cache (server, tool, args_hash, args, output, cached_at) \
         VALUES ({server_lit}, {tool_lit}, {hash_lit}, {args_lit}::jsonb, \
                 {output_lit}::jsonb, clock_timestamp()) \
         ON CONFLICT (server, tool, args_hash) DO UPDATE SET \
             args = EXCLUDED.args, \
             output = EXCLUDED.output, \
             cached_at = clock_timestamp()"
    );
    let _ = pgrx::Spi::run(&sql);
}

fn quote_ident(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

// ---- typed wrapper generator ---------------------------------------------

/// Generate one typed SQL function per discovered tool of `server`.
///
/// Reads `rvbbit.mcp_tools` for that server; for each tool, builds a
/// `CREATE OR REPLACE FUNCTION <server>.<tool>(args…) RETURNS SETOF jsonb`
/// in a per-server schema. Args are derived from the MCP tool's JSON
/// Schema: required props have no default, optional ones default NULL
/// (and an unset arg is omitted from the JSON sent to the tool — not
/// forwarded as null). All wrappers return SETOF jsonb via
/// `rvbbit.mcp_rows`, so they compose with JOIN / WHERE / GROUP BY.
///
/// Idempotent — drops the schema and re-creates from scratch each call.
/// Returns the number of wrappers generated. Call after
/// `rvbbit.refresh_mcp_server(server)`; re-run after schema drift.
///
/// Example:
///   SELECT rvbbit.refresh_mcp_server('github');
///   SELECT rvbbit.generate_mcp_wrappers('github');
///   SELECT r->>'full_name' FROM github.search_repositories(
///       query => 'rust', perpage => 5) r;
#[pg_extern]
fn generate_mcp_wrappers(server: &str) -> i32 {
    let schema_ident = quote_ident(server);
    let _ = pgrx::Spi::run(&format!("DROP SCHEMA IF EXISTS {schema_ident} CASCADE"));
    if let Err(e) = pgrx::Spi::run(&format!("CREATE SCHEMA {schema_ident}")) {
        pgrx::error!("rvbbit.generate_mcp_wrappers('{}'): {}", server, e);
    }

    let tools = fetch_tools_for_server(server);
    let mut n = 0;
    for tool in tools {
        match build_wrapper_ddl(server, &schema_ident, &tool) {
            Ok(ddl) => {
                if let Err(e) = pgrx::Spi::run(&ddl) {
                    pgrx::warning!(
                        "rvbbit.generate_mcp_wrappers: skipped tool '{}' ({})",
                        tool.name,
                        e
                    );
                } else {
                    n += 1;
                }
            }
            Err(reason) => {
                pgrx::warning!(
                    "rvbbit.generate_mcp_wrappers: skipped tool '{}' ({})",
                    tool.name,
                    reason
                );
            }
        }
    }
    n
}

struct ToolMeta {
    name: String,
    input_schema: Value,
}

fn fetch_tools_for_server(server: &str) -> Vec<ToolMeta> {
    let server_lit = sql_lit(server);
    let sql = format!(
        "SELECT name, input_schema FROM rvbbit.mcp_tools WHERE server = {server_lit} ORDER BY name"
    );
    let mut out = Vec::new();
    let _: Result<(), pgrx::spi::Error> = pgrx::Spi::connect(|client| {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let schema: Option<pgrx::JsonB> = row.get(2)?;
            if let Some(n) = name {
                out.push(ToolMeta {
                    name: n,
                    input_schema: schema
                        .map(|j| j.0)
                        .unwrap_or_else(|| Value::Object(Default::default())),
                });
            }
        }
        Ok(())
    });
    out
}

fn build_wrapper_ddl(server: &str, schema_ident: &str, tool: &ToolMeta) -> Result<String, String> {
    let props = tool
        .input_schema
        .get("properties")
        .and_then(|p| p.as_object());
    let required: std::collections::HashSet<String> = tool
        .input_schema
        .get("required")
        .and_then(|r| r.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();

    let mut arg_decls: Vec<String> = Vec::new();
    let mut arg_builds: Vec<String> = Vec::new();
    let mut seen_lower = std::collections::HashSet::new();

    if let Some(props) = props {
        for (name, prop) in props {
            let lower = name.to_lowercase();
            if !seen_lower.insert(lower.clone()) {
                return Err(format!(
                    "argument name collision when lowercased: '{}'",
                    name
                ));
            }
            let sql_type = json_schema_to_sql_type(prop);
            let arg_ident = quote_ident(&lower);
            let default = if required.contains(name) {
                ""
            } else {
                " DEFAULT NULL"
            };
            arg_decls.push(format!("{arg_ident} {sql_type}{default}"));
            // Body: only add the arg to the JSON if the caller supplied a
            // non-null value (so optional args truly stay optional, instead
            // of being sent as null).
            arg_builds.push(format!(
                "    IF {arg_ident} IS NOT NULL THEN args := args || jsonb_build_object({key_lit}, {arg_ident}); END IF;",
                key_lit = sql_lit(name),
            ));
        }
    }

    let func_name = quote_ident(&tool.name);
    let args_signature = arg_decls.join(", ");
    let args_body = arg_builds.join("\n");
    let server_lit = sql_lit(server);
    let tool_lit = sql_lit(&tool.name);

    Ok(format!(
        "CREATE OR REPLACE FUNCTION {schema_ident}.{func_name}({args_signature}) \
         RETURNS SETOF jsonb \
         LANGUAGE plpgsql AS $mcp$ \
         DECLARE args jsonb := '{{}}'::jsonb; \
         BEGIN \
{args_body} \
             RETURN QUERY SELECT r FROM rvbbit.mcp_rows({server_lit}, {tool_lit}, args) r; \
         END $mcp$"
    ))
}

fn json_schema_to_sql_type(prop: &Value) -> &'static str {
    // MCP tools' JSON Schema is usually a simple `{type: "string"|"integer"|…}`.
    // `type` is sometimes an array (`["string","null"]`); we take the first
    // non-null element. Anything we can't classify falls back to text.
    let t = prop.get("type");
    let kind = match t {
        Some(Value::String(s)) => Some(s.as_str()),
        Some(Value::Array(a)) => a.iter().find_map(|v| v.as_str().filter(|s| *s != "null")),
        _ => None,
    };
    match kind {
        Some("string") => "text",
        Some("integer") => "bigint",
        Some("number") => "double precision",
        Some("boolean") => "boolean",
        Some("array") | Some("object") => "jsonb",
        _ => "text",
    }
}
