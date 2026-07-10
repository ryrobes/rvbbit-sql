use std::cell::RefCell;
use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::fs::File;
use std::fs::OpenOptions;
use std::hash::{Hash, Hasher};
use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use arrow::array::{
    cast::{as_boolean_array, as_primitive_array, as_string_array},
    Array, ArrayRef,
};
use arrow::datatypes::{
    DataType, Float32Type, Float64Type, Int16Type, Int32Type, Int64Type, Int8Type, UInt16Type,
    UInt32Type, UInt64Type, UInt8Type,
};
use arrow::ipc::reader::StreamReader;
use arrow::record_batch::RecordBatch;
use arrow::util::display::array_value_to_string;
use pgrx::prelude::*;
use pgrx::{pg_sys, JsonB, Spi};
use serde_json::{json, Map, Value};
use std::ffi::{CStr, CString, OsStr};

const DEFAULT_DUCK_BIN: &str = "/usr/local/bin/rvbbit-duck";
const DEFAULT_GQE_BIN: &str = "/usr/local/bin/rvbbit-gqe";
const DEFAULT_MAX_ROWS: i32 = 100_000;
const DEFAULT_TIMEOUT_S: i32 = 300;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SidecarResultFormat {
    Json,
    ArrowIpcFile,
}

impl SidecarResultFormat {
    fn as_str(self) -> &'static str {
        match self {
            SidecarResultFormat::Json => "json",
            SidecarResultFormat::ArrowIpcFile => "arrow_ipc_file",
        }
    }
}

