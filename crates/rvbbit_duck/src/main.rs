use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, HashMap};
use std::env;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::hash::{Hash, Hasher};
use std::io::{self, BufRead, BufReader, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{self, Child, Command, Output, Stdio};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, bail, Context, Result};
use arrow_flight::decode::FlightRecordBatchStream;
use arrow_flight::error::FlightError;
use arrow_flight::flight_descriptor::DescriptorType;
use arrow_flight::flight_service_client::FlightServiceClient;
use arrow_flight::sql::{
    CommandGetTables, CommandStatementSubstraitPlan, ProstMessageExt, SubstraitPlan,
};
use arrow_flight::{FlightData, FlightDescriptor};
use datafusion::arrow::array::{
    cast::{as_boolean_array, as_primitive_array, as_string_array},
    Array, ArrayRef, BinaryArray, Int32Array, Int64Array, StringArray,
};
use datafusion::arrow::datatypes::{
    DataType, Date32Type, Field, Float32Type, Float64Type, Int16Type, Int32Type, Int64Type,
    Int8Type, Schema, SchemaRef, TimeUnit, TimestampMicrosecondType, TimestampMillisecondType,
    TimestampNanosecondType, TimestampSecondType, UInt16Type, UInt32Type, UInt64Type, UInt8Type,
};
use datafusion::arrow::ipc::writer::StreamWriter;
use datafusion::arrow::record_batch::RecordBatch;
use datafusion::arrow::util::display::array_value_to_string;
use datafusion::catalog::{Session, TableProvider};
use datafusion::common::stats::Precision;
use datafusion::common::Statistics;
use datafusion::physical_plan::empty::EmptyExec;
use datafusion::physical_plan::ExecutionPlan;
use datafusion::prelude::{Expr, ParquetReadOptions, SessionConfig, SessionContext};
use datafusion::sql::sqlparser::dialect::PostgreSqlDialect;
use datafusion::sql::sqlparser::parser::Parser;
use duckdb::types::ValueRef;
use duckdb::Connection;
use futures::TryStreamExt;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use postgres::{Client, NoTls};
use prost::Message;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::runtime::{Builder as RuntimeBuilder, Runtime};
use tonic::transport::Channel;

const DEFAULT_DSN: &str = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench";
const DEFAULT_DUCK_BIN: &str = "/usr/local/bin/rvbbit-duck";
const DEFAULT_PGDATA_PREFIX: &str = "/var/lib/postgresql";
const DEFAULT_VISIBLE_PGDATA_PREFIX: &str = "/rvbbit_pgdata";
const DEFAULT_GQE_SERVER_URL: &str = "http://127.0.0.1:50051";
const DEFAULT_GQE_CLI: &str = "/opt/gqe/rust/target/release/gqe-cli";
const DEFAULT_GQE_NODE_MANAGER: &str = "/opt/gqe/build/src/node_manager/gqe_node_manager";
const DEFAULT_GQE_TASK_MANAGER: &str = "/opt/gqe/build/src/task_manager/gqe_task_manager";
const DEFAULT_GQE_WORK_DIR: &str = "/tmp/rvbbit-gqe";
const GQE_CATALOG_VERSION: &str = "gqe-adapter-v4-date-year-sidecar";
const GQE_ADAPTER_VERSION: &str = GQE_CATALOG_VERSION;
const GQE_FLIGHT_SUBSTRAIT_VERSION: &str = "0.63.0";
const GQE_FLIGHT_MAX_DECODING_MESSAGE_SIZE: usize = 512 * 1024 * 1024;
const DEFAULT_TELEMETRY_QUEUE_CAPACITY: usize = 8192;
const DEFAULT_TELEMETRY_BATCH_SIZE: usize = 64;
const DEFAULT_TELEMETRY_FLUSH_MS: u64 = 250;
const DEFAULT_TELEMETRY_HEARTBEAT_MS: u64 = 5000;
const DEFAULT_BROKER_QUEUE_CAPACITY: usize = 1024;
const DEFAULT_MAX_REQUEST_BYTES: usize = 16 * 1024 * 1024;
const DEFAULT_SOCKET_IO_TIMEOUT_S: u64 = 30;

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
    // Calling Postgres session's search_path (CSV), so unqualified table names
    // resolve to the same schema PG would pick when the same relname exists in
    // more than one schema (e.g. public.customer vs tpcds.customer).
    search_path: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Engine {
    Duck,
    DataFusion,
    GpuGqe,
}

