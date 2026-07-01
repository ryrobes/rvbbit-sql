//! Remote file-source sync orchestrator for the document brain.
//!
//! The pure-SQL engine (migrations 0046/0047) does everything except the network:
//! it computes the connector request, lands the returned manifest, fills bodies via
//! the extract_doc operator, and reconciles the corpus. This module is the thin HTTP
//! shell that ties it together — it POSTs to a connector sidecar (gdrive/s3/nfs/local)
//! and otherwise drives SQL via SPI. One DB writer: rvbbit.
//!
//! Flow (per source):
//!   1. SELECT rvbbit.brain_sync_request(id)  → {endpoint, auth_env, payload{folders,cursor,known}}
//!      (`folders` is the legacy key for configured Drive folder/doc locations)
//!   2. POST {endpoint} payload               → {files[], pending_grants[], cursor}
//!   3. SELECT rvbbit.brain_sync_write_manifest(id, files, pending, cursor)
//!   4. SELECT rvbbit.brain_sync_extract_bodies(id)        (new/changed binaries → markdown)
//!   5. SELECT rvbbit.brain_sync_apply_manifest(id, trig)  → reconcile (ingest/ACL/tombstone)

use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::{json, Value};

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

fn jsonb_lit(v: &Value) -> String {
    format!("{}::jsonb", sql_lit(&v.to_string()))
}

/// Sync one remote source: trigger its connector, extract new/changed files, reconcile.
#[pg_extern]
fn brain_sync_source(p_source_id: i64, p_trigger: default!(String, "'manual'")) -> JsonB {
    let trigger = if p_trigger.trim().is_empty() { "manual".to_string() } else { p_trigger };
    JsonB(sync_one(p_source_id, &trigger).unwrap_or_else(|e| json!({"source_id": p_source_id, "error": e})))
}

/// Sync every enabled remote source (a connector endpoint is configured). For the nightly cron.
#[pg_extern]
fn brain_sync_sources(p_trigger: default!(String, "'auto'")) -> JsonB {
    let trigger = if p_trigger.trim().is_empty() { "auto".to_string() } else { p_trigger };
    let ids: Vec<i64> = Spi::connect(|client| {
        let mut out = Vec::new();
        if let Ok(table) = client.select(
            "SELECT source_id FROM rvbbit.brain_sources \
             WHERE enabled AND coalesce(config->>'endpoint', \
                   (SELECT endpoint_url FROM rvbbit.backends b WHERE b.name = coalesce(config->>'connector','gdrive_connector'))) IS NOT NULL \
             ORDER BY source_id",
            None, &[],
        ) {
            for row in table {
                if let Ok(Some(id)) = row.get::<i64>(1) {
                    out.push(id);
                }
            }
        }
        out
    });

    let mut results = Vec::with_capacity(ids.len());
    for id in ids {
        let r = sync_one(id, &trigger).unwrap_or_else(|e| json!({"source_id": id, "error": e}));
        results.push(r);
    }
    JsonB(json!({ "sources": results.len(), "results": results }))
}

fn sync_one(source_id: i64, trigger: &str) -> Result<Value, String> {
    // 1. What the connector call needs.
    let req: pgrx::JsonB = Spi::get_one(&format!(
        "SELECT rvbbit.brain_sync_request({source_id}::bigint)"
    ))
    .map_err(|e| format!("brain_sync_request: {e}"))?
    .ok_or_else(|| format!("source {source_id} not found"))?;
    let req = req.0;

    let endpoint = req
        .get("endpoint")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!(
            "no connector endpoint for source {source_id} (set config.endpoint or install the connector capability)"
        ))?
        .to_string();
    let payload = req.get("payload").cloned().unwrap_or_else(|| json!({}));
    let auth_env = req.get("auth_env").and_then(Value::as_str).filter(|s| !s.is_empty());

    // 2. Trigger the connector.
    let mut http = crate::specialists::http_client()
        .post(&endpoint)
        .timeout(std::time::Duration::from_secs(900))
        .json(&payload);
    if let Some(env_name) = auth_env {
        if let Ok(token) = std::env::var(env_name) {
            http = http.bearer_auth(token);
        }
    }
    let resp = http.send().map_err(|e| format!("connector POST {endpoint} failed: {e}"))?;
    let status = resp.status();
    let body = resp.text().map_err(|e| format!("connector response read failed: {e}"))?;
    if !status.is_success() {
        return Err(format!("connector returned HTTP {}: {}", status.as_u16(), truncate(&body, 400)));
    }
    let parsed: Value = serde_json::from_str(&body)
        .map_err(|e| format!("connector returned invalid JSON: {e}: {}", truncate(&body, 400)))?;

    let files = parsed.get("files").cloned().unwrap_or_else(|| json!([]));
    let pending = parsed.get("pending_grants").cloned().unwrap_or_else(|| json!([]));
    let cursor_sql = match parsed.get("cursor").and_then(Value::as_str) {
        Some(c) => sql_lit(c),
        None => "NULL".to_string(),
    };

    // 3. Land the manifest (single writer).
    Spi::run(&format!(
        "SELECT rvbbit.brain_sync_write_manifest({source_id}::bigint, {}, {}, {})",
        jsonb_lit(&files), jsonb_lit(&pending), cursor_sql
    ))
    .map_err(|e| format!("brain_sync_write_manifest: {e}"))?;

    // 4. Extract new/changed binaries to markdown (no-op if the capability isn't installed).
    let extracted: i32 = Spi::get_one(&format!(
        "SELECT rvbbit.brain_sync_extract_bodies({source_id}::bigint)"
    ))
    .map_err(|e| format!("brain_sync_extract_bodies: {e}"))?
    .unwrap_or(0);

    // 5. Reconcile (ingest new/changed, sync ACL, tombstone gone).
    let summary: pgrx::JsonB = Spi::get_one(&format!(
        "SELECT rvbbit.brain_sync_apply_manifest({source_id}::bigint, {})",
        sql_lit(trigger)
    ))
    .map_err(|e| format!("brain_sync_apply_manifest: {e}"))?
    .ok_or_else(|| "apply_manifest returned NULL".to_string())?;

    let mut out = summary.0;
    if let Value::Object(ref mut m) = out {
        m.insert("extracted".into(), json!(extracted));
    }
    Ok(out)
}

fn truncate(s: &str, n: usize) -> String {
    if s.len() <= n { s.to_string() } else { format!("{}…", &s[..n]) }
}
