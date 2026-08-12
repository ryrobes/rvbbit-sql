//! Peer queue transport — Colony (docs/PEER_CAPABILITIES_PLAN.md). Routes a
//! specialist/LLM call through rvbbit.peer_capability_requests instead of
//! HTTP: a DataRabbit client's local runner claims the request, calls
//! whatever it has running locally (Ollama, a Gradio specialist, a local
//! MCP-backed model), and completes it. SPI is illegal on a prewarm pool
//! thread, so this opens its own client-side postgres::Client connection —
//! the same pattern route_log.rs uses for its background writer.
//!
//! Always two round trips (enqueue_peer_request, then poll_peer_response),
//! never the combined form: a single plpgsql call that both INSERTs and
//! polls would hold that INSERT uncommitted for the whole wait, so the
//! claiming runner (a different backend) could never see it — proved live
//! while building the P0 raw-SQL round trip, see migration
//! 0212_colony_peer_capabilities.sql.
//!
//! Parameters travel as text with an explicit SQL-side cast (`$2::text::jsonb`,
//! `$1::text::uuid`) rather than typed bindings — postgres-types' uuid/serde_json
//! integrations aren't enabled on this crate's `postgres` dependency, and
//! text+cast is the standard way around that without a Cargo.toml change.
//! The extra `::text` matters: casting straight to the target type (e.g.
//! `$2::jsonb`) makes Postgres infer that parameter's wire type as jsonb/uuid,
//! and String/&str's ToSql::accepts() only claims TEXT-family OIDs — bound
//! against a non-text inferred type it fails with "error serializing
//! parameter" before the query ever reaches the server.
//!
//! Identity: the side connection lands via unix-socket peer auth as
//! whatever role the postgres process itself runs as (superuser in every
//! deployment this was tested against). Superuser bypasses pg_has_role()
//! entirely per Postgres's own documented semantics — so without a fix,
//! EVERY call through this transport would skip Colony's scope check no
//! matter who is actually calling (confirmed live: colony_bob, who is not
//! a member of the backend's scope role, got a real answer back). The fix:
//! read the REAL calling role via the same cheap non-SPI FFI pattern
//! route_log.rs already uses (GetUserId/GetUserNameFromId — a plain read of
//! already-resident backend state, safe from any thread, no query
//! involved), then `SET SESSION AUTHORIZATION` to it right after
//! connecting. That changes session_user (not just current_user, the way
//! `SET ROLE` would) and correctly drops the superuser bypass for the rest
//! of this connection, so enqueue_peer_request's pg_has_role(session_user,
//! ...) check evaluates against the real caller. Requires the connection
//! to legitimately hold superuser only to perform that one switch — after
//! it, permission checks are the real caller's, same as burrow mode's own
//! "trusted service account becomes a specific user" convention.

use std::ffi::CStr;

use pgrx::pg_sys;
use postgres::{Client, NoTls};
use serde_json::Value;

use super::{SpecialistResponse, SpecialistSpec, Transport};
use crate::providers::ProviderError;

pub struct PeerQueueTransport;

impl PeerQueueTransport {
    pub fn new() -> Self {
        Self
    }
}

impl Transport for PeerQueueTransport {
    fn name(&self) -> &'static str {
        "peer_queue"
    }

    fn client_batches(&self) -> bool {
        // Each input is its own queued request/response pair — per-item
        // dispatch, same shape as Gradio (server-side batching, if any, is
        // the peer's own business).
        false
    }

    fn predict(
        &self,
        spec: &SpecialistSpec,
        inputs: &[Value],
    ) -> Result<SpecialistResponse, ProviderError> {
        let t0 = std::time::Instant::now();
        let backend_name = peer_backend_name(spec);
        let caller = current_user_name().ok_or_else(|| {
            ProviderError::BadResponse(
                "peer_queue: could not determine the calling role; refusing to dispatch \
                 rather than risk an unscoped call"
                    .to_string(),
            )
        })?;
        let mut client = connect_as(&caller)
            .map_err(|e| ProviderError::BadResponse(format!("peer_queue: connect failed: {e}")))?;

        let mut outputs = Vec::with_capacity(inputs.len());
        for input in inputs {
            outputs.push(dispatch_one(
                &mut client,
                &backend_name,
                input,
                spec.timeout_ms,
            )?);
        }

        let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;
        Ok(SpecialistResponse {
            outputs,
            usage: Vec::new(),
            latency_ms,
        })
    }
}

/// `rvbbit.backends.name` doubles as `rvbbit.peer_backends.backend_name` by
/// convention (register_backend + register_peer_backend under the same
/// name); `transport_opts.backend_name` overrides it for callers that want
/// the two names to differ.
fn peer_backend_name(spec: &SpecialistSpec) -> String {
    spec.transport_opts
        .get("backend_name")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| spec.name.clone())
}