impl Engine {
    fn as_str(self) -> &'static str {
        match self {
            Engine::Duck => "duck",
            Engine::DataFusion => "datafusion",
            Engine::GpuGqe => "gpu_gqe",
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
    #[serde(skip_serializing_if = "Option::is_none")]
    gqe: Option<GqeQueryStats>,
}

#[derive(Debug, Clone, Default, Serialize)]
struct GqeQueryStats {
    runs: usize,
    client_mode: String,
    median_total_ms: f64,
    median_server_ready_ms: f64,
    median_rewrite_ms: f64,
    median_cli_ms: f64,
    median_flight_ms: f64,
    median_result_read_ms: f64,
    median_materialize_ms: f64,
    median_cleanup_ms: f64,
    result_files: usize,
    result_bytes: u64,
    result_batches: usize,
    result_rows: usize,
}

#[derive(Debug, Clone, Default)]
struct GqeRunStats {
    client_mode: &'static str,
    total_ms: f64,
    server_ready_ms: f64,
    rewrite_ms: f64,
    cli_ms: f64,
    flight_ms: f64,
    result_read_ms: f64,
    materialize_ms: f64,
    cleanup_ms: f64,
    result_files: usize,
    result_bytes: u64,
    result_batches: usize,
    result_rows: usize,
}

#[derive(Debug, Clone, Default)]
struct GqeResultReadStats {
    files: usize,
    bytes: u64,
    batches: usize,
    rows: usize,
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
    if env::args().skip(1).any(|arg| arg == "--rvbbit-probe") {
        println!("{}", serde_json::to_string(&gqe_probe_status()).unwrap());
        return;
    }

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
    ensure_query_tables_authoritative(&mut pg, sql, &catalog, args.search_path.as_deref())?;
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
        Engine::GpuGqe => run_gqe_once(args, sql, catalog, cache),
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
    apply_duck_search_path(&con, &catalog, args.search_path.as_deref())?;
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

fn run_gqe_once(
    args: &Args,
    sql: &str,
    catalog: BTreeMap<String, RvbbitDuckTable>,
    mut cache: CacheSummary,
) -> Result<QuerySummary> {
    prepare_gqe_catalog(&catalog)?;
    if args.explain_only {
        return Ok(empty_query_summary(args.timeout_s, &catalog, cache));
    }

    let mut elapsed = Vec::with_capacity(args.repeat.max(1));
    let mut last = QueryRows::default();
    let mut gqe_runs = Vec::with_capacity(args.repeat.max(1));
    for _ in 0..args.repeat.max(1) {
        cleanup_query_rows(&mut last);
        let start = Instant::now();
        let (rows, stats) = execute_gqe_query_result(
            sql,
            &catalog,
            args.max_rows,
            args.result_format,
            args.timeout_s,
        )?;
        last = rows;
        gqe_runs.push(stats);
        elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    elapsed.sort_by(|a, b| a.total_cmp(b));
    cache.gqe = GqeQueryStats::from_runs(&gqe_runs);
    Ok(query_summary_from_rows(
        elapsed[elapsed.len() / 2],
        args.repeat.max(1),
        args.timeout_s,
        last,
        &catalog,
        cache,
    ))
}

fn prepare_gqe_catalog(catalog: &BTreeMap<String, RvbbitDuckTable>) -> Result<()> {
    ensure_gqe_server_ready()?;
    let root = gqe_catalog_root(catalog)?;
    let rel_counts = relation_counts(catalog);
    let mut script = String::new();
    for table in catalog.values() {
        if table_uses_vortex(table) {
            bail!(
                "GPU/GQE bridge can only expose parquet-backed tables; {}.{} uses {:?}",
                table.schema,
                table.relname,
                table.layout
            );
        }
        let location = prepare_gqe_table_dir(&root, table)?;
        script.push_str(&format!(
            "CREATE OR REPLACE EXTERNAL TABLE {} (\n{}\n) STORED AS PARQUET LOCATION {};\n",
            gqe_table_ddl_name(&gqe_table_name(table, &rel_counts)),
            gqe_table_columns(table).join(",\n"),
            gqe_quote_string(&location)
        ));
    }
    if let Err(err) = run_gqe_cli_sql(&script, None, 60) {
        if !gqe_table_already_exists_error(&err) {
            return Err(err);
        }
    }
    Ok(())
}

fn gqe_table_already_exists_error(err: &anyhow::Error) -> bool {
    err.to_string().contains("already exists")
}

fn execute_gqe_query_result(
    sql: &str,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    max_rows: usize,
    result_format: ResultFormat,
    timeout_s: u64,
) -> Result<(QueryRows, GqeRunStats)> {
    let total_start = Instant::now();
    let mut stats = GqeRunStats::default();
    stats.client_mode = "cli";

    let server_ready_start = Instant::now();
    ensure_gqe_server_ready()?;
    stats.server_ready_ms = server_ready_start.elapsed().as_secs_f64() * 1000.0;

    let rewrite_start = Instant::now();
    if let Some(reason) = gqe_shape_gate_reason(sql, catalog) {
        bail!("{reason}");
    }
    let gqe_sql = rewrite_gqe_sql(sql, catalog);
    if let Some(reason) = gqe_unsupported_temporal_reason(&gqe_sql, catalog) {
        bail!("{reason}");
    }
    if let Some(reason) = gqe_unsupported_function_reason(&gqe_sql) {
        bail!("{reason}");
    }
    if let Some(reason) = gqe_unsupported_grouping_reason(&gqe_sql) {
        bail!("{reason}");
    }
    if let Some(reason) = gqe_lossy_type_reason(&gqe_sql, catalog) {
        bail!("{reason}");
    }
    stats.rewrite_ms = rewrite_start.elapsed().as_secs_f64() * 1000.0;

    let result_path = gqe_result_path()?;
    let cli_start = Instant::now();
    run_gqe_cli_sql(&gqe_sql, Some(&result_path), timeout_s)?;
    // The CLI exited 0, but it must have written the result at `result_path`
    // (we passed --parquet). If nothing is there, this is a failed run, NOT an
    // authoritative empty answer — returning empty would silently drop every
    // row for a query that actually matches. Bail so fail-open falls back to an
    // exact engine.
    if !Path::new(&result_path).exists() {
        bail!(
            "GPU/GQE reported success but wrote no result file at {result_path}; \
             refusing to treat a missing result as an empty answer"
        );
    }
    stats.cli_ms = cli_start.elapsed().as_secs_f64() * 1000.0;

    let read_start = Instant::now();
    let (batches, read_stats) = read_parquet_batches_from_path(Path::new(&result_path))?;
    stats.result_read_ms = read_start.elapsed().as_secs_f64() * 1000.0;
    stats.result_files = read_stats.files;
    stats.result_bytes = read_stats.bytes;
    stats.result_batches = read_stats.batches;
    stats.result_rows = read_stats.rows;

    let materialize_start = Instant::now();
    let result = match result_format {
        ResultFormat::Json => record_batches_to_query_rows(&batches, max_rows),
        ResultFormat::ArrowIpcFile => record_batches_to_arrow_ipc_rows(&batches, max_rows),
    }?;
    if result_format == ResultFormat::Json {
        reject_nul_text_rows(&result)?;
    }
    stats.materialize_ms = materialize_start.elapsed().as_secs_f64() * 1000.0;

    let cleanup_start = Instant::now();
    let _ = remove_path_best_effort(Path::new(&result_path));
    stats.cleanup_ms = cleanup_start.elapsed().as_secs_f64() * 1000.0;
    stats.total_ms = total_start.elapsed().as_secs_f64() * 1000.0;
    Ok((result, stats))
}

fn reject_nul_text_rows(rows: &QueryRows) -> Result<()> {
    for (row_idx, row) in rows.rows.iter().enumerate() {
        for (col_idx, value) in row.iter().enumerate() {
            if value_contains_nul_text(value) {
                bail!(
                    "GPU/GQE returned text containing NUL bytes at row {}, column {}; refusing JSON result",
                    row_idx + 1,
                    col_idx + 1
                );
            }
        }
    }
    Ok(())
}

fn value_contains_nul_text(value: &Value) -> bool {
    match value {
        Value::String(text) => text.contains('\0'),
        Value::Array(items) => items.iter().any(value_contains_nul_text),
        Value::Object(map) => map.values().any(value_contains_nul_text),
        _ => false,
    }
}

fn gqe_probe_status() -> Value {
    if !gpu_visible() {
        return json!({
            "status": "unavailable",
            "adapter_version": GQE_ADAPTER_VERSION,
            "reason": "no NVIDIA GPU is visible to the GQE bridge"
        });
    }
    let Some(cli) = gqe_cli() else {
        return json!({
            "status": "unavailable",
            "adapter_version": GQE_ADAPTER_VERSION,
            "reason": "gqe-cli is not installed; set RVBBIT_GQE_CLI or install GQE under /opt/gqe"
        });
    };
    let server_url = gqe_server_url();
    if gqe_server_reachable(&server_url) {
        return json!({
            "status": "ok",
            "adapter_version": GQE_ADAPTER_VERSION,
            "detail": "GQE server is reachable",
            "gqe_cli": cli,
            "server_url": server_url
        });
    }
    if gqe_auto_start_enabled() && gqe_node_manager().is_some() && gqe_task_manager().is_some() {
        return json!({
            "status": "ok",
            "adapter_version": GQE_ADAPTER_VERSION,
            "detail": "GQE server is not running yet, but auto-start is available",
            "gqe_cli": cli,
            "server_url": server_url
        });
    }
    json!({
        "status": "unavailable",
        "adapter_version": GQE_ADAPTER_VERSION,
        "reason": "GQE server is not reachable and auto-start binaries are not installed",
        "gqe_cli": cli,
        "server_url": server_url
    })
}

fn ensure_gqe_server_ready() -> Result<()> {
    let server_url = gqe_server_url();
    if gqe_server_reachable(&server_url) {
        return Ok(());
    }
    if !gqe_auto_start_enabled() {
        bail!("GQE server {server_url} is not reachable and RVBBIT_GQE_AUTO_START is disabled");
    }
    start_gqe_server(&server_url)
}

fn run_gqe_cli_sql(sql: &str, parquet_output: Option<&str>, timeout_s: u64) -> Result<()> {
    let cli = gqe_cli().ok_or_else(|| {
        anyhow!("gqe-cli is not installed; set RVBBIT_GQE_CLI or install GQE under /opt/gqe")
    })?;
    let mut command = Command::new(&cli);
    command
        .arg("--server-url")
        .arg(gqe_server_url())
        .arg("--sql-file")
        .arg("-");
    if let Some(path) = parquet_output {
        command.arg("--parquet").arg(path);
    }
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .with_context(|| format!("starting gqe-cli at {cli}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("gqe-cli stdin unavailable"))?
        .write_all(sql.as_bytes())
        .context("writing SQL to gqe-cli")?;
    let output = wait_child_output(child, Duration::from_secs(timeout_s.max(1) + 5), "gqe-cli")?;
    if !output.status.success() {
        bail!(
            "gqe-cli failed ({}): stdout={} stderr={}",
            output.status,
            child_output_snippet(&output.stdout),
            child_output_snippet(&output.stderr)
        );
    }
    Ok(())
}

fn start_gqe_server(server_url: &str) -> Result<()> {
    let (address, port) = gqe_server_endpoint(server_url)?;
    if !matches!(address.as_str(), "127.0.0.1" | "localhost" | "0.0.0.0") {
        bail!("GQE auto-start only supports local server URLs, got {server_url}");
    }
    let node_manager = gqe_node_manager().ok_or_else(|| {
        anyhow!(
            "gqe_node_manager is not installed; set RVBBIT_GQE_NODE_MANAGER or install GQE under /opt/gqe"
        )
    })?;
    let task_manager = gqe_task_manager().ok_or_else(|| {
        anyhow!(
            "gqe_task_manager is not installed; set RVBBIT_GQE_TASK_MANAGER or install GQE under /opt/gqe"
        )
    })?;
    let work_dir = gqe_work_dir()?;
    let lock_path = work_dir.join("node-manager-start.lock");
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&lock_path)
    {
        Ok(_lock) => {
            let log_path = work_dir.join("node-manager.log");
            let log = OpenOptions::new()
                .create(true)
                .append(true)
                .open(&log_path)
                .with_context(|| format!("opening GQE node manager log {}", log_path.display()))?;
            let child = Command::new(&node_manager)
                .arg("--address")
                .arg(&address)
                .arg("--port")
                .arg(port.to_string())
                .arg("--num-gpus")
                .arg(gqe_num_gpus().to_string())
                .arg("--task-manager-binary")
                .arg(&task_manager)
                .stdin(Stdio::null())
                .stdout(Stdio::from(log.try_clone()?))
                .stderr(Stdio::from(log))
                .spawn()
                .with_context(|| format!("starting GQE node manager at {node_manager}"))?;
            fs::write(work_dir.join("node-manager.pid"), child.id().to_string()).ok();
            let result = wait_for_gqe_server(server_url, Duration::from_secs(60));
            let _ = fs::remove_file(&lock_path);
            result
        }
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
            wait_for_gqe_server(server_url, Duration::from_secs(60))
        }
        Err(err) => Err(err).with_context(|| format!("creating {}", lock_path.display())),
    }
}

fn wait_for_gqe_server(server_url: &str, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if gqe_server_reachable(server_url) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
    bail!("GQE server {server_url} did not become reachable")
}

fn gqe_server_reachable(server_url: &str) -> bool {
    let Ok((host, port)) = gqe_server_endpoint(server_url) else {
        return false;
    };
    let Ok(mut addrs) = format!("{host}:{port}").to_socket_addrs() else {
        return false;
    };
    addrs.any(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok())
}

fn gqe_server_endpoint(server_url: &str) -> Result<(String, u16)> {
    let without_scheme = server_url
        .strip_prefix("http://")
        .or_else(|| server_url.strip_prefix("https://"))
        .unwrap_or(server_url);
    let authority = without_scheme.split('/').next().unwrap_or(without_scheme);
    let (host, port) = authority
        .rsplit_once(':')
        .ok_or_else(|| anyhow!("GQE server URL must include host:port, got {server_url}"))?;
    Ok((host.to_string(), port.parse()?))
}

fn gqe_server_url() -> String {
    env::var("RVBBIT_GQE_SERVER_URL")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_GQE_SERVER_URL.to_string())
}

fn gqe_auto_start_enabled() -> bool {
    env_enabled("RVBBIT_GQE_AUTO_START", true)
}

fn gqe_flight_client_enabled() -> bool {
    !env::var("RVBBIT_GQE_CLIENT_MODE")
        .ok()
        .map(|value| value.trim().eq_ignore_ascii_case("cli"))
        .unwrap_or(false)
}

fn gqe_flight_fallback_enabled() -> bool {
    env_enabled("RVBBIT_GQE_FLIGHT_FALLBACK", true)
}

fn gqe_cli() -> Option<String> {
    configured_executable("RVBBIT_GQE_CLI", DEFAULT_GQE_CLI, "gqe-cli")
}

fn gqe_node_manager() -> Option<String> {
    configured_executable(
        "RVBBIT_GQE_NODE_MANAGER",
        DEFAULT_GQE_NODE_MANAGER,
        "gqe_node_manager",
    )
}

fn gqe_task_manager() -> Option<String> {
    configured_executable(
        "RVBBIT_GQE_TASK_MANAGER",
        DEFAULT_GQE_TASK_MANAGER,
        "gqe_task_manager",
    )
}

fn configured_executable(env_name: &str, default_path: &str, path_name: &str) -> Option<String> {
    if let Ok(value) = env::var(env_name) {
        let trimmed = value.trim();
        if !trimmed.is_empty() && executable_file(Path::new(trimmed)) {
            return Some(trimmed.to_string());
        }
    }
    if executable_file(Path::new(default_path)) {
        return Some(default_path.to_string());
    }
    find_executable_on_path(path_name)
}

fn executable_file(path: &Path) -> bool {
    path.is_file()
        && fs::metadata(path)
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
}

fn find_executable_on_path(name: &str) -> Option<String> {
    env::var_os("PATH").and_then(|path| {
        env::split_paths(&path)
            .map(|dir| dir.join(name))
            .find(|candidate| executable_file(candidate))
            .map(|path| path.display().to_string())
    })
}

fn gpu_visible() -> bool {
    if env_enabled("RVBBIT_GQE_ASSUME_GPU", false) {
        return true;
    }
    if Command::new("nvidia-smi")
        .arg("-L")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
    {
        return true;
    }
    Path::new("/dev/nvidiactl").exists()
        || fs::read_dir("/proc/driver/nvidia/gpus")
            .map(|mut entries| entries.next().is_some())
            .unwrap_or(false)
}

fn gqe_num_gpus() -> usize {
    env_usize("RVBBIT_GQE_NUM_GPUS", detected_gpu_count().unwrap_or(1)).max(1)
}

fn detected_gpu_count() -> Option<usize> {
    let output = Command::new("nvidia-smi")
        .arg("-L")
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let count = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| line.trim_start().starts_with("GPU "))
        .count();
    (count > 0).then_some(count)
}

fn gqe_work_dir() -> Result<PathBuf> {
    let dir = env::var("RVBBIT_GQE_WORK_DIR")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_GQE_WORK_DIR));
    fs::create_dir_all(&dir).with_context(|| format!("creating GQE work dir {}", dir.display()))?;
    match fs::set_permissions(&dir, fs::Permissions::from_mode(0o1777)) {
        Ok(()) => {}
        Err(err)
            if err.kind() == io::ErrorKind::PermissionDenied && gqe_work_dir_is_shared(&dir) => {}
        Err(err) => {
            return Err(err)
                .with_context(|| format!("setting GQE work dir permissions on {}", dir.display()));
        }
    }
    Ok(dir)
}

