use std::cell::RefCell;
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::time::Instant;

use pgrx::prelude::*;
use pgrx::{pg_sys, JsonB, Spi};
use serde_json::{json, Map, Value};
use std::ffi::{CStr, CString};
use std::io::{BufRead, BufReader, Write};

const DEFAULT_DUCK_BIN: &str = "/usr/local/bin/rvbbit-duck";
const DEFAULT_MAX_ROWS: i32 = 100_000;
const DEFAULT_TIMEOUT_S: i32 = 300;

thread_local! {
    static DUCK_SESSION: RefCell<Option<DuckSession>> = const { RefCell::new(None) };
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DuckSessionKey {
    binary: String,
    dsn: String,
    engine: String,
    layout: String,
}

struct DuckSession {
    key: DuckSessionKey,
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl DuckSession {
    fn spawn(key: DuckSessionKey) -> Result<Self, String> {
        let mut child = Command::new(&key.binary)
            .arg("--serve")
            .arg("--engine")
            .arg(&key.engine)
            .arg("--layout")
            .arg(&key.layout)
            .arg("--dsn")
            .arg(&key.dsn)
            .arg("--pgdata-prefix")
            .arg(pgdata_prefix())
            .arg("--visible-pgdata-prefix")
            .arg(visible_pgdata_prefix())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("failed to start persistent rvbbit-duck: {e}"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "persistent rvbbit-duck stdin unavailable".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "persistent rvbbit-duck stdout unavailable".to_string())?;
        Ok(Self {
            key,
            child,
            stdin,
            stdout: BufReader::new(stdout),
        })
    }

    fn request(&mut self, request: &str) -> Result<Value, String> {
        self.stdin
            .write_all(request.as_bytes())
            .map_err(|e| format!("persistent rvbbit-duck write failed: {e}"))?;
        self.stdin
            .write_all(b"\n")
            .map_err(|e| format!("persistent rvbbit-duck write failed: {e}"))?;
        self.stdin
            .flush()
            .map_err(|e| format!("persistent rvbbit-duck flush failed: {e}"))?;
        let mut response = String::new();
        let bytes = self
            .stdout
            .read_line(&mut response)
            .map_err(|e| format!("persistent rvbbit-duck read failed: {e}"))?;
        if bytes == 0 {
            return Err("persistent rvbbit-duck exited without a response".to_string());
        }
        serde_json::from_str(response.trim_end())
            .map_err(|e| format!("invalid persistent rvbbit-duck JSON: {e}"))
    }
}

impl Drop for DuckSession {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub(crate) fn backend_enabled() -> bool {
    let enabled = guc_setting("rvbbit.duck_backend")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND", true));
    enabled && duck_binary().is_some()
}

pub(crate) fn max_rows() -> i32 {
    std::env::var("RVBBIT_DUCK_BACKEND_MAX_ROWS")
        .ok()
        .and_then(|s| s.parse::<i32>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_MAX_ROWS)
}

fn timeout_s() -> i32 {
    std::env::var("RVBBIT_DUCK_BACKEND_TIMEOUT_S")
        .ok()
        .and_then(|s| s.parse::<i32>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_TIMEOUT_S)
}

fn persistent_enabled() -> bool {
    guc_setting("rvbbit.duck_backend_persistent")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND_PERSISTENT", true))
}

pub(crate) fn fail_open_enabled() -> bool {
    guc_setting("rvbbit.duck_backend_fail_open")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND_FAIL_OPEN", true))
}

/// Phase 1: when on, the `datafusion` engine routes through the in-process
/// DataFusion in `crate::df` instead of forking the rvbbit-duck sidecar.
/// Default **on** as of the post-bench flip — measured wins or ties on
/// every query at both 100k and (multi-row-group) 1M scale, with safe
/// transparent fallback to the sidecar on any in-process error.
/// Disable with `SET rvbbit.df_inprocess = off` for explicit A/B.
fn df_inprocess_enabled() -> bool {
    guc_setting("rvbbit.df_inprocess")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DF_INPROCESS", true))
}

fn env_enabled(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => setting_enabled(&value, default),
        Err(_) => default,
    }
}

fn setting_enabled(value: &str, default: bool) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return default;
    }
    !matches!(
        trimmed.to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off" | "disabled"
    )
}

