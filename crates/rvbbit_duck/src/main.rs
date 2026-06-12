use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, HashMap};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::hash::{Hash, Hasher};
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::process;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, bail, Context, Result};
use datafusion::arrow::array::{
    cast::{as_boolean_array, as_primitive_array, as_string_array},
    Array, ArrayRef,
};
use datafusion::arrow::datatypes::{
    DataType, Float32Type, Float64Type, Int16Type, Int32Type, Int64Type, Int8Type, SchemaRef,
    UInt16Type, UInt32Type, UInt64Type, UInt8Type,
};
use datafusion::arrow::ipc::writer::StreamWriter;
use datafusion::arrow::record_batch::RecordBatch;
use datafusion::arrow::util::display::array_value_to_string;
use datafusion::prelude::{ParquetReadOptions, SessionConfig, SessionContext};
use duckdb::types::ValueRef;
use duckdb::Connection;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use postgres::{Client, NoTls};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::runtime::{Builder as RuntimeBuilder, Runtime};

const DEFAULT_DSN: &str = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench";
const DEFAULT_DUCK_BIN: &str = "/usr/local/bin/rvbbit-duck";
const DEFAULT_PGDATA_PREFIX: &str = "/var/lib/postgresql";
const DEFAULT_VISIBLE_PGDATA_PREFIX: &str = "/rvbbit_pgdata";
const DEFAULT_TELEMETRY_QUEUE_CAPACITY: usize = 8192;
const DEFAULT_TELEMETRY_BATCH_SIZE: usize = 64;
const DEFAULT_TELEMETRY_FLUSH_MS: u64 = 250;
const DEFAULT_TELEMETRY_HEARTBEAT_MS: u64 = 5000;

static TELEMETRY_SINK: OnceLock<Option<Arc<TelemetrySink>>> = OnceLock::new();
static TELEMETRY_QUEUE_DEPTH: AtomicI64 = AtomicI64::new(0);
static TELEMETRY_EVENTS_ENQUEUED: AtomicI64 = AtomicI64::new(0);
static TELEMETRY_EVENTS_WRITTEN: AtomicI64 = AtomicI64::new(0);
static TELEMETRY_EVENTS_DROPPED: AtomicI64 = AtomicI64::new(0);
static BROKER_QUEUE_DEPTH: AtomicI64 = AtomicI64::new(0);
static BROKER_ACTIVE_WORKERS: AtomicI64 = AtomicI64::new(0);

#[derive(Debug, Clone)]
struct Args {
    engine: Engine,
    dsn: String,
    sql: Option<String>,
    repeat: usize,
    timeout_s: u64,
    threads: usize,
    max_rows: usize,
    pgdata_prefix: String,
    visible_pgdata_prefix: String,
    layout: String,
    result_format: ResultFormat,
    explain_only: bool,
    serve: bool,
    serve_socket: Option<String>,
    workers: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Engine {
    Duck,
    DataFusion,
}

impl Engine {
    fn as_str(self) -> &'static str {
        match self {
            Engine::Duck => "duck",
            Engine::DataFusion => "datafusion",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ResultFormat {
    Json,
    ArrowIpcFile,
}

impl ResultFormat {
    fn as_str(self) -> &'static str {
        match self {
            ResultFormat::Json => "json",
            ResultFormat::ArrowIpcFile => "arrow_ipc_file",
        }
    }
}

#[derive(Debug, Clone)]
struct RvbbitDuckTable {
    schema: String,
    relname: String,
    paths: Vec<String>,
    columns: Vec<(String, String)>,
    layout: Option<String>,
    partition_cols: Vec<(String, String)>,
    row_group_rows: i64,
    row_group_bytes: i64,
}

#[derive(Debug, Clone, Default, Serialize)]
struct CacheSummary {
    catalog_fingerprint: String,
    catalog_cache_hit: bool,
    executor_cache_hit: bool,
    route_safety_cache_hit: bool,
    route_safety_local_hit: bool,
    route_safety_check_ms: f64,
    route_safety_cache_entries: usize,
    parquet_footer_hits: usize,
    parquet_footer_misses: usize,
    parquet_footer_files: usize,
    parquet_footer_rows: i64,
    parquet_footer_row_groups: usize,
    parquet_footer_columns: usize,
    parquet_footer_schema_bytes: usize,
    parquet_prewarm_ms: f64,
}

#[derive(Debug, Serialize)]
struct QuerySummary {
    status: String,
    elapsed_ms: f64,
    repeat: usize,
    timeout_s: u64,
    row_count: usize,
    columns: Vec<String>,
    rows: Vec<Vec<Value>>,
    result_format: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    arrow_ipc_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    arrow_ipc_bytes: Option<u64>,
    tables: Vec<TableSummary>,
    cache: CacheSummary,
}

#[derive(Debug, Serialize)]
struct TableSummary {
    schema: String,
    relname: String,
    files: usize,
    rows: i64,
    bytes: i64,
    layout: Option<String>,
}

#[derive(Clone)]
struct TelemetrySink {
    tx: mpsc::SyncSender<TelemetryMessage>,
}

enum TelemetryMessage {
    Query(QueryTelemetryEvent),
}

#[derive(Clone)]
struct TelemetryConfig {
    instance_id: String,
    hostname: String,
    node_id: String,
    pid: i32,
    mode: String,
    engine: String,
    layout: String,
    socket_path: Option<String>,
    dsn_hash: String,
    dsn: String,
    worker_count: i32,
    duck_threads: i32,
    binary_path: Option<String>,
    batch_size: usize,
    flush_ms: u64,
    heartbeat_ms: u64,
    metadata_json: String,
}

#[derive(Clone)]
struct QueryTelemetryEvent {
    worker_id: Option<i32>,
    command: Option<String>,
    query_hash: Option<String>,
    status: String,
    queue_wait_ms: Option<f64>,
    elapsed_ms: f64,
    execute_ms: Option<f64>,
    route_safety_ms: Option<f64>,
    parquet_prewarm_ms: Option<f64>,
    row_count: Option<i64>,
    result_format: Option<String>,
    arrow_ipc_bytes: Option<i64>,
    repeat_count: Option<i32>,
    timeout_s: Option<i32>,
    max_rows: Option<i32>,
    error: Option<String>,
    cache_json: String,
    tables_json: String,
    metadata_json: String,
}

fn main() {
    let args = match parse_args() {
        Ok(args) => args,
        Err(err) => {
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "status": "fallback",
                    "error": format_error_chain(&err),
                }))
                .unwrap()
            );
            std::process::exit(2);
        }
    };
    if args.serve_socket.is_some() {
        if let Err(err) = run_socket_server(args) {
            eprintln!("rvbbit-duck socket server error: {err:#}");
            std::process::exit(2);
        }
        return;
    }
    if args.serve {
        if let Err(err) = run_server(args) {
            eprintln!("rvbbit-duck server error: {err:#}");
            std::process::exit(2);
        }
        return;
    }
    let started = Instant::now();
    let query_hash = args.sql.as_deref().map(stable_hash_hex);
    match run_once_from_args(&args) {
        Ok(summary) => {
            record_oneshot_query_telemetry(
                &args,
                QueryTelemetryEvent {
                    worker_id: None,
                    command: None,
                    query_hash,
                    status: summary.status.clone(),
                    queue_wait_ms: None,
                    elapsed_ms: started.elapsed().as_secs_f64() * 1000.0,
                    execute_ms: Some(summary.elapsed_ms),
                    route_safety_ms: Some(summary.cache.route_safety_check_ms),
                    parquet_prewarm_ms: Some(summary.cache.parquet_prewarm_ms),
                    row_count: Some(summary.row_count as i64),
                    result_format: Some(summary.result_format.clone()),
                    arrow_ipc_bytes: summary.arrow_ipc_bytes.map(|value| value as i64),
                    repeat_count: Some(args.repeat.max(1) as i32),
                    timeout_s: Some(args.timeout_s as i32),
                    max_rows: Some(args.max_rows as i32),
                    error: None,
                    cache_json: json_string(&summary.cache, "{}"),
                    tables_json: json_string(&summary.tables, "[]"),
                    metadata_json: json!({"explain_only": args.explain_only}).to_string(),
                },
            );
            println!("{}", serde_json::to_string_pretty(&summary).unwrap())
        }
        Err(err) => {
            let error = format_error_chain(&err);
            record_oneshot_query_telemetry(
                &args,
                QueryTelemetryEvent {
                    worker_id: None,
                    command: None,
                    query_hash,
                    status: "fallback".to_string(),
                    queue_wait_ms: None,
                    elapsed_ms: started.elapsed().as_secs_f64() * 1000.0,
                    execute_ms: None,
                    route_safety_ms: None,
                    parquet_prewarm_ms: None,
                    row_count: None,
                    result_format: None,
                    arrow_ipc_bytes: None,
                    repeat_count: Some(args.repeat.max(1) as i32),
                    timeout_s: Some(args.timeout_s as i32),
                    max_rows: Some(args.max_rows as i32),
                    error: Some(error.clone()),
                    cache_json: "{}".to_string(),
                    tables_json: "[]".to_string(),
                    metadata_json: json!({"explain_only": args.explain_only}).to_string(),
                },
            );
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "status": "fallback",
                    "error": error,
                }))
                .unwrap()
            );
            std::process::exit(2);
        }
    }
}

fn format_error_chain(err: &anyhow::Error) -> String {
    let mut parts = err.chain().map(ToString::to_string);
    let Some(first) = parts.next() else {
        return err.to_string();
    };
    let rest = parts.collect::<Vec<_>>();
    if rest.is_empty() {
        first
    } else {
        format!("{first}: {}", rest.join(": "))
    }
}

impl TelemetrySink {
    fn start(args: &Args) -> Option<Arc<Self>> {
        if !env_enabled("RVBBIT_DUCK_TELEMETRY", true) {
            return None;
        }
        let config = Arc::new(TelemetryConfig::from_args(args));
        let capacity = env_usize(
            "RVBBIT_DUCK_TELEMETRY_QUEUE",
            DEFAULT_TELEMETRY_QUEUE_CAPACITY,
        )
        .max(1);
        let (tx, rx) = mpsc::sync_channel(capacity);
        let worker_config = Arc::clone(&config);
        if let Err(err) = thread::Builder::new()
            .name("rvbbit-duck-telemetry".to_string())
            .spawn(move || telemetry_writer_loop(worker_config, rx))
        {
            eprintln!("rvbbit-duck telemetry disabled: failed to start writer: {err}");
            return None;
        }
        Some(Arc::new(Self { tx }))
    }