fn gqe_work_dir_is_shared(dir: &Path) -> bool {
    fs::metadata(dir)
        .map(|meta| meta.is_dir() && (meta.permissions().mode() & 0o007) == 0o007)
        .unwrap_or(false)
}

fn gqe_catalog_root(catalog: &BTreeMap<String, RvbbitDuckTable>) -> Result<PathBuf> {
    let signature = format!("{GQE_CATALOG_VERSION}\n{}", catalog_signature(catalog));
    let root = gqe_work_dir()?
        .join("catalog")
        .join(stable_hash_hex(&signature));
    fs::create_dir_all(&root)
        .with_context(|| format!("creating GQE catalog root {}", root.display()))?;
    Ok(root)
}

fn prepare_gqe_table_dir(root: &Path, table: &RvbbitDuckTable) -> Result<String> {
    let table_dir = root.join(format!(
        "{}__{}",
        sanitize_path_segment(&table.schema),
        sanitize_path_segment(&table.relname)
    ));
    fs::create_dir_all(&table_dir)
        .with_context(|| format!("creating GQE table dir {}", table_dir.display()))?;
    let needs_temporal_transform = table_needs_gqe_temporal_transform(table);
    for (idx, source) in table.paths.iter().enumerate() {
        let link = table_dir.join(format!("part-{idx:05}.parquet"));
        if link.exists() {
            continue;
        }
        if needs_temporal_transform {
            write_gqe_temporal_parquet(source, &link, table)?;
        } else if env_enabled("RVBBIT_GQE_COPY_PARQUET", false) {
            fs::copy(source, &link)
                .with_context(|| format!("copying parquet {source} to {}", link.display()))?;
        } else {
            std::os::unix::fs::symlink(source, &link)
                .with_context(|| format!("symlinking parquet {source} to {}", link.display()))?;
        }
    }
    Ok(table_dir.display().to_string())
}

fn table_needs_gqe_temporal_transform(table: &RvbbitDuckTable) -> bool {
    table
        .columns
        .iter()
        .any(|(_, typ)| typ == "date" || typ.starts_with("timestamp") || pg_type_is_text(typ))
}

fn gqe_table_columns(table: &RvbbitDuckTable) -> Vec<String> {
    let mut out = table
        .columns
        .iter()
        .map(|(name, typ)| format!("  {} {}", gqe_quote_ident(name), gqe_sql_type(typ)))
        .collect::<Vec<_>>();
    for (name, typ) in &table.columns {
        if pg_type_is_text(typ) {
            out.push(format!(
                "  {} INTEGER",
                gqe_quote_ident(&gqe_len_column(name))
            ));
        }
        if typ.starts_with("timestamp") {
            out.push(format!(
                "  {} INTEGER",
                gqe_quote_ident(&gqe_minute_column(name))
            ));
            out.push(format!(
                "  {} VARCHAR",
                gqe_quote_ident(&gqe_minute_ts_column(name))
            ));
        }
        if typ == "date" {
            out.push(format!(
                "  {} INTEGER",
                gqe_quote_ident(&gqe_year_column(name))
            ));
        }
    }
    out
}

fn write_gqe_temporal_parquet(source: &str, target: &Path, table: &RvbbitDuckTable) -> Result<()> {
    let input = File::open(source).with_context(|| format!("opening parquet {source}"))?;
    let reader = ParquetRecordBatchReaderBuilder::try_new(input)
        .with_context(|| format!("reading parquet footer {source}"))?
        .build()
        .with_context(|| format!("building parquet reader {source}"))?;
    let mut output = Some(
        File::create(target)
            .with_context(|| format!("creating transformed GQE parquet {}", target.display()))?,
    );
    let mut writer: Option<ArrowWriter<File>> = None;
    for batch in reader {
        let batch = batch.with_context(|| format!("reading parquet batch {source}"))?;
        let transformed = transform_gqe_temporal_batch(&batch, table)?;
        if writer.is_none() {
            writer = Some(
                ArrowWriter::try_new(
                    output
                        .take()
                        .expect("output file available before writer init"),
                    transformed.schema(),
                    None,
                )
                .with_context(|| format!("opening parquet writer {}", target.display()))?,
            );
        }
        writer
            .as_mut()
            .expect("writer initialized")
            .write(&transformed)
            .with_context(|| format!("writing transformed GQE parquet {}", target.display()))?;
    }
    if let Some(writer) = writer {
        writer
            .close()
            .with_context(|| format!("closing transformed GQE parquet {}", target.display()))?;
    }
    Ok(())
}

fn transform_gqe_temporal_batch(
    batch: &RecordBatch,
    table: &RvbbitDuckTable,
) -> Result<RecordBatch> {
    let type_by_column = table
        .columns
        .iter()
        .map(|(name, typ)| (name.as_str(), typ.as_str()))
        .collect::<HashMap<_, _>>();
    let mut fields = Vec::with_capacity(batch.num_columns());
    let mut columns = Vec::with_capacity(batch.num_columns());
    for (field, column) in batch.schema().fields().iter().zip(batch.columns()) {
        let pg_type = type_by_column.get(field.name().as_str()).copied();
        match pg_type {
            Some("date") => {
                fields.push(Field::new(
                    field.name(),
                    DataType::Utf8,
                    field.is_nullable(),
                ));
                let days = date_column_to_days_values(column)?;
                columns.push(Arc::new(StringArray::from(
                    days.iter()
                        .map(|value| value.map(format_date32))
                        .collect::<Vec<_>>(),
                )) as ArrayRef);
                fields.push(Field::new(
                    gqe_year_column(field.name()),
                    DataType::Int32,
                    field.is_nullable(),
                ));
                columns.push(Arc::new(Int32Array::from(
                    days.iter()
                        .map(|value| value.map(date32_year))
                        .collect::<Vec<_>>(),
                )) as ArrayRef);
            }
            Some(typ) if typ.starts_with("timestamp") => {
                fields.push(Field::new(
                    field.name(),
                    DataType::Utf8,
                    field.is_nullable(),
                ));
                let micros = timestamp_column_to_micros_values(column)?;
                columns.push(Arc::new(StringArray::from(
                    micros
                        .iter()
                        .map(|value| value.map(format_timestamp_micros))
                        .collect::<Vec<_>>(),
                )) as ArrayRef);
                fields.push(Field::new(
                    gqe_minute_column(field.name()),
                    DataType::Int32,
                    field.is_nullable(),
                ));
                columns.push(Arc::new(Int32Array::from(
                    micros
                        .iter()
                        .map(|value| value.map(timestamp_minute))
                        .collect::<Vec<_>>(),
                )) as ArrayRef);
                fields.push(Field::new(
                    gqe_minute_ts_column(field.name()),
                    DataType::Utf8,
                    field.is_nullable(),
                ));
                columns.push(Arc::new(StringArray::from(
                    micros
                        .iter()
                        .map(|value| value.map(format_timestamp_minute_micros))
                        .collect::<Vec<_>>(),
                )) as ArrayRef);
            }
            Some(typ) if pg_type_is_text(typ) => {
                fields.push(field.as_ref().clone());
                columns.push(Arc::clone(column));
                fields.push(Field::new(
                    gqe_len_column(field.name()),
                    DataType::Int32,
                    field.is_nullable(),
                ));
                columns.push(text_length_column(column)?);
            }
            _ => {
                fields.push(field.as_ref().clone());
                columns.push(Arc::clone(column));
            }
        }
    }
    RecordBatch::try_new(Arc::new(Schema::new(fields)), columns)
        .context("building transformed GQE record batch")
}

fn date_column_to_days_values(column: &ArrayRef) -> Result<Vec<Option<i32>>> {
    let values = match column.data_type() {
        DataType::Date32 => {
            let array = as_primitive_array::<Date32Type>(column.as_ref());
            (0..array.len())
                .map(|idx| {
                    if array.is_null(idx) {
                        None
                    } else {
                        Some(array.value(idx))
                    }
                })
                .collect::<Vec<_>>()
        }
        DataType::Int32 => {
            let array = as_primitive_array::<Int32Type>(column.as_ref());
            (0..array.len())
                .map(|idx| {
                    if array.is_null(idx) {
                        None
                    } else {
                        Some(array.value(idx))
                    }
                })
                .collect::<Vec<_>>()
        }
        other => bail!("cannot expose GQE date column from Arrow type {other:?}"),
    };
    Ok(values)
}

fn text_length_column(column: &ArrayRef) -> Result<ArrayRef> {
    let array = as_string_array(column.as_ref());
    let values = (0..array.len())
        .map(|idx| {
            if array.is_null(idx) {
                None
            } else {
                Some(array.value(idx).chars().count() as i32)
            }
        })
        .collect::<Vec<_>>();
    Ok(Arc::new(Int32Array::from(values)) as ArrayRef)
}

fn timestamp_column_to_micros_values(column: &ArrayRef) -> Result<Vec<Option<i64>>> {
    let values = match column.data_type() {
        DataType::Timestamp(TimeUnit::Second, _) => {
            timestamp_values_to_micros::<TimestampSecondType>(column, 1_000_000)
        }
        DataType::Timestamp(TimeUnit::Millisecond, _) => {
            timestamp_values_to_micros::<TimestampMillisecondType>(column, 1_000)
        }
        DataType::Timestamp(TimeUnit::Microsecond, _) => {
            timestamp_values_to_micros::<TimestampMicrosecondType>(column, 1)
        }
        DataType::Timestamp(TimeUnit::Nanosecond, _) => {
            timestamp_values_to_micros::<TimestampNanosecondType>(column, 1)
                .into_iter()
                .map(|value| value.map(|micros| micros / 1_000))
                .collect()
        }
        DataType::Int64 => {
            let array = as_primitive_array::<Int64Type>(column.as_ref());
            (0..array.len())
                .map(|idx| {
                    if array.is_null(idx) {
                        None
                    } else {
                        Some(array.value(idx))
                    }
                })
                .collect()
        }
        other => bail!("cannot expose GQE timestamp column from Arrow type {other:?}"),
    };
    Ok(values)
}

fn timestamp_values_to_micros<T>(column: &ArrayRef, multiplier: i64) -> Vec<Option<i64>>
where
    T: datafusion::arrow::datatypes::ArrowPrimitiveType<Native = i64>,
{
    let array = as_primitive_array::<T>(column.as_ref());
    (0..array.len())
        .map(|idx| {
            if array.is_null(idx) {
                None
            } else {
                Some(array.value(idx) * multiplier)
            }
        })
        .collect()
}

fn gqe_result_path() -> Result<String> {
    let dir = gqe_work_dir()?.join("results");
    fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    Ok(dir
        .join(format!("result-{}-{nanos}", process::id()))
        .display()
        .to_string())
}