fn guc_setting(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() })
    }
}

fn duck_binary() -> Option<String> {
    let configured = std::env::var("RVBBIT_DUCK_BIN").ok();
    let path = configured.unwrap_or_else(|| DEFAULT_DUCK_BIN.to_string());
    Path::new(&path).exists().then_some(path)
}

/// PGDATA root the extension sees — what `rvbbit.row_groups.path` is rooted at.
/// Falls back to `data_directory` GUC; can be overridden via env (useful when
/// the sidecar runs in a separate mount namespace).
fn pgdata_prefix() -> String {
    if let Ok(p) = std::env::var("RVBBIT_PGDATA_PREFIX") {
        return p;
    }
    guc_setting("data_directory").unwrap_or_else(|| "/var/lib/postgresql".to_string())
}

/// PGDATA root the rvbbit-duck process sees. Typically identical to
/// `pgdata_prefix()` (same container / same host). Override only when the
/// sidecar's view of the FS differs (e.g. a bind-mount under another path).
fn visible_pgdata_prefix() -> String {
    std::env::var("RVBBIT_VISIBLE_PGDATA_PREFIX").unwrap_or_else(|_| pgdata_prefix())
}

#[pg_extern(volatile)]
fn duck_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("duck", "scan", query, column_names, max_rows)
}

#[pg_extern(volatile)]
fn datafusion_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("datafusion", "scan", query, column_names, max_rows)
}

#[pg_extern(volatile)]
fn duck_hive_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("duck", "hive", query, column_names, max_rows)
}

#[pg_extern(volatile)]
fn duck_vortex_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("duck", "vortex", query, column_names, max_rows)
}

#[pg_extern(volatile)]
fn datafusion_hive_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("datafusion", "hive", query, column_names, max_rows)
}

#[pg_extern(volatile)]
fn datafusion_vortex_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("datafusion", "vortex", query, column_names, max_rows)
}

#[pg_extern(volatile)]
fn datafusion_mem_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("datafusion", "mem", query, column_names, max_rows)
}

fn engine_query_json(
    engine: &str,
    layout: &str,
    query: &str,
    column_names: JsonB,
    max_rows: i32,
) -> JsonB {
    let columns = parse_column_names(&column_names.0);
    if columns.is_empty() {
        pgrx::error!("rvbbit.{engine}_query_json: column_names must be a non-empty JSON array");
    }
    let max_rows = if max_rows > 0 {
        max_rows
    } else {
        self::max_rows()
    };
    let start = Instant::now();

    // Phase 1: in-process DataFusion path. Only takes the datafusion engine
    // (DuckDB still goes through the sidecar). If we hit an error, fall
    // through to the sidecar path — safe rollback by design.
    let inprocess_payload = if engine == "datafusion" && df_inprocess_enabled() {
        match crate::df::query_engine(layout, query, max_rows) {
            Ok(payload) => Some(payload),
            Err(err) => {
                if matches!(layout, "mem" | "memory") {
                    return fail_open_or_error(engine, query, max_rows, &err);
                }
                if matches!(layout, "vortex" | "vortex_scan") {
                    return engine_error(engine, &err);
                }
                pgrx::warning!(
                    "rvbbit.{engine}_query_json: in-process DF failed ({err}); falling back to sidecar"
                );
                None
            }
        }
    } else {
        None
    };

    let payload = if let Some(p) = inprocess_payload {
        p
    } else {
        let binary = duck_binary().unwrap_or_else(|| {
            pgrx::error!("rvbbit.{engine}_query_json: rvbbit-duck binary not found")
        });
        let dsn = duck_dsn();
        let timeout = timeout_s();
        match run_engine_query(engine, layout, &binary, &dsn, query, max_rows, timeout) {
            Ok(p) => p,
            Err(err) if matches!(layout, "vortex" | "vortex_scan") => {
                return engine_error(engine, &err);
            }
            Err(err) => return fail_open_or_error(engine, query, max_rows, &err),
        }
    };

    if payload.get("status").and_then(Value::as_str) != Some("ok") {
        let err = payload
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("rvbbit-duck returned non-ok status");
        if matches!(layout, "vortex" | "vortex_scan") {
            return engine_error(engine, err);
        }
        return fail_open_or_error(engine, query, max_rows, err);
    }

    let row_count = payload
        .get("row_count")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let Some(rows) = payload.get("rows").and_then(Value::as_array) else {
        return fail_open_or_error(
            engine,
            query,
            max_rows,
            "rvbbit-duck returned no rows array",
        );
    };
    if row_count > rows.len() {
        return fail_open_or_error(
            engine,
            query,
            max_rows,
            &format!("result has {row_count} row(s), exceeding backend cap {max_rows}"),
        );
    }

    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(values) = row.as_array() else {
            return fail_open_or_error(engine, query, max_rows, "row is not a JSON array");
        };
        if values.len() != columns.len() {
            return fail_open_or_error(
                engine,
                query,
                max_rows,
                &format!(
                    "row width {} does not match expected width {}",
                    values.len(),
                    columns.len()
                ),
            );
        }
        let mut obj = Map::with_capacity(columns.len());
        for (name, value) in columns.iter().zip(values.iter()) {
            obj.insert(name.clone(), value.clone());
        }
        out.push(Value::Object(obj));
    }

    if env_enabled("RVBBIT_DUCK_BACKEND_OBSERVE", false) {
        let candidate = match (engine, layout) {
            ("datafusion", "mem") => "datafusion_mem",
            ("datafusion", "hive") => "datafusion_hive",
            ("datafusion", "vortex") => "datafusion_vortex",
            ("datafusion", _) => "datafusion_vector",
            ("duck", "vortex") => "duck_vortex",
            (_, "hive") => "duck_hive",
            _ => "duck_vector",
        };
        record_engine_observation(query, candidate, start.elapsed().as_secs_f64() * 1000.0);
    }
    JsonB(Value::Array(out))
}