    fn record(&self, event: QueryTelemetryEvent) {
        TELEMETRY_QUEUE_DEPTH.fetch_add(1, Ordering::Relaxed);
        match self.tx.try_send(TelemetryMessage::Query(event)) {
            Ok(()) => {
                TELEMETRY_EVENTS_ENQUEUED.fetch_add(1, Ordering::Relaxed);
            }
            Err(_) => {
                TELEMETRY_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
                TELEMETRY_EVENTS_DROPPED.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}

impl TelemetryConfig {
    fn from_args(args: &Args) -> Self {
        let hostname = local_hostname();
        let node_id = env::var("RVBBIT_NODE_ID")
            .or_else(|_| env::var("RVBBIT_DUCK_NODE_ID"))
            .unwrap_or_else(|_| hostname.clone());
        let pid = process::id() as i32;
        let started_nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0);
        let instance_id = env::var("RVBBIT_DUCK_INSTANCE_ID").unwrap_or_else(|_| {
            format!(
                "{}-{}-{}",
                node_id.replace(|c: char| !c.is_ascii_alphanumeric() && c != '-', "_"),
                pid,
                started_nanos
            )
        });
        let mode = if args.serve_socket.is_some() {
            "shared_broker"
        } else if args.serve {
            "local_persistent"
        } else {
            "local_oneshot"
        }
        .to_string();
        let binary_path = env::current_exe()
            .ok()
            .map(|path| path.display().to_string());
        let metadata_json = json!({
            "pgdata_prefix": args.pgdata_prefix,
            "visible_pgdata_prefix": args.visible_pgdata_prefix,
            "max_rows_default": args.max_rows,
            "timeout_s_default": args.timeout_s,
            "result_format_default": args.result_format.as_str(),
        })
        .to_string();
        Self {
            instance_id,
            hostname,
            node_id,
            pid,
            mode,
            engine: args.engine.as_str().to_string(),
            layout: args.layout.clone(),
            socket_path: args.serve_socket.clone(),
            dsn_hash: stable_hash_hex(&args.dsn),
            dsn: args.dsn.clone(),
            worker_count: if args.serve_socket.is_some() {
                args.workers.max(1) as i32
            } else {
                1
            },
            duck_threads: args.threads.max(1) as i32,
            binary_path,
            batch_size: env_usize("RVBBIT_DUCK_TELEMETRY_BATCH", DEFAULT_TELEMETRY_BATCH_SIZE)
                .max(1),
            flush_ms: env_u64("RVBBIT_DUCK_TELEMETRY_FLUSH_MS", DEFAULT_TELEMETRY_FLUSH_MS).max(1),
            heartbeat_ms: env_u64(
                "RVBBIT_DUCK_TELEMETRY_HEARTBEAT_MS",
                DEFAULT_TELEMETRY_HEARTBEAT_MS,
            ),
            metadata_json,
        }
    }
}

fn telemetry_sink(args: &Args) -> Option<Arc<TelemetrySink>> {
    TELEMETRY_SINK
        .get_or_init(|| TelemetrySink::start(args))
        .clone()
}

fn record_oneshot_query_telemetry(args: &Args, event: QueryTelemetryEvent) {
    if !env_enabled("RVBBIT_DUCK_TELEMETRY", true) {
        return;
    }
    let config = TelemetryConfig::from_args(args);
    let Ok(mut pg) = connect_telemetry_pg(&config.dsn) else {
        return;
    };
    let _ = upsert_sidecar_instance(&mut pg, &config);
    if write_query_telemetry_batch(&mut pg, &config, &[event]).is_ok() {
        TELEMETRY_EVENTS_WRITTEN.fetch_add(1, Ordering::Relaxed);
    }
    let _ = write_heartbeat(&mut pg, &config);
}

fn telemetry_writer_loop(config: Arc<TelemetryConfig>, rx: mpsc::Receiver<TelemetryMessage>) {
    let mut client: Option<Client> = None;
    let mut batch = Vec::<QueryTelemetryEvent>::with_capacity(config.batch_size);
    let mut last_heartbeat = Instant::now()
        .checked_sub(Duration::from_millis(config.heartbeat_ms.max(1)))
        .unwrap_or_else(Instant::now);
    loop {
        match rx.recv_timeout(Duration::from_millis(config.flush_ms)) {
            Ok(TelemetryMessage::Query(event)) => {
                TELEMETRY_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
                batch.push(event);
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
        while batch.len() < config.batch_size {
            match rx.try_recv() {
                Ok(TelemetryMessage::Query(event)) => {
                    TELEMETRY_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
                    batch.push(event);
                }
                Err(_) => break,
            }
        }

        if client.is_none() {
            client = match connect_telemetry_pg(&config.dsn) {
                Ok(mut pg) => {
                    let _ = upsert_sidecar_instance(&mut pg, &config);
                    Some(pg)
                }
                Err(_) => {
                    if !batch.is_empty() {
                        TELEMETRY_EVENTS_DROPPED.fetch_add(batch.len() as i64, Ordering::Relaxed);
                        batch.clear();
                    }
                    continue;
                }
            };
        }

        let Some(pg) = client.as_mut() else {
            continue;
        };

        if !batch.is_empty() {
            match write_query_telemetry_batch(pg, &config, &batch) {
                Ok(()) => {
                    TELEMETRY_EVENTS_WRITTEN.fetch_add(batch.len() as i64, Ordering::Relaxed);
                }
                Err(_) => {
                    TELEMETRY_EVENTS_DROPPED.fetch_add(batch.len() as i64, Ordering::Relaxed);
                    client = None;
                }
            }
            batch.clear();
        }

        if config.heartbeat_ms > 0
            && last_heartbeat.elapsed() >= Duration::from_millis(config.heartbeat_ms)
        {
            if let Some(pg) = client.as_mut() {
                if write_heartbeat(pg, &config).is_err() {
                    client = None;
                } else {
                    last_heartbeat = Instant::now();
                }
            }
        }
    }
}

fn connect_telemetry_pg(dsn: &str) -> Result<Client> {
    let mut pg = Client::connect(dsn, NoTls).context("connecting telemetry to Postgres")?;
    pg.simple_query("SET application_name = 'rvbbit-duck-telemetry'")
        .context("setting telemetry application_name")?;
    Ok(pg)
}

fn upsert_sidecar_instance(pg: &mut Client, config: &TelemetryConfig) -> Result<()> {
    pg.execute(
        "INSERT INTO rvbbit.duck_sidecar_instances \
         (instance_id, hostname, node_id, pid, mode, engine, layout, socket_path, dsn_hash, \
          worker_count, duck_threads, binary_path, last_heartbeat_at, status, metadata) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, clock_timestamp(), $13, $14::text::jsonb) \
         ON CONFLICT (instance_id) DO UPDATE SET \
             hostname = excluded.hostname, \
             node_id = excluded.node_id, \
             pid = excluded.pid, \
             mode = excluded.mode, \
             engine = excluded.engine, \
             layout = excluded.layout, \
             socket_path = excluded.socket_path, \
             dsn_hash = excluded.dsn_hash, \
             worker_count = excluded.worker_count, \
             duck_threads = excluded.duck_threads, \
             binary_path = excluded.binary_path, \
             last_heartbeat_at = excluded.last_heartbeat_at, \
             status = excluded.status, \
             metadata = excluded.metadata",
        &[
            &config.instance_id,
            &config.hostname,
            &config.node_id,
            &config.pid,
            &config.mode,
            &config.engine,
            &config.layout,
            &config.socket_path,
            &config.dsn_hash,
            &config.worker_count,
            &config.duck_threads,
            &config.binary_path,
            &"online",
            &config.metadata_json,
        ],
    )?;
    Ok(())
}

fn write_heartbeat(pg: &mut Client, config: &TelemetryConfig) -> Result<()> {
    let rss_bytes = process_rss_bytes();
    let queue_depth = BROKER_QUEUE_DEPTH.load(Ordering::Relaxed) as i32;
    let active_workers = BROKER_ACTIVE_WORKERS.load(Ordering::Relaxed) as i32;
    let telemetry_queue_depth = TELEMETRY_QUEUE_DEPTH.load(Ordering::Relaxed);
    let metadata_json = json!({
        "telemetry_queue_depth": telemetry_queue_depth,
    })
    .to_string();
    pg.execute(
        "INSERT INTO rvbbit.duck_sidecar_heartbeats \
         (instance_id, hostname, node_id, pid, mode, engine, layout, queue_depth, active_workers, \
          worker_count, duck_threads, rss_bytes, pg_connections, events_enqueued, events_written, events_dropped, metadata) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NULL, $13, $14, $15, $16::text::jsonb)",
        &[
            &config.instance_id,
            &config.hostname,
            &config.node_id,
            &config.pid,
            &config.mode,
            &config.engine,
            &config.layout,
            &queue_depth,
            &active_workers,
            &config.worker_count,
            &config.duck_threads,
            &rss_bytes,
            &TELEMETRY_EVENTS_ENQUEUED.load(Ordering::Relaxed),
            &TELEMETRY_EVENTS_WRITTEN.load(Ordering::Relaxed),
            &TELEMETRY_EVENTS_DROPPED.load(Ordering::Relaxed),
            &metadata_json,
        ],
    )?;
    upsert_sidecar_instance(pg, config)
}

fn write_query_telemetry_batch(
    pg: &mut Client,
    config: &TelemetryConfig,
    batch: &[QueryTelemetryEvent],
) -> Result<()> {
    let mut tx = pg.transaction()?;
    let stmt = tx.prepare(
        "INSERT INTO rvbbit.duck_sidecar_query_events \
         (instance_id, hostname, node_id, pid, mode, engine, layout, worker_id, command, query_hash, status, \
          queue_wait_ms, elapsed_ms, execute_ms, route_safety_ms, parquet_prewarm_ms, row_count, result_format, \
          arrow_ipc_bytes, repeat_count, timeout_s, max_rows, error, cache, tables, metadata) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, \
                 $19, $20, $21, $22, $23, $24::text::jsonb, $25::text::jsonb, $26::text::jsonb)",
    )?;
    for event in batch {
        tx.execute(
            &stmt,
            &[
                &config.instance_id,
                &config.hostname,
                &config.node_id,
                &config.pid,
                &config.mode,
                &config.engine,
                &config.layout,
                &event.worker_id,
                &event.command,
                &event.query_hash,
                &event.status,
                &event.queue_wait_ms,
                &event.elapsed_ms,
                &event.execute_ms,
                &event.route_safety_ms,
                &event.parquet_prewarm_ms,
                &event.row_count,
                &event.result_format,
                &event.arrow_ipc_bytes,
                &event.repeat_count,
                &event.timeout_s,
                &event.max_rows,
                &event.error,
                &event.cache_json,
                &event.tables_json,
                &event.metadata_json,
            ],
        )?;
    }
    tx.commit()?;
    Ok(())
}

fn process_rss_bytes() -> Option<i64> {
    let statm = fs::read_to_string("/proc/self/statm").ok()?;
    let rss_pages = statm
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse::<i64>().ok())?;
    Some(rss_pages.saturating_mul(4096))
}

fn local_hostname() -> String {
    env::var("RVBBIT_HOSTNAME")
        .or_else(|_| env::var("HOSTNAME"))
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            fs::read_to_string("/etc/hostname")
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
        .unwrap_or_else(|| "unknown".to_string())
}

fn env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(default)
}

fn env_u64(name: &str, default: u64) -> u64 {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(default)
}

fn stable_hash_hex(value: &str) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in value.as_bytes() {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn json_string<T: Serialize>(value: &T, fallback: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| fallback.to_string())
}

fn run_once_from_args(args: &Args) -> Result<QuerySummary> {
    let sql = args
        .sql
        .as_deref()
        .ok_or_else(|| anyhow!("--sql is required unless --serve is set"))?;
    guarded_safe_select(sql)?;

    let mut pg = connect_pg(args)?;
    let catalog = rvbbit_row_group_catalog(&mut pg, args)?;
    ensure_query_tables_authoritative(&mut pg, sql, &catalog)?;
    if catalog.is_empty() {
        bail!("no authoritative compacted Rvbbit parquet tables are visible");
    }
    let mut footer_cache = ParquetFooterCache::default();
    let mut cache = CacheSummary {
        catalog_fingerprint: catalog_signature(&catalog),
        ..CacheSummary::default()
    };
    let footer = prewarm_parquet_metadata(&catalog, &mut footer_cache)?;
    cache.apply_footer_stats(footer);

    match args.engine {
        Engine::Duck => run_duck_once(args, sql, catalog, cache),
        Engine::DataFusion => run_datafusion_once(args, sql, catalog, cache),
    }
}

fn connect_pg(args: &Args) -> Result<Client> {
    let mut pg = Client::connect(&args.dsn, NoTls).context("connecting to Postgres")?;
    pg.simple_query("SET application_name = 'rvbbit-duck-sidecar'")
        .context("setting Postgres application_name")?;
    Ok(pg)
}

fn run_duck_once(
    args: &Args,
    sql: &str,
    catalog: BTreeMap<String, RvbbitDuckTable>,
    cache: CacheSummary,
) -> Result<QuerySummary> {
    let con = open_duck(args.threads)?;
    create_duck_views(&con, &catalog)?;
    let mut explain = con
        .prepare(&format!("EXPLAIN {sql}"))
        .context("preparing DuckDB EXPLAIN")?;
    let _ = explain.query([])?.next();
    drop(explain);
    if args.explain_only {
        return Ok(QuerySummary {
            status: "ok".to_string(),
            elapsed_ms: 0.0,
            repeat: 0,
            timeout_s: args.timeout_s,
            row_count: 0,
            columns: Vec::new(),
            rows: Vec::new(),
            result_format: ResultFormat::Json.as_str().to_string(),
            arrow_ipc_path: None,
            arrow_ipc_bytes: None,
            tables: table_summaries(&catalog),
            cache,
        });
    }

    let mut elapsed = Vec::with_capacity(args.repeat);
    let mut last = QueryRows::default();
    for _ in 0..args.repeat.max(1) {
        cleanup_query_rows(&mut last);
        let start = Instant::now();
        last = execute_duck_query_result(&con, sql, args.max_rows, args.result_format)?;
        elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    elapsed.sort_by(|a, b| a.total_cmp(b));
    let median = elapsed[elapsed.len() / 2];
    Ok(query_summary_from_rows(
        median,
        args.repeat.max(1),
        args.timeout_s,
        last,
        &catalog,
        cache,
    ))
}

fn run_datafusion_once(
    args: &Args,
    sql: &str,
    catalog: BTreeMap<String, RvbbitDuckTable>,
    cache: CacheSummary,
) -> Result<QuerySummary> {
    let runtime = datafusion_runtime(args.threads)?;
    runtime.block_on(async { run_datafusion_once_async(args, sql, catalog, cache).await })
}

async fn run_datafusion_once_async(
    args: &Args,
    sql: &str,
    catalog: BTreeMap<String, RvbbitDuckTable>,
    cache: CacheSummary,
) -> Result<QuerySummary> {
    let ctx = datafusion_context(args.threads);
    create_datafusion_views(&ctx, &catalog).await?;

    ctx.sql(&format!("EXPLAIN {sql}"))
        .await
        .context("preparing DataFusion EXPLAIN")?
        .collect()
        .await
        .context("running DataFusion EXPLAIN")?;
    if args.explain_only {
        return Ok(QuerySummary {
            status: "ok".to_string(),
            elapsed_ms: 0.0,
            repeat: 0,
            timeout_s: args.timeout_s,
            row_count: 0,
            columns: Vec::new(),
            rows: Vec::new(),
            result_format: ResultFormat::Json.as_str().to_string(),
            arrow_ipc_path: None,
            arrow_ipc_bytes: None,
            tables: table_summaries(&catalog),
            cache,
        });
    }

    let mut elapsed = Vec::with_capacity(args.repeat.max(1));
    let mut last = QueryRows::default();
    for _ in 0..args.repeat.max(1) {
        cleanup_query_rows(&mut last);
        let start = Instant::now();
        last =
            execute_datafusion_query_result(&ctx, sql, args.max_rows, args.result_format).await?;
        elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    elapsed.sort_by(|a, b| a.total_cmp(b));
    Ok(query_summary_from_rows(
        elapsed[elapsed.len() / 2],
        args.repeat.max(1),
        args.timeout_s,
        last,
        &catalog,
        cache,
    ))
}

async fn create_datafusion_views(
    ctx: &SessionContext,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
) -> Result<()> {
    let mut rel_counts = BTreeMap::<String, usize>::new();
    for table in catalog.values() {
        *rel_counts.entry(table.relname.clone()).or_default() += 1;
    }

    for table in catalog.values() {
        let raw_name = datafusion_raw_table_name(table);
        let mut read_options = ParquetReadOptions::default()
            .parquet_pruning(true)
            .skip_metadata(true);
        if !table.partition_cols.is_empty() {
            let partition_cols = table
                .partition_cols
                .iter()
                .map(|(name, typ)| (name.clone(), datafusion_partition_type(typ)))
                .collect::<Vec<_>>();
            read_options = read_options.table_partition_cols(partition_cols);
        }
        let raw_df = ctx
            .read_parquet(table.paths.clone(), read_options)
            .await
            .with_context(|| {
                format!(
                    "reading parquet for DataFusion table {}.{}",
                    table.schema, table.relname
                )
            })?;
        ctx.register_table(raw_name.clone(), raw_df.into_view())
            .with_context(|| format!("registering DataFusion table {raw_name}"))?;

        if rel_counts.get(&table.relname).copied().unwrap_or(0) == 1 {
            let select_list = if table.columns.is_empty() {
                "*".to_string()
            } else {
                table
                    .columns
                    .iter()
                    .map(|(col, typ)| datafusion_select_expr(col, typ))
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let view_sql = format!("SELECT {select_list} FROM {}", quote_ident(&raw_name));
            let view_df = ctx
                .sql(&view_sql)
                .await
                .with_context(|| format!("planning DataFusion view for {}", table.relname))?;
            ctx.register_table(table.relname.clone(), view_df.into_view())
                .with_context(|| format!("registering DataFusion view {}", table.relname))?;
        }
    }
    Ok(())
}

fn datafusion_raw_table_name(table: &RvbbitDuckTable) -> String {
    format!(
        "__rvbbit_raw_{}_{}",
        sanitize_datafusion_ident(&table.schema),
        sanitize_datafusion_ident(&table.relname)
    )
}

fn sanitize_datafusion_ident(value: &str) -> String {
    value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

fn datafusion_select_expr(col: &str, typname: &str) -> String {
    let ident = quote_ident(col);
    if typname == "date" {
        format!("CAST({ident} AS DATE) AS {ident}")
    } else {
        ident
    }
}

fn datafusion_partition_type(typname: &str) -> DataType {
    match typname {
        "boolean" => DataType::Boolean,
        "smallint" => DataType::Int16,
        "integer" => DataType::Int32,
        "bigint" => DataType::Int64,
        "real" => DataType::Float32,
        "double precision" | "numeric" => DataType::Float64,
        _ => DataType::Utf8,
    }
}

async fn execute_datafusion_query(
    ctx: &SessionContext,
    sql: &str,
    max_rows: usize,
) -> Result<QueryRows> {
    let dataframe = ctx.sql(sql).await.context("planning DataFusion query")?;
    let batches = dataframe
        .collect()
        .await
        .context("executing DataFusion query")?;
    Ok(record_batches_to_query_rows(&batches, max_rows)?)
}

async fn execute_datafusion_query_result(
    ctx: &SessionContext,
    sql: &str,
    max_rows: usize,
    result_format: ResultFormat,
) -> Result<QueryRows> {
    match result_format {
        ResultFormat::Json => execute_datafusion_query(ctx, sql, max_rows).await,
        ResultFormat::ArrowIpcFile => execute_datafusion_query_arrow_ipc(ctx, sql, max_rows).await,
    }
}

async fn execute_datafusion_query_arrow_ipc(
    ctx: &SessionContext,
    sql: &str,
    max_rows: usize,
) -> Result<QueryRows> {
    let dataframe = ctx.sql(sql).await.context("planning DataFusion query")?;
    let batches = dataframe
        .collect()
        .await
        .context("executing DataFusion query")?;
    record_batches_to_arrow_ipc_rows(&batches, max_rows)
}

fn record_batches_to_query_rows(batches: &[RecordBatch], max_rows: usize) -> Result<QueryRows> {
    let columns = batches
        .first()
        .map(|batch| {
            batch
                .schema()
                .fields()
                .iter()
                .map(|field| field.name().clone())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let mut out = QueryRows {
        columns,
        rows: Vec::new(),
        row_count: 0,
        result_format: ResultFormat::Json,
        arrow_ipc_path: None,
        arrow_ipc_bytes: None,
    };
    for batch in batches {
        for row_idx in 0..batch.num_rows() {
            if out.rows.len() < max_rows {
                let mut row = Vec::with_capacity(batch.num_columns());
                for col_idx in 0..batch.num_columns() {
                    row.push(arrow_value_to_json(batch.column(col_idx), row_idx)?);
                }
                out.rows.push(row);
            }
            out.row_count += 1;
        }
    }
    Ok(out)
}

fn record_batches_to_arrow_ipc_rows(batches: &[RecordBatch], max_rows: usize) -> Result<QueryRows> {
    let Some(first) = batches.first() else {
        return Ok(QueryRows::default());
    };
    let columns = schema_column_names(&first.schema());
    let (capped, row_count) = capped_record_batches(batches, max_rows);
    if capped.is_empty() {
        return Ok(QueryRows {
            columns,
            rows: Vec::new(),
            row_count,
            result_format: ResultFormat::Json,
            arrow_ipc_path: None,
            arrow_ipc_bytes: None,
        });
    }
    let (path, bytes) = write_arrow_ipc_file(first.schema(), &capped)?;
    Ok(QueryRows {
        columns,
        rows: Vec::new(),
        row_count,
        result_format: ResultFormat::ArrowIpcFile,
        arrow_ipc_path: Some(path),
        arrow_ipc_bytes: Some(bytes),
    })
}

fn capped_record_batches(batches: &[RecordBatch], max_rows: usize) -> (Vec<RecordBatch>, usize) {
    let mut row_count = 0usize;
    let mut remaining = max_rows;
    let mut capped = Vec::new();
    for batch in batches {
        row_count += batch.num_rows();
        if remaining == 0 {
            continue;
        }
        let len = remaining.min(batch.num_rows());
        if len > 0 {
            capped.push(batch.slice(0, len));
            remaining -= len;
        }
    }
    (capped, row_count)
}

fn schema_column_names(schema: &SchemaRef) -> Vec<String> {
    schema
        .fields()
        .iter()
        .map(|field| field.name().clone())
        .collect()
}

fn write_arrow_ipc_file(schema: SchemaRef, batches: &[RecordBatch]) -> Result<(String, u64)> {
    let dir =
        env::var("RVBBIT_ARROW_IPC_DIR").unwrap_or_else(|_| "/tmp/rvbbit-arrow-ipc".to_string());
    fs::create_dir_all(&dir).with_context(|| format!("creating Arrow IPC dir {dir}"))?;
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o777))
        .with_context(|| format!("setting Arrow IPC dir permissions on {dir}"))?;
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let path = format!("{dir}/rvbbit-{}-{nanos}.arrow", process::id());
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .with_context(|| format!("creating Arrow IPC file {path}"))?;
    let mut writer = StreamWriter::try_new(file, &schema)?;
    for batch in batches {
        writer.write(batch)?;
    }
    writer.finish()?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o666))
        .with_context(|| format!("setting Arrow IPC file permissions on {path}"))?;
    let bytes = fs::metadata(&path)?.len();
    Ok((path, bytes))
}

fn arrow_value_to_json(array: &ArrayRef, row_idx: usize) -> Result<Value> {
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
        DataType::Date32 => {
            json!(array_value_to_string(array.as_ref(), row_idx)?)
        }
        DataType::Timestamp(_, _) => json!(array_value_to_string(array.as_ref(), row_idx)?),
        _ => json!(array_value_to_string(array.as_ref(), row_idx)?),
    };
    Ok(value)
}

#[derive(Debug, Deserialize)]
struct ServerRequest {
    sql: Option<String>,
    command: Option<String>,
    repeat: Option<usize>,
    timeout_s: Option<u64>,
    threads: Option<usize>,
    max_rows: Option<usize>,
    result_format: Option<String>,
    explain_only: Option<bool>,
}

#[derive(Clone)]
struct CatalogSnapshot {
    fingerprint: String,
    catalog: BTreeMap<String, RvbbitDuckTable>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct FileIdentity {
    len: u64,
    modified_nanos: u128,
}

#[derive(Debug, Clone)]
struct ParquetFooterEntry {
    identity: FileIdentity,
    rows: i64,
    row_groups: usize,
    columns: usize,
    schema_signature: String,
}

#[derive(Default)]
struct ParquetFooterCache {
    entries: HashMap<String, ParquetFooterEntry>,
}

#[derive(Default)]
struct RouteSafetyCache {
    fingerprint: String,
    entries: HashMap<String, ()>,
}

#[derive(Default)]
struct RouteSafetyStats {
    hit: bool,
    local: bool,
    entries: usize,
    elapsed_ms: f64,
}

#[derive(Default)]
struct FooterCacheStats {
    hits: usize,
    misses: usize,
    files: usize,
    rows: i64,
    row_groups: usize,
    columns: usize,
    schema_bytes: usize,
    elapsed_ms: f64,
}

impl CacheSummary {
    fn apply_route_safety_stats(&mut self, stats: RouteSafetyStats) {
        self.route_safety_cache_hit = stats.hit;
        self.route_safety_local_hit = stats.local;
        self.route_safety_check_ms = stats.elapsed_ms;
        self.route_safety_cache_entries = stats.entries;
    }

    fn apply_footer_stats(&mut self, stats: FooterCacheStats) {
        self.parquet_footer_hits = stats.hits;
        self.parquet_footer_misses = stats.misses;
        self.parquet_footer_files = stats.files;
        self.parquet_footer_rows = stats.rows;
        self.parquet_footer_row_groups = stats.row_groups;
        self.parquet_footer_columns = stats.columns;
        self.parquet_footer_schema_bytes = stats.schema_bytes;
        self.parquet_prewarm_ms = stats.elapsed_ms;
    }
}

struct ServerState {
    pg: Client,
    engine: Engine,
    executor: Option<ServerExecutor>,
    catalog: Option<CatalogSnapshot>,
    executor_fingerprint: String,
    footer_cache: ParquetFooterCache,
    route_safety_cache: RouteSafetyCache,
    threads: usize,
    telemetry: Option<Arc<TelemetrySink>>,
    worker_id: Option<i32>,
}

enum ServerExecutor {
    Duck(Connection),
    DataFusion {
        runtime: Runtime,
        ctx: SessionContext,
    },
}

impl ServerState {
    fn new(args: &Args, worker_id: Option<usize>) -> Result<Self> {
        let pg = connect_pg(args)?;
        Ok(Self {
            pg,
            engine: args.engine,
            executor: None,
            catalog: None,
            executor_fingerprint: String::new(),
            footer_cache: ParquetFooterCache::default(),
            route_safety_cache: RouteSafetyCache::default(),
            threads: args.threads.max(1),
            telemetry: telemetry_sink(args),
            worker_id: worker_id.map(|value| value as i32),
        })
    }

    fn record_query_telemetry(&self, event: QueryTelemetryEvent) {
        if let Some(telemetry) = &self.telemetry {
            telemetry.record(event);
        }
    }

    fn execute(&mut self, args: &Args, req: ServerRequest) -> Result<QuerySummary> {
        if req
            .command
            .as_deref()
            .is_some_and(|command| command.eq_ignore_ascii_case("prewarm"))
        {
            return self.prewarm(args, req);
        }

        let sql = req
            .sql
            .as_deref()
            .ok_or_else(|| anyhow!("server request requires sql"))?;
        guarded_safe_select(sql)?;
        let repeat = req.repeat.unwrap_or(args.repeat).max(1);
        let timeout_s = req.timeout_s.unwrap_or(args.timeout_s);
        let max_rows = req.max_rows.unwrap_or(args.max_rows);
        let result_format = req
            .result_format
            .as_deref()
            .map(parse_result_format)
            .transpose()?
            .unwrap_or(args.result_format);
        let explain_only = req.explain_only.unwrap_or(args.explain_only);
        let threads = req.threads.unwrap_or(args.threads).max(1);

        let (catalog, mut cache) = self.load_catalog(args)?;
        let safety_stats = self.ensure_query_tables_authoritative_cached(
            sql,
            &cache.catalog_fingerprint,
            &catalog,
        )?;
        cache.apply_route_safety_stats(safety_stats);
        if catalog.is_empty() {
            bail!("no authoritative compacted Rvbbit parquet tables are visible");
        }

        let needs_executor = self.executor.is_none()
            || cache.catalog_fingerprint != self.executor_fingerprint
            || threads != self.threads;
        cache.executor_cache_hit = !needs_executor;
        if needs_executor {
            let footer_stats = prewarm_parquet_metadata(&catalog, &mut self.footer_cache)?;
            cache.apply_footer_stats(footer_stats);
            self.executor = Some(Self::new_executor(self.engine, threads, &catalog)?);
            self.executor_fingerprint = cache.catalog_fingerprint.clone();
            self.threads = threads;
        } else {
            cache.apply_footer_stats(self.footer_cache.snapshot_stats(&catalog));
        }

        match self
            .executor
            .as_mut()
            .ok_or_else(|| anyhow!("persistent executor unavailable"))?
        {
            ServerExecutor::Duck(con) => {
                if explain_only {
                    let mut explain = con
                        .prepare(&format!("EXPLAIN {sql}"))
                        .context("preparing DuckDB EXPLAIN")?;
                    let _ = explain.query([])?.next();
                    drop(explain);
                    return Ok(empty_query_summary(timeout_s, &catalog, cache));
                }

                let mut elapsed = Vec::with_capacity(repeat);
                let mut last = QueryRows::default();
                for _ in 0..repeat {
                    cleanup_query_rows(&mut last);
                    let start = Instant::now();
                    last = execute_duck_query_result(con, sql, max_rows, result_format)?;
                    elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
                }
                elapsed.sort_by(|a, b| a.total_cmp(b));
                Ok(query_summary_from_rows(
                    elapsed[elapsed.len() / 2],
                    repeat,
                    timeout_s,
                    last,
                    &catalog,
                    cache,
                ))
            }
            ServerExecutor::DataFusion { runtime, ctx } => runtime.block_on(async {
                if explain_only {
                    ctx.sql(&format!("EXPLAIN {sql}"))
                        .await
                        .context("preparing DataFusion EXPLAIN")?
                        .collect()
                        .await
                        .context("running DataFusion EXPLAIN")?;
                    return Ok(empty_query_summary(timeout_s, &catalog, cache));
                }

                let mut elapsed = Vec::with_capacity(repeat);
                let mut last = QueryRows::default();
                for _ in 0..repeat {
                    cleanup_query_rows(&mut last);
                    let start = Instant::now();
                    last =
                        execute_datafusion_query_result(ctx, sql, max_rows, result_format).await?;
                    elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
                }
                elapsed.sort_by(|a, b| a.total_cmp(b));
                Ok(query_summary_from_rows(
                    elapsed[elapsed.len() / 2],
                    repeat,
                    timeout_s,
                    last,
                    &catalog,
                    cache,
                ))
            }),
        }
    }

    fn prewarm(&mut self, args: &Args, req: ServerRequest) -> Result<QuerySummary> {
        let timeout_s = req.timeout_s.unwrap_or(args.timeout_s);
        let threads = req.threads.unwrap_or(args.threads).max(1);
        let (catalog, mut cache) = self.load_catalog(args)?;
        if catalog.is_empty() {
            bail!("no authoritative compacted Rvbbit parquet tables are visible");
        }

        let needs_executor = self.executor.is_none()
            || cache.catalog_fingerprint != self.executor_fingerprint
            || threads != self.threads;
        cache.executor_cache_hit = !needs_executor;
        let footer_stats = prewarm_parquet_metadata(&catalog, &mut self.footer_cache)?;
        cache.apply_footer_stats(footer_stats);
        if needs_executor {
            self.executor = Some(Self::new_executor(self.engine, threads, &catalog)?);
            self.executor_fingerprint = cache.catalog_fingerprint.clone();
            self.threads = threads;
        }
        Ok(empty_query_summary(timeout_s, &catalog, cache))
    }

    fn load_catalog(
        &mut self,
        args: &Args,
    ) -> Result<(BTreeMap<String, RvbbitDuckTable>, CacheSummary)> {
        let mut cache = CacheSummary::default();
        if !metadata_cache_enabled() {
            let catalog = rvbbit_row_group_catalog(&mut self.pg, args)?;
            cache.catalog_fingerprint = catalog_signature(&catalog);
            return Ok((catalog, cache));
        }

        let fingerprint = rvbbit_catalog_fingerprint(&mut self.pg, args)?;
        cache.catalog_fingerprint = fingerprint.clone();
        if let Some(snapshot) = &self.catalog {
            if snapshot.fingerprint == fingerprint {
                cache.catalog_cache_hit = true;
                return Ok((snapshot.catalog.clone(), cache));
            }
        }

        let catalog = rvbbit_row_group_catalog(&mut self.pg, args)?;
        self.catalog = Some(CatalogSnapshot {
            fingerprint,
            catalog: catalog.clone(),
        });
        Ok((catalog, cache))
    }

    fn ensure_query_tables_authoritative_cached(
        &mut self,
        sql: &str,
        catalog_fingerprint: &str,
        catalog: &BTreeMap<String, RvbbitDuckTable>,
    ) -> Result<RouteSafetyStats> {
        let start = Instant::now();
        if !route_safety_cache_enabled() {
            ensure_query_tables_authoritative(&mut self.pg, sql, catalog)?;
            return Ok(RouteSafetyStats {
                hit: false,
                local: false,
                entries: self.route_safety_cache.entries.len(),
                elapsed_ms: start.elapsed().as_secs_f64() * 1000.0,
            });
        }

        if self.route_safety_cache.fingerprint != catalog_fingerprint {
            self.route_safety_cache.fingerprint = catalog_fingerprint.to_string();
            self.route_safety_cache.entries.clear();
        }

        if self.route_safety_cache.entries.contains_key(sql) {
            return Ok(RouteSafetyStats {
                hit: true,
                local: false,
                entries: self.route_safety_cache.entries.len(),
                elapsed_ms: start.elapsed().as_secs_f64() * 1000.0,
            });
        }

        let mut local = false;
        if route_safety_local_enabled() && ensure_query_tables_authoritative_local(sql, catalog) {
            local = true;
        } else {
            ensure_query_tables_authoritative(&mut self.pg, sql, catalog)?;
        }
        let max_entries = route_safety_cache_max_entries();
        if max_entries > 0 {
            if self.route_safety_cache.entries.len() >= max_entries {
                self.route_safety_cache.entries.clear();
            }
            self.route_safety_cache.entries.insert(sql.to_string(), ());
        }
        Ok(RouteSafetyStats {
            hit: false,
            local,
            entries: self.route_safety_cache.entries.len(),
            elapsed_ms: start.elapsed().as_secs_f64() * 1000.0,
        })
    }

    fn new_executor(
        engine: Engine,
        threads: usize,
        catalog: &BTreeMap<String, RvbbitDuckTable>,
    ) -> Result<ServerExecutor> {
        match engine {
            Engine::Duck => {
                let con = open_duck(threads)?;
                create_duck_views(&con, catalog)?;
                Ok(ServerExecutor::Duck(con))
            }
            Engine::DataFusion => {
                let runtime = datafusion_runtime(threads)?;
                let ctx = datafusion_context(threads);
                runtime.block_on(async { create_datafusion_views(&ctx, catalog).await })?;
                Ok(ServerExecutor::DataFusion { runtime, ctx })
            }
        }
    }
}

fn run_server(args: Args) -> Result<()> {
    let mut state = ServerState::new(&args, None)?;
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line.context("reading server request")?;
        if line.trim().is_empty() {
            continue;
        }
        writeln!(
            stdout,
            "{}",
            server_response_json(&mut state, &args, &line, None)
        )?;
        stdout.flush()?;
    }
    Ok(())
}

struct SocketJob {
    line: String,
    received_at: Instant,
    respond: mpsc::Sender<String>,
}

fn run_socket_server(args: Args) -> Result<()> {
    let socket_path = args
        .serve_socket
        .as_deref()
        .ok_or_else(|| anyhow!("--serve-socket requires a path"))?;
    if let Some(parent) = Path::new(socket_path).parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating socket directory {}", parent.display()))?;
    }
    let _ = fs::remove_file(socket_path);
    let listener =
        UnixListener::bind(socket_path).with_context(|| format!("binding {socket_path}"))?;
    fs::set_permissions(socket_path, fs::Permissions::from_mode(0o777))
        .with_context(|| format!("setting socket permissions on {socket_path}"))?;
    let workers = args.workers.max(1);
    let (tx, rx) = mpsc::channel::<SocketJob>();
    let rx = Arc::new(Mutex::new(rx));

    for idx in 0..workers {
        let worker_args = args.clone();
        let rx = Arc::clone(&rx);
        thread::Builder::new()
            .name(format!("rvbbit-duck-worker-{idx}"))
            .spawn(move || {
                let mut state = match ServerState::new(&worker_args, Some(idx)) {
                    Ok(state) => state,
                    Err(err) => {
                        eprintln!("rvbbit-duck worker startup failed: {err:#}");
                        return;
                    }
                };
                loop {
                    let job = {
                        let guard = match rx.lock() {
                            Ok(guard) => guard,
                            Err(_) => return,
                        };
                        guard.recv()
                    };
                    match job {
                        Ok(job) => {
                            BROKER_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
                            BROKER_ACTIVE_WORKERS.fetch_add(1, Ordering::Relaxed);
                            let queue_wait_ms = job.received_at.elapsed().as_secs_f64() * 1000.0;
                            let response = server_response_json(
                                &mut state,
                                &worker_args,
                                &job.line,
                                Some(queue_wait_ms),
                            );
                            BROKER_ACTIVE_WORKERS.fetch_sub(1, Ordering::Relaxed);
                            let _ = job.respond.send(response);
                        }
                        Err(_) => return,
                    }
                }
            })
            .context("spawning rvbbit-duck worker")?;
    }

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let tx = tx.clone();
                thread::spawn(move || {
                    let _ = handle_socket_client(stream, tx);
                });
            }
            Err(err) => eprintln!("rvbbit-duck socket accept failed: {err}"),
        }
    }
    Ok(())
}