fn read_parquet_batches_from_path(path: &Path) -> Result<(Vec<RecordBatch>, GqeResultReadStats)> {
    let mut files = Vec::new();
    if path.is_dir() {
        for entry in fs::read_dir(path).with_context(|| format!("reading {}", path.display()))? {
            let entry = entry?;
            let candidate = entry.path();
            if candidate.extension().is_some_and(|ext| ext == "parquet") {
                files.push(candidate);
            }
        }
        files.sort();
    } else if path.exists() {
        files.push(path.to_path_buf());
    } else {
        return Ok((Vec::new(), GqeResultReadStats::default()));
    }

    let mut batches = Vec::new();
    let mut stats = GqeResultReadStats::default();
    for file_path in files {
        stats.files += 1;
        stats.bytes += fs::metadata(&file_path)
            .with_context(|| format!("statting GQE result {}", file_path.display()))?
            .len();
        let file = File::open(&file_path)
            .with_context(|| format!("opening GQE result {}", file_path.display()))?;
        let reader = ParquetRecordBatchReaderBuilder::try_new(file)
            .with_context(|| format!("reading GQE result footer {}", file_path.display()))?
            .build()
            .with_context(|| format!("building GQE result reader {}", file_path.display()))?;
        for batch in reader {
            let batch = batch
                .with_context(|| format!("reading GQE result batch {}", file_path.display()))?;
            stats.batches += 1;
            stats.rows += batch.num_rows();
            batches.push(batch);
        }
    }
    Ok((batches, stats))
}

struct GqeFlightExecutor {
    runtime: Runtime,
    client: GqeFlightClient,
}

impl GqeFlightExecutor {
    fn connect(threads: usize) -> Result<Self> {
        ensure_gqe_server_ready()?;
        let runtime = RuntimeBuilder::new_multi_thread()
            .enable_all()
            .worker_threads(threads.max(1))
            .build()
            .context("creating GQE Flight runtime")?;
        let client = runtime.block_on(GqeFlightClient::connect(&gqe_server_url()))?;
        Ok(Self { runtime, client })
    }

    fn execute_query(
        &mut self,
        sql: &str,
        catalog: &BTreeMap<String, RvbbitDuckTable>,
        max_rows: usize,
        result_format: ResultFormat,
    ) -> Result<(QueryRows, GqeRunStats)> {
        let total_start = Instant::now();
        let mut stats = GqeRunStats::default();
        stats.client_mode = "flight";

        let rewrite_start = Instant::now();
        if let Some(reason) = gqe_shape_gate_reason(sql, catalog) {
            bail!("{reason}");
        }
        let gqe_sql = rewrite_gqe_sql(sql, catalog);
        if let Some(reason) = gqe_unsupported_temporal_reason(&gqe_sql, catalog) {
            bail!("{reason}");
        }
        if let Some(reason) = gqe_unsupported_function_reason(&gqe_sql) {
            bail!("{reason}");
        }
        if let Some(reason) = gqe_unsupported_grouping_reason(&gqe_sql) {
            bail!("{reason}");
        }
        stats.rewrite_ms = rewrite_start.elapsed().as_secs_f64() * 1000.0;

        let flight_start = Instant::now();
        let batches = self
            .runtime
            .block_on(self.client.execute_select(&gqe_sql))
            .context("executing GQE query through persistent Flight client")?;
        stats.flight_ms = flight_start.elapsed().as_secs_f64() * 1000.0;
        stats.result_batches = batches.len();
        stats.result_rows = batches.iter().map(RecordBatch::num_rows).sum();
        stats.result_bytes = batches
            .iter()
            .map(|batch| batch.get_array_memory_size() as u64)
            .sum();

        let materialize_start = Instant::now();
        let result = match result_format {
            ResultFormat::Json => record_batches_to_query_rows(&batches, max_rows),
            ResultFormat::ArrowIpcFile => record_batches_to_arrow_ipc_rows(&batches, max_rows),
        }?;
        if result_format == ResultFormat::Json {
            reject_nul_text_rows(&result)?;
        }
        stats.materialize_ms = materialize_start.elapsed().as_secs_f64() * 1000.0;
        stats.total_ms = total_start.elapsed().as_secs_f64() * 1000.0;
        Ok((result, stats))
    }
}

struct GqeFlightClient {
    client: FlightServiceClient<Channel>,
    ctx: SessionContext,
}

impl GqeFlightClient {
    async fn connect(server_url: &str) -> Result<Self> {
        let channel = tonic::transport::Endpoint::new(server_url.to_string())
            .with_context(|| format!("configuring GQE Flight endpoint {server_url}"))?
            .connect()
            .await
            .with_context(|| format!("connecting to GQE Flight endpoint {server_url}"))?;
        let mut client = FlightServiceClient::new(channel)
            .max_decoding_message_size(GQE_FLIGHT_MAX_DECODING_MESSAGE_SIZE);
        let discovered = gqe_flight_discover_tables(&mut client).await?;
        let ctx = SessionContext::new();
        for table in discovered {
            ctx.register_table(
                &table.name,
                Arc::new(GqeSchemaOnlyTable {
                    schema: table.schema,
                    row_count: table.row_count,
                }),
            )
            .with_context(|| format!("registering GQE planning table {}", table.name))?;
        }
        Ok(Self { client, ctx })
    }

    async fn execute_select(&mut self, sql: &str) -> Result<Vec<RecordBatch>> {
        let statements = Parser::parse_sql(&PostgreSqlDialect {}, sql)
            .context("parsing GQE SQL for Flight planning")?;
        if statements.is_empty() {
            bail!("GQE Flight query contains no statements");
        }

        let mut last = Vec::new();
        for statement in statements {
            let sql_text = statement.to_string();
            let df = self
                .ctx
                .sql(&sql_text)
                .await
                .with_context(|| format!("planning GQE SQL through DataFusion: {sql_text}"))?;
            let plan = df
                .into_optimized_plan()
                .context("optimizing GQE Flight SQL plan")?;
            let substrait_plan = datafusion_substrait::logical_plan::producer::to_substrait_plan(
                &plan,
                &self.ctx.state(),
            )
            .context("converting GQE query to Substrait")?;
            let mut plan_bytes = Vec::new();
            substrait_plan
                .encode(&mut plan_bytes)
                .context("encoding GQE Substrait plan")?;
            last = gqe_flight_execute_substrait_plan(&mut self.client, plan_bytes).await?;
        }
        Ok(last)
    }
}

struct GqeDiscoveredTable {
    name: String,
    schema: SchemaRef,
    row_count: Option<usize>,
}

#[derive(Debug)]
struct GqeSchemaOnlyTable {
    schema: SchemaRef,
    row_count: Option<usize>,
}

#[async_trait::async_trait]
impl TableProvider for GqeSchemaOnlyTable {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn schema(&self) -> SchemaRef {
        self.schema.clone()
    }

    fn table_type(&self) -> datafusion::logical_expr::TableType {
        datafusion::logical_expr::TableType::Base
    }

    async fn scan(
        &self,
        _state: &dyn Session,
        projection: Option<&Vec<usize>>,
        _filters: &[Expr],
        _limit: Option<usize>,
    ) -> datafusion::common::Result<Arc<dyn ExecutionPlan>> {
        let projected_schema = match projection {
            Some(indices) => Arc::new(self.schema.project(indices)?),
            None => self.schema.clone(),
        };
        Ok(Arc::new(EmptyExec::new(projected_schema)))
    }

    fn statistics(&self) -> Option<Statistics> {
        self.row_count.map(|n| Statistics {
            num_rows: Precision::Exact(n),
            total_byte_size: Precision::Absent,
            column_statistics: vec![],
        })
    }
}

impl fmt::Display for GqeSchemaOnlyTable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "GqeSchemaOnlyTable")
    }
}

async fn gqe_flight_discover_tables(
    client: &mut FlightServiceClient<Channel>,
) -> Result<Vec<GqeDiscoveredTable>> {
    let get_tables = CommandGetTables {
        catalog: None,
        db_schema_filter_pattern: None,
        table_name_filter_pattern: None,
        table_types: vec![],
        include_schema: true,
    };
    let descriptor = FlightDescriptor {
        r#type: DescriptorType::Cmd as i32,
        cmd: get_tables.as_any().encode_to_vec().into(),
        path: vec![],
    };
    let flight_info = client
        .get_flight_info(tonic::Request::new(descriptor))
        .await
        .map_err(|status| gqe_flight_status_error("requesting GQE Flight table metadata", status))?
        .into_inner();
    let ticket = flight_info
        .endpoint
        .first()
        .and_then(|endpoint| endpoint.ticket.clone())
        .ok_or_else(|| anyhow!("GQE Flight GetTables returned no endpoint ticket"))?;
    let stream = client
        .do_get(tonic::Request::new(ticket))
        .await
        .map_err(|status| gqe_flight_status_error("fetching GQE Flight table metadata", status))?
        .into_inner();
    let batches = gqe_flight_decode_stream(stream).await?;

    let mut tables = Vec::new();
    for batch in batches {
        let table_names = batch
            .column_by_name("table_name")
            .and_then(|column| column.as_any().downcast_ref::<StringArray>())
            .ok_or_else(|| anyhow!("GQE Flight metadata is missing table_name"))?;
        let table_schemas = batch
            .column_by_name("table_schema")
            .and_then(|column| column.as_any().downcast_ref::<BinaryArray>())
            .ok_or_else(|| anyhow!("GQE Flight metadata is missing table_schema"))?;
        let row_counts = batch
            .column_by_name("row_count")
            .and_then(|column| column.as_any().downcast_ref::<Int64Array>());
        for idx in 0..batch.num_rows() {
            let row_count = row_counts
                .filter(|array| !array.is_null(idx))
                .map(|array| array.value(idx) as usize);
            tables.push(GqeDiscoveredTable {
                name: table_names.value(idx).to_string(),
                schema: Arc::new(
                    datafusion::arrow::ipc::convert::try_schema_from_ipc_buffer(
                        table_schemas.value(idx),
                    )
                    .context("decoding GQE Flight table schema")?,
                ),
                row_count,
            });
        }
    }
    Ok(tables)
}

async fn gqe_flight_execute_substrait_plan(
    client: &mut FlightServiceClient<Channel>,
    plan_bytes: Vec<u8>,
) -> Result<Vec<RecordBatch>> {
    let substrait_msg = SubstraitPlan {
        plan: plan_bytes.into(),
        version: GQE_FLIGHT_SUBSTRAIT_VERSION.to_string(),
    };
    let cmd = CommandStatementSubstraitPlan {
        plan: Some(substrait_msg),
        transaction_id: None,
    };
    let descriptor = FlightDescriptor {
        r#type: DescriptorType::Cmd as i32,
        cmd: cmd.as_any().encode_to_vec().into(),
        path: vec![],
    };
    let flight_info = client
        .get_flight_info(tonic::Request::new(descriptor))
        .await
        .map_err(|status| gqe_flight_status_error("requesting GQE Flight query execution", status))?
        .into_inner();
    let ticket = flight_info
        .endpoint
        .first()
        .and_then(|endpoint| endpoint.ticket.clone())
        .ok_or_else(|| anyhow!("GQE Flight query returned no endpoint ticket"))?;
    let stream = client
        .do_get(tonic::Request::new(ticket))
        .await
        .map_err(|status| gqe_flight_status_error("fetching GQE Flight query results", status))?
        .into_inner();
    gqe_flight_decode_stream(stream).await
}