fn engine_error(engine: &str, err: &str) -> JsonB {
    pgrx::error!("rvbbit.{engine}_query_json: {err}");
}

fn fail_open_or_error(engine: &str, query: &str, max_rows: i32, err: &str) -> JsonB {
    if fail_open_enabled() {
        pgrx::warning!(
            "rvbbit.{engine}_query_json: {err}; falling back to native PostgreSQL/Rvbbit execution"
        );
        return native_fallback_query_json(query, max_rows).unwrap_or_else(|fallback_err| {
            pgrx::error!(
                "rvbbit.{engine}_query_json: {err}; native fallback failed: {fallback_err}"
            )
        });
    }
    pgrx::error!("rvbbit.{engine}_query_json: {err}");
}

fn native_fallback_query_json(query: &str, max_rows: i32) -> Result<JsonB, String> {
    let limit = max_rows.max(1).saturating_add(1);
    let query = query.trim().trim_end_matches(';');
    let sql = format!(
        "SELECT coalesce(jsonb_agg(to_jsonb(rvbbit_fallback_row)), '[]'::jsonb) \
         FROM (SELECT * FROM ({query}) AS rvbbit_source LIMIT {limit}) AS rvbbit_fallback_row"
    );
    let mut out = None;
    Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        client.select(
            "SELECT pg_catalog.set_config('rvbbit.duck_backend', 'off', true)",
            None,
            &[],
        )?;
        let rows = client.select(&sql, None, &[])?;
        for row in rows {
            out = row.get::<JsonB>(1)?;
        }
        Ok(())
    })
    .map_err(|e| e.to_string())?;
    Ok(out.unwrap_or_else(|| JsonB(json!([]))))
}

fn run_engine_query(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
) -> Result<Value, String> {
    if persistent_enabled() {
        execute_persistent(engine, layout, binary, dsn, query, max_rows, timeout).or_else(|_| {
            execute_engine_oneshot(engine, layout, binary, dsn, query, max_rows, timeout)
        })
    } else {
        execute_engine_oneshot(engine, layout, binary, dsn, query, max_rows, timeout)
    }
}

fn execute_persistent(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
) -> Result<Value, String> {
    let key = DuckSessionKey {
        binary: binary.to_string(),
        dsn: dsn.to_string(),
        engine: engine.to_string(),
        layout: layout.to_string(),
    };
    let request = serde_json::to_string(&json!({
        "sql": query,
        "repeat": 1,
        "timeout_s": timeout,
        "max_rows": max_rows,
    }))
    .map_err(|e| e.to_string())?;
    DUCK_SESSION.with(|slot| {
        let mut slot = slot.borrow_mut();
        match send_persistent_request(&mut slot, &key, &request) {
            Ok(value) => Ok(value),
            Err(first) => {
                slot.take();
                send_persistent_request(&mut slot, &key, &request)
                    .map_err(|second| format!("{first}; retry failed: {second}"))
            }
        }
    })
}