fn handle_socket_client(mut stream: UnixStream, tx: mpsc::Sender<SocketJob>) -> Result<()> {
    let reader_stream = stream.try_clone().context("cloning Unix stream")?;
    let mut reader = BufReader::new(reader_stream);
    let mut line = String::new();
    let bytes = reader
        .read_line(&mut line)
        .context("reading socket request")?;
    if bytes == 0 || line.trim().is_empty() {
        return Ok(());
    }
    let (respond, response_rx) = mpsc::channel();
    BROKER_QUEUE_DEPTH.fetch_add(1, Ordering::Relaxed);
    if let Err(err) = tx.send(SocketJob {
        line,
        received_at: Instant::now(),
        respond,
    }) {
        BROKER_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
        return Err(anyhow!("dispatching socket request: {err}"));
    }
    let response = response_rx.recv().context("waiting for socket response")?;
    stream
        .write_all(response.as_bytes())
        .context("writing socket response")?;
    stream
        .write_all(b"\n")
        .context("writing socket response newline")?;
    stream.flush().context("flushing socket response")?;
    Ok(())
}

fn server_response_json(
    state: &mut ServerState,
    args: &Args,
    line: &str,
    queue_wait_ms: Option<f64>,
) -> String {
    let request_started = Instant::now();
    let response = match serde_json::from_str::<ServerRequest>(line) {
        Ok(req) => {
            let command = req.command.clone();
            let query_hash = req.sql.as_deref().map(stable_hash_hex);
            let repeat_count = req.repeat.unwrap_or(args.repeat).max(1) as i32;
            let timeout_s = req.timeout_s.unwrap_or(args.timeout_s) as i32;
            let max_rows = req.max_rows.unwrap_or(args.max_rows) as i32;
            let metadata_json = json!({
                "explain_only": req.explain_only.unwrap_or(args.explain_only),
                "requested_threads": req.threads.unwrap_or(args.threads),
            })
            .to_string();
            match state.execute(args, req) {
                Ok(summary) => {
                    state.record_query_telemetry(QueryTelemetryEvent {
                        worker_id: state.worker_id,
                        command,
                        query_hash,
                        status: summary.status.clone(),
                        queue_wait_ms,
                        elapsed_ms: request_started.elapsed().as_secs_f64() * 1000.0,
                        execute_ms: Some(summary.elapsed_ms),
                        route_safety_ms: Some(summary.cache.route_safety_check_ms),
                        parquet_prewarm_ms: Some(summary.cache.parquet_prewarm_ms),
                        row_count: Some(summary.row_count as i64),
                        result_format: Some(summary.result_format.clone()),
                        arrow_ipc_bytes: summary.arrow_ipc_bytes.map(|value| value as i64),
                        repeat_count: Some(repeat_count),
                        timeout_s: Some(timeout_s),
                        max_rows: Some(max_rows),
                        error: None,
                        cache_json: json_string(&summary.cache, "{}"),
                        tables_json: json_string(&summary.tables, "[]"),
                        metadata_json,
                    });
                    serde_json::to_value(summary).unwrap_or_else(
                        |err| json!({"status": "fallback", "error": err.to_string()}),
                    )
                }
                Err(err) => {
                    let error = format_error_chain(&err);
                    state.record_query_telemetry(QueryTelemetryEvent {
                        worker_id: state.worker_id,
                        command,
                        query_hash,
                        status: "fallback".to_string(),
                        queue_wait_ms,
                        elapsed_ms: request_started.elapsed().as_secs_f64() * 1000.0,
                        execute_ms: None,
                        route_safety_ms: None,
                        parquet_prewarm_ms: None,
                        row_count: None,
                        result_format: None,
                        arrow_ipc_bytes: None,
                        repeat_count: Some(repeat_count),
                        timeout_s: Some(timeout_s),
                        max_rows: Some(max_rows),
                        error: Some(error.clone()),
                        cache_json: "{}".to_string(),
                        tables_json: "[]".to_string(),
                        metadata_json,
                    });
                    json!({"status": "fallback", "error": error})
                }
            }
        }
        Err(err) => {
            let error = format!("invalid request JSON: {err}");
            state.record_query_telemetry(QueryTelemetryEvent {
                worker_id: state.worker_id,
                command: None,
                query_hash: None,
                status: "fallback".to_string(),
                queue_wait_ms,
                elapsed_ms: request_started.elapsed().as_secs_f64() * 1000.0,
                execute_ms: None,
                route_safety_ms: None,
                parquet_prewarm_ms: None,
                row_count: None,
                result_format: None,
                arrow_ipc_bytes: None,
                repeat_count: None,
                timeout_s: None,
                max_rows: None,
                error: Some(error.clone()),
                cache_json: "{}".to_string(),
                tables_json: "[]".to_string(),
                metadata_json: "{}".to_string(),
            });
            json!({"status": "fallback", "error": error})
        }
    };
    serde_json::to_string(&response)
        .unwrap_or_else(|err| json!({"status": "fallback", "error": err.to_string()}).to_string())
}