fn gqe_flight_status_error(action: &str, status: tonic::Status) -> anyhow::Error {
    let details_len = status.details().len();
    if details_len == 0 {
        anyhow!(
            "{action}: code={:?} message={:?}",
            status.code(),
            status.message()
        )
    } else {
        anyhow!(
            "{action}: code={:?} message={:?} details_len={details_len}",
            status.code(),
            status.message()
        )
    }
}

async fn gqe_flight_decode_stream(
    stream: tonic::Streaming<FlightData>,
) -> Result<Vec<RecordBatch>> {
    let mut stream = FlightRecordBatchStream::new_from_flight_data(
        stream.map_err(|err| FlightError::Tonic(Box::new(err))),
    );
    let mut batches = Vec::new();
    while let Some(batch) = stream
        .try_next()
        .await
        .context("decoding GQE Flight record batch stream")?
    {
        batches.push(batch);
    }
    Ok(batches)
}

fn remove_path_best_effort(path: &Path) -> io::Result<()> {
    if path.is_dir() {
        fs::remove_dir_all(path)
    } else if path.exists() {
        fs::remove_file(path)
    } else {
        Ok(())
    }
}

fn relation_counts(catalog: &BTreeMap<String, RvbbitDuckTable>) -> BTreeMap<String, usize> {
    let mut rel_counts = BTreeMap::<String, usize>::new();
    for table in catalog.values() {
        *rel_counts.entry(table.relname.clone()).or_default() += 1;
    }
    rel_counts
}

fn gqe_table_name(table: &RvbbitDuckTable, rel_counts: &BTreeMap<String, usize>) -> String {
    if rel_counts.get(&table.relname).copied().unwrap_or(0) == 1 {
        table.relname.clone()
    } else {
        format!("{}__{}", table.schema, table.relname)
    }
}

fn gqe_sql_type(typname: &str) -> &'static str {
    match typname {
        "boolean" => "BOOLEAN",
        "smallint" => "SMALLINT",
        "integer" => "INTEGER",
        "bigint" => "BIGINT",
        "real" => "REAL",
        "double precision" | "numeric" => "DOUBLE",
        "date" => "VARCHAR",
        "timestamp" | "timestamp without time zone" | "timestamp with time zone" => "VARCHAR",
        "character" | "character varying" | "text" => "VARCHAR",
        _ => "VARCHAR",
    }
}

fn pg_type_is_text(typname: &str) -> bool {
    matches!(typname, "character" | "character varying" | "text" | "name")
}

fn gqe_len_column(column: &str) -> String {
    format!("__rvbbit_len_{}", sanitize_path_segment(column))
}

fn gqe_minute_column(column: &str) -> String {
    format!("__rvbbit_minute_{}", sanitize_path_segment(column))
}

fn gqe_minute_ts_column(column: &str) -> String {
    format!("__rvbbit_minute_ts_{}", sanitize_path_segment(column))
}

fn gqe_year_column(column: &str) -> String {
    format!("__rvbbit_year_{}", sanitize_path_segment(column))
}

fn timestamp_minute(micros_since_epoch: i64) -> i32 {
    ((micros_since_epoch.rem_euclid(86_400_000_000) / 60_000_000) % 60) as i32
}

fn format_timestamp_minute_micros(micros_since_epoch: i64) -> String {
    let minute_bucket = micros_since_epoch.div_euclid(60_000_000) * 60_000_000;
    format_timestamp_micros(minute_bucket)
}

fn rewrite_gqe_sql(sql: &str, catalog: &BTreeMap<String, RvbbitDuckTable>) -> String {
    let mut rewritten = sql.to_string();
    let rel_counts = relation_counts(catalog);
    for table in catalog.values() {
        let table_name = gqe_table_ddl_name(&gqe_table_name(table, &rel_counts));
        let select_list = table
            .columns
            .iter()
            .map(|(name, _)| gqe_quote_ident(name))
            .collect::<Vec<_>>()
            .join(", ");
        rewritten = replace_ci(
            &rewritten,
            &format!("SELECT * FROM {table_name}"),
            &format!("SELECT {select_list} FROM {table_name}"),
        );
        for (column, typ) in &table.columns {
            if pg_type_is_text(typ) {
                let derived = gqe_quote_ident(&gqe_len_column(column));
                for function_name in ["length", "character_length"] {
                    let pattern = format!("{function_name}({})", gqe_quote_ident(column));
                    rewritten = replace_ci(&rewritten, &pattern, &derived);
                }
            }
            if typ.starts_with("timestamp") {
                let minute = gqe_quote_ident(&gqe_minute_column(column));
                let minute_ts = gqe_quote_ident(&gqe_minute_ts_column(column));
                let ident = gqe_quote_ident(column);
                for pattern in [
                    format!("extract(minute FROM {ident})"),
                    format!("extract(minute from {ident})"),
                    format!("EXTRACT(minute FROM {ident})"),
                    format!("EXTRACT(MINUTE FROM {ident})"),
                ] {
                    rewritten = replace_ci(&rewritten, &pattern, &minute);
                }
                for pattern in [
                    format!("date_trunc('minute', {ident})"),
                    format!("DATE_TRUNC('minute', {ident})"),
                    format!("date_trunc('MINUTE', {ident})"),
                    format!("DATE_TRUNC('MINUTE', {ident})"),
                ] {
                    rewritten = replace_ci(&rewritten, &pattern, &minute_ts);
                }
            }
            if typ == "date" {
                let year = gqe_quote_ident(&gqe_year_column(column));
                let mut ident_forms = vec![gqe_quote_ident(column), column.to_string()];
                ident_forms.sort();
                ident_forms.dedup();
                for ident in ident_forms {
                    for pattern in [
                        format!("extract(year FROM {ident})"),
                        format!("extract(year from {ident})"),
                        format!("EXTRACT(year FROM {ident})"),
                        format!("EXTRACT(YEAR FROM {ident})"),
                    ] {
                        rewritten = replace_ci(&rewritten, &pattern, &year);
                    }
                }
            }
        }
    }
    rewrite_gqe_group_by_first_literal(&rewritten)
}

fn rewrite_gqe_group_by_first_literal(sql: &str) -> String {
    let lowered = sql_stringless(sql).to_ascii_lowercase();
    // Byte offsets found in `lowered` are only valid to slice `sql` when the two
    // have identical byte lengths. sql_stringless collapses each char inside a
    // string literal/comment to a single-byte space, so a multibyte char there
    // (e.g. WHERE note='café') shortens `lowered` and misaligns the offsets —
    // slicing `sql` at them would split a UTF-8 char and panic. When the lengths
    // differ, skip this cosmetic rewrite (the query still runs correctly).
    if lowered.len() != sql.len() {
        return sql.to_string();
    }
    let trimmed = lowered.trim_start();
    if !(trimmed.starts_with("select 1,") || trimmed.starts_with("select 1 ,")) {
        return sql.to_string();
    }
    let Some(group_pos) = lowered.find("group by 1,") else {
        return sql.to_string();
    };
    let remove_start = group_pos + "group by ".len();
    let remove_end = group_pos + "group by 1,".len();
    format!("{}{}", &sql[..remove_start], sql[remove_end..].trim_start())
}

fn replace_ci(input: &str, pattern: &str, replacement: &str) -> String {
    let lowered = input.to_ascii_lowercase();
    let pattern_lower = pattern.to_ascii_lowercase();
    let mut out = String::with_capacity(input.len());
    let mut start = 0usize;
    while let Some(pos) = lowered[start..].find(&pattern_lower) {
        let abs = start + pos;
        out.push_str(&input[start..abs]);
        out.push_str(replacement);
        start = abs + pattern.len();
    }
    out.push_str(&input[start..]);
    out
}

fn gqe_shape_gate_reason(sql: &str, catalog: &BTreeMap<String, RvbbitDuckTable>) -> Option<String> {
    gqe_shape_gate_reason_inner(
        sql,
        catalog,
        env_enabled("RVBBIT_GQE_ALLOW_RISKY_SHAPES", false),
    )
}

fn gqe_shape_gate_reason_inner(
    sql: &str,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    allow_risky: bool,
) -> Option<String> {
    if allow_risky {
        return None;
    }

    let lowered = sql_stringless(sql).to_ascii_lowercase();
    let refs = query_relation_refs(sql, catalog);
    let ref_count = refs.len();

    if sql_has_unsupported_gqe_join(&lowered) {
        return Some(
            "GPU/GQE supports only simple inner/left/cross joins with explicit ON predicates"
                .to_string(),
        );
    }
    if refs.iter().any(|(schema, _)| schema.is_some()) {
        return Some(
            "GPU/GQE does not safely support schema-qualified table references yet".to_string(),
        );
    }
    if gqe_query_selects_qualified_star(&lowered) {
        return Some("GPU/GQE does not support qualified SELECT * projections".to_string());
    }
    if gqe_query_selects_star(sql) && ref_count > 1 {
        return Some("GPU/GQE does not support SELECT * over multiple tables".to_string());
    }
    if gqe_wide_row_retrieval_shape(&lowered) {
        return Some(
            "GPU/GQE wide SELECT * text-filter/order/limit shapes are disabled to avoid high RMM memory pressure"
                .to_string(),
        );
    }
    None
}

fn sql_has_unsupported_gqe_join(lowered: &str) -> bool {
    [
        "full join",
        "full outer join",
        "right join",
        "right outer join",
        "natural join",
        " lateral ",
        " using ",
    ]
    .iter()
    .any(|needle| lowered.contains(needle))
}

fn gqe_query_selects_qualified_star(lowered: &str) -> bool {
    lowered.contains(".*") || lowered.contains(". *")
}

fn gqe_wide_row_retrieval_shape(lowered: &str) -> bool {
    gqe_query_selects_star(lowered)
        && lowered.contains(" where ")
        && lowered.contains(" like ")
        && lowered.contains(" order by ")
        && lowered.contains(" limit ")
}