thread_local! {
    static DUCK_SESSION: RefCell<Option<DuckSession>> = const { RefCell::new(None) };
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DuckSessionKey {
    binary: String,
    dsn: String,
    engine: String,
    layout: String,
    threads: usize,
}

struct DuckSession {
    key: DuckSessionKey,
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
    stdout_buf: Vec<u8>,
}

#[derive(Clone, Debug, Hash)]
struct DuckSharedKey {
    binary: String,
    dsn: String,
    engine: String,
    layout: String,
    threads: usize,
    workers: usize,
    pgdata_prefix: String,
    visible_pgdata_prefix: String,
}

#[derive(Clone, Debug)]
struct GqeProbeCacheEntry {
    key: String,
    expires_at: Instant,
    available: bool,
    reason: String,
}

static GQE_PROBE_CACHE: OnceLock<Mutex<Option<GqeProbeCacheEntry>>> = OnceLock::new();

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
            .arg("--threads")
            .arg(key.threads.to_string())
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
            stdout,
            stdout_buf: Vec::new(),
        })
    }

    fn request(&mut self, request: &str, timeout: i32) -> Result<Value, String> {
        self.stdin
            .write_all(request.as_bytes())
            .map_err(|e| format!("persistent rvbbit-duck write failed: {e}"))?;
        self.stdin
            .write_all(b"\n")
            .map_err(|e| format!("persistent rvbbit-duck write failed: {e}"))?;
        self.stdin
            .flush()
            .map_err(|e| format!("persistent rvbbit-duck flush failed: {e}"))?;
        let response = read_line_with_timeout(
            &mut self.stdout,
            &mut self.stdout_buf,
            sidecar_io_timeout(timeout),
            "persistent rvbbit-duck",
        )?;
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

pub(crate) fn duck_backend_config_enabled() -> bool {
    let enabled = guc_setting("rvbbit.duck_backend")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND", true));
    enabled
}

pub(crate) fn gqe_backend_config_enabled() -> bool {
    guc_setting("rvbbit.gpu_gqe_backend")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_GPU_GQE_BACKEND", true))
}

pub(crate) fn backend_enabled() -> bool {
    duck_backend_config_enabled() && duck_binary().is_some()
}

pub(crate) fn duck_routes_available() -> (bool, String) {
    if !duck_backend_config_enabled() {
        return (
            false,
            "Duck sidecar routes are disabled by rvbbit.duck_backend".to_string(),
        );
    }
    match duck_binary() {
        Some(path) => (
            true,
            format!("rvbbit-duck sidecar binary available at {path}"),
        ),
        None => (
            false,
            "rvbbit-duck binary not found; Duck sidecar routes are unavailable".to_string(),
        ),
    }
}

pub(crate) fn datafusion_routes_available() -> (bool, String) {
    if datafusion_inprocess_enabled() {
        return (
            true,
            "in-process DataFusion is enabled; no rvbbit-duck binary is required".to_string(),
        );
    }
    let (duck_available, duck_reason) = duck_routes_available();
    if duck_available {
        (
            true,
            "in-process DataFusion is disabled, but DataFusion can run through rvbbit-duck"
                .to_string(),
        )
    } else {
        (
            false,
            format!("DataFusion routes unavailable: rvbbit.df_inprocess is off and {duck_reason}"),
        )
    }
}

pub(crate) fn gqe_routes_available() -> (bool, String) {
    if !gqe_backend_config_enabled() {
        return (
            false,
            "GPU/GQE routes are disabled by rvbbit.gpu_gqe_backend".to_string(),
        );
    }
    match gqe_binary() {
        Some(path) => cached_probe_gqe_binary(&path),
        None => (
            false,
            "rvbbit-gqe binary not found; GPU/GQE routes are unavailable".to_string(),
        ),
    }
}

pub(crate) fn accelerator_route_runtime_stamp() -> String {
    format!(
        "duck_cfg:{}|duck_bin:{}|df_inproc:{}|gqe_cfg:{}|gqe_bin:{}|shared:{}|native_vortex:{}",
        duck_backend_config_enabled(),
        duck_binary().as_deref().unwrap_or("missing"),
        datafusion_inprocess_enabled(),
        gqe_backend_config_enabled(),
        gqe_binary().as_deref().unwrap_or("missing"),
        shared_enabled(),
        native_vortex_enabled()
    )
}

pub(crate) fn max_rows() -> i32 {
    std::env::var("RVBBIT_DUCK_BACKEND_MAX_ROWS")
        .ok()
        .and_then(|s| s.parse::<i32>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_MAX_ROWS)
}

fn timeout_s() -> i32 {
    // GUC first so callers can bound a single sidecar call transactionally
    // (SET LOCAL) — statement_timeout can't interrupt a blocking sidecar wait,
    // so e.g. the route optimizer pins this to its bench budget; otherwise a
    // heavy candidate runs to the full default despite the 60s bench cap.
    guc_setting("rvbbit.duck_backend_timeout_s")
        .and_then(|s| s.trim().parse::<i32>().ok())
        .filter(|v| *v > 0)
        .or_else(|| {
            std::env::var("RVBBIT_DUCK_BACKEND_TIMEOUT_S")
                .ok()
                .and_then(|s| s.parse::<i32>().ok())
                .filter(|v| *v > 0)
        })
        .unwrap_or(DEFAULT_TIMEOUT_S)
}

fn duck_threads() -> usize {
    guc_setting("rvbbit.duck_threads")
        .or_else(|| std::env::var("RVBBIT_DUCK_THREADS").ok())
        .and_then(|s| s.trim().parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(4)
}

fn persistent_enabled() -> bool {
    guc_setting("rvbbit.duck_backend_persistent")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND_PERSISTENT", true))
}

fn shared_enabled() -> bool {
    guc_setting("rvbbit.duck_backend_shared")
        .map(|value| setting_enabled(&value, false))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND_SHARED", false))
}

fn shared_strict_enabled() -> bool {
    let value = guc_setting("rvbbit.duck_backend_shared_strict")
        .or_else(|| std::env::var("RVBBIT_DUCK_BACKEND_SHARED_STRICT").ok());
    shared_strict_value(value.as_deref())
}

fn shared_strict_value(value: Option<&str>) -> bool {
    value
        .map(|value| setting_enabled(value, false))
        .unwrap_or(false)
}

fn shared_target_enabled(engine: &str, layout: &str) -> bool {
    let raw = raw_shared_targets();
    shared_target_list_matches(&raw, engine, layout)
}

fn shared_target_list_matches(raw: &str, engine: &str, layout: &str) -> bool {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("all") || trimmed == "*" {
        return true;
    }
    let wanted = format!("{}:{}", engine.trim(), layout.trim()).to_ascii_lowercase();
    let engine = engine.trim().to_ascii_lowercase();
    let layout = layout.trim().to_ascii_lowercase();
    trimmed.split(',').any(|entry| {
        let entry = entry.trim().to_ascii_lowercase();
        entry == "*" || entry == "all" || entry == wanted || entry == engine || entry == layout
    })
}

fn shared_workers() -> usize {
    guc_setting("rvbbit.duck_backend_shared_workers")
        .or_else(|| std::env::var("RVBBIT_DUCK_BACKEND_SHARED_WORKERS").ok())
        .and_then(|s| s.trim().parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(4)
}

fn shared_launch_enabled() -> bool {
    guc_setting("rvbbit.duck_backend_shared_launch")
        .map(|value| setting_enabled(&value, false))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_BACKEND_SHARED_LAUNCH", false))
}

fn shared_socket_dir() -> String {
    guc_setting("rvbbit.duck_backend_shared_dir")
        .or_else(|| std::env::var("RVBBIT_DUCK_BACKEND_SHARED_DIR").ok())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "/tmp/rvbbit-duck".to_string())
}

fn shared_socket_override() -> Option<String> {
    guc_setting("rvbbit.duck_backend_shared_socket")
        .or_else(|| std::env::var("RVBBIT_DUCK_BACKEND_SHARED_SOCKET").ok())
        .filter(|value| !value.trim().is_empty())
}

fn arrow_ipc_enabled() -> bool {
    guc_setting("rvbbit.duck_arrow_ipc")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_ARROW_IPC", true))
}

fn arrow_ipc_fallback_enabled() -> bool {
    guc_setting("rvbbit.duck_arrow_ipc_fallback")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DUCK_ARROW_IPC_FALLBACK", true))
}

fn sidecar_result_format() -> SidecarResultFormat {
    if arrow_ipc_enabled() {
        SidecarResultFormat::ArrowIpcFile
    } else {
        SidecarResultFormat::Json
    }
}

/// The calling session's search_path as a normalized CSV, forwarded to the
/// sidecar so unqualified table names in the query SQL resolve to the same
/// schema Postgres would pick (e.g. public.customer vs tpcds.customer — the
/// sidecar only creates an unqualified alias view when a relname is unique
/// across schemas, so without this an ambiguous name errors and fails open to
/// native). Unresolvable entries like "$user" are passed through; the sidecar
/// filters to schemas that actually exist in its catalog.
fn session_search_path_csv() -> Option<String> {
    guc_setting("search_path").map(|raw| {
        raw.split(',')
            .map(|s| s.trim().trim_matches('"').to_string())
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(",")
    })
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
pub(crate) fn datafusion_inprocess_enabled() -> bool {
    guc_setting("rvbbit.df_inprocess")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_DF_INPROCESS", true))
}

/// Phase 0 of NATIVE_VORTEX_PLAN: gate for the in-process native+vortex reader
/// (rvbbit's CustomScan reading `.vortex` row groups via `crate::vortex_adapter`
/// instead of parquet). Default **off** — ships dark. A later phase adds a
/// per-query router decision that additionally gates activation.
pub(crate) fn native_vortex_enabled() -> bool {
    guc_setting("rvbbit.native_vortex")
        .map(|value| setting_enabled(&value, false))
        .unwrap_or_else(|| env_enabled("RVBBIT_NATIVE_VORTEX", false))
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

pub(crate) fn guc_setting(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() })
    }
}

fn duck_binary() -> Option<String> {
    if let Ok(configured) = std::env::var("RVBBIT_DUCK_BIN") {
        return resolve_duck_binary_candidate(&configured);
    }
    if executable_file(Path::new(DEFAULT_DUCK_BIN)) {
        return Some(DEFAULT_DUCK_BIN.to_string());
    }
    find_executable_on_path("rvbbit-duck", std::env::var_os("PATH").as_deref())
}

fn gqe_binary() -> Option<String> {
    if let Some(configured) = guc_setting("rvbbit.gqe_bin").filter(|value| !value.trim().is_empty())
    {
        return resolve_duck_binary_candidate(&configured);
    }
    if let Some(configured) = std::env::var("RVBBIT_GQE_BIN")
        .ok()
        .filter(|value| !value.trim().is_empty())
    {
        return resolve_duck_binary_candidate(&configured);
    }
    if executable_file(Path::new(DEFAULT_GQE_BIN)) {
        return Some(DEFAULT_GQE_BIN.to_string());
    }
    find_executable_on_path("rvbbit-gqe", std::env::var_os("PATH").as_deref())
}

fn probe_gqe_binary(path: &str) -> (bool, String) {
    let mut child = match Command::new(path)
        .arg("--rvbbit-probe")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(err) => return (false, format!("rvbbit-gqe probe failed to start: {err}")),
    };
    let output = match wait_child_output_with_timeout(
        &mut child,
        5,
        "rvbbit-gqe probe",
        Duration::from_secs(5),
    ) {
        Ok(output) => output,
        Err(err) => return (false, err),
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let parsed = serde_json::from_str::<Value>(stdout.trim()).ok();
    if let Some(value) = parsed {
        let status = value.get("status").and_then(Value::as_str).unwrap_or("");
        let detail = value
            .get("detail")
            .or_else(|| value.get("reason"))
            .or_else(|| value.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("rvbbit-gqe probe returned no detail");
        return match status {
            "ok" | "available" => (true, detail.to_string()),
            "unavailable" | "error" => (false, detail.to_string()),
            other => (
                false,
                format!("rvbbit-gqe probe returned unknown status {other:?}: {detail}"),
            ),
        };
    }
    if output.status.success() {
        (
            false,
            "rvbbit-gqe probe succeeded but returned no JSON status".to_string(),
        )
    } else {
        let stderr_line = first_line(&stderr);
        let detail = if stderr_line.is_empty() {
            "no stderr"
        } else {
            stderr_line
        };
        (false, format!("rvbbit-gqe probe failed: {detail}"))
    }
}

fn cached_probe_gqe_binary(path: &str) -> (bool, String) {
    let ttl = gqe_probe_cache_ttl();
    if ttl.is_zero() {
        return probe_gqe_binary(path);
    }

    let key = gqe_probe_cache_key(path);
    let now = Instant::now();
    let cache = GQE_PROBE_CACHE.get_or_init(|| Mutex::new(None));
    if let Ok(guard) = cache.lock() {
        if let Some(entry) = guard.as_ref() {
            if entry.key == key && now < entry.expires_at {
                return (entry.available, entry.reason.clone());
            }
        }
    }

    let (available, reason) = probe_gqe_binary(path);
    if let Ok(mut guard) = cache.lock() {
        *guard = Some(GqeProbeCacheEntry {
            key,
            expires_at: now + ttl,
            available,
            reason: reason.clone(),
        });
    }
    (available, reason)
}

fn gqe_probe_cache_ttl() -> Duration {
    Duration::from_millis(gqe_probe_cache_ttl_ms())
}

fn gqe_probe_cache_ttl_ms() -> u64 {
    guc_setting("rvbbit.gqe_probe_cache_ttl_ms")
        .or_else(|| std::env::var("RVBBIT_GQE_PROBE_CACHE_TTL_MS").ok())
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(10_000)
}

fn gqe_probe_cache_key(path: &str) -> String {
    format!(
        "path:{}|backend:{}|server:{}|cli:{}|node:{}|task:{}|num_gpus:{}",
        path,
        gqe_backend_config_enabled(),
        std::env::var("RVBBIT_GQE_SERVER_URL").unwrap_or_default(),
        std::env::var("RVBBIT_GQE_CLI").unwrap_or_default(),
        std::env::var("RVBBIT_GQE_NODE_MANAGER").unwrap_or_default(),
        std::env::var("RVBBIT_GQE_TASK_MANAGER").unwrap_or_default(),
        std::env::var("RVBBIT_GQE_NUM_GPUS").unwrap_or_default(),
    )
}

fn gqe_route_gate_enabled() -> bool {
    // Default ON, in sync with the router's candidate gate (router.rs
    // candidate_gate_enabled). The gate is inert without a GQE binary — the
    // runtime check gates actual availability — so defaulting on just avoids a
    // manual opt-in on GQE-capable machines.
    guc_setting("rvbbit.route_gpu_gqe")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_ROUTE_GPU_GQE", true))
}

fn raw_shared_targets() -> String {
    guc_setting("rvbbit.duck_backend_shared_targets")
        .or_else(|| std::env::var("RVBBIT_DUCK_BACKEND_SHARED_TARGETS").ok())
        .unwrap_or_default()
}

pub(crate) fn accelerator_runtime_status_value(live: bool) -> Value {
    let duck_config_enabled = duck_backend_config_enabled();
    let duck_binary = duck_binary();
    let duck_routes_available = duck_config_enabled && duck_binary.is_some();
    let gqe_config_enabled = gqe_backend_config_enabled();
    let gqe_binary = gqe_binary();
    let (gqe_routes_available, gqe_reason) = if gqe_config_enabled {
        match gqe_binary.as_deref() {
            Some(path) if live => probe_gqe_binary(path),
            Some(path) => cached_probe_gqe_binary(path),
            None => (
                false,
                "rvbbit-gqe binary not found; GPU/GQE routes are unavailable".to_string(),
            ),
        }
    } else {
        (
            false,
            "GPU/GQE routes are disabled by rvbbit.gpu_gqe_backend".to_string(),
        )
    };
    let gqe_route_gate_enabled = gqe_route_gate_enabled();
    let df_inprocess = datafusion_inprocess_enabled();
    let datafusion_routes_available = df_inprocess || duck_routes_available;
    let shared = shared_enabled();
    let persistent = persistent_enabled();
    let mut recommendations = Vec::new();

    if duck_config_enabled && duck_binary.is_none() {
        recommendations.push(json!({
            "severity": "info",
            "action": "install_rvbbit_duck",
            "detail": "Install rvbbit-duck at /usr/local/bin/rvbbit-duck or set RVBBIT_DUCK_BIN to enable Duck-backed routes."
        }));
    }
    if !df_inprocess && !duck_routes_available {
        recommendations.push(json!({
            "severity": "warn",
            "action": "enable_datafusion_or_install_sidecar",
            "detail": "Enable rvbbit.df_inprocess or install rvbbit-duck; otherwise DataFusion routes are unavailable and routing falls back to native/PostgreSQL paths."
        }));
    }
    // GQE is an OPTIONAL accelerator: when it's unavailable, native/duck/datafusion
    // still route, so its absence never degrades the system and must not raise the
    // overall status to "warn". With the route gate now default-on, "gate on but
    // GQE unavailable" is the normal state on every non-GPU box — so this is only
    // an informational note, and only when a binary is actually present but won't
    // serve routes (a probe failure worth surfacing on a GPU box).
    if gqe_route_gate_enabled && gqe_config_enabled && gqe_binary.is_some() && !gqe_routes_available
    {
        recommendations.push(json!({
            "severity": "info",
            "action": "check_rvbbit_gqe",
            "detail": format!("GPU/GQE binary is present but routes are unavailable: {gqe_reason}")
        }));
    }

    let (shared_socket, shared_online) = if let Some(binary) = duck_binary.as_deref() {
        let socket =
            shared_socket_hint_no_create(binary, &duck_dsn(), "duck", "vortex", duck_threads())
                .ok();
        let online = if live && shared {
            socket
                .as_deref()
                .map(|path| UnixStream::connect(path).is_ok())
        } else {
            None
        };
        (socket, online)
    } else {
        (None, None)
    };

    if shared && duck_routes_available && live && shared_online == Some(false) {
        recommendations.push(json!({
            "severity": "warn",
            "action": "start_or_check_shared_broker",
            "detail": "Shared Duck broker is enabled but the derived broker socket was not reachable; queries will fall back to local persistent or one-shot sidecars."
        }));
    }

    // GQE availability is deliberately NOT part of the warn condition: it's an
    // optional accelerator with a full native/duck/datafusion fallback, so its
    // absence (the norm on any non-GPU box, now that the gate defaults on) is not
    // a degraded state. Its status is surfaced under "gpu_gqe" + the info note above.
    let status = if (!datafusion_routes_available && !duck_routes_available)
        || (shared && duck_routes_available && live && shared_online == Some(false))
    {
        "warn"
    } else {
        "ok"
    };

    json!({
        "status": status,
        "duck_backend_enabled": duck_routes_available,
        "duck": {
            "config_enabled": duck_config_enabled,
            "binary_found": duck_binary.is_some(),
            "binary_path": duck_binary,
            "routes_available": duck_routes_available,
            "impact_if_unavailable": "Duck-backed routes are skipped; DataFusion in-process, native Rvbbit, and PostgreSQL rowstore paths can still run."
        },
        "datafusion": {
            "inprocess_enabled": df_inprocess,
            "routes_available": datafusion_routes_available,
            "mode": if df_inprocess {
                "in_process"
            } else if duck_routes_available {
                "rvbbit_duck_sidecar"
            } else {
                "unavailable"
            },
            "impact_if_unavailable": "DataFusion candidates are skipped; routing falls back to Duck, native Rvbbit, or PostgreSQL rowstore when available."
        },
        "gpu_gqe": {
            "config_enabled": gqe_config_enabled,
            "route_gate_enabled": gqe_route_gate_enabled,
            "binary_found": gqe_binary.is_some(),
            "binary_path": gqe_binary,
            "probe_cache_ttl_ms": gqe_probe_cache_ttl_ms(),
            "routes_available": gqe_routes_available,
            "reason": gqe_reason,
            "protocol": "rvbbit sidecar JSON/Arrow IPC bridge",
            "impact_if_unavailable": "gpu_gqe is skipped unless forced; native Rvbbit and PostgreSQL rowstore remain available."
        },
        "native": {
            "custom_scan_available": true,
            "native_vortex_enabled": native_vortex_enabled()
        },
        "sidecar": {
            "persistent_enabled": persistent,
            "shared_enabled": shared,
            "shared_strict": shared_strict_enabled(),
            "shared_targets": raw_shared_targets(),
            "shared_workers": shared_workers(),
            "shared_socket_duck_vortex": shared_socket,
            "shared_socket_online": shared_online,
            "result_format": sidecar_result_format().as_str(),
            "fail_open": fail_open_enabled()
        },
        "limits": {
            "max_rows": max_rows(),
            "timeout_s": timeout_s(),
            "threads": duck_threads()
        },
        "recommendations": recommendations
    })
}

#[pg_extern]
fn accelerator_runtime_status(live: default!(bool, "false")) -> JsonB {
    JsonB(accelerator_runtime_status_value(live))
}

fn resolve_duck_binary_candidate(candidate: &str) -> Option<String> {
    let candidate = candidate.trim();
    if candidate.is_empty() {
        return None;
    }
    if candidate.contains('/') {
        return executable_file(Path::new(candidate)).then_some(candidate.to_string());
    }
    find_executable_on_path(candidate, std::env::var_os("PATH").as_deref())
}

fn find_executable_on_path(name: &str, path_env: Option<&OsStr>) -> Option<String> {
    let path_env = path_env?;
    for dir in std::env::split_paths(path_env) {
        let candidate = dir.join(name);
        if executable_file(&candidate) {
            return Some(candidate.to_string_lossy().into_owned());
        }
    }
    None
}

fn executable_file(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
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

#[pg_extern(volatile)]
fn gpu_gqe_query_json(query: &str, column_names: JsonB, max_rows: i32) -> JsonB {
    engine_query_json("gpu_gqe", "scan", query, column_names, max_rows)
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
    let result_format = sidecar_result_format();
    let threads = duck_threads();
    let mut sidecar_context: Option<(String, String, i32)> = None;

    // Phase 1: in-process DataFusion path. Only takes the datafusion engine
    // (DuckDB still goes through the sidecar). If we hit an error, fall
    // through to the sidecar path — safe rollback by design.
    let inprocess_payload = if engine == "datafusion" && datafusion_inprocess_enabled() {
        match crate::df::query_engine(layout, query, max_rows) {
            Ok(payload) => Some(payload),
            Err(err) => {
                // ops-03: vortex (like mem) falls back to native/heap, not the
                // sidecar — re-reading the same .vortex via the sidecar would just
                // re-fail. Every layout must honor "heap is always the fallback".
                if matches!(layout, "mem" | "memory" | "vortex" | "vortex_scan") {
                    return fail_open_or_error(engine, query, max_rows, &err);
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

    let mut payload = if let Some(p) = inprocess_payload {
        p
    } else {
        if engine == "gpu_gqe" {
            if !gqe_backend_config_enabled() {
                return fail_open_or_error(
                    engine,
                    query,
                    max_rows,
                    "GPU/GQE sidecar routes are disabled by rvbbit.gpu_gqe_backend",
                );
            }
            let binary = match gqe_binary() {
                Some(binary) => binary,
                None => {
                    return fail_open_or_error(
                        engine,
                        query,
                        max_rows,
                        "rvbbit-gqe binary not found",
                    );
                }
            };
            let dsn = duck_dsn();
            let timeout = timeout_s();
            sidecar_context = Some((binary.clone(), dsn.clone(), timeout));
            match run_engine_query(
                engine,
                layout,
                &binary,
                &dsn,
                query,
                max_rows,
                timeout,
                threads,
                result_format,
            ) {
                Ok(p) => p,
                Err(err) => return fail_open_or_error(engine, query, max_rows, &err),
            }
        } else {
            if !duck_backend_config_enabled() {
                return fail_open_or_error(
                    engine,
                    query,
                    max_rows,
                    "rvbbit-duck sidecar routes are disabled by rvbbit.duck_backend",
                );
            }
            let binary = match duck_binary() {
                Some(binary) => binary,
                None => {
                    return fail_open_or_error(
                        engine,
                        query,
                        max_rows,
                        "rvbbit-duck binary not found",
                    );
                }
            };
            let dsn = duck_dsn();
            let timeout = timeout_s();
            sidecar_context = Some((binary.clone(), dsn.clone(), timeout));
            match run_engine_query(
                engine,
                layout,
                &binary,
                &dsn,
                query,
                max_rows,
                timeout,
                threads,
                result_format,
            ) {
                Ok(p) => p,
                // ops-03: vortex sidecar errors fall open to native/heap like every other layout.
                Err(err) => return fail_open_or_error(engine, query, max_rows, &err),
            }
        }
    };

    if payload.get("status").and_then(Value::as_str) != Some("ok") {
        let err = payload
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("rvbbit-duck returned non-ok status");
        if result_format == SidecarResultFormat::ArrowIpcFile
            && arrow_ipc_fallback_enabled()
            && sidecar_context.is_some()
        {
            pgrx::warning!(
                "rvbbit.{engine}_query_json: Arrow IPC sidecar response failed ({err}); retrying JSON sidecar transport"
            );
            let (binary, dsn, timeout) = sidecar_context.as_ref().unwrap();
            match run_engine_query(
                engine,
                layout,
                binary,
                dsn,
                query,
                max_rows,
                *timeout,
                threads,
                SidecarResultFormat::Json,
            ) {
                Ok(fallback_payload)
                    if fallback_payload.get("status").and_then(Value::as_str) == Some("ok") =>
                {
                    payload = fallback_payload;
                }
                Ok(fallback_payload) => {
                    let fallback_err = fallback_payload
                        .get("error")
                        .and_then(Value::as_str)
                        .unwrap_or("rvbbit-duck returned non-ok status");
                    return fail_open_or_error(engine, query, max_rows, fallback_err);
                }
                Err(fallback_err) => {
                    return fail_open_or_error(engine, query, max_rows, &fallback_err);
                }
            }
        } else {
            return fail_open_or_error(engine, query, max_rows, err);
        }
    }

    let out = match payload_to_json_objects(&payload, &columns, max_rows) {
        Ok(out) => out,
        Err(err)
            if result_format == SidecarResultFormat::ArrowIpcFile
                && arrow_ipc_fallback_enabled()
                && sidecar_context.is_some() =>
        {
            pgrx::warning!(
                "rvbbit.{engine}_query_json: Arrow IPC decode failed ({err}); retrying JSON sidecar transport"
            );
            let (binary, dsn, timeout) = sidecar_context.as_ref().unwrap();
            let fallback_payload = match run_engine_query(
                engine,
                layout,
                binary,
                dsn,
                query,
                max_rows,
                *timeout,
                threads,
                SidecarResultFormat::Json,
            ) {
                Ok(p) => p,
                // ops-03: vortex falls open to native/heap like every other layout.
                Err(sidecar_err) => {
                    return fail_open_or_error(engine, query, max_rows, &sidecar_err)
                }
            };
            if fallback_payload.get("status").and_then(Value::as_str) != Some("ok") {
                let err = fallback_payload
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("rvbbit-duck returned non-ok status");
                return fail_open_or_error(engine, query, max_rows, err);
            }
            match payload_to_json_objects(&fallback_payload, &columns, max_rows) {
                Ok(out) => out,
                Err(json_err) => return fail_open_or_error(engine, query, max_rows, &json_err),
            }
        }
        Err(err) => return fail_open_or_error(engine, query, max_rows, &err),
    };

    if env_enabled("RVBBIT_DUCK_BACKEND_OBSERVE", false) {
        let candidate = match (engine, layout) {
            ("datafusion", "mem") => "datafusion_mem",
            ("datafusion", "hive") => "datafusion_hive",
            ("datafusion", "vortex") => "datafusion_vortex",
            ("datafusion", _) => "datafusion_vector",
            ("gpu_gqe", _) => "gpu_gqe",
            ("duck", "vortex") => "duck_vortex",
            (_, "hive") => "duck_hive",
            _ => "duck_vector",
        };
        record_engine_observation(query, candidate, start.elapsed().as_secs_f64() * 1000.0);
    }
    JsonB(Value::Array(out))
}

fn payload_to_json_objects(
    payload: &Value,
    columns: &[String],
    max_rows: i32,
) -> Result<Vec<Value>, String> {
    match payload
        .get("result_format")
        .and_then(Value::as_str)
        .unwrap_or("json")
    {
        "arrow_ipc_file" => arrow_payload_to_json_objects(payload, columns, max_rows),
        _ => json_payload_to_json_objects(payload, columns, max_rows),
    }
}

fn json_payload_to_json_objects(
    payload: &Value,
    columns: &[String],
    max_rows: i32,
) -> Result<Vec<Value>, String> {
    let row_count = payload
        .get("row_count")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let Some(rows) = payload.get("rows").and_then(Value::as_array) else {
        return Err("rvbbit-duck returned no rows array".to_string());
    };
    if row_count > rows.len() {
        return Err(format!(
            "result has {row_count} row(s), exceeding backend cap {max_rows}"
        ));
    }

    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(values) = row.as_array() else {
            return Err("row is not a JSON array".to_string());
        };
        out.push(values_to_json_object(values, columns)?);
    }
    Ok(out)
}

fn arrow_payload_to_json_objects(
    payload: &Value,
    columns: &[String],
    max_rows: i32,
) -> Result<Vec<Value>, String> {
    let row_count = payload
        .get("row_count")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let Some(path) = payload.get("arrow_ipc_path").and_then(Value::as_str) else {
        if row_count == 0 {
            return Ok(Vec::new());
        }
        return Err("rvbbit-duck returned no Arrow IPC path".to_string());
    };
    let result = read_arrow_ipc_objects(path, columns, max_rows.max(1) as usize);
    let _ = fs::remove_file(path);
    let out = result?;
    if row_count > out.len() {
        return Err(format!(
            "result has {row_count} row(s), exceeding backend cap {max_rows}"
        ));
    }
    Ok(out)
}

fn read_arrow_ipc_objects(
    path: &str,
    columns: &[String],
    max_rows: usize,
) -> Result<Vec<Value>, String> {
    let file = File::open(path).map_err(|e| format!("opening Arrow IPC file {path}: {e}"))?;
    let reader = StreamReader::try_new(file, None)
        .map_err(|e| format!("reading Arrow IPC stream {path}: {e}"))?;
    let mut out = Vec::new();
    for batch in reader {
        let batch = batch.map_err(|e| format!("reading Arrow IPC batch {path}: {e}"))?;
        if batch.num_columns() != columns.len() {
            return Err(format!(
                "Arrow IPC row width {} does not match expected width {}",
                batch.num_columns(),
                columns.len()
            ));
        }
        for row_idx in 0..batch.num_rows() {
            if out.len() >= max_rows {
                return Ok(out);
            }
            out.push(arrow_row_to_json_object(&batch, row_idx, columns)?);
        }
    }
    Ok(out)
}

fn values_to_json_object(values: &[Value], columns: &[String]) -> Result<Value, String> {
    if values.len() != columns.len() {
        return Err(format!(
            "row width {} does not match expected width {}",
            values.len(),
            columns.len()
        ));
    }
    let mut obj = Map::with_capacity(columns.len());
    for (name, value) in columns.iter().zip(values.iter()) {
        obj.insert(name.clone(), value.clone());
    }
    Ok(Value::Object(obj))
}

fn arrow_row_to_json_object(
    batch: &RecordBatch,
    row_idx: usize,
    columns: &[String],
) -> Result<Value, String> {
    let mut obj = Map::with_capacity(columns.len());
    for (col_idx, name) in columns.iter().enumerate() {
        obj.insert(
            name.clone(),
            arrow_value_to_json(batch.column(col_idx), row_idx)?,
        );
    }
    Ok(Value::Object(obj))
}

fn arrow_value_to_json(array: &ArrayRef, row_idx: usize) -> Result<Value, String> {
    if array.is_null(row_idx) {
        return Ok(Value::Null);
    }
    let value = match array.data_type() {
        DataType::Boolean => json!(as_boolean_array(array.as_ref()).value(row_idx)),
        DataType::Int8 => json!(as_primitive_array::<Int8Type>(array.as_ref()).value(row_idx)),
        DataType::Int16 => json!(as_primitive_array::<Int16Type>(array.as_ref()).value(row_idx)),
        DataType::Int32 => json!(as_primitive_array::<Int32Type>(array.as_ref()).value(row_idx)),
        DataType::Int64 => json!(as_primitive_array::<Int64Type>(array.as_ref()).value(row_idx)),
        DataType::UInt8 => json!(as_primitive_array::<UInt8Type>(array.as_ref()).value(row_idx)),
        DataType::UInt16 => json!(as_primitive_array::<UInt16Type>(array.as_ref()).value(row_idx)),
        DataType::UInt32 => json!(as_primitive_array::<UInt32Type>(array.as_ref()).value(row_idx)),
        DataType::UInt64 => json!(as_primitive_array::<UInt64Type>(array.as_ref()).value(row_idx)),
        DataType::Float32 => {
            json!(as_primitive_array::<Float32Type>(array.as_ref()).value(row_idx))
        }
        DataType::Float64 => {
            json!(as_primitive_array::<Float64Type>(array.as_ref()).value(row_idx))
        }
        DataType::Utf8 => json!(as_string_array(array.as_ref()).value(row_idx)),
        DataType::Date32 | DataType::Timestamp(_, _) => {
            json!(array_value_to_string(array.as_ref(), row_idx).map_err(|e| e.to_string())?)
        }
        _ => {
            json!(array_value_to_string(array.as_ref(), row_idx).map_err(|e| e.to_string())?)
        }
    };
    Ok(value)
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
    let out = out.unwrap_or_else(|| JsonB(json!([])));
    // ops-04: the LIMIT above is max_rows+1 purely to detect overflow. If we hit
    // it, the true result exceeds the engine row cap — fail loudly rather than
    // silently return a truncated, order-undefined set (this is the fail-open
    // fallback that is supposed to be heap-equivalent). Running uncapped instead
    // would risk OOM materializing the whole result as one jsonb, so we error and
    // let the caller add a LIMIT or raise the cap.
    if out
        .0
        .as_array()
        .is_some_and(|arr| arr.len() as i64 > max_rows.max(1) as i64)
    {
        return Err(format!(
            "result exceeds rvbbit.duck_backend_max_rows ({max_rows}); add a LIMIT to the query \
             or raise rvbbit.duck_backend_max_rows to materialize the full result"
        ));
    }
    Ok(out)
}

fn run_engine_query(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
    threads: usize,
    result_format: SidecarResultFormat,
) -> Result<Value, String> {
    // Fleet mode: an explicit remote engine endpoint overrides BOTH local
    // topologies (shared daemon / per-backend sidecar) — the request goes to a
    // rvbbit-duck --serve-tcp worker on another node, which reads PUBLISHED
    // artifacts from shared object storage. A remote failure falls through to
    // the normal local paths below — fail-open: a dead warren is a slower
    // query, never an error the user sees.
    if let Some(endpoint) = fleet_endpoint() {
        match send_fleet_request(&endpoint, query, max_rows, timeout, threads, result_format) {
            Ok(value) => return Ok(value),
            Err(err) => {
                pgrx::warning!(
                    "rvbbit: fleet endpoint {endpoint} failed ({err}); falling back to local engine"
                );
            }
        }
    }
    if shared_enabled() && shared_target_enabled(engine, layout) {
        execute_shared(
            engine,
            layout,
            binary,
            dsn,
            query,
            max_rows,
            timeout,
            threads,
            result_format,
        )
        .or_else(|shared_err| {
            if shared_strict_enabled() {
                return Err(format!(
                    "shared rvbbit-duck failed with rvbbit.duck_backend_shared_strict enabled: {shared_err}"
                ));
            }
            pgrx::warning!(
                "rvbbit.{engine}_query_json: shared rvbbit-duck failed ({shared_err}); falling back to per-backend sidecar"
            );
            let socket_path = shared_socket_hint(binary, dsn, engine, layout, threads).ok();
            if persistent_enabled() {
                crate::duck_telemetry::record_shared_fallback(
                    engine,
                    layout,
                    socket_path.as_deref(),
                    &shared_err,
                    "local_persistent_or_oneshot",
                    query,
                );
                execute_persistent(
                    engine,
                    layout,
                    binary,
                    dsn,
                    query,
                    max_rows,
                    timeout,
                    threads,
                    result_format,
                )
                .or_else(|_| {
                    execute_engine_oneshot(
                        engine,
                        layout,
                        binary,
                        dsn,
                        query,
                        max_rows,
                        timeout,
                        threads,
                        result_format,
                    )
                })
            } else {
                crate::duck_telemetry::record_shared_fallback(
                    engine,
                    layout,
                    socket_path.as_deref(),
                    &shared_err,
                    "local_oneshot",
                    query,
                );
                execute_engine_oneshot(
                    engine,
                    layout,
                    binary,
                    dsn,
                    query,
                    max_rows,
                    timeout,
                    threads,
                    result_format,
                )
            }
        })
    } else if persistent_enabled() {
        execute_persistent(
            engine,
            layout,
            binary,
            dsn,
            query,
            max_rows,
            timeout,
            threads,
            result_format,
        )
        .or_else(|_| {
            execute_engine_oneshot(
                engine,
                layout,
                binary,
                dsn,
                query,
                max_rows,
                timeout,
                threads,
                result_format,
            )
        })
    } else {
        execute_engine_oneshot(
            engine,
            layout,
            binary,
            dsn,
            query,
            max_rows,
            timeout,
            threads,
            result_format,
        )
    }
}

fn shared_socket_hint(
    binary: &str,
    dsn: &str,
    engine: &str,
    layout: &str,
    threads: usize,
) -> Result<String, String> {
    let key = DuckSharedKey {
        binary: binary.to_string(),
        dsn: dsn.to_string(),
        engine: engine.to_string(),
        layout: layout.to_string(),
        threads,
        workers: shared_workers(),
        pgdata_prefix: pgdata_prefix(),
        visible_pgdata_prefix: visible_pgdata_prefix(),
    };
    shared_socket_path(&key)
}

fn shared_socket_hint_no_create(
    binary: &str,
    dsn: &str,
    engine: &str,
    layout: &str,
    threads: usize,
) -> Result<String, String> {
    let key = DuckSharedKey {
        binary: binary.to_string(),
        dsn: dsn.to_string(),
        engine: engine.to_string(),
        layout: layout.to_string(),
        threads,
        workers: shared_workers(),
        pgdata_prefix: pgdata_prefix(),
        visible_pgdata_prefix: visible_pgdata_prefix(),
    };
    shared_socket_path_no_create(&key)
}

fn execute_shared(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
    threads: usize,
    result_format: SidecarResultFormat,
) -> Result<Value, String> {
    let key = DuckSharedKey {
        binary: binary.to_string(),
        dsn: dsn.to_string(),
        engine: engine.to_string(),
        layout: layout.to_string(),
        threads,
        workers: shared_workers(),
        pgdata_prefix: pgdata_prefix(),
        visible_pgdata_prefix: visible_pgdata_prefix(),
    };
    let request = serde_json::to_string(&json!({
        "sql": query,
        "repeat": 1,
        "timeout_s": timeout,
        "max_rows": max_rows,
        "threads": threads,
        "result_format": result_format.as_str(),
        "search_path": session_search_path_csv(),
    }))
    .map_err(|e| e.to_string())?;
    let socket_path = shared_socket_path(&key)?;
    match send_shared_request(&socket_path, &request, timeout) {
        Ok(value) => Ok(value),
        Err(first) => {
            if !shared_launch_enabled() {
                return Err(first);
            }
            ensure_shared_daemon(&key, &socket_path)?;
            let wait_deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match send_shared_request(&socket_path, &request, timeout) {
                    Ok(value) => return Ok(value),
                    Err(err) if Instant::now() < wait_deadline => {
                        thread::sleep(Duration::from_millis(25));
                        if err.contains("Connection refused") || err.contains("No such file") {
                            continue;
                        }
                        return Err(format!("{first}; retry failed: {err}"));
                    }
                    Err(err) => return Err(format!("{first}; retry failed: {err}")),
                }
            }
        }
    }
}

fn shared_socket_path(key: &DuckSharedKey) -> Result<String, String> {
    if let Some(path) = shared_socket_override() {
        if let Some(parent) = Path::new(&path).parent() {
            fs::create_dir_all(parent).map_err(|e| {
                format!("creating shared rvbbit-duck dir {}: {e}", parent.display())
            })?;
        }
        return Ok(path);
    }
    let dir = shared_socket_dir();
    fs::create_dir_all(&dir).map_err(|e| format!("creating shared rvbbit-duck dir {dir}: {e}"))?;
    Ok(derived_shared_socket_path(&dir, key))
}

fn shared_socket_path_no_create(key: &DuckSharedKey) -> Result<String, String> {
    if let Some(path) = shared_socket_override() {
        return Ok(path);
    }
    let dir = shared_socket_dir();
    Ok(derived_shared_socket_path(&dir, key))
}

fn derived_shared_socket_path(dir: &str, key: &DuckSharedKey) -> String {
    let mut hasher = DefaultHasher::new();
    key.hash(&mut hasher);
    format!(
        "{}/rvbbit-duck-{:016x}.sock",
        dir.trim_end_matches('/'),
        hasher.finish()
    )
}

/// Fleet mode gate. Resolution order:
///   1. `SET rvbbit.duck_fleet_endpoint = 'host:port'` — session override/pin
///      (same pattern as route_force_candidate).
///   2. The registry (rvbbit.fleet_endpoints): the enabled endpoint whose last
///      probe succeeded, most recently probed first — so a node that failed
///      its health check stops receiving work without operator action.
/// Empty/absent = local execution.
pub(crate) fn fleet_endpoint() -> Option<String> {
    let cname = std::ffi::CString::new("rvbbit.duck_fleet_endpoint").ok()?;
    let ptr = unsafe { pgrx::pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if !ptr.is_null() {
        let value = unsafe { std::ffi::CStr::from_ptr(ptr).to_string_lossy().into_owned() };
        let value = value.trim().to_string();
        if !value.is_empty() {
            return Some(value);
        }
    }
    // Registry fallback — tolerate pre-0136 catalogs. One indexed read per
    // routed engine query; cheap relative to the scan it precedes.
    pgrx::Spi::get_one::<String>(
        "SELECT endpoint FROM rvbbit.fleet_endpoints \
         WHERE enabled AND last_probe_ok \
         ORDER BY last_probe_at DESC NULLS LAST, name LIMIT 1",
    )
    .ok()
    .flatten()
}

/// rvbbit.fleet_probe(name) — send `SELECT 1` through the fleet transport to a
/// registered endpoint and record the outcome on its registry row. The health
/// half of the registry: a failed probe takes the node out of the dispatch
/// rotation (see fleet_endpoint) until a later probe succeeds.
#[pg_extern]
fn fleet_probe(node_name: &str) -> pgrx::JsonB {
    let endpoint = pgrx::Spi::get_one_with_args::<String>(
        "SELECT endpoint FROM rvbbit.fleet_endpoints WHERE name = $1",
        &[node_name.into()],
    )
    .ok()
    .flatten()
    .unwrap_or_else(|| pgrx::error!("rvbbit.fleet_probe: unknown fleet node '{node_name}'"));
    let started = std::time::Instant::now();
    // prewarm is the health check with teeth: it proves transport, token,
    // the worker's DSN back to this catalog, AND artifact visibility — not
    // just a TCP accept. (Plain SELECT 1 would be rejected by the engine's
    // safety gate anyway: no rvbbit tables referenced.)
    let result = send_fleet_json(&endpoint, json!({"command": "prewarm"}), 15);
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
    let (ok, error) = match &result {
        Ok(value) => {
            let status_ok = value.get("status").and_then(|s| s.as_str()) == Some("ok");
            if status_ok {
                (true, None)
            } else {
                let err = value
                    .get("error")
                    .and_then(|e| e.as_str())
                    .unwrap_or("engine returned non-ok status")
                    .to_string();
                (false, Some(err))
            }
        }
        Err(e) => (false, Some(e.clone())),
    };
    let _ = pgrx::Spi::run_with_args(
        "UPDATE rvbbit.fleet_endpoints \
         SET last_probe_at = clock_timestamp(), last_probe_ms = $2, \
             last_probe_ok = $3, last_probe_error = $4 \
         WHERE name = $1",
        &[
            node_name.into(),
            elapsed_ms.into(),
            ok.into(),
            error.clone().into(),
        ],
    );
    pgrx::JsonB(serde_json::json!({
        "name": node_name,
        "endpoint": endpoint,
        "ok": ok,
        "probe_ms": (elapsed_ms * 10.0).round() / 10.0,
        "error": error,
    }))
}

/// One JSONL request/response over TCP to a fleet engine worker. The shared
/// token rides in the request body (from the brain's environment, never PG);
/// the remote's own DSN/catalog resolves paths, so the request carries only
/// the query envelope — same contract as the local daemon.
fn send_fleet_request(
    endpoint: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
    threads: usize,
    result_format: SidecarResultFormat,
) -> Result<Value, String> {
    send_fleet_json(
        endpoint,
        json!({
            "sql": query,
            "repeat": 1,
            "timeout_s": timeout,
            "max_rows": max_rows,
            "threads": threads,
            "result_format": result_format.as_str(),
            "search_path": session_search_path_csv(),
        }),
        timeout,
    )
}

/// Send one JSONL body to a fleet endpoint, injecting the shared token from
/// the brain's environment. Command bodies (prewarm probes) and query bodies
/// share this transport.
fn send_fleet_json(endpoint: &str, mut body: Value, timeout: i32) -> Result<Value, String> {
    let token = std::env::var("RVBBIT_ENGINE_TOKEN")
        .map_err(|_| "fleet endpoint configured but RVBBIT_ENGINE_TOKEN is not in the server environment".to_string())?;
    if let Some(map) = body.as_object_mut() {
        map.insert("token".to_string(), Value::String(token));
    }
    let request = serde_json::to_string(&body).map_err(|e| e.to_string())?;
    let io_timeout = sidecar_io_timeout(timeout);
    let mut stream = std::net::TcpStream::connect(endpoint)
        .map_err(|e| format!("connecting to fleet engine {endpoint}: {e}"))?;
    let _ = stream.set_read_timeout(Some(io_timeout));
    let _ = stream.set_write_timeout(Some(io_timeout));
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("fleet engine write failed: {e}"))?;
    stream
        .write_all(b"\n")
        .map_err(|e| format!("fleet engine write failed: {e}"))?;
    stream
        .flush()
        .map_err(|e| format!("fleet engine flush failed: {e}"))?;
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    let bytes = reader
        .read_line(&mut response)
        .map_err(|e| format!("fleet engine read failed: {e}"))?;
    if bytes == 0 {
        return Err("fleet engine returned no response".to_string());
    }
    serde_json::from_str(response.trim_end())
        .map_err(|e| format!("invalid fleet engine JSON: {e}"))
}

fn send_shared_request(socket_path: &str, request: &str, timeout: i32) -> Result<Value, String> {
    let mut stream = UnixStream::connect(socket_path)
        .map_err(|e| format!("connecting to shared rvbbit-duck {socket_path}: {e}"))?;
    let io_timeout = sidecar_io_timeout(timeout);
    let _ = stream.set_read_timeout(Some(io_timeout));
    let _ = stream.set_write_timeout(Some(io_timeout));
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("shared rvbbit-duck write failed: {e}"))?;
    stream
        .write_all(b"\n")
        .map_err(|e| format!("shared rvbbit-duck write failed: {e}"))?;
    stream
        .flush()
        .map_err(|e| format!("shared rvbbit-duck flush failed: {e}"))?;
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    let bytes = reader
        .read_line(&mut response)
        .map_err(|e| format!("shared rvbbit-duck read failed: {e}"))?;
    if bytes == 0 {
        return Err("shared rvbbit-duck returned no response".to_string());
    }
    serde_json::from_str(response.trim_end())
        .map_err(|e| format!("invalid shared rvbbit-duck JSON: {e}"))
}

fn ensure_shared_daemon(key: &DuckSharedKey, socket_path: &str) -> Result<(), String> {
    let lock_path = format!("{socket_path}.lock");
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&lock_path)
    {
        Ok(_lock) => {
            // resources-04: always remove the lock file, even when spawn fails.
            // Previously the `?` early-return on a transient spawn error (EAGAIN,
            // binary momentarily missing) leaked the lock, permanently blocking
            // auto-launch of the shared daemon until an operator deleted it.
            let spawn = spawn_shared_daemon(key, socket_path);
            let result = match spawn {
                Ok(()) => wait_for_shared_socket(socket_path, Duration::from_secs(5)),
                Err(e) => Err(e),
            };
            let _ = fs::remove_file(&lock_path);
            result
        }
        Err(_) => wait_for_shared_socket(socket_path, Duration::from_secs(5)),
    }
}

fn spawn_shared_daemon(key: &DuckSharedKey, socket_path: &str) -> Result<(), String> {
    Command::new(&key.binary)
        .arg("--serve-socket")
        .arg(socket_path)
        .arg("--workers")
        .arg(key.workers.to_string())
        .arg("--engine")
        .arg(&key.engine)
        .arg("--layout")
        .arg(&key.layout)
        .arg("--dsn")
        .arg(&key.dsn)
        .arg("--threads")
        .arg(key.threads.to_string())
        .arg("--pgdata-prefix")
        .arg(&key.pgdata_prefix)
        .arg("--visible-pgdata-prefix")
        .arg(&key.visible_pgdata_prefix)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("starting shared rvbbit-duck daemon: {e}"))?;
    Ok(())
}

fn wait_for_shared_socket(socket_path: &str, timeout: Duration) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if UnixStream::connect(socket_path).is_ok() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(25));
    }
    Err(format!(
        "shared rvbbit-duck socket {socket_path} did not become ready"
    ))
}