fn open_duck(threads: usize) -> Result<Connection> {
    let con = Connection::open_in_memory().context("opening DuckDB")?;
    con.execute_batch(&format!("PRAGMA threads={}", threads.max(1)))
        .context("setting DuckDB threads")?;
    Ok(con)
}

fn datafusion_runtime(threads: usize) -> Result<Runtime> {
    RuntimeBuilder::new_multi_thread()
        .worker_threads(threads.max(1))
        .enable_all()
        .build()
        .context("creating DataFusion runtime")
}

fn datafusion_context(threads: usize) -> SessionContext {
    let config = SessionConfig::new()
        .with_target_partitions(threads.max(1))
        .with_information_schema(true);
    SessionContext::new_with_config(config)
}

fn empty_query_summary(
    timeout_s: u64,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    cache: CacheSummary,
) -> QuerySummary {
    QuerySummary {
        status: "ok".to_string(),
        elapsed_ms: 0.0,
        repeat: 0,
        timeout_s,
        row_count: 0,
        columns: Vec::new(),
        rows: Vec::new(),
        result_format: ResultFormat::Json.as_str().to_string(),
        arrow_ipc_path: None,
        arrow_ipc_bytes: None,
        tables: table_summaries(catalog),
        cache,
    }
}

fn query_summary_from_rows(
    elapsed_ms: f64,
    repeat: usize,
    timeout_s: u64,
    rows: QueryRows,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    cache: CacheSummary,
) -> QuerySummary {
    QuerySummary {
        status: "ok".to_string(),
        elapsed_ms,
        repeat,
        timeout_s,
        row_count: rows.row_count,
        columns: rows.columns,
        rows: rows.rows,
        result_format: rows.result_format.as_str().to_string(),
        arrow_ipc_path: rows.arrow_ipc_path,
        arrow_ipc_bytes: rows.arrow_ipc_bytes,
        tables: table_summaries(catalog),
        cache,
    }
}