fn send_persistent_request(
    slot: &mut Option<DuckSession>,
    key: &DuckSessionKey,
    request: &str,
) -> Result<Value, String> {
    let needs_new = slot.as_ref().is_none_or(|session| session.key != *key);
    if needs_new {
        slot.take();
        *slot = Some(DuckSession::spawn(key.clone())?);
    }
    slot.as_mut()
        .ok_or_else(|| "persistent rvbbit-duck session unavailable".to_string())?
        .request(request)
}

fn execute_engine_oneshot(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
) -> Result<Value, String> {
    let output = Command::new(binary)
        .arg("--engine")
        .arg(engine)
        .arg("--layout")
        .arg(layout)
        .arg("--dsn")
        .arg(dsn)
        .arg("--sql")
        .arg(query)
        .arg("--repeat")
        .arg("1")
        .arg("--timeout-s")
        .arg(timeout.to_string())
        .arg("--max-rows")
        .arg(max_rows.to_string())
        .arg("--pgdata-prefix")
        .arg(pgdata_prefix())
        .arg("--visible-pgdata-prefix")
        .arg(visible_pgdata_prefix())
        .output()
        .map_err(|e| format!("failed to start rvbbit-duck: {e}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(&stdout).map_err(|e| {
        let stderr = String::from_utf8_lossy(&output.stderr);
        format!(
            "invalid rvbbit-duck JSON: {e}; stderr={}",
            first_line(&stderr)
        )
    })
}

fn parse_column_names(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default()
}

fn duck_dsn() -> String {
    if let Ok(dsn) = std::env::var("RVBBIT_DUCK_DSN") {
        return dsn;
    }
    let db = unsafe { current_database_name() }.unwrap_or_else(|| "postgres".to_string());
    let user = unsafe { current_user_name() }.unwrap_or_else(|| "postgres".to_string());
    format!(
        "host={} dbname={} user={}",
        conninfo_value("/var/run/postgresql"),
        conninfo_value(&db),
        conninfo_value(&user)
    )
}

unsafe fn current_database_name() -> Option<String> {
    let ptr = pg_sys::get_database_name(pg_sys::MyDatabaseId);
    if ptr.is_null() {
        return None;
    }
    Some(CStr::from_ptr(ptr).to_string_lossy().into_owned())
}

unsafe fn current_user_name() -> Option<String> {
    let ptr = pg_sys::GetUserNameFromId(pg_sys::GetUserId(), false);
    if ptr.is_null() {
        return None;
    }
    Some(CStr::from_ptr(ptr).to_string_lossy().into_owned())
}

fn conninfo_value(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
}

fn first_line(value: &str) -> &str {
    value.lines().next().unwrap_or("").trim()
}

fn record_engine_observation(query: &str, candidate: &str, elapsed_ms: f64) {
    let query_lit = sql_lit(query);
    let candidate_lit = sql_lit(candidate);
    let _ = Spi::run(&format!(
        "SELECT rvbbit.route_record_observation({query_lit}, {candidate_lit}, {elapsed_ms}, 'ok', 'backend-{candidate}')"
    ));
}

fn sql_lit(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn env_enabled_rejects_falsey_values() {
        std::env::set_var("RVBBIT_DUCK_BACKEND_TEST_FLAG", "off");
        assert!(!env_enabled("RVBBIT_DUCK_BACKEND_TEST_FLAG", true));
        std::env::remove_var("RVBBIT_DUCK_BACKEND_TEST_FLAG");
        assert!(env_enabled("RVBBIT_DUCK_BACKEND_TEST_FLAG", true));
    }

    #[test]
    fn parse_column_names_requires_array_strings() {
        assert_eq!(
            parse_column_names(&json!(["a", "b"])),
            vec!["a".to_string(), "b".to_string()]
        );
        assert!(parse_column_names(&json!({"a": 1})).is_empty());
    }
}