fn execute_persistent(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
    threads: usize,
    result_format: SidecarResultFormat,
) -> Result<Value, String> {
    let key = DuckSessionKey {
        binary: binary.to_string(),
        dsn: dsn.to_string(),
        engine: engine.to_string(),
        layout: layout.to_string(),
        threads,
    };
    let request = serde_json::to_string(&json!({
        "sql": query,
        "repeat": 1,
        "timeout_s": timeout,
        "max_rows": max_rows,
        "threads": threads,
        "result_format": result_format.as_str(),
        "search_path": session_search_path_csv(),
    }))
    .map_err(|e| e.to_string())?;
    DUCK_SESSION.with(|slot| {
        let mut slot = slot.borrow_mut();
        match send_persistent_request(&mut slot, &key, &request, timeout) {
            Ok(value) => Ok(value),
            Err(first) => {
                slot.take();
                send_persistent_request(&mut slot, &key, &request, timeout)
                    .map_err(|second| format!("{first}; retry failed: {second}"))
            }
        }
    })
}

fn send_persistent_request(
    slot: &mut Option<DuckSession>,
    key: &DuckSessionKey,
    request: &str,
    timeout: i32,
) -> Result<Value, String> {
    let needs_new = slot.as_ref().is_none_or(|session| session.key != *key);
    if needs_new {
        slot.take();
        *slot = Some(DuckSession::spawn(key.clone())?);
    }
    slot.as_mut()
        .ok_or_else(|| "persistent rvbbit-duck session unavailable".to_string())?
        .request(request, timeout)
}