fn cleanup_query_rows(rows: &mut QueryRows) {
    if let Some(path) = rows.arrow_ipc_path.take() {
        let _ = fs::remove_file(path);
    }
}

fn parse_args() -> Result<Args> {
    let mut engine = env::var("RVBBIT_ENGINE")
        .ok()
        .as_deref()
        .map(parse_engine)
        .transpose()?
        .unwrap_or(Engine::Duck);
    let mut dsn = env::var("RVBBIT_DSN").unwrap_or_else(|_| DEFAULT_DSN.to_string());
    let mut sql = None;
    let mut repeat = 1usize;
    let mut timeout_s = 300u64;
    let mut threads = 4usize;
    let mut max_rows = 20usize;
    let mut pgdata_prefix =
        env::var("RVBBIT_PGDATA_PREFIX").unwrap_or_else(|_| DEFAULT_PGDATA_PREFIX.to_string());
    let mut visible_pgdata_prefix = env::var("RVBBIT_VISIBLE_PGDATA_PREFIX")
        .unwrap_or_else(|_| DEFAULT_VISIBLE_PGDATA_PREFIX.to_string());
    let mut layout = env::var("RVBBIT_PARQUET_LAYOUT").unwrap_or_else(|_| "scan".to_string());
    let mut result_format = env::var("RVBBIT_RESULT_FORMAT")
        .ok()
        .as_deref()
        .map(parse_result_format)
        .transpose()?
        .unwrap_or(ResultFormat::Json);
    let mut explain_only = false;
    let mut serve = false;
    let mut serve_socket = None;
    let mut serve_derived_socket = false;
    let mut workers = env::var("RVBBIT_DUCK_WORKERS")
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(4);

    let mut it = env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--engine" => engine = parse_engine(&need_value(&mut it, "--engine")?)?,
            "--dsn" => dsn = need_value(&mut it, "--dsn")?,
            "--sql" => sql = Some(need_value(&mut it, "--sql")?),
            "--repeat" => repeat = need_value(&mut it, "--repeat")?.parse()?,
            "--timeout-s" => timeout_s = need_value(&mut it, "--timeout-s")?.parse()?,
            "--threads" => threads = need_value(&mut it, "--threads")?.parse()?,
            "--max-rows" => max_rows = need_value(&mut it, "--max-rows")?.parse()?,
            "--pgdata-prefix" => pgdata_prefix = need_value(&mut it, "--pgdata-prefix")?,
            "--visible-pgdata-prefix" => {
                visible_pgdata_prefix = need_value(&mut it, "--visible-pgdata-prefix")?
            }
            "--layout" => layout = need_value(&mut it, "--layout")?,
            "--result-format" => {
                result_format = parse_result_format(&need_value(&mut it, "--result-format")?)?
            }
            "--explain-only" => explain_only = true,
            "--serve" => serve = true,
            "--serve-socket" => serve_socket = Some(need_value(&mut it, "--serve-socket")?),
            "--serve-derived-socket" => serve_derived_socket = true,
            "--workers" => workers = need_value(&mut it, "--workers")?.parse()?,
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            other => bail!("unknown argument: {other}"),
        }
    }

    let mut args = Args {
        engine,
        dsn,
        sql,
        repeat,
        timeout_s,
        threads,
        max_rows,
        pgdata_prefix,
        visible_pgdata_prefix,
        layout,
        result_format,
        explain_only,
        serve,
        serve_socket,
        workers,
    };

    if serve_derived_socket {
        if args.serve_socket.is_some() {
            bail!("--serve-derived-socket cannot be combined with --serve-socket");
        }
        args.serve_socket = Some(derived_shared_socket_path(&args)?);
    }

    if !args.serve && args.serve_socket.is_none() && args.sql.is_none() {
        bail!("--sql is required unless --serve is set");
    }
    Ok(args)
}

fn duck_binary_key() -> String {
    env::var("RVBBIT_DUCK_BIN")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_DUCK_BIN.to_string())
}

fn derived_shared_socket_path(args: &Args) -> Result<String> {
    let dir = env::var("RVBBIT_DUCK_BACKEND_SHARED_DIR")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "/tmp/rvbbit-duck".to_string());
    fs::create_dir_all(&dir).with_context(|| format!("creating shared socket dir {dir}"))?;

    let mut hasher = DefaultHasher::new();
    duck_binary_key().hash(&mut hasher);
    args.dsn.hash(&mut hasher);
    args.engine.as_str().hash(&mut hasher);
    args.layout.hash(&mut hasher);
    args.threads.hash(&mut hasher);
    args.workers.hash(&mut hasher);
    args.pgdata_prefix.hash(&mut hasher);
    args.visible_pgdata_prefix.hash(&mut hasher);

    Ok(format!(
        "{}/rvbbit-duck-{:016x}.sock",
        dir.trim_end_matches('/'),
        hasher.finish()
    ))
}

fn parse_engine(raw: &str) -> Result<Engine> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "" | "duck" | "duckdb" => Ok(Engine::Duck),
        "datafusion" | "df" => Ok(Engine::DataFusion),
        other => bail!("unsupported engine: {other}"),
    }
}

fn parse_result_format(raw: &str) -> Result<ResultFormat> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "" | "json" => Ok(ResultFormat::Json),
        "arrow" | "arrow_ipc" | "arrow_ipc_file" => Ok(ResultFormat::ArrowIpcFile),
        other => bail!("unsupported result format: {other}"),
    }
}

fn need_value(it: &mut impl Iterator<Item = String>, name: &str) -> Result<String> {
    it.next().ok_or_else(|| anyhow!("{name} requires a value"))
}

fn print_help() {
    println!(
        "rvbbit-duck --sql SQL [--engine duck|datafusion] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--repeat N] [--timeout-s N] [--threads N] [--max-rows N] [--result-format json|arrow_ipc_file]\n\
         rvbbit-duck --serve [--engine duck|datafusion] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--threads N]\n\
         rvbbit-duck --serve-socket PATH [--workers N] [--engine duck|datafusion] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--threads N]\n\
         rvbbit-duck --serve-derived-socket [--workers N] [--engine duck|datafusion] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--threads N]\n\
         Server JSONL requests: {{\"sql\":\"SELECT ...\",\"result_format\":\"json|arrow_ipc_file\"}} or {{\"command\":\"prewarm\"}}"
    );
}

fn guarded_safe_select(sql: &str) -> Result<()> {
    let stripped = sql.trim();
    let lowered = sql_stringless(stripped).to_lowercase();
    if !(lowered.starts_with("select") || lowered.starts_with("with")) {
        bail!("not a read-only SELECT");
    }
    if lowered.trim_end_matches(';').contains(';') {
        bail!("multiple statements are not supported");
    }
    for token in [
        "insert",
        "update",
        "delete",
        "merge",
        "copy",
        "create",
        "alter",
        "drop",
        "truncate",
        "vacuum",
        "grant",
        "revoke",
        "call",
        "do",
        "refresh",
        "listen",
        "notify",
        "rvbbit.",
        "pg_",
        "nextval",
        "setval",
        "currval",
        "set_config",
        "current_setting",
        "random",
        "regex_replace",
        "regexp_replace",
        "::json",
        "::jsonb",
        "->",
        "$$",
    ] {
        if unsupported_token_present(&lowered, token) {
            bail!("unsupported token: {token}");
        }
    }
    Ok(())
}

fn unsupported_token_present(sql: &str, token: &str) -> bool {
    if matches!(
        token,
        "rvbbit." | "pg_" | "::json" | "::jsonb" | "->" | "$$"
    ) {
        return sql.contains(token);
    }
    contains_identifier_token(sql, token)
}

fn contains_identifier_token(sql: &str, token: &str) -> bool {
    sql.match_indices(token).any(|(idx, _)| {
        let before = sql[..idx].chars().next_back();
        let after = sql[idx + token.len()..].chars().next();
        !before.is_some_and(is_identifier_char) && !after.is_some_and(is_identifier_char)
    })
}

fn is_identifier_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_' || ch == '$'
}

