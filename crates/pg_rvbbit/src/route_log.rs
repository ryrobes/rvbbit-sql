//! Low-overhead route decision telemetry.
//!
//! The rewrite hook is latency-sensitive and cannot safely do catalog writes
//! for every SELECT. This module records decisions by pushing compact events
//! into a bounded per-backend queue. A background Rust thread owns a separate
//! local PostgreSQL connection and writes batches into `rvbbit.route_decisions`.
//!
//! This is intentionally best-effort telemetry: if the queue is full or the
//! writer cannot connect, route execution continues and counters record the
//! drop/error.

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::CStr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use crossbeam_channel::{bounded, Receiver, Sender};
use pgrx::pg_guard;
use pgrx::prelude::*;
use pgrx::{pg_sys, JsonB};
use postgres::{Client, NoTls};
use serde_json::{json, Value};

const DEFAULT_QUEUE_CAPACITY: usize = 8192;
const DEFAULT_BATCH_SIZE: usize = 64;
const DEFAULT_FLUSH_MS: u64 = 250;
const DEFAULT_EXIT_FLUSH_MS: u64 = 500;

static LOGGER: OnceLock<DecisionLogger> = OnceLock::new();
static EXIT_HOOK_REGISTERED: AtomicBool = AtomicBool::new(false);
static mut PREV_EXECUTOR_START_HOOK: pg_sys::ExecutorStart_hook_type = None;
static mut PREV_EXECUTOR_END_HOOK: pg_sys::ExecutorEnd_hook_type = None;

thread_local! {
    static PENDING_EXECUTIONS: RefCell<HashMap<String, RouteExecutionTemplate>> = RefCell::new(HashMap::new());
    static ACTIVE_EXECUTIONS: RefCell<HashMap<usize, ActiveRouteExecution>> = RefCell::new(HashMap::new());
}

struct DecisionLogger {
    tx: Sender<RouteLogEvent>,
    counters: Arc<DecisionLogCounters>,
}

#[derive(Default)]
struct DecisionLogCounters {
    enqueued: AtomicU64,
    dropped: AtomicU64,
    written: AtomicU64,
    decision_written: AtomicU64,
    execution_written: AtomicU64,
    write_errors: AtomicU64,
    connect_errors: AtomicU64,
}

enum RouteLogEvent {
    Decision(RouteDecisionEvent),
    Execution(RouteExecutionEvent),
}

impl RouteLogEvent {
    fn dsn(&self) -> &str {
        match self {
            RouteLogEvent::Decision(event) => &event.dsn,
            RouteLogEvent::Execution(event) => &event.template.dsn,
        }
    }
}

#[derive(Debug)]
struct RouteDecisionEvent {
    dsn: String,
    backend_pid: i32,
    database_name: String,
    role_name: String,
    query_hash: String,
    shape_key: String,
    shape_family: String,
    route: String,
    candidate: Option<String>,
    profile_name: Option<String>,
    profile_source: String,
    route_source: String,
    reason: String,
    confidence: Option<f64>,
    cache_hit: bool,
    rewritten: bool,
    features_json: String,
    route_doc_json: String,
    node: Option<String>,
}

#[derive(Clone, Debug)]
struct RouteExecutionTemplate {
    dsn: String,
    query_text: String, // representative SQL captured per shape for the auto-optimizer
    // The caller's search_path at capture time — the optimizer replays the
    // sample under it so unqualified table names (e.g. a tpcds-schema
    // workload) resolve like they did for the original session.
    search_path: String,
    backend_pid: i32,
    database_name: String,
    role_name: String,
    query_hash: String,
    shape_key: String,
    shape_family: String,
    route: String,
    candidate: Option<String>,
    profile_name: Option<String>,
    profile_source: String,
    route_source: String,
    reason: String,
    confidence: Option<f64>,
    cache_hit: bool,
    rewritten: bool,
    features_json: String,
    route_doc_json: String,
    // Fleet identity, stamped at ExecutorEnd from the dispatch-time record
    // (duck_backend::last_fleet_dispatch): the endpoint that ACTUALLY served
    // the query (NULL = the brain), and the engine the worker reports having
    // used. Never re-resolved from the registry — under random rotation that
    // is a fresh draw, not where the query went.
    node: Option<String>,
    executed_engine: Option<String>,
}