fn execute_engine_oneshot(
    engine: &str,
    layout: &str,
    binary: &str,
    dsn: &str,
    query: &str,
    max_rows: i32,
    timeout: i32,
    threads: usize,
    result_format: SidecarResultFormat,
) -> Result<Value, String> {
    let mut command = Command::new(binary);
    command
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
        .arg("--threads")
        .arg(threads.to_string())
        .arg("--max-rows")
        .arg(max_rows.to_string())
        .arg("--result-format")
        .arg(result_format.as_str())
        .arg("--pgdata-prefix")
        .arg(pgdata_prefix())
        .arg("--visible-pgdata-prefix")
        .arg(visible_pgdata_prefix())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(sp) = session_search_path_csv() {
        command.arg("--search-path").arg(sp);
    }
    let child = command
        .spawn()
        .map_err(|e| format!("failed to start rvbbit-duck: {e}"))?;
    let output = wait_child_output(child, timeout, "rvbbit-duck one-shot")?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(&stdout).map_err(|e| {
        let stderr = String::from_utf8_lossy(&output.stderr);
        format!(
            "invalid rvbbit-duck JSON: {e}; stderr={}",
            first_line(&stderr)
        )
    })
}

fn sidecar_io_timeout(timeout: i32) -> Duration {
    Duration::from_secs(timeout.max(1) as u64 + 5)
}