fn sql_stringless(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len());
    let chars: Vec<char> = sql.chars().collect();
    let mut i = 0;
    let mut in_line_comment = false;
    let mut in_block_comment = false;
    let mut in_string = false;
    while i < chars.len() {
        let ch = chars[i];
        let next = chars.get(i + 1).copied().unwrap_or('\0');
        if in_line_comment {
            if ch == '\n' {
                in_line_comment = false;
                out.push(ch);
            } else {
                out.push(' ');
            }
            i += 1;
            continue;
        }
        if in_block_comment {
            if ch == '*' && next == '/' {
                in_block_comment = false;
                out.push_str("  ");
                i += 2;
            } else {
                out.push(' ');
                i += 1;
            }
            continue;
        }
        if in_string {
            if ch == '\'' {
                if next == '\'' {
                    out.push_str("  ");
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            out.push(' ');
            i += 1;
            continue;
        }
        if ch == '-' && next == '-' {
            in_line_comment = true;
            out.push_str("  ");
            i += 2;
            continue;
        }
        if ch == '/' && next == '*' {
            in_block_comment = true;
            out.push_str("  ");
            i += 2;
            continue;
        }
        if ch == '\'' {
            in_string = true;
            out.push(' ');
            i += 1;
            continue;
        }
        out.push(ch);
        i += 1;
    }
    out
}

fn metadata_cache_enabled() -> bool {
    env_enabled("RVBBIT_PARQUET_META_CACHE", true)
}

fn parquet_prewarm_enabled() -> bool {
    metadata_cache_enabled() && env_enabled("RVBBIT_PARQUET_PREWARM", true)
}

fn route_safety_cache_enabled() -> bool {
    env_enabled("RVBBIT_ROUTE_SAFETY_CACHE", true) && route_safety_cache_max_entries() > 0
}

fn route_safety_local_enabled() -> bool {
    env_enabled("RVBBIT_ROUTE_SAFETY_LOCAL", true)
}

fn route_safety_cache_max_entries() -> usize {
    env::var("RVBBIT_ROUTE_SAFETY_CACHE_MAX")
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(4096)
}

fn env_enabled(name: &str, default: bool) -> bool {
    match env::var(name) {
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

fn rvbbit_catalog_fingerprint(pg: &mut Client, args: &Args) -> Result<String> {
    let rows = pg.query(&catalog_fingerprint_sql_for_layout(&args.layout)?, &[])?;
    Ok(rows
        .first()
        .map(|row| {
            row.get::<_, Option<String>>(0)
                .unwrap_or_else(|| "empty".to_string())
        })
        .unwrap_or_else(|| "empty".to_string()))
}

fn catalog_fingerprint_sql_for_layout(layout: &str) -> Result<String> {
    let trimmed = layout.trim();
    let lower = trimmed.to_ascii_lowercase();
    match lower.as_str() {
        "" | "scan" | "canonical" | "default" => Ok(catalog_fingerprint_sql(
            "NULL::text",
            "NULL::text",
            "rvbbit.row_groups rg",
            "",
        )),
        "hive" | "cluster" => {
            let prefix = format!("{lower}:%");
            Ok(variant_catalog_fingerprint_sql(&format!(
                "rg.layout LIKE '{}'",
                prefix.replace('\'', "''")
            )))
        }
        "vortex" | "vortex_scan" => Ok(variant_catalog_fingerprint_sql_exact(
            "rg.layout = 'vortex_scan'",
        )),
        _ if lower.starts_with("hive:") || lower.starts_with("cluster:") => {
            validate_layout_name(trimmed)?;
            Ok(variant_catalog_fingerprint_sql(&format!(
                "rg.layout = '{}'",
                trimmed.replace('\'', "''")
            )))
        }
        other => bail!("unsupported parquet layout: {other}"),
    }
}

fn variant_catalog_fingerprint_sql(layout_predicate: &str) -> String {
    format!(
        "
        WITH chosen_layout AS (
            SELECT rg.table_oid, min(rg.layout) AS layout
            FROM rvbbit.row_group_variants rg
            JOIN rvbbit.layout_variant_status s
              ON s.table_oid = rg.table_oid AND s.layout = rg.layout
            WHERE {layout_predicate}
              AND s.status = 'ready'
            GROUP BY rg.table_oid
        ),
        table_state AS (
            SELECT n.nspname,
                   c.relname,
                   c.oid::bigint AS oid,
                   cl.layout AS layout,
                   count(rg.*)::bigint AS row_groups,
                   coalesce(sum(rg.n_rows), 0)::bigint AS row_group_rows,
                   coalesce(sum(rg.n_bytes), 0)::bigint AS row_group_bytes,
                   coalesce(max(rg.rg_id), -1)::bigint AS max_rg_id,
                   coalesce((extract(epoch FROM max(rg.created_at)) * 1000000)::bigint, 0)::bigint AS max_created_us,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid) AS deletes,
                   coalesce(md5(string_agg(rg.path || ':' || rg.n_rows || ':' || rg.n_bytes || ':' ||
                           coalesce((extract(epoch FROM rg.created_at) * 1000000)::bigint, 0), ',' ORDER BY rg.rg_id)), '') AS path_sig,
                   coalesce((
                       SELECT md5(string_agg(a.attname::text || ':' || a.atttypid::regtype::text, ',' ORDER BY a.attnum))
                       FROM pg_attribute a
                       WHERE a.attrelid = c.oid
                         AND a.attnum > 0
                         AND NOT a.attisdropped
                   ), '') AS column_sig
            FROM rvbbit.row_group_variants rg
            JOIN chosen_layout cl ON cl.table_oid = rg.table_oid AND cl.layout = rg.layout
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
            GROUP BY n.nspname, c.relname, c.oid, cl.layout, t.shadow_heap_retained, t.shadow_heap_dirty
            UNION ALL
            SELECT n.nspname,
                   c.relname,
                   c.oid::bigint AS oid,
                   NULL::text AS layout,
                   count(rg.*)::bigint AS row_groups,
                   coalesce(sum(rg.n_rows), 0)::bigint AS row_group_rows,
                   coalesce(sum(rg.n_bytes), 0)::bigint AS row_group_bytes,
                   coalesce(max(rg.rg_id), -1)::bigint AS max_rg_id,
                   coalesce((extract(epoch FROM max(rg.created_at)) * 1000000)::bigint, 0)::bigint AS max_created_us,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid) AS deletes,
                   coalesce(md5(string_agg(rg.path || ':' || rg.n_rows || ':' || rg.n_bytes || ':' ||
                           coalesce((extract(epoch FROM rg.created_at) * 1000000)::bigint, 0), ',' ORDER BY rg.rg_id)), '') AS path_sig,
                   coalesce((
                       SELECT md5(string_agg(a.attname::text || ':' || a.atttypid::regtype::text, ',' ORDER BY a.attnum))
                       FROM pg_attribute a
                       WHERE a.attrelid = c.oid
                         AND a.attnum > 0
                         AND NOT a.attisdropped
                   ), '') AS column_sig
            FROM rvbbit.row_groups rg
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
              AND NOT EXISTS (
                  SELECT 1
                  FROM chosen_layout cl
                  WHERE cl.table_oid = rg.table_oid
              )
            GROUP BY n.nspname, c.relname, c.oid, t.shadow_heap_retained, t.shadow_heap_dirty
        )
        SELECT coalesce(string_agg(
            nspname || '.' || relname || ':' || oid || ':' || coalesce(layout, 'scan') || ':' ||
            row_groups || ':' || row_group_rows || ':' || row_group_bytes || ':' || max_rg_id || ':' ||
            max_created_us || ':' || heap_bytes || ':' || shadow_heap_retained || ':' ||
            shadow_heap_dirty || ':' || deletes || ':' || path_sig || ':' || column_sig,
            E'\\n' ORDER BY nspname, relname, coalesce(layout, 'scan')
        ), 'empty') AS fingerprint
        FROM table_state
        "
    )
}

fn variant_catalog_fingerprint_sql_exact(layout_predicate: &str) -> String {
    format!(
        "
        WITH table_state AS (
            SELECT n.nspname,
                   c.relname,
                   c.oid::bigint AS oid,
                   rg.layout AS layout,
                   count(rg.*)::bigint AS row_groups,
                   coalesce(sum(rg.n_rows), 0)::bigint AS row_group_rows,
                   coalesce(sum(rg.n_bytes), 0)::bigint AS row_group_bytes,
                   coalesce(max(rg.rg_id), -1)::bigint AS max_rg_id,
                   coalesce((extract(epoch FROM max(rg.created_at)) * 1000000)::bigint, 0)::bigint AS max_created_us,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid) AS deletes,
                   coalesce(md5(string_agg(rg.path || ':' || rg.n_rows || ':' || rg.n_bytes || ':' ||
                           coalesce((extract(epoch FROM rg.created_at) * 1000000)::bigint, 0), ',' ORDER BY rg.rg_id)), '') AS path_sig,
                   coalesce((
                       SELECT md5(string_agg(a.attname::text || ':' || a.atttypid::regtype::text, ',' ORDER BY a.attnum))
                       FROM pg_attribute a
                       WHERE a.attrelid = c.oid
                         AND a.attnum > 0
                         AND NOT a.attisdropped
                   ), '') AS column_sig
            FROM rvbbit.row_group_variants rg
            JOIN rvbbit.layout_variant_status s
              ON s.table_oid = rg.table_oid AND s.layout = rg.layout
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
              AND {layout_predicate}
              AND s.status = 'ready'
            GROUP BY n.nspname, c.relname, c.oid, rg.layout, t.shadow_heap_retained, t.shadow_heap_dirty
        )
        SELECT coalesce(string_agg(
            nspname || '.' || relname || ':' || oid || ':' || coalesce(layout, 'scan') || ':' ||
            row_groups || ':' || row_group_rows || ':' || row_group_bytes || ':' || max_rg_id || ':' ||
            max_created_us || ':' || heap_bytes || ':' || shadow_heap_retained || ':' ||
            shadow_heap_dirty || ':' || deletes || ':' || path_sig || ':' || column_sig,
            E'\\n' ORDER BY nspname, relname, coalesce(layout, 'scan')
        ), 'empty') AS fingerprint
        FROM table_state
        "
    )
}

fn catalog_fingerprint_sql(
    layout_expr: &str,
    layout_group_expr: &str,
    rg_relation: &str,
    extra_join: &str,
) -> String {
    format!(
        "
        WITH table_state AS (
            SELECT n.nspname,
                   c.relname,
                   c.oid::bigint AS oid,
                   {layout_expr} AS layout,
                   count(rg.*)::bigint AS row_groups,
                   coalesce(sum(rg.n_rows), 0)::bigint AS row_group_rows,
                   coalesce(sum(rg.n_bytes), 0)::bigint AS row_group_bytes,
                   coalesce(max(rg.rg_id), -1)::bigint AS max_rg_id,
                   coalesce((extract(epoch FROM max(rg.created_at)) * 1000000)::bigint, 0)::bigint AS max_created_us,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*)::bigint FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid) AS deletes,
                   coalesce(md5(string_agg(rg.path || ':' || rg.n_rows || ':' || rg.n_bytes || ':' ||
                           coalesce((extract(epoch FROM rg.created_at) * 1000000)::bigint, 0), ',' ORDER BY rg.rg_id)), '') AS path_sig,
                   coalesce((
                       SELECT md5(string_agg(a.attname::text || ':' || a.atttypid::regtype::text, ',' ORDER BY a.attnum))
                       FROM pg_attribute a
                       WHERE a.attrelid = c.oid
                         AND a.attnum > 0
                         AND NOT a.attisdropped
                   ), '') AS column_sig
            FROM {rg_relation}
            {extra_join}
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
            GROUP BY n.nspname, c.relname, c.oid, {layout_group_expr}, t.shadow_heap_retained, t.shadow_heap_dirty
        )
        SELECT coalesce(string_agg(
            nspname || '.' || relname || ':' || oid || ':' || coalesce(layout, 'scan') || ':' ||
            row_groups || ':' || row_group_rows || ':' || row_group_bytes || ':' || max_rg_id || ':' ||
            max_created_us || ':' || heap_bytes || ':' || shadow_heap_retained || ':' ||
            shadow_heap_dirty || ':' || deletes || ':' || path_sig || ':' || column_sig,
            E'\\n' ORDER BY nspname, relname, coalesce(layout, 'scan')
        ), 'empty') AS fingerprint
        FROM table_state
        "
    )
}

fn rvbbit_row_group_catalog(
    pg: &mut Client,
    args: &Args,
) -> Result<BTreeMap<String, RvbbitDuckTable>> {
    let mut catalog = BTreeMap::new();
    let rows = pg.query(&catalog_sql_for_layout(&args.layout)?, &[])?;
    for row in rows {
        let schema: String = row.get(0);
        let relname: String = row.get(1);
        let layout: Option<String> = row.get(2);
        let paths: Vec<String> = row.get(3);
        let row_group_rows: i64 = row.get(4);
        let row_group_bytes: i64 = row.get(5);
        let heap_bytes: i64 = row.get(6);
        let shadow_heap_retained: bool = row.get(7);
        let shadow_heap_dirty: bool = row.get(8);
        let deletes: i64 = row.get(9);
        if deletes != 0 || (heap_bytes != 0 && !(shadow_heap_retained && !shadow_heap_dirty)) {
            continue;
        }
        let mut mapped = Vec::with_capacity(paths.len());
        for path in paths {
            let suffix = path
                .strip_prefix(&format!("{}/", args.pgdata_prefix.trim_end_matches('/')))
                .ok_or_else(|| anyhow!("path {path} is outside {}", args.pgdata_prefix))?;
            let visible = format!(
                "{}/{}",
                args.visible_pgdata_prefix.trim_end_matches('/'),
                suffix
            );
            if !Path::new(&visible).exists() {
                mapped.clear();
                break;
            }
            mapped.push(visible);
        }
        if mapped.is_empty() {
            continue;
        }
        catalog.insert(
            format!("{}.{}", schema, relname),
            RvbbitDuckTable {
                schema,
                relname,
                paths: mapped,
                columns: Vec::new(),
                layout,
                partition_cols: Vec::new(),
                row_group_rows,
                row_group_bytes,
            },
        );
    }

    let col_rows = pg.query(
        "
        SELECT n.nspname, c.relname, a.attname, a.atttypid::regtype::text
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_am am ON am.oid = c.relam
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE am.amname = 'rvbbit'
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY n.nspname, c.relname, a.attnum
        ",
        &[],
    )?;
    let mut unsupported = Vec::new();
    for row in col_rows {
        let schema: String = row.get(0);
        let relname: String = row.get(1);
        let attname: String = row.get(2);
        let typname: String = row.get(3);
        let key = format!("{}.{}", schema, relname);
        if !catalog.contains_key(&key) {
            continue;
        }
        if !supported_pg_type(&typname) {
            unsupported.push(key);
            continue;
        }
        catalog
            .get_mut(&key)
            .expect("key exists")
            .columns
            .push((attname, typname));
    }
    for key in unsupported {
        catalog.remove(&key);
    }
    attach_partition_columns(&mut catalog)?;
    Ok(catalog)
}

fn catalog_sql_for_layout(layout: &str) -> Result<String> {
    let trimmed = layout.trim();
    let lower = trimmed.to_ascii_lowercase();
    match lower.as_str() {
        "" | "scan" | "canonical" | "default" => Ok(
            "
            SELECT n.nspname,
                   c.relname,
                   NULL::text AS layout,
                   array_agg(rg.path ORDER BY rg.rg_id) AS paths,
                   sum(rg.n_rows)::bigint AS row_group_rows,
                   sum(rg.n_bytes)::bigint AS row_group_bytes,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid)::bigint AS deletes
            FROM rvbbit.row_groups rg
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
            GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
            "
            .to_string(),
        ),
        "hive" | "cluster" => {
            let prefix = format!("{lower}:%");
            Ok(variant_catalog_sql(&format!(
                "rg.layout LIKE '{}'",
                prefix.replace('\'', "''")
            )))
        }
        "vortex" | "vortex_scan" => Ok(variant_catalog_sql_exact("rg.layout = 'vortex_scan'")),
        _ if lower.starts_with("hive:") || lower.starts_with("cluster:") => {
            validate_layout_name(trimmed)?;
            Ok(variant_catalog_sql(&format!(
                "rg.layout = '{}'",
                trimmed.replace('\'', "''")
            )))
        }
        other => bail!("unsupported parquet layout: {other}"),
    }
}