fn query_relation_refs(
    sql: &str,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
) -> Vec<(Option<String>, String)> {
    let tokens = tokenize_sql_for_refs(&sql_stringless(sql));
    let mut refs = Vec::new();
    let mut expect_relation = false;
    let mut depth = 0usize;
    let mut idx = 0usize;
    while idx < tokens.len() {
        match &tokens[idx] {
            SqlTok::LParen => {
                depth += 1;
                idx += 1;
            }
            SqlTok::RParen => {
                depth = depth.saturating_sub(1);
                idx += 1;
            }
            SqlTok::Ident(word) if word == "from" || word == "join" => {
                expect_relation = true;
                idx += 1;
            }
            SqlTok::Comma if expect_relation => {
                idx += 1;
            }
            SqlTok::Ident(word) if expect_relation && (word == "only" || word == "lateral") => {
                idx += 1;
            }
            _ if expect_relation => {
                if matches!(&tokens[idx], SqlTok::LParen) || depth > 0 {
                    expect_relation = false;
                    idx += 1;
                    continue;
                }
                if let Some((schema, relname, consumed)) = read_relation_name(&tokens[idx..]) {
                    if catalog_contains_relation(catalog, schema.as_deref(), &relname) {
                        let item = (schema, relname);
                        if !refs.contains(&item) {
                            refs.push(item);
                        }
                    }
                    idx += consumed;
                } else {
                    idx += 1;
                }
                expect_relation = false;
            }
            _ => {
                idx += 1;
            }
        }
    }
    refs
}

fn gqe_unsupported_temporal_reason(
    sql: &str,
    _catalog: &BTreeMap<String, RvbbitDuckTable>,
) -> Option<String> {
    if gqe_query_selects_star(sql) {
        return Some("GPU/GQE SELECT * must be rewritten before execution".to_string());
    }
    None
}

fn gqe_unsupported_function_reason(sql: &str) -> Option<String> {
    let lowered = sql_stringless(sql).to_ascii_lowercase();
    if gqe_function_call_present(&lowered, "length")
        || gqe_function_call_present(&lowered, "character_length")
    {
        return Some(
            "GPU/GQE does not support character_length/length scalar functions".to_string(),
        );
    }
    if gqe_function_call_present(&lowered, "extract")
        || gqe_function_call_present(&lowered, "date_trunc")
    {
        return Some("GPU/GQE does not support temporal scalar functions".to_string());
    }
    None
}

fn gqe_function_call_present(sql: &str, function_name: &str) -> bool {
    let mut start = 0usize;
    while let Some(pos) = sql[start..].find(function_name) {
        let abs = start + pos;
        let before = if abs == 0 {
            None
        } else {
            sql[..abs].chars().next_back()
        };
        let after_name = abs + function_name.len();
        let after_name_char = sql[after_name..].chars().next();
        if !before.is_some_and(is_identifier_char)
            && !after_name_char.is_some_and(is_identifier_char)
            && sql[after_name..].trim_start().starts_with('(')
        {
            return true;
        }
        start = after_name;
    }
    false
}

fn gqe_unsupported_grouping_reason(sql: &str) -> Option<String> {
    let lowered = sql_stringless(sql).to_ascii_lowercase();
    if gqe_group_by_ordinal_present(&lowered) {
        return Some("GPU/GQE does not safely support GROUP BY ordinal expressions".to_string());
    }
    None
}

/// GQE has no exact-decimal type — `numeric` is declared to it as DOUBLE, so
/// sum()/avg() over a numeric column drift from Postgres's exact result — and it
/// renders `timestamp with time zone` through a lossy string transform that
/// shifts under a non-UTC session. When the query references such a column by
/// name, veto GQE so it falls back (fail-open) to an exact engine. `timestamp
/// without time zone` is timezone-agnostic and stays eligible.
fn gqe_lossy_type_reason(
    sql: &str,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
) -> Option<String> {
    let lowered = sql_stringless(sql).to_ascii_lowercase();
    for table in catalog.values() {
        for (name, typ) in &table.columns {
            let hit = || identifier_referenced(&lowered, &name.to_ascii_lowercase());
            if typ == "numeric" && hit() {
                return Some(format!(
                    "GPU/GQE cannot represent exact numeric column {name:?} without precision loss"
                ));
            }
            if typ == "timestamp with time zone" && hit() {
                return Some(format!(
                    "GPU/GQE cannot safely render timestamptz column {name:?} (timezone loss)"
                ));
            }
        }
    }
    None
}

/// True if `ident_lower` appears in `lowered_sql` as a whole identifier (not a
/// substring of a longer word). Quote characters count as boundaries, so it
/// matches both bare and double-quoted references.
fn identifier_referenced(lowered_sql: &str, ident_lower: &str) -> bool {
    if ident_lower.is_empty() {
        return false;
    }
    let mut start = 0usize;
    while let Some(pos) = lowered_sql[start..].find(ident_lower) {
        let abs = start + pos;
        let before = if abs == 0 {
            None
        } else {
            lowered_sql[..abs].chars().next_back()
        };
        let after = lowered_sql[abs + ident_lower.len()..].chars().next();
        if !before.is_some_and(is_identifier_char) && !after.is_some_and(is_identifier_char) {
            return true;
        }
        start = abs + ident_lower.len();
    }
    false
}

fn gqe_group_by_ordinal_present(sql: &str) -> bool {
    let Some(group_pos) = sql.find("group by") else {
        return false;
    };
    let after_group = &sql[group_pos + "group by".len()..];
    let end = [
        " having ",
        " order by ",
        " limit ",
        " offset ",
        " union ",
        " except ",
        " intersect ",
    ]
    .iter()
    .filter_map(|marker| after_group.find(marker))
    .min()
    .unwrap_or(after_group.len());
    after_group[..end].split(',').any(|expr| {
        let expr = expr.trim();
        !expr.is_empty() && expr.chars().all(|ch| ch.is_ascii_digit())
    })
}

fn gqe_query_selects_star(sql: &str) -> bool {
    let lowered = sql_stringless(sql).to_ascii_lowercase();
    let Some(after_select) = lowered.trim_start().strip_prefix("select") else {
        return false;
    };
    let mut rest = after_select.trim_start();
    if let Some(after_distinct) = rest.strip_prefix("distinct") {
        rest = after_distinct.trim_start();
    }
    rest.starts_with('*')
}

fn gqe_quote_ident(ident: &str) -> String {
    quote_ident(ident)
}

fn gqe_table_ddl_name(ident: &str) -> String {
    if is_simple_gqe_identifier(ident) {
        ident.to_string()
    } else {
        gqe_quote_ident(ident)
    }
}

fn is_simple_gqe_identifier(ident: &str) -> bool {
    let mut chars = ident.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    (first.is_ascii_lowercase() || first == '_')
        && chars.all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_')
}

fn gqe_quote_string(value: &str) -> String {
    quote_sql_string(value)
}

fn sanitize_path_segment(value: &str) -> String {
    value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

fn wait_child_output(mut child: Child, timeout: Duration, label: &str) -> Result<Output> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().context("collecting child output"),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                bail!("{label} timed out after {}s", timeout.as_secs());
            }
            Err(err) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(err).with_context(|| format!("waiting for {label}"));
            }
        }
    }
}

fn child_output_snippet(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    text.lines()
        .take(4)
        .collect::<Vec<_>>()
        .join(" | ")
        .chars()
        .take(600)
        .collect()
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
    search_path: Option<String>,
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

impl GqeQueryStats {
    fn from_runs(runs: &[GqeRunStats]) -> Option<Self> {
        let last = runs.last()?;
        Some(Self {
            runs: runs.len(),
            client_mode: last.client_mode.to_string(),
            median_total_ms: median_gqe_metric(runs, |run| run.total_ms),
            median_server_ready_ms: median_gqe_metric(runs, |run| run.server_ready_ms),
            median_rewrite_ms: median_gqe_metric(runs, |run| run.rewrite_ms),
            median_cli_ms: median_gqe_metric(runs, |run| run.cli_ms),
            median_flight_ms: median_gqe_metric(runs, |run| run.flight_ms),
            median_result_read_ms: median_gqe_metric(runs, |run| run.result_read_ms),
            median_materialize_ms: median_gqe_metric(runs, |run| run.materialize_ms),
            median_cleanup_ms: median_gqe_metric(runs, |run| run.cleanup_ms),
            result_files: last.result_files,
            result_bytes: last.result_bytes,
            result_batches: last.result_batches,
            result_rows: last.result_rows,
        })
    }
}

fn median_gqe_metric(runs: &[GqeRunStats], metric: impl Fn(&GqeRunStats) -> f64) -> f64 {
    let mut values = runs.iter().map(metric).collect::<Vec<_>>();
    values.sort_by(|a, b| a.total_cmp(b));
    values[values.len() / 2]
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
    GpuGqe {
        flight: Option<GqeFlightExecutor>,
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
            req.search_path.as_deref().or(args.search_path.as_deref()),
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
                apply_duck_search_path(
                    con,
                    &catalog,
                    req.search_path.as_deref().or(args.search_path.as_deref()),
                )?;
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
            ServerExecutor::GpuGqe { flight } => {
                if explain_only {
                    return Ok(empty_query_summary(timeout_s, &catalog, cache));
                }

                let mut elapsed = Vec::with_capacity(repeat);
                let mut last = QueryRows::default();
                let mut gqe_runs = Vec::with_capacity(repeat);
                for _ in 0..repeat {
                    cleanup_query_rows(&mut last);
                    let start = Instant::now();
                    let (rows, stats) = if let Some(flight) = flight.as_mut() {
                        match flight.execute_query(sql, &catalog, max_rows, result_format) {
                            Ok(result) => result,
                            Err(flight_err) if gqe_flight_fallback_enabled() => {
                                eprintln!(
                                    "GQE Flight client failed ({flight_err:#}); falling back to gqe-cli"
                                );
                                let (rows, mut stats) = execute_gqe_query_result(
                                    sql,
                                    &catalog,
                                    max_rows,
                                    result_format,
                                    timeout_s,
                                )?;
                                stats.client_mode = "flight_fallback_cli";
                                (rows, stats)
                            }
                            Err(flight_err) => return Err(flight_err),
                        }
                    } else {
                        execute_gqe_query_result(sql, &catalog, max_rows, result_format, timeout_s)?
                    };
                    last = rows;
                    gqe_runs.push(stats);
                    elapsed.push(start.elapsed().as_secs_f64() * 1000.0);
                }
                elapsed.sort_by(|a, b| a.total_cmp(b));
                cache.gqe = GqeQueryStats::from_runs(&gqe_runs);
                Ok(query_summary_from_rows(
                    elapsed[elapsed.len() / 2],
                    repeat,
                    timeout_s,
                    last,
                    &catalog,
                    cache,
                ))
            }
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
        search_path: Option<&str>,
    ) -> Result<RouteSafetyStats> {
        let start = Instant::now();
        // The same SQL can reference different tables under different
        // search_paths, so the path is part of the cache identity.
        let cache_key = format!("{}\u{1}{}", search_path.unwrap_or(""), sql);
        if !route_safety_cache_enabled() {
            ensure_query_tables_authoritative(&mut self.pg, sql, catalog, search_path)?;
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

        if self.route_safety_cache.entries.contains_key(&cache_key) {
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
            ensure_query_tables_authoritative(&mut self.pg, sql, catalog, search_path)?;
        }
        let max_entries = route_safety_cache_max_entries();
        if max_entries > 0 {
            if self.route_safety_cache.entries.len() >= max_entries {
                self.route_safety_cache.entries.clear();
            }
            self.route_safety_cache.entries.insert(cache_key, ());
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
            Engine::GpuGqe => {
                prepare_gqe_catalog(catalog)?;
                let flight = if gqe_flight_client_enabled() {
                    Some(GqeFlightExecutor::connect(threads)?)
                } else {
                    None
                };
                Ok(ServerExecutor::GpuGqe { flight })
            }
        }
    }
}

fn run_server(args: Args) -> Result<()> {
    let mut state = ServerState::new(&args, None)?;
    let stdin = io::stdin();
    let mut reader = BufReader::new(stdin.lock());
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    while let Some(line) =
        read_bounded_line(&mut reader, max_request_bytes()).context("reading server request")?
    {
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
    let socket_path_ref = Path::new(socket_path);
    if let Some(parent) = socket_path_ref.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating socket directory {}", parent.display()))?;
    }
    remove_stale_socket(socket_path_ref)?;
    let listener =
        UnixListener::bind(socket_path).with_context(|| format!("binding {socket_path}"))?;
    fs::set_permissions(socket_path, fs::Permissions::from_mode(0o777))
        .with_context(|| format!("setting socket permissions on {socket_path}"))?;
    let workers = args.workers.max(1);
    let queue_capacity =
        env_usize("RVBBIT_DUCK_BROKER_QUEUE", DEFAULT_BROKER_QUEUE_CAPACITY).max(workers);
    let max_request_bytes = max_request_bytes();
    let socket_timeout = socket_io_timeout();
    let default_response_timeout_s = args.timeout_s.max(1);
    let (tx, rx) = mpsc::sync_channel::<SocketJob>(queue_capacity);
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
                let response_timeout_s = default_response_timeout_s;
                thread::spawn(move || {
                    let _ = handle_socket_client(
                        stream,
                        tx,
                        max_request_bytes,
                        socket_timeout,
                        response_timeout_s,
                    );
                });
            }
            Err(err) => eprintln!("rvbbit-duck socket accept failed: {err}"),
        }
    }
    Ok(())
}