fn read_line_with_timeout<R: Read + AsRawFd>(
    reader: &mut R,
    pending: &mut Vec<u8>,
    timeout: Duration,
    label: &str,
) -> Result<String, String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(pos) = pending.iter().position(|byte| *byte == b'\n') {
            let line = pending.drain(..=pos).collect::<Vec<_>>();
            return String::from_utf8(line).map_err(|e| format!("{label} returned non-UTF8: {e}"));
        }

        let now = Instant::now();
        if now >= deadline {
            return Err(format!(
                "{label} read timed out after {}s",
                timeout.as_secs()
            ));
        }
        poll_readable(
            reader.as_raw_fd(),
            deadline.saturating_duration_since(now),
            label,
        )?;

        let mut chunk = [0_u8; 8192];
        match reader.read(&mut chunk) {
            Ok(0) if pending.is_empty() => {
                return Err(format!("{label} exited without a response"));
            }
            Ok(0) => {
                let line = std::mem::take(pending);
                return String::from_utf8(line)
                    .map_err(|e| format!("{label} returned non-UTF8: {e}"));
            }
            Ok(n) => pending.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => return Err(format!("{label} read failed: {e}")),
        }
    }
}

fn poll_readable(fd: RawFd, timeout: Duration, label: &str) -> Result<(), String> {
    let timeout_ms = timeout.as_millis().min(i32::MAX as u128) as libc::c_int;
    let mut fd = libc::pollfd {
        fd,
        events: libc::POLLIN | libc::POLLHUP | libc::POLLERR,
        revents: 0,
    };
    loop {
        let rc = unsafe { libc::poll(&mut fd, 1, timeout_ms) };
        if rc > 0 {
            return Ok(());
        }
        if rc == 0 {
            return Err(format!("{label} read timed out after {timeout_ms}ms"));
        }
        let err = std::io::Error::last_os_error();
        if err.kind() == ErrorKind::Interrupted {
            continue;
        }
        return Err(format!("{label} poll failed: {err}"));
    }
}