#[derive(Debug)]
struct ActiveRouteExecution {
    template: RouteExecutionTemplate,
    started_at: Instant,
}

#[derive(Debug)]
struct RouteExecutionEvent {
    template: RouteExecutionTemplate,
    elapsed_ms: f64,
    rows_returned: i64,
    status: String,
}

pub unsafe fn register_hooks() {
    PREV_EXECUTOR_START_HOOK = pg_sys::ExecutorStart_hook;
    PREV_EXECUTOR_END_HOOK = pg_sys::ExecutorEnd_hook;
    pg_sys::ExecutorStart_hook = Some(rvbbit_executor_start_hook);
    pg_sys::ExecutorEnd_hook = Some(rvbbit_executor_end_hook);
}

#[pg_extern(volatile)]
fn route_decision_log_status() -> JsonB {
    let Some(logger) = LOGGER.get() else {
        return JsonB(json!({
            "enabled": enabled(),
            "started": false,
            "scope": "backend",
            "backend_pid": unsafe { pg_sys::MyProcPid },
            "queue_len": 0,
        }));
    };
    JsonB(json!({
        "enabled": enabled(),
        "started": true,
        "scope": "backend",
        "backend_pid": unsafe { pg_sys::MyProcPid },
        "queue_len": logger.tx.len(),
        "queue_capacity": logger.tx.capacity(),
        "enqueued": logger.counters.enqueued.load(Ordering::Relaxed),
        "dropped": logger.counters.dropped.load(Ordering::Relaxed),
        "written": logger.counters.written.load(Ordering::Relaxed),
        "decision_written": logger.counters.decision_written.load(Ordering::Relaxed),
        "execution_written": logger.counters.execution_written.load(Ordering::Relaxed),
        "write_errors": logger.counters.write_errors.load(Ordering::Relaxed),
        "connect_errors": logger.counters.connect_errors.load(Ordering::Relaxed),
    }))
}