fn remove_stale_socket(socket_path: &Path) -> Result<()> {
    match fs::symlink_metadata(socket_path) {
        Ok(metadata) if metadata.file_type().is_socket() => fs::remove_file(socket_path)
            .with_context(|| format!("removing stale socket {}", socket_path.display())),
        Ok(_) => bail!(
            "refusing to remove non-socket path {}",
            socket_path.display()
        ),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err).with_context(|| format!("checking socket {}", socket_path.display())),
    }
}

fn handle_socket_client(
    mut stream: UnixStream,
    tx: mpsc::SyncSender<SocketJob>,
    max_request_bytes: usize,
    socket_timeout: Duration,
    default_response_timeout_s: u64,
) -> Result<()> {
    let reader_stream = stream.try_clone().context("cloning Unix stream")?;
    reader_stream
        .set_read_timeout(Some(socket_timeout))
        .context("setting socket read timeout")?;
    stream
        .set_write_timeout(Some(socket_timeout))
        .context("setting socket write timeout")?;
    let mut reader = BufReader::new(reader_stream);
    let line = match read_bounded_line(&mut reader, max_request_bytes) {
        Ok(Some(line)) => line,
        Ok(None) => return Ok(()),
        Err(err) => {
            let error = format_error_chain(&err);
            let _ = write_socket_response(
                &mut stream,
                &json!({"status": "fallback", "error": error}).to_string(),
            );
            return Err(err.context("reading socket request"));
        }
    };
    if line.trim().is_empty() {
        return Ok(());
    }
    let response_timeout = socket_response_timeout_s(&line, default_response_timeout_s);
    let (respond, response_rx) = mpsc::channel();
    BROKER_QUEUE_DEPTH.fetch_add(1, Ordering::Relaxed);
    match tx.try_send(SocketJob {
        line,
        received_at: Instant::now(),
        respond,
    }) {
        Ok(()) => {}
        Err(mpsc::TrySendError::Full(_)) => {
            BROKER_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
            return write_socket_response(
                &mut stream,
                &json!({
                    "status": "fallback",
                    "error": "rvbbit-duck broker queue is full"
                })
                .to_string(),
            );
        }
        Err(mpsc::TrySendError::Disconnected(_)) => {
            BROKER_QUEUE_DEPTH.fetch_sub(1, Ordering::Relaxed);
            return Err(anyhow!(
                "dispatching socket request: broker workers stopped"
            ));
        }
    }
    let response = match response_rx.recv_timeout(Duration::from_secs(response_timeout)) {
        Ok(response) => response,
        Err(err) => {
            let error =
                format!("rvbbit-duck broker response timed out after {response_timeout}s: {err}");
            let _ = write_socket_response(
                &mut stream,
                &json!({"status": "fallback", "error": error}).to_string(),
            );
            return Err(anyhow!(
                "waiting for socket response for {response_timeout}s: {err}"
            ));
        }
    };
    write_socket_response(&mut stream, &response)?;
    Ok(())
}

fn write_socket_response(stream: &mut UnixStream, response: &str) -> Result<()> {
    stream
        .write_all(response.as_bytes())
        .context("writing socket response")?;
    stream
        .write_all(b"\n")
        .context("writing socket response newline")?;
    stream.flush().context("flushing socket response")?;
    Ok(())
}

fn read_bounded_line<R: BufRead>(reader: &mut R, max_bytes: usize) -> Result<Option<String>> {
    let max_bytes = max_bytes.max(1);
    let mut out = Vec::new();
    loop {
        let available = reader.fill_buf().context("filling request buffer")?;
        if available.is_empty() {
            break;
        }
        let take = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|idx| idx + 1)
            .unwrap_or(available.len());
        if out.len().saturating_add(take) > max_bytes {
            bail!("request line exceeds {max_bytes} bytes");
        }
        out.extend_from_slice(&available[..take]);
        reader.consume(take);
        if out.ends_with(b"\n") {
            break;
        }
    }
    if out.is_empty() {
        return Ok(None);
    }
    String::from_utf8(out)
        .map(Some)
        .context("request line is not valid UTF-8")
}

fn max_request_bytes() -> usize {
    env_usize("RVBBIT_DUCK_MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES).max(1)
}

fn socket_io_timeout() -> Duration {
    Duration::from_secs(
        env_u64(
            "RVBBIT_DUCK_SOCKET_IO_TIMEOUT_S",
            DEFAULT_SOCKET_IO_TIMEOUT_S,
        )
        .clamp(1, 3600),
    )
}

fn socket_response_timeout_s(line: &str, default_timeout_s: u64) -> u64 {
    let requested = serde_json::from_str::<Value>(line)
        .ok()
        .and_then(|value| value.get("timeout_s").and_then(Value::as_u64))
        .unwrap_or(default_timeout_s)
        .max(1);
    requested.saturating_add(5).min(86_400)
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
    // Remote (fleet) mode: this sidecar reads PUBLISHED artifacts from object
    // storage instead of brain-local files. Configure httpfs from the same
    // AWS_* env contract the brain's publisher uses (GCS via its S3-interop
    // endpoint included).
    if sidecar_remote_mode() {
        con.execute_batch("INSTALL httpfs; LOAD httpfs;")
            .context("loading httpfs for remote artifact reads")?;
        let esc = |s: String| s.replace('\'', "''");
        if let Ok(ep) = std::env::var("AWS_ENDPOINT") {
            let host = ep.trim_start_matches("https://").trim_start_matches("http://");
            con.execute_batch(&format!(
                "SET s3_endpoint='{}'; SET s3_url_style='path';",
                esc(host.to_string())
            ))
            .context("setting s3 endpoint")?;
        }
        if let (Ok(k), Ok(s)) = (
            std::env::var("AWS_ACCESS_KEY_ID"),
            std::env::var("AWS_SECRET_ACCESS_KEY"),
        ) {
            con.execute_batch(&format!(
                "SET s3_access_key_id='{}'; SET s3_secret_access_key='{}';",
                esc(k),
                esc(s)
            ))
            .context("setting s3 credentials")?;
        }
        if let Ok(r) = std::env::var("AWS_DEFAULT_REGION") {
            let _ = con.execute_batch(&format!("SET s3_region='{}';", esc(r)));
        }
    }
    Ok(con)
}

/// Fleet remote mode: RVBBIT_SIDECAR_REMOTE=1 — resolve row groups via their
/// published object-store URLs (rvbbit.row_groups.published_url, migration
/// 0134) instead of brain-local paths, and skip local-visibility checks for
/// remote schemes. This is what lets a warren with no access to the brain's
/// disk serve queries over the same catalog.
fn sidecar_remote_mode() -> bool {
    std::env::var("RVBBIT_SIDECAR_REMOTE")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false)
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
        .unwrap_or_else(default_engine_for_binary);
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
    let mut search_path: Option<String> = None;
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
            "--search-path" => search_path = Some(need_value(&mut it, "--search-path")?),
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
        search_path,
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
        "gpu_gqe" | "gpu-gqe" | "gqe" => Ok(Engine::GpuGqe),
        other => bail!("unsupported engine: {other}"),
    }
}