fn dispatch_one(
    client: &mut Client,
    backend_name: &str,
    input: &Value,
    timeout_ms: u64,
) -> Result<Value, ProviderError> {
    let payload_text =
        serde_json::to_string(input).map_err(|e| ProviderError::BadResponse(e.to_string()))?;

    let request_id: String = client
        .query_one(
            "SELECT rvbbit.enqueue_peer_request($1, $2::text::jsonb)::text",
            &[&backend_name, &payload_text],
        )
        .map_err(|e| ProviderError::BadResponse(format!("peer_queue: enqueue failed: {e}")))?
        .get(0);

    let timeout_ms_i32 = timeout_ms.min(i32::MAX as u64) as i32;
    let row = client
        .query_one(
            "SELECT rvbbit.poll_peer_response($1::text::uuid, $2)::text",
            &[&request_id, &timeout_ms_i32],
        )
        .map_err(|e| ProviderError::BadResponse(format!("peer_queue: {e}")))?;

    let response_text: Option<String> = row.get(0);
    match response_text {
        Some(text) => {
            let parsed: Value = serde_json::from_str(&text).map_err(|e| {
                ProviderError::BadResponse(format!("peer_queue: bad response json: {e}"))
            })?;
            Ok(extract_content(parsed))
        }
        None => Ok(Value::Null),
    }
}

/// The DataRabbit runner's chat/specialist reply is its own envelope —
/// `{"content": "...", "model": "..."}` — but every other transport's
/// `predict()` already hands back a bare scalar (openai_chat.rs extracts
/// `content` before returning; gradio.rs picks a single `output_index`), and
/// that bare-scalar contract is what `providers::chat()` builds
/// `ChatResponse.content` from. Left unextracted, the whole envelope leaked
/// into operator output as a stringified JSON blob instead of the answer
/// text. Only unwrap when `content` is actually a string: an MCP tool reply
/// shapes `content` as an array of content blocks per the MCP spec, so this
/// guard leaves those (and anything else non-chat-shaped) untouched.
fn extract_content(value: Value) -> Value {
    match &value {
        Value::Object(map) => match map.get("content") {
            Some(Value::String(s)) => Value::String(s.clone()),
            _ => value,
        },
        _ => value,
    }
}

/// Connect (lands as whatever role peer auth gives the postgres process —
/// superuser in practice), then immediately `SET SESSION AUTHORIZATION` to
/// the real caller so every check downstream (pg_has_role(session_user,
/// ...) inside enqueue_peer_request) evaluates against them, not the
/// connection's own ambient superuser identity. If `caller` isn't itself a
/// superuser this also drops superuser for the rest of the connection,
/// which is the point — a scope check that a superuser bypass could always
/// satisfy trivially would not be a scope check.
fn connect_as(caller: &str) -> Result<Client, postgres::Error> {
    let mut client = Client::connect(&dsn(), NoTls)?;
    client.batch_execute(&format!(
        "SET SESSION AUTHORIZATION {}",
        quote_pg_ident(caller)
    ))?;
    Ok(client)
}

fn dsn() -> String {
    let db = current_database_name().unwrap_or_else(|| "postgres".to_string());
    format!(
        "host={} dbname={} application_name=rvbbit_peer_queue",
        conninfo_value("/var/run/postgresql"),
        conninfo_value(&db),
    )
}

fn conninfo_value(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
}

/// Double-quoted Postgres identifier — `caller` comes from GetUserNameFromId
/// (an existing, already-authenticated role from Postgres's own catalog,
/// not free-form input), but quoting it properly costs nothing and avoids
/// relying on that assumption holding forever.
fn quote_pg_ident(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

fn current_database_name() -> Option<String> {
    let ptr = unsafe { pg_sys::get_database_name(pg_sys::MyDatabaseId) };
    if ptr.is_null() {
        return None;
    }
    Some(
        unsafe { CStr::from_ptr(ptr) }
            .to_string_lossy()
            .into_owned(),
    )
}

/// The REAL calling session's role — plain FFI read of already-resident
/// backend state (MyProcPort/roleid via GetUserId), not SPI, not a query.
/// Same technique route_log.rs's current_user_name() already uses.
fn current_user_name() -> Option<String> {
    let ptr = unsafe { pg_sys::GetUserNameFromId(pg_sys::GetUserId(), false) };
    if ptr.is_null() {
        return None;
    }
    Some(
        unsafe { CStr::from_ptr(ptr) }
            .to_string_lossy()
            .into_owned(),
    )
}