pub(crate) fn enqueue_decision(
    query_sql: &str,
    route_doc: &Value,
    cache_hit: bool,
    rewritten: bool,
) {
    if !enabled() {
        return;
    }
    let Some(event) = build_event(query_sql, route_doc, cache_hit, rewritten) else {
        return;
    };
    let logger = LOGGER.get_or_init(start_logger);
    match logger.tx.try_send(RouteLogEvent::Decision(event)) {
        Ok(()) => {
            logger.counters.enqueued.fetch_add(1, Ordering::Relaxed);
        }
        Err(_) => {
            logger.counters.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

pub(crate) fn record_pending_execution(
    query_sql: &str,
    route_doc: &Value,
    cache_hit: bool,
    rewritten: bool,
) {
    if !enabled() {
        return;
    }
    // Fresh query → no dispatch has happened yet; a stale record from the
    // previous query on this backend must never leak into this one's stamp.
    crate::duck_backend::clear_fleet_dispatch();
    let Some(template) = build_execution_template(query_sql, route_doc, cache_hit, rewritten)
    else {
        return;
    };
    let key = normalized_sql_key(query_sql);
    if key.is_empty() {
        return;
    }
    PENDING_EXECUTIONS.with(|pending| {
        let mut pending = pending.borrow_mut();
        if pending.len() > 512 {
            pending.clear();
        }
        pending.insert(key, template);
    });
}

fn build_event(
    _query_sql: &str,
    route_doc: &Value,
    cache_hit: bool,
    rewritten: bool,
) -> Option<RouteDecisionEvent> {
    let template = build_execution_template(_query_sql, route_doc, cache_hit, rewritten)?;
    Some(RouteDecisionEvent {
        dsn: template.dsn,
        backend_pid: template.backend_pid,
        database_name: template.database_name,
        role_name: template.role_name,
        query_hash: template.query_hash,
        shape_key: template.shape_key,
        shape_family: template.shape_family,
        route: template.route,
        candidate: template.candidate,
        profile_name: template.profile_name,
        profile_source: template.profile_source,
        route_source: template.route_source,
        reason: template.reason,
        confidence: template.confidence,
        cache_hit: template.cache_hit,
        rewritten: template.rewritten,
        features_json: template.features_json,
        route_doc_json: template.route_doc_json,
        node: template.node,
    })
}

fn build_execution_template(
    query_sql: &str,
    route_doc: &Value,
    cache_hit: bool,
    rewritten: bool,
) -> Option<RouteExecutionTemplate> {
    let features = route_doc.get("features")?;
    let query_hash = features
        .get("sql_hash")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())?
        .to_string();
    if !sampled(&query_hash) {
        return None;
    }
    let has_rvbbit_table = route_doc
        .get("rvbbit_tables")
        .and_then(Value::as_array)
        .is_some_and(|tables| !tables.is_empty());
    if !has_rvbbit_table {
        return None;
    }

    Some(RouteExecutionTemplate {
        dsn: log_dsn(),
        // One representative SQL per shape for the auto-optimizer (route_shape_samples).
        // Skip pathologically long queries — we need the full runnable text to benchmark.
        query_text: if query_sql.len() <= 65_536 {
            query_sql.to_string()
        } else {
            String::new()
        },
        search_path: crate::duck_backend::guc_setting("search_path").unwrap_or_default(),
        backend_pid: unsafe { pg_sys::MyProcPid },
        database_name: current_database_name().unwrap_or_else(|| "unknown".to_string()),
        role_name: current_user_name().unwrap_or_else(|| "unknown".to_string()),
        query_hash,
        shape_key: features
            .get("shape_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        shape_family: features
            .get("shape_family")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        route: route_doc
            .get("route")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        candidate: route_doc
            .get("chosen_candidate")
            .and_then(Value::as_str)
            .map(str::to_string),
        // Placement is unknown until dispatch actually happens — stamped in
        // enqueue_execution from the dispatch-time record. (This used to
        // re-resolve fleet_endpoint() speculatively; with rotation that's a
        // second independent draw and labels lie.)
        node: None,
        executed_engine: None,
        profile_name: route_doc
            .get("profile_name")
            .and_then(Value::as_str)
            .map(str::to_string),
        profile_source: route_doc
            .get("profile_source")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        route_source: route_doc
            .get("route_source")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        reason: route_doc
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(1024)
            .collect(),
        confidence: route_doc.get("confidence").and_then(Value::as_f64),
        cache_hit,
        rewritten,
        features_json: serde_json::to_string(features).unwrap_or_else(|_| "{}".to_string()),
        route_doc_json: serde_json::to_string(route_doc).unwrap_or_else(|_| "{}".to_string()),
    })
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_executor_start_hook(
    query_desc: *mut pg_sys::QueryDesc,
    eflags: std::ffi::c_int,
) {
    if let Some(prev) = PREV_EXECUTOR_START_HOOK {
        prev(query_desc, eflags);
    } else {
        pg_sys::standard_ExecutorStart(query_desc, eflags);
    }
    if query_desc.is_null() {
        return;
    }
    let Some(source) = query_desc_source_text(query_desc) else {
        return;
    };
    let key = normalized_sql_key(&source);
    if key.is_empty() {
        return;
    }
    let template = PENDING_EXECUTIONS.with(|pending| pending.borrow_mut().remove(&key));
    if let Some(template) = template {
        ACTIVE_EXECUTIONS.with(|active| {
            active.borrow_mut().insert(
                query_desc as usize,
                ActiveRouteExecution {
                    template,
                    started_at: Instant::now(),
                },
            );
        });
    }
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_executor_end_hook(query_desc: *mut pg_sys::QueryDesc) {
    if !query_desc.is_null() {
        let active =
            ACTIVE_EXECUTIONS.with(|active| active.borrow_mut().remove(&(query_desc as usize)));
        if let Some(active) = active {
            enqueue_execution(active, rows_processed(query_desc), "ok");
        }
    }
    if let Some(prev) = PREV_EXECUTOR_END_HOOK {
        prev(query_desc);
    } else {
        pg_sys::standard_ExecutorEnd(query_desc);
    }
}

fn enqueue_execution(active: ActiveRouteExecution, rows_returned: i64, status: &str) {
    if !enabled() {
        return;
    }
    let logger = LOGGER.get_or_init(start_logger);
    let mut template = active.template;
    // Stamp placement truth: if this query's engine dispatch actually went to
    // a fleet node, record the endpoint + the engine the worker reports. The
    // candidate-prefix gate keeps a (cleared-per-query, but belt-and-
    // suspenders) record from ever landing on a native/rowstore row.
    if template
        .candidate
        .as_deref()
        .map(|c| c.starts_with("duck") || c.starts_with("datafusion"))
        .unwrap_or(false)
    {
        if let Some((endpoint, engine_used)) = crate::duck_backend::last_fleet_dispatch() {
            template.node = Some(endpoint);
            template.executed_engine = Some(engine_used);
        }
    }
    let event = RouteExecutionEvent {
        template,
        elapsed_ms: active.started_at.elapsed().as_secs_f64() * 1000.0,
        rows_returned,
        status: status.to_string(),
    };
    match logger.tx.try_send(RouteLogEvent::Execution(event)) {
        Ok(()) => {
            logger.counters.enqueued.fetch_add(1, Ordering::Relaxed);
        }
        Err(_) => {
            logger.counters.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

unsafe fn query_desc_source_text(query_desc: *mut pg_sys::QueryDesc) -> Option<String> {
    if query_desc.is_null() || (*query_desc).sourceText.is_null() {
        return None;
    }
    Some(
        CStr::from_ptr((*query_desc).sourceText)
            .to_string_lossy()
            .into_owned(),
    )
}

unsafe fn rows_processed(query_desc: *mut pg_sys::QueryDesc) -> i64 {
    if query_desc.is_null() || (*query_desc).estate.is_null() {
        return 0;
    }
    let rows = (*(*query_desc).estate).es_processed;
    rows.min(i64::MAX as u64) as i64
}

fn normalized_sql_key(sql: &str) -> String {
    sql.trim().trim_end_matches(';').trim().to_string()
}

fn start_logger() -> DecisionLogger {
    let capacity = std::env::var("RVBBIT_ROUTE_DECISION_LOG_QUEUE")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_QUEUE_CAPACITY);
    let (tx, rx) = bounded(capacity);
    let counters = Arc::new(DecisionLogCounters::default());
    let worker_counters = Arc::clone(&counters);
    if let Err(e) = std::thread::Builder::new()
        .name("rvbbit-route-log".to_string())
        .spawn(move || writer_loop(rx, worker_counters))
    {
        pgrx::warning!("rvbbit: failed to start route log writer: {e}");
    }
    register_exit_flush_hook();
    DecisionLogger { tx, counters }
}

fn register_exit_flush_hook() {
    if EXIT_HOOK_REGISTERED
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_ok()
    {
        unsafe {
            pg_sys::on_proc_exit(Some(route_log_proc_exit), pg_sys::Datum::from(0));
        }
    }
}

unsafe extern "C-unwind" fn route_log_proc_exit(_code: std::ffi::c_int, _arg: pg_sys::Datum) {
    wait_for_logger_drain();
}

fn wait_for_logger_drain() {
    let Some(logger) = LOGGER.get() else {
        return;
    };
    let timeout_ms = std::env::var("RVBBIT_ROUTE_DECISION_LOG_EXIT_FLUSH_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(DEFAULT_EXIT_FLUSH_MS);
    if timeout_ms == 0 {
        return;
    }
    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        let enqueued = logger.counters.enqueued.load(Ordering::Relaxed);
        let completed = logger.counters.written.load(Ordering::Relaxed)
            + logger.counters.dropped.load(Ordering::Relaxed);
        if completed >= enqueued {
            break;
        }
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn writer_loop(rx: Receiver<RouteLogEvent>, counters: Arc<DecisionLogCounters>) {
    let mut client: Option<Client> = None;
    let mut client_dsn = String::new();
    let mut batch = Vec::with_capacity(DEFAULT_BATCH_SIZE);
    loop {
        match rx.recv_timeout(Duration::from_millis(DEFAULT_FLUSH_MS)) {
            Ok(event) => batch.push(event),
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        }
        while batch.len() < DEFAULT_BATCH_SIZE {
            match rx.try_recv() {
                Ok(event) => batch.push(event),
                Err(_) => break,
            }
        }
        if batch.is_empty() {
            continue;
        }
        let dsn = batch[0].dsn().to_string();
        if client.is_none() || client_dsn != dsn {
            client = match Client::connect(&dsn, NoTls) {
                Ok(client) => {
                    client_dsn = dsn;
                    Some(client)
                }
                Err(_) => {
                    counters.connect_errors.fetch_add(1, Ordering::Relaxed);
                    counters
                        .dropped
                        .fetch_add(batch.len() as u64, Ordering::Relaxed);
                    batch.clear();
                    None
                }
            };
        }
        let Some(active_client) = client.as_mut() else {
            continue;
        };
        match write_batch(active_client, &batch) {
            Ok((decisions, executions)) => {
                let written = decisions + executions;
                counters.written.fetch_add(written, Ordering::Relaxed);
                counters
                    .decision_written
                    .fetch_add(decisions, Ordering::Relaxed);
                counters
                    .execution_written
                    .fetch_add(executions, Ordering::Relaxed);
            }
            Err(_) => {
                counters.write_errors.fetch_add(1, Ordering::Relaxed);
                counters
                    .dropped
                    .fetch_add(batch.len() as u64, Ordering::Relaxed);
                client = None;
                client_dsn.clear();
            }
        }
        batch.clear();
    }
}

fn write_batch(
    client: &mut Client,
    batch: &[RouteLogEvent],
) -> Result<(u64, u64), postgres::Error> {
    let mut tx = client.transaction()?;
    let decision_stmt = tx.prepare(
        "INSERT INTO rvbbit.route_decisions \
         (backend_pid, database_name, role_name, query_hash, shape_key, shape_family, \
          route, candidate, profile_name, profile_source, route_source, reason, confidence, \
          cache_hit, rewritten, features, route_doc, node) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::text::jsonb, $17::text::jsonb, $18)",
    )?;
    let execution_stmt = tx.prepare(
        "INSERT INTO rvbbit.route_executions \
         (backend_pid, database_name, role_name, query_hash, shape_key, shape_family, \
          route, candidate, profile_name, profile_source, route_source, reason, confidence, cache_hit, rewritten, \
          elapsed_ms, rows_returned, status, features, route_doc, node, executed_engine) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19::text::jsonb, $20::text::jsonb, $21, $22)",
    )?;
    let mut decisions = 0;
    let mut executions = 0;
    // One representative SQL per (new) shape, deduped within the batch; upserted AFTER commit
    // (best-effort, autocommit) so a missing route_shape_samples table can't poison the logs.
    let mut shape_samples: std::collections::HashMap<String, (String, String, String)> =
        std::collections::HashMap::new();
    for event in batch {
        match event {
            RouteLogEvent::Decision(event) => {
                tx.execute(
                    &decision_stmt,
                    &[
                        &event.backend_pid,
                        &event.database_name,
                        &event.role_name,
                        &event.query_hash,
                        &event.shape_key,
                        &event.shape_family,
                        &event.route,
                        &event.candidate,
                        &event.profile_name,
                        &event.profile_source,
                        &event.route_source,
                        &event.reason,
                        &event.confidence,
                        &event.cache_hit,
                        &event.rewritten,
                        &event.features_json,
                        &event.route_doc_json,
                        &event.node,
                    ],
                )?;
                decisions += 1;
            }
            RouteLogEvent::Execution(event) => {
                let template = &event.template;
                tx.execute(
                    &execution_stmt,
                    &[
                        &template.backend_pid,
                        &template.database_name,
                        &template.role_name,
                        &template.query_hash,
                        &template.shape_key,
                        &template.shape_family,
                        &template.route,
                        &template.candidate,
                        &template.profile_name,
                        &template.profile_source,
                        &template.route_source,
                        &template.reason,
                        &template.confidence,
                        &template.cache_hit,
                        &template.rewritten,
                        &event.elapsed_ms,
                        &event.rows_returned,
                        &event.status,
                        &template.features_json,
                        &template.route_doc_json,
                        &template.node,
                        &template.executed_engine,
                    ],
                )?;
                executions += 1;
                if !template.query_text.is_empty() && !template.shape_key.is_empty() {
                    shape_samples.entry(template.shape_key.clone()).or_insert_with(|| {
                        (
                            template.shape_family.clone(),
                            template.query_text.clone(),
                            template.search_path.clone(),
                        )
                    });
                }
            }
        }
    }
    tx.commit()?;

    // Best-effort, isolated from the committed logs: capture a representative SQL per shape.
    // Each upsert is its own autocommit statement; a missing table just errors harmlessly.
    // The search_path column arrives with migration 0128 — fall back to the
    // 3-column insert on installs that haven't migrated yet.
    for (shape_key, (shape_family, sql, search_path)) in shape_samples {
        if client
            .execute(
                "INSERT INTO rvbbit.route_shape_samples (shape_key, shape_family, sql, search_path) \
                 VALUES ($1, $2, $3, $4) ON CONFLICT (shape_key) DO NOTHING",
                &[&shape_key, &shape_family, &sql, &search_path],
            )
            .is_err()
        {
            let _ = client.execute(
                "INSERT INTO rvbbit.route_shape_samples (shape_key, shape_family, sql) \
                 VALUES ($1, $2, $3) ON CONFLICT (shape_key) DO NOTHING",
                &[&shape_key, &shape_family, &sql],
            );
        }
    }
    Ok((decisions, executions))
}

fn enabled() -> bool {
    guc_setting("rvbbit.route_decision_log")
        .map(|value| setting_enabled(&value, true))
        .unwrap_or_else(|| env_enabled("RVBBIT_ROUTE_DECISION_LOG", true))
}

fn sampled(query_hash: &str) -> bool {
    let rate = guc_setting("rvbbit.route_decision_log_sample")
        .and_then(|value| value.parse::<f64>().ok())
        .or_else(|| {
            std::env::var("RVBBIT_ROUTE_DECISION_LOG_SAMPLE")
                .ok()
                .and_then(|value| value.parse::<f64>().ok())
        })
        .unwrap_or(1.0);
    if rate >= 1.0 {
        return true;
    }
    if rate <= 0.0 {
        return false;
    }
    let prefix = query_hash.get(0..16).unwrap_or(query_hash);
    let Ok(value) = u64::from_str_radix(prefix, 16) else {
        return true;
    };
    (value as f64 / u64::MAX as f64) < rate
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
    match trimmed.to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => true,
        "0" | "false" | "no" | "off" => false,
        _ => default,
    }
}

fn guc_setting(name: &str) -> Option<String> {
    let cname = std::ffi::CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(
            unsafe { CStr::from_ptr(ptr) }
                .to_string_lossy()
                .into_owned(),
        )
    }
}

fn log_dsn() -> String {
    if let Ok(dsn) = std::env::var("RVBBIT_ROUTE_DECISION_LOG_DSN") {
        return dsn;
    }
    let db = current_database_name().unwrap_or_else(|| "postgres".to_string());
    format!(
        "host={} dbname={} application_name=rvbbit_route_log",
        conninfo_value("/var/run/postgresql"),
        conninfo_value(&db),
    )
}

fn conninfo_value(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_rate_bounds_are_respected() {
        std::env::set_var("RVBBIT_ROUTE_DECISION_LOG_SAMPLE", "0");
        assert!(!sampled("ffffffffffffffff"));
        std::env::set_var("RVBBIT_ROUTE_DECISION_LOG_SAMPLE", "1");
        assert!(sampled("0000000000000000"));
        std::env::remove_var("RVBBIT_ROUTE_DECISION_LOG_SAMPLE");
    }
}