fn default_engine_for_binary() -> Engine {
    env::args()
        .next()
        .and_then(|arg| {
            Path::new(&arg)
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
        })
        .filter(|name| name.contains("gqe"))
        .map(|_| Engine::GpuGqe)
        .unwrap_or(Engine::Duck)
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
        "rvbbit-duck --sql SQL [--engine duck|datafusion|gpu_gqe] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--repeat N] [--timeout-s N] [--threads N] [--max-rows N] [--result-format json|arrow_ipc_file]\n\
         rvbbit-duck --serve [--engine duck|datafusion|gpu_gqe] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--threads N]\n\
         rvbbit-duck --serve-socket PATH [--workers N] [--engine duck|datafusion|gpu_gqe] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--threads N]\n\
         rvbbit-duck --serve-derived-socket [--workers N] [--engine duck|datafusion|gpu_gqe] [--layout scan|hive|cluster|vortex|hive:col] [--dsn DSN] [--threads N]\n\
         rvbbit-gqe-bridge --rvbbit-probe\n\
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
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
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
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
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
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
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
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
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
            // Fleet mode: published object-store URLs pass through as-is —
            // there is no local file to remap or stat on this node.
            if sidecar_remote_mode()
                && (path.starts_with("s3://")
                    || path.starts_with("gs://")
                    || path.starts_with("http://")
                    || path.starts_with("https://"))
            {
                mapped.push(path);
                continue;
            }
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
        FROM rvbbit.tables t
        JOIN pg_class c ON c.oid = t.table_oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE coalesce(t.acceleration_enabled, true)
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
        "" | "scan" | "canonical" | "default" => Ok(format!(
            "
            SELECT n.nspname,
                   c.relname,
                   NULL::text AS layout,
                   array_agg({path_expr} ORDER BY rg.rg_id) AS paths,
                   sum(rg.n_rows)::bigint AS row_group_rows,
                   sum(rg.n_bytes)::bigint AS row_group_bytes,
                   pg_relation_size(c.oid)::bigint AS heap_bytes,
                   coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
                   coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
                   (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid)::bigint AS deletes
            FROM rvbbit.row_groups rg
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
            GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
            ",
            path_expr = if sidecar_remote_mode() {
                // Fleet mode: prefer the published object-store copy; a row
                // group without one is invisible to this (remote) sidecar.
                "coalesce(rg.published_url, rg.path)"
            } else {
                "rg.path"
            }
        )),
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
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
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
            JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
            JOIN pg_class c ON c.oid = rg.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE coalesce(t.acceleration_enabled, true)
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
        JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
        JOIN pg_class c ON c.oid = rg.table_oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE coalesce(t.acceleration_enabled, true)
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
    search_path: Option<&str>,
) -> Result<()> {
    // The safety probe replans the caller's SQL on the sidecar's OWN Postgres
    // connection — pin it to the caller's search_path so unqualified table
    // names resolve to the same schema the caller sees (public.customer vs
    // tpcds.customer). Reset when the caller sent none: this connection is
    // long-lived, so a previous request's path must never leak.
    let quoted: Vec<String> = search_path
        .unwrap_or("")
        .split(',')
        .map(|s| s.trim().trim_matches('"'))
        .filter(|s| !s.is_empty())
        .map(|s| format!("\"{}\"", s.replace('"', "\"\"")))
        .collect();
    let set_sql = if quoted.is_empty() {
        "RESET search_path".to_string()
    } else {
        format!("SET search_path = {}", quoted.join(", "))
    };
    pg.simple_query(&set_sql)
        .context("pinning search_path for Rvbbit route safety check")?;
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
            | "time without time zone"
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

/// Resolve unqualified table names the way the CALLING Postgres session would:
/// apply its search_path (CSV) to this DuckDB session. Without this, a relname
/// that exists in more than one schema (e.g. public.customer vs tpcds.customer)
/// has no unqualified alias view (see create_duck_views) and unqualified SQL
/// fails with an ambiguity error -> fail-open to native, silently losing the
/// duck engines. Only schemas present in the catalog are included (DuckDB
/// errors on unknown schemas) and 'main' is always appended so the unique-name
/// alias views keep resolving. Applied on EVERY request so a pooled/persistent
/// session can never leak the previous caller's path.
fn apply_duck_search_path(
    con: &Connection,
    catalog: &BTreeMap<String, RvbbitDuckTable>,
    requested: Option<&str>,
) -> Result<()> {
    let mut parts: Vec<String> = Vec::new();
    if let Some(raw) = requested {
        for entry in raw.split(',') {
            let name = entry.trim().trim_matches('"').to_string();
            if name.is_empty() || parts.iter().any(|p| *p == name) {
                continue;
            }
            if catalog.values().any(|t| t.schema == name) {
                parts.push(name);
            }
        }
    }
    if !parts.iter().any(|p| p == "main") {
        parts.push("main".to_string());
    }
    let csv = parts.join(",");
    con.execute_batch(&format!("SET search_path = {}", quote_sql_string(&csv)))
        .context("applying session search_path to DuckDB")?;
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

fn date32_year(days_since_epoch: i32) -> i32 {
    civil_from_days(days_since_epoch as i64).0 as i32
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
    use std::io::Read;

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

    #[test]
    fn gqe_shape_gate_rejects_known_risky_shapes() {
        let catalog = test_catalog();
        assert!(gqe_shape_gate_reason_inner(
            "SELECT * FROM hits h JOIN lineitem l ON h.id = l.id",
            &catalog,
            false
        )
        .is_some_and(|reason| reason.contains("multiple tables")));
        assert!(
            gqe_shape_gate_reason_inner("SELECT h.* FROM hits h", &catalog, false)
                .is_some_and(|reason| reason.contains("qualified SELECT *"))
        );
        assert!(
            gqe_shape_gate_reason_inner("SELECT count(*) FROM public.hits", &catalog, false)
                .is_some_and(|reason| reason.contains("schema-qualified"))
        );
        assert!(gqe_shape_gate_reason_inner(
            r#"SELECT * FROM hits WHERE "URL" LIKE '%google%' ORDER BY "EventTime" LIMIT 10"#,
            &catalog,
            false
        )
        .is_some_and(|reason| reason.contains("wide SELECT *")));
        assert!(gqe_shape_gate_reason_inner(
            "SELECT h.id, l.id FROM hits h JOIN lineitem l ON h.id = l.id",
            &catalog,
            false
        )
        .is_none());
    }

    #[test]
    fn gqe_shape_gate_can_be_overridden_for_experiments() {
        let catalog = test_catalog();
        assert!(gqe_shape_gate_reason_inner(
            "SELECT * FROM hits h JOIN lineitem l ON h.id = l.id",
            &catalog,
            true
        )
        .is_none());
    }

    fn lossy_type_catalog() -> BTreeMap<String, RvbbitDuckTable> {
        let mut catalog = BTreeMap::new();
        catalog.insert(
            "public.sales".to_string(),
            RvbbitDuckTable {
                schema: "public".to_string(),
                relname: "sales".to_string(),
                paths: Vec::new(),
                columns: vec![
                    ("id".to_string(), "bigint".to_string()),
                    ("amount".to_string(), "numeric".to_string()),
                    ("booked_at".to_string(), "timestamp with time zone".to_string()),
                    ("event_at".to_string(), "timestamp without time zone".to_string()),
                    ("region".to_string(), "text".to_string()),
                ],
                layout: None,
                partition_cols: Vec::new(),
                row_group_rows: 0,
                row_group_bytes: 0,
            },
        );
        catalog
    }

    #[test]
    fn gqe_lossy_type_gate_vetoes_numeric_and_timestamptz() {
        let catalog = lossy_type_catalog();
        // Exact numeric aggregate -> precision loss -> veto.
        assert!(gqe_lossy_type_reason("SELECT sum(amount) FROM sales", &catalog)
            .is_some_and(|r| r.contains("numeric")));
        // Bare timestamptz projection -> timezone loss -> veto.
        assert!(gqe_lossy_type_reason("SELECT booked_at FROM sales", &catalog)
            .is_some_and(|r| r.contains("timestamptz")));
        // timestamp WITHOUT time zone is safe; plain int/text queries are safe.
        assert!(gqe_lossy_type_reason("SELECT event_at, region FROM sales", &catalog).is_none());
        assert!(gqe_lossy_type_reason("SELECT id, region FROM sales", &catalog).is_none());
        // Substring of a wider identifier must NOT false-veto (region vs regional).
        assert!(gqe_lossy_type_reason("SELECT count(*) FROM sales", &catalog).is_none());
    }

    #[test]
    fn gqe_group_by_rewrite_is_utf8_safe_with_multibyte_string_literals() {
        // A multibyte char inside a string literal before GROUP BY shortens
        // sql_stringless output; the rewrite must not panic-slice the original.
        let sql = "SELECT 1, count(*) FROM hits WHERE note='café' GROUP BY 1, x";
        let out = rewrite_gqe_group_by_first_literal(sql);
        // Length mismatch -> rewrite skipped, original returned unchanged.
        assert_eq!(out, sql);
        // Pure-ASCII case still rewrites as before.
        let ascii = "SELECT 1, count(*) FROM hits GROUP BY 1, x";
        assert_eq!(
            rewrite_gqe_group_by_first_literal(ascii),
            "SELECT 1, count(*) FROM hits GROUP BY x"
        );
    }

    #[test]
    fn gqe_rewrites_date_extract_year_to_derived_column() {
        let mut catalog = BTreeMap::new();
        catalog.insert(
            "public.orders".to_string(),
            RvbbitDuckTable {
                schema: "public".to_string(),
                relname: "orders".to_string(),
                paths: Vec::new(),
                columns: vec![
                    ("o_orderdate".to_string(), "date".to_string()),
                    ("o_orderkey".to_string(), "bigint".to_string()),
                ],
                layout: None,
                partition_cols: Vec::new(),
                row_group_rows: 0,
                row_group_bytes: 0,
            },
        );

        let rewritten = rewrite_gqe_sql(
            "SELECT extract(year FROM o_orderdate) AS o_year FROM orders",
            &catalog,
        );
        assert_eq!(
            rewritten,
            "SELECT \"__rvbbit_year_o_orderdate\" AS o_year FROM orders"
        );
        assert!(gqe_unsupported_function_reason(&rewritten).is_none());
    }

    #[test]
    fn bounded_line_preserves_following_bytes() {
        let mut reader = BufReader::new("abc\nrest".as_bytes());
        assert_eq!(
            read_bounded_line(&mut reader, 8).unwrap(),
            Some("abc\n".to_string())
        );

        let mut remaining = String::new();
        reader.read_to_string(&mut remaining).unwrap();
        assert_eq!(remaining, "rest");
    }

    #[test]
    fn bounded_line_rejects_oversized_request() {
        let mut reader = BufReader::new("abcdef\n".as_bytes());
        let err = read_bounded_line(&mut reader, 4).unwrap_err().to_string();
        assert!(err.contains("request line exceeds 4 bytes"));
    }

    #[test]
    fn socket_response_timeout_uses_request_timeout_with_grace() {
        assert_eq!(socket_response_timeout_s(r#"{"timeout_s": 10}"#, 300), 15);
        assert_eq!(socket_response_timeout_s("not json", 300), 305);
        assert_eq!(
            socket_response_timeout_s(r#"{"timeout_s": 999999}"#, 300),
            86_400
        );
    }

    #[test]
    fn stale_socket_removal_rejects_regular_files() {
        let path = env::temp_dir().join(format!(
            "rvbbit-duck-test-{}-regular",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::write(&path, b"not a socket").unwrap();

        let err = remove_stale_socket(&path).unwrap_err().to_string();
        assert!(err.contains("refusing to remove non-socket path"));
        assert!(path.exists());
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn stale_socket_removal_removes_socket_files() {
        let path = env::temp_dir().join(format!(
            "rvbbit-duck-test-{}-socket",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let listener = UnixListener::bind(&path).unwrap();
        drop(listener);

        remove_stale_socket(&path).unwrap();
        assert!(!path.exists());
    }

    #[test]
    fn socket_client_returns_fallback_when_broker_queue_is_full() {
        let before_depth = BROKER_QUEUE_DEPTH.load(Ordering::Relaxed);
        let (mut client, server) = UnixStream::pair().unwrap();
        let (tx, _rx) = mpsc::sync_channel::<SocketJob>(0);

        client.write_all(br#"{"sql":"select 1"}"#).unwrap();
        client.write_all(b"\n").unwrap();

        handle_socket_client(server, tx, 1024, Duration::from_secs(1), 1).unwrap();

        let mut response = String::new();
        client.read_to_string(&mut response).unwrap();
        assert!(response.contains("\"status\":\"fallback\""));
        assert!(response.contains("broker queue is full"));
        assert_eq!(BROKER_QUEUE_DEPTH.load(Ordering::Relaxed), before_depth);
    }
}