fn variant_catalog_sql(layout_predicate: &str) -> String {
    format!(
        "
        WITH chosen_layout AS (
            SELECT rg.table_oid, min(rg.layout) AS layout
            FROM rvbbit.row_group_variants rg
            JOIN rvbbit.layout_variant_status s
              ON s.table_oid = rg.table_oid AND s.layout = rg.layout
            WHERE {layout_predicate}
              AND s.status = 'ready'
            GROUP BY rg.table_oid
        ),
        variant_rows AS (
            SELECT n.nspname,
                   c.relname,
                   cl.layout,
                   array_agg(rg.path ORDER BY rg.rg_id) AS paths,
                   sum(rg.n_rows)::bigint AS row_group_rows,
                   sum(rg.n_bytes)::bigint AS row_group_bytes,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid)::bigint AS deletes
            FROM rvbbit.row_group_variants rg
            JOIN chosen_layout cl ON cl.table_oid = rg.table_oid AND cl.layout = rg.layout
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
            GROUP BY n.nspname, c.oid, c.relname, cl.layout, t.shadow_heap_retained, t.shadow_heap_dirty
        ),
        canonical_rows AS (
            SELECT n.nspname,
                   c.relname,
                   NULL::text AS layout,
                   array_agg(rg.path ORDER BY rg.rg_id) AS paths,
                   sum(rg.n_rows)::bigint AS row_group_rows,
                   sum(rg.n_bytes)::bigint AS row_group_bytes,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid)::bigint AS deletes
            FROM rvbbit.row_groups rg
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_am am ON am.oid = c.relam
            LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
            WHERE am.amname = 'rvbbit'
              AND NOT EXISTS (
                  SELECT 1
                  FROM chosen_layout cl
                  WHERE cl.table_oid = rg.table_oid
              )
            GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
        )
        SELECT * FROM variant_rows
        UNION ALL
        SELECT * FROM canonical_rows
        "
    )
}

fn variant_catalog_sql_exact(layout_predicate: &str) -> String {
    format!(
        "
        SELECT n.nspname,
               c.relname,
               rg.layout,
               array_agg(rg.path ORDER BY rg.rg_id) AS paths,
               sum(rg.n_rows)::bigint AS row_group_rows,
               sum(rg.n_bytes)::bigint AS row_group_bytes,
               pg_relation_size(c.oid)::bigint AS heap_bytes,
               coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
               coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
               (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid)::bigint AS deletes
        FROM rvbbit.row_group_variants rg
        JOIN rvbbit.layout_variant_status s
          ON s.table_oid = rg.table_oid AND s.layout = rg.layout
        JOIN pg_class c ON c.oid = rg.table_oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_am am ON am.oid = c.relam
        LEFT JOIN rvbbit.tables t ON t.table_oid = c.oid
        WHERE am.amname = 'rvbbit'
          AND {layout_predicate}
          AND s.status = 'ready'
        GROUP BY n.nspname, c.oid, c.relname, rg.layout, t.shadow_heap_retained, t.shadow_heap_dirty
        "
    )
}

fn validate_layout_name(layout: &str) -> Result<()> {
    if layout
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, ':' | '_' | '-'))
    {
        Ok(())
    } else {
        bail!("invalid parquet layout name: {layout}")
    }
}

fn attach_partition_columns(catalog: &mut BTreeMap<String, RvbbitDuckTable>) -> Result<()> {
    for table in catalog.values_mut() {
        let Some(layout) = table.layout.clone() else {
            continue;
        };
        let Some(col) = layout.strip_prefix("hive:") else {
            continue;
        };
        let Some((_, typ)) = table.columns.iter().find(|(name, _)| name == col) else {
            bail!(
                "hive layout {} for {}.{} references unknown partition column {}",
                layout,
                table.schema,
                table.relname,
                col
            );
        };
        table.partition_cols.push((col.to_string(), typ.clone()));
    }
    Ok(())
}

fn prewarm_parquet_metadata(
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    cache: &mut ParquetFooterCache,
) -> Result<FooterCacheStats> {
    let start = Instant::now();
    let mut stats = FooterCacheStats::default();
    if !parquet_prewarm_enabled() {
        return Ok(stats);
    }

    for table in catalog.values().filter(|table| !table_uses_vortex(table)) {
        for path in &table.paths {
            stats.files += 1;
            let (hit, entry) = cache.ensure(path)?;
            if hit {
                stats.hits += 1;
            } else {
                stats.misses += 1;
            }
            stats.rows += entry.rows;
            stats.row_groups += entry.row_groups;
            stats.columns += entry.columns;
            stats.schema_bytes += entry.schema_signature.len();
        }
    }
    stats.elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
    Ok(stats)
}

impl ParquetFooterCache {
    fn snapshot_stats(&self, catalog: &BTreeMap<String, RvbbitDuckTable>) -> FooterCacheStats {
        let mut stats = FooterCacheStats::default();
        if !parquet_prewarm_enabled() {
            return stats;
        }

        for table in catalog.values().filter(|table| !table_uses_vortex(table)) {
            for path in &table.paths {
                stats.files += 1;
                if let Some(entry) = self.entries.get(path) {
                    stats.hits += 1;
                    stats.rows += entry.rows;
                    stats.row_groups += entry.row_groups;
                    stats.columns += entry.columns;
                    stats.schema_bytes += entry.schema_signature.len();
                } else {
                    stats.misses += 1;
                }
            }
        }
        stats
    }

    fn ensure(&mut self, path: &str) -> Result<(bool, ParquetFooterEntry)> {
        let identity = file_identity(path)?;
        if let Some(entry) = self.entries.get(path) {
            if entry.identity == identity {
                return Ok((true, entry.clone()));
            }
        }
        let entry = read_parquet_footer(path, identity)?;
        self.entries.insert(path.to_string(), entry.clone());
        Ok((false, entry))
    }
}

fn file_identity(path: &str) -> Result<FileIdentity> {
    let metadata = fs::metadata(path).with_context(|| format!("stat parquet file {path}"))?;
    let modified_nanos = metadata
        .modified()
        .ok()
        .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    Ok(FileIdentity {
        len: metadata.len(),
        modified_nanos,
    })
}