fn wait_child_output(
    mut child: Child,
    timeout_s: i32,
    label: &str,
) -> Result<std::process::Output, String> {
    wait_child_output_with_timeout(&mut child, timeout_s, label, sidecar_io_timeout(timeout_s))
}

fn wait_child_output_with_timeout(
    child: &mut Child,
    timeout_s: i32,
    label: &str,
    timeout: Duration,
) -> Result<std::process::Output, String> {
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut stdout = Vec::new();
                if let Some(mut pipe) = child.stdout.take() {
                    pipe.read_to_end(&mut stdout)
                        .map_err(|e| format!("reading {label} stdout: {e}"))?;
                }
                let mut stderr = Vec::new();
                if let Some(mut pipe) = child.stderr.take() {
                    pipe.read_to_end(&mut stderr)
                        .map_err(|e| format!("reading {label} stderr: {e}"))?;
                }
                return Ok(std::process::Output {
                    status,
                    stdout,
                    stderr,
                });
            }
            Ok(None) if start.elapsed() < timeout => {
                thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "{label} exceeded timeout_s={timeout_s} (waited {}s including grace)",
                    timeout.as_secs()
                ));
            }
            Err(e) => return Err(format!("waiting for {label}: {e}")),
        }
    }
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
        "host={} dbname={} user={} application_name={}",
        conninfo_value("/var/run/postgresql"),
        conninfo_value(&db),
        conninfo_value(&user),
        conninfo_value("rvbbit-duck-sidecar")
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

    #[test]
    fn shared_target_list_filters_engine_layout_pairs() {
        assert!(shared_target_list_matches("", "duck", "scan"));
        assert!(shared_target_list_matches("all", "duck", "scan"));
        assert!(shared_target_list_matches("duck:vortex", "duck", "vortex"));
        assert!(!shared_target_list_matches("duck:vortex", "duck", "scan"));
        assert!(shared_target_list_matches(
            "datafusion, duck:hive",
            "datafusion",
            "scan"
        ));
        assert!(shared_target_list_matches("vortex", "duck", "vortex"));
    }

    #[test]
    fn shared_strict_value_defaults_off_and_accepts_truthy_settings() {
        assert!(!shared_strict_value(None));
        assert!(!shared_strict_value(Some("")));
        assert!(!shared_strict_value(Some("off")));
        assert!(!shared_strict_value(Some("false")));
        assert!(shared_strict_value(Some("on")));
        assert!(shared_strict_value(Some("true")));
    }

    #[test]
    fn wait_child_output_times_out_slow_processes() {
        let child = Command::new("sh")
            .arg("-c")
            .arg("sleep 2")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut child = child;
        let err =
            wait_child_output_with_timeout(&mut child, 1, "test child", Duration::from_millis(20))
                .unwrap_err();
        assert!(err.contains("test child exceeded timeout_s=1"));
    }

    #[test]
    fn persistent_read_times_out_slow_processes() {
        let mut child = Command::new("sh")
            .arg("-c")
            .arg("sleep 2; printf '{\"status\":\"ok\"}\\n'")
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdout = child.stdout.take().unwrap();
        let mut pending = Vec::new();
        let err = read_line_with_timeout(
            &mut stdout,
            &mut pending,
            Duration::from_millis(20),
            "test persistent",
        )
        .unwrap_err();
        assert!(err.contains("test persistent read timed out"));
        let _ = child.kill();
        let _ = child.wait();
    }

    #[test]
    fn finds_duck_binary_on_path() {
        let dir =
            std::env::temp_dir().join(format!("rvbbit-duck-path-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let binary = dir.join("rvbbit-duck");
        fs::write(&binary, b"#!/bin/sh\n").unwrap();
        let mut perms = fs::metadata(&binary).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&binary, perms).unwrap();

        assert_eq!(
            find_executable_on_path("rvbbit-duck", Some(dir.as_os_str())),
            Some(binary.to_string_lossy().into_owned())
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn ignores_non_executable_duck_binary_on_path() {
        let dir = std::env::temp_dir().join(format!(
            "rvbbit-duck-path-nonexec-test-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let binary = dir.join("rvbbit-duck");
        fs::write(&binary, b"not executable\n").unwrap();
        let mut perms = fs::metadata(&binary).unwrap().permissions();
        perms.set_mode(0o644);
        fs::set_permissions(&binary, perms).unwrap();

        assert_eq!(
            find_executable_on_path("rvbbit-duck", Some(dir.as_os_str())),
            None
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