fn read_parquet_footer(path: &str, identity: FileIdentity) -> Result<ParquetFooterEntry> {
    let file = File::open(path).with_context(|| format!("opening parquet footer {path}"))?;
    let builder =
        ParquetRecordBatchReaderBuilder::try_new(file).context("reading parquet footer")?;
    let schema = builder.schema();
    let metadata = builder.metadata();
    let schema_signature = schema
        .fields()
        .iter()
        .map(|field| {
            format!(
                "{}:{:?}:{}",
                field.name(),
                field.data_type(),
                field.is_nullable()
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    Ok(ParquetFooterEntry {
        identity,
        rows: metadata.file_metadata().num_rows(),
        row_groups: metadata.num_row_groups(),
        columns: schema.fields().len(),
        schema_signature,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum SqlTok {
    Ident(String),
    Dot,
    Comma,
    LParen,
    RParen,
}

fn ensure_query_tables_authoritative_local(
    sql: &str,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
) -> bool {
    let lowered = sql_stringless(sql).to_ascii_lowercase();
    if lowered.trim_start().starts_with("with") || lowered.contains(" natural ") {
        return false;
    }

    let tokens = tokenize_sql_for_refs(&sql_stringless(sql));
    let mut refs = 0usize;
    let mut in_from_list = false;
    let mut expect_relation = false;
    let mut depth = 0usize;
    let mut idx = 0usize;
    while idx < tokens.len() {
        match &tokens[idx] {
            SqlTok::LParen => {
                depth += 1;
                if expect_relation {
                    return false;
                }
                idx += 1;
            }
            SqlTok::RParen => {
                depth = depth.saturating_sub(1);
                idx += 1;
            }
            SqlTok::Ident(word) if depth > 0 && word == "select" => return false,
            _ if depth > 0 => {
                idx += 1;
            }
            SqlTok::Ident(word) if is_from_clause_terminator(word) => {
                in_from_list = false;
                expect_relation = false;
                idx += 1;
            }
            SqlTok::Ident(word) if word == "from" || word == "join" => {
                in_from_list = true;
                expect_relation = true;
                idx += 1;
            }
            SqlTok::Comma if in_from_list => {
                expect_relation = true;
                idx += 1;
            }
            SqlTok::Ident(word) if expect_relation && (word == "only" || word == "lateral") => {
                idx += 1;
            }
            _ if expect_relation => {
                let Some((schema, relname, consumed)) = read_relation_name(&tokens[idx..]) else {
                    return false;
                };
                if !catalog_contains_relation(catalog, schema.as_deref(), &relname) {
                    return false;
                }
                refs += 1;
                expect_relation = false;
                idx += consumed;
            }
            _ => {
                idx += 1;
            }
        }
    }
    refs > 0
}

fn tokenize_sql_for_refs(sql: &str) -> Vec<SqlTok> {
    let mut tokens = Vec::new();
    let chars: Vec<char> = sql.chars().collect();
    let mut idx = 0usize;
    while idx < chars.len() {
        let ch = chars[idx];
        if ch.is_whitespace() {
            idx += 1;
            continue;
        }
        match ch {
            '"' => {
                let mut ident = String::new();
                idx += 1;
                while idx < chars.len() {
                    if chars[idx] == '"' {
                        if chars.get(idx + 1).copied() == Some('"') {
                            ident.push('"');
                            idx += 2;
                            continue;
                        }
                        idx += 1;
                        break;
                    }
                    ident.push(chars[idx]);
                    idx += 1;
                }
                if !ident.is_empty() {
                    tokens.push(SqlTok::Ident(ident));
                }
            }
            '.' => {
                tokens.push(SqlTok::Dot);
                idx += 1;
            }
            ',' => {
                tokens.push(SqlTok::Comma);
                idx += 1;
            }
            '(' => {
                tokens.push(SqlTok::LParen);
                idx += 1;
            }
            ')' => {
                tokens.push(SqlTok::RParen);
                idx += 1;
            }
            _ if is_identifier_start(ch) => {
                let start = idx;
                idx += 1;
                while idx < chars.len() && is_identifier_char(chars[idx]) {
                    idx += 1;
                }
                let ident = chars[start..idx].iter().collect::<String>();
                tokens.push(SqlTok::Ident(ident.to_ascii_lowercase()));
            }
            _ => {
                idx += 1;
            }
        }
    }
    tokens
}

fn is_identifier_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || ch == '_'
}

fn read_relation_name(tokens: &[SqlTok]) -> Option<(Option<String>, String, usize)> {
    let SqlTok::Ident(first) = tokens.first()? else {
        return None;
    };
    if matches!(tokens.get(1), Some(SqlTok::Dot)) {
        let SqlTok::Ident(second) = tokens.get(2)? else {
            return None;
        };
        if matches!(tokens.get(3), Some(SqlTok::Dot)) {
            return None;
        }
        return Some((Some(first.clone()), second.clone(), 3));
    }
    Some((None, first.clone(), 1))
}

fn catalog_contains_relation(
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    schema: Option<&str>,
    relname: &str,
) -> bool {
    if let Some(schema) = schema {
        return catalog.contains_key(&format!("{schema}.{relname}"));
    }
    catalog
        .values()
        .filter(|table| table.relname == relname)
        .count()
        == 1
}

fn is_from_clause_terminator(word: &str) -> bool {
    matches!(
        word,
        "where"
            | "group"
            | "having"
            | "order"
            | "limit"
            | "offset"
            | "fetch"
            | "union"
            | "except"
            | "intersect"
            | "window"
            | "qualify"
    )
}

fn ensure_query_tables_authoritative(
    pg: &mut Client,
    sql: &str,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
) -> Result<()> {
    let row = pg
        .query_one("SELECT rvbbit.route_explain($1)::text", &[&sql])
        .context("checking Rvbbit route safety")?;
    let route_json: String = row.get(0);
    let route_doc: Value =
        serde_json::from_str(&route_json).context("parsing Rvbbit route safety JSON")?;
    if !route_doc
        .get("safe_select")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        let reason = route_doc
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("not a safe read-only SELECT");
        bail!("{reason}");
    }

    let tables = route_doc
        .get("rvbbit_tables")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("Rvbbit route safety response did not include table metrics"))?;
    if tables.is_empty() {
        bail!("query does not reference Rvbbit tables");
    }

    for table in tables {
        let schema = table
            .get("schema")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Rvbbit table metric is missing schema"))?;
        let relname = table
            .get("table")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Rvbbit table metric is missing table name"))?;
        let key = format!("{schema}.{relname}");
        let row_groups = table.get("row_groups").and_then(Value::as_i64).unwrap_or(0);
        let heap_bytes = table.get("heap_bytes").and_then(Value::as_i64).unwrap_or(0);
        let shadow_heap_retained = table
            .get("shadow_heap_retained")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let shadow_heap_dirty = table
            .get("shadow_heap_dirty")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let delete_count = table
            .get("delete_count")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        if row_groups <= 0 {
            bail!("referenced Rvbbit table {key} has no compacted parquet row groups");
        }
        if heap_bytes > 0 && !(shadow_heap_retained && !shadow_heap_dirty) {
            bail!("referenced Rvbbit table {key} has a {heap_bytes} byte heap tail");
        }
        if delete_count > 0 {
            bail!("referenced Rvbbit table {key} has {delete_count} pending delete row(s)");
        }
        if !catalog.contains_key(&key) {
            bail!("referenced Rvbbit table {key} has no authoritative visible parquet files");
        }
    }
    Ok(())
}

fn supported_pg_type(typname: &str) -> bool {
    matches!(
        typname,
        "boolean"
            | "smallint"
            | "integer"
            | "bigint"
            | "real"
            | "double precision"
            | "numeric"
            | "text"
            | "character"
            | "character varying"
            | "date"
            | "timestamp without time zone"
            | "timestamp with time zone"
    )
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DuckSourceFormat {
    Parquet,
    Vortex,
}

fn table_source_format(table: &RvbbitDuckTable) -> DuckSourceFormat {
    match table
        .layout
        .as_deref()
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("vortex") | Some("vortex_scan") => DuckSourceFormat::Vortex,
        _ => DuckSourceFormat::Parquet,
    }
}

fn table_uses_vortex(table: &RvbbitDuckTable) -> bool {
    table_source_format(table) == DuckSourceFormat::Vortex
}

fn ensure_duck_vortex(con: &Connection) -> Result<()> {
    if con.execute_batch("LOAD vortex").is_ok() {
        return Ok(());
    }
    con.execute_batch("INSTALL vortex")
        .context("installing DuckDB vortex extension")?;
    con.execute_batch("LOAD vortex")
        .context("loading DuckDB vortex extension")?;
    Ok(())
}

fn create_duck_views(con: &Connection, catalog: &BTreeMap<String, RvbbitDuckTable>) -> Result<()> {
    if catalog.values().any(table_uses_vortex) {
        ensure_duck_vortex(con)?;
    }
    let mut rel_counts = BTreeMap::<String, usize>::new();
    for table in catalog.values() {
        *rel_counts.entry(table.relname.clone()).or_default() += 1;
    }
    for table in catalog.values() {
        let paths = table
            .paths
            .iter()
            .map(|path| quote_sql_string(path))
            .collect::<Vec<_>>()
            .join(", ");
        let source_format = table_source_format(table);
        let source = match source_format {
            DuckSourceFormat::Vortex => format!("read_vortex([{paths}])"),
            DuckSourceFormat::Parquet if table.partition_cols.is_empty() => {
                format!("read_parquet([{paths}], union_by_name=true)")
            }
            DuckSourceFormat::Parquet => {
                format!("read_parquet([{paths}], union_by_name=true, hive_partitioning=true)")
            }
        };
        let select_list = if table.columns.is_empty() {
            "*".to_string()
        } else {
            table
                .columns
                .iter()
                .map(|(col, typ)| duck_select_expr(col, typ, source_format))
                .collect::<Vec<_>>()
                .join(", ")
        };
        con.execute_batch(&format!(
            "CREATE SCHEMA IF NOT EXISTS {}",
            quote_ident(&table.schema)
        ))?;
        con.execute_batch(&format!(
            "CREATE VIEW {} AS SELECT {select_list} FROM {source}",
            quote_qualified(&table.schema, &table.relname)
        ))?;
        if rel_counts.get(&table.relname).copied().unwrap_or(0) == 1 {
            con.execute_batch(&format!(
                "CREATE VIEW {} AS SELECT * FROM {}",
                quote_ident(&table.relname),
                quote_qualified(&table.schema, &table.relname)
            ))?;
        }
    }
    Ok(())
}

fn catalog_signature(catalog: &BTreeMap<String, RvbbitDuckTable>) -> String {
    let mut out = String::new();
    for (key, table) in catalog {
        out.push_str(key);
        out.push('|');
        out.push_str(&table.row_group_rows.to_string());
        out.push('|');
        out.push_str(&table.row_group_bytes.to_string());
        out.push('|');
        if let Some(layout) = &table.layout {
            out.push_str(layout);
        }
        out.push('|');
        out.push_str(&table.paths.join(","));
        out.push('|');
        for (name, typ) in &table.columns {
            out.push_str(name);
            out.push(':');
            out.push_str(typ);
            out.push(',');
        }
        out.push('\n');
    }
    out
}

fn quote_ident(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

fn quote_qualified(schema: &str, relname: &str) -> String {
    format!("{}.{}", quote_ident(schema), quote_ident(relname))
}

fn quote_sql_string(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn duck_select_expr(col: &str, typname: &str, source_format: DuckSourceFormat) -> String {
    let ident = quote_ident(col);
    if typname == "date" {
        format!("(DATE '1970-01-01' + CAST({ident} AS INTEGER)) AS {ident}")
    } else if source_format == DuckSourceFormat::Vortex && typname == "timestamp without time zone"
    {
        format!("make_timestamp(CAST({ident} AS BIGINT)) AS {ident}")
    } else if source_format == DuckSourceFormat::Vortex && typname == "timestamp with time zone" {
        format!("make_timestamptz(CAST({ident} AS BIGINT)) AS {ident}")
    } else {
        ident
    }
}

struct QueryRows {
    columns: Vec<String>,
    rows: Vec<Vec<Value>>,
    row_count: usize,
    result_format: ResultFormat,
    arrow_ipc_path: Option<String>,
    arrow_ipc_bytes: Option<u64>,
}

impl Default for QueryRows {
    fn default() -> Self {
        Self {
            columns: Vec::new(),
            rows: Vec::new(),
            row_count: 0,
            result_format: ResultFormat::Json,
            arrow_ipc_path: None,
            arrow_ipc_bytes: None,
        }
    }
}

fn execute_duck_query(con: &Connection, sql: &str, max_rows: usize) -> Result<QueryRows> {
    let mut stmt = con.prepare(sql)?;
    let mut rows = stmt.query([])?;
    let stmt_ref = rows
        .as_ref()
        .ok_or_else(|| anyhow!("DuckDB query did not return a statement"))?;
    let columns = stmt_ref.column_names();
    let column_count = stmt_ref.column_count();
    let mut out = QueryRows {
        columns,
        rows: Vec::new(),
        row_count: 0,
        result_format: ResultFormat::Json,
        arrow_ipc_path: None,
        arrow_ipc_bytes: None,
    };
    while let Some(row) = rows.next()? {
        if out.rows.len() < max_rows {
            let mut values = Vec::with_capacity(column_count);
            for idx in 0..column_count {
                values.push(value_ref_to_json(row.get_ref(idx)?));
            }
            out.rows.push(values);
        }
        out.row_count += 1;
    }
    Ok(out)
}

fn execute_duck_query_result(
    con: &Connection,
    sql: &str,
    max_rows: usize,
    result_format: ResultFormat,
) -> Result<QueryRows> {
    match result_format {
        ResultFormat::Json => execute_duck_query(con, sql, max_rows),
        ResultFormat::ArrowIpcFile => execute_duck_query_arrow_ipc(con, sql, max_rows),
    }
}

fn execute_duck_query_arrow_ipc(con: &Connection, sql: &str, max_rows: usize) -> Result<QueryRows> {
    let mut stmt = con.prepare(sql)?;
    let mut arrow = stmt.query_arrow([])?;
    let schema = arrow.get_schema();
    let columns = schema_column_names(&schema);
    let mut row_count = 0usize;
    let mut remaining = max_rows;
    let mut capped = Vec::new();
    for batch in &mut arrow {
        row_count += batch.num_rows();
        if remaining == 0 {
            continue;
        }
        let len = batch.num_rows().min(remaining);
        if len > 0 {
            capped.push(batch.slice(0, len));
            remaining -= len;
        }
    }
    if capped.is_empty() {
        return Ok(QueryRows {
            columns,
            rows: Vec::new(),
            row_count,
            result_format: ResultFormat::Json,
            arrow_ipc_path: None,
            arrow_ipc_bytes: None,
        });
    }
    let (path, bytes) = write_arrow_ipc_file(schema, &capped)?;
    Ok(QueryRows {
        columns,
        rows: Vec::new(),
        row_count,
        result_format: ResultFormat::ArrowIpcFile,
        arrow_ipc_path: Some(path),
        arrow_ipc_bytes: Some(bytes),
    })
}

fn value_ref_to_json(value: ValueRef<'_>) -> Value {
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Boolean(v) => json!(v),
        ValueRef::TinyInt(v) => json!(v),
        ValueRef::SmallInt(v) => json!(v),
        ValueRef::Int(v) => json!(v),
        ValueRef::BigInt(v) => json!(v),
        ValueRef::HugeInt(v) => json!(v.to_string()),
        ValueRef::UTinyInt(v) => json!(v),
        ValueRef::USmallInt(v) => json!(v),
        ValueRef::UInt(v) => json!(v),
        ValueRef::UBigInt(v) => json!(v),
        ValueRef::Float(v) => json!(v),
        ValueRef::Double(v) => json!(v),
        ValueRef::Decimal(v) => json!(v.to_string()),
        ValueRef::Timestamp(unit, v) => json!(format_timestamp_micros(unit.to_micros(v))),
        ValueRef::Text(v) => json!(String::from_utf8_lossy(v).to_string()),
        ValueRef::Blob(v) => json!(hex_bytes(v)),
        ValueRef::Date32(v) => json!(format_date32(v)),
        ValueRef::Time64(unit, v) => json!(format_time_micros(unit.to_micros(v))),
        ValueRef::Interval {
            months,
            days,
            nanos,
        } => json!({"months": months, "days": days, "nanos": nanos}),
        other => json!(format!("{other:?}")),
    }
}

fn format_date32(days_since_epoch: i32) -> String {
    let (year, month, day) = civil_from_days(days_since_epoch as i64);
    format!("{year:04}-{month:02}-{day:02}")
}

fn format_timestamp_micros(micros_since_epoch: i64) -> String {
    let days = micros_since_epoch.div_euclid(86_400_000_000);
    let micros_of_day = micros_since_epoch.rem_euclid(86_400_000_000);
    let (year, month, day) = civil_from_days(days);
    let hour = micros_of_day / 3_600_000_000;
    let minute = (micros_of_day / 60_000_000) % 60;
    let second = (micros_of_day / 1_000_000) % 60;
    let micros = micros_of_day % 1_000_000;
    if micros == 0 {
        format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}")
    } else {
        format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{micros:06}")
    }
}

fn format_time_micros(micros: i64) -> String {
    let micros = micros.rem_euclid(86_400_000_000);
    let hour = micros / 3_600_000_000;
    let minute = (micros / 60_000_000) % 60;
    let second = (micros / 1_000_000) % 60;
    let micros = micros % 1_000_000;
    if micros == 0 {
        format!("{hour:02}:{minute:02}:{second:02}")
    } else {
        format!("{hour:02}:{minute:02}:{second:02}.{micros:06}")
    }
}

fn civil_from_days(days_since_epoch: i64) -> (i64, u32, u32) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year, month as u32, day as u32)
}

fn hex_bytes(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn table_summaries(catalog: &BTreeMap<String, RvbbitDuckTable>) -> Vec<TableSummary> {
    catalog
        .values()
        .map(|table| TableSummary {
            schema: table.schema.clone(),
            relname: table.relname.clone(),
            files: table.paths.len(),
            rows: table.row_group_rows,
            bytes: table.row_group_bytes,
            layout: table.layout.clone(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_catalog() -> BTreeMap<String, RvbbitDuckTable> {
        let mut catalog = BTreeMap::new();
        for (schema, relname) in [("public", "hits"), ("public", "lineitem")] {
            catalog.insert(
                format!("{schema}.{relname}"),
                RvbbitDuckTable {
                    schema: schema.to_string(),
                    relname: relname.to_string(),
                    paths: Vec::new(),
                    columns: Vec::new(),
                    layout: None,
                    partition_cols: Vec::new(),
                    row_group_rows: 0,
                    row_group_bytes: 0,
                },
            );
        }
        catalog
    }

    #[test]
    fn formats_date32_for_postgres_json_recordset() {
        assert_eq!(format_date32(0), "1970-01-01");
        assert_eq!(format_date32(15_901), "2013-07-15");
    }

    #[test]
    fn formats_timestamp_micros_for_postgres_json_recordset() {
        assert_eq!(
            format_timestamp_micros(1_373_892_000_000_000),
            "2013-07-15 12:40:00"
        );
        assert_eq!(
            format_timestamp_micros(1_373_892_000_000_123),
            "2013-07-15 12:40:00.000123"
        );
    }

    #[test]
    fn local_route_safety_accepts_simple_rvbbit_refs() {
        let catalog = test_catalog();
        assert!(ensure_query_tables_authoritative_local(
            r#"SELECT "UserID", count(*) FROM hits GROUP BY "UserID""#,
            &catalog
        ));
        assert!(ensure_query_tables_authoritative_local(
            r#"SELECT extract(minute FROM "EventTime") AS m, count(*) FROM hits GROUP BY m"#,
            &catalog
        ));
        assert!(ensure_query_tables_authoritative_local(
            "SELECT * FROM public.hits h JOIN lineitem l ON h.id = l.id",
            &catalog
        ));
    }

    #[test]
    fn local_route_safety_falls_back_for_complex_or_unknown_refs() {
        let catalog = test_catalog();
        assert!(!ensure_query_tables_authoritative_local(
            "WITH x AS (SELECT * FROM hits) SELECT * FROM x",
            &catalog
        ));
        assert!(!ensure_query_tables_authoritative_local(
            "SELECT * FROM hits JOIN heap_table h ON true",
            &catalog
        ));
        assert!(!ensure_query_tables_authoritative_local(
            "SELECT * FROM (SELECT * FROM hits) h",
            &catalog
        ));
        assert!(!ensure_query_tables_authoritative_local(
            "SELECT * FROM hits WHERE EXISTS (SELECT 1 FROM heap_table h)",
            &catalog
        ));
    }
}
