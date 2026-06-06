use anyhow::{anyhow, bail, Context, Result};
use postgres::{Client, NoTls};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Clone)]
struct Config {
    dsn: String,
    node_name: String,
    work_dir: PathBuf,
    template_dir: PathBuf,
    advertise_base_url: Option<String>,
    docker_network: String,
    poll_ms: u64,
    once: bool,
    dry_run: bool,
    labels: Value,
    capacity: Value,
    port_base: u16,
    metrics_ms: u64,
    reconcile_ms: u64,
    /// Command (argv) used to invoke the trainer for `model_training` jobs.
    /// Default `["rvbbit-trainer"]`; override e.g. to exec into a worker
    /// container (`docker compose exec -T bench python /.../rvbbit-trainer`).
    trainer_cmd: Vec<String>,
    /// DSN the trainer uses to reach Postgres. Defaults to the agent's DSN, but
    /// can differ when the trainer runs in a different network context (e.g. the
    /// agent is on a control host and the trainer execs into a worker container).
    trainer_dsn: Option<String>,
    /// Hostname Postgres uses to reach a locally-served sidecar the trainer
    /// stands up (`--serve-host`). Defaults to the node name.
    trainer_serve_host: Option<String>,
    /// Output root the trainer writes model bundles to (must be valid in the
    /// trainer's environment). Defaults to `<work_dir>/trained-models`.
    trainer_output_root: Option<String>,
}

#[derive(Debug)]
struct WarrenJob {
    job_id: String,
    kind: String,
    desired_state: String,
    name: String,
    manifest: Value,
    target_selector: Value,
}

#[derive(Debug)]
struct DeploymentResult {
    endpoint_url: String,
    probe_url: String,
    container_name: String,
    published_host_port: bool,
    backend_name: Option<String>,
    operator_name: Option<String>,
    runtime_name: Option<String>,
    compose_project: String,
    work_dir: PathBuf,
    health: Value,
}

#[derive(Debug)]
struct LifecycleDeploymentMetadata {
    deployment_id: String,
    node_name: String,
    name: String,
    status: String,
    endpoint_url: Option<String>,
    backend_name: Option<String>,
    operator_name: Option<String>,
    runtime_name: Option<String>,
    compose_project: Option<String>,
    work_dir: PathBuf,
}

#[derive(Debug)]
struct DeploymentObservationTarget {
    deployment_id: String,
    name: String,
    status: String,
    compose_project: Option<String>,
    work_dir: Option<PathBuf>,
}

#[derive(Debug)]
struct DockerObservation {
    observed_state: String,
    observation: Value,
    error: Option<String>,
}

fn main() -> Result<()> {
    let config = Config::from_env_args()?;
    fs::create_dir_all(&config.work_dir)
        .with_context(|| format!("creating work dir {}", config.work_dir.display()))?;

    let mut db = Client::connect(&config.dsn, NoTls).context("connecting to Postgres")?;
    register_node(&mut db, &config)?;
    let mut metrics = MetricsSampler::default();
    try_record_metrics(&mut db, &config, &mut metrics);
    let mut last_metrics_at = Instant::now();
    let mut last_reconcile_at = Instant::now();
    println!(
        "warren-agent {} registered node={} work_dir={}",
        VERSION,
        config.node_name,
        config.work_dir.display()
    );

    loop {
        heartbeat(&mut db, &config, "ready")?;
        maybe_record_metrics(&mut db, &config, &mut metrics, &mut last_metrics_at);
        maybe_reconcile_deployments(&mut db, &config, &mut last_reconcile_at);
        let mut failed_once_job: Option<String> = None;
        match claim_next(&mut db, &config)? {
            Some(job) => {
                println!(
                    "claimed job={} kind={} name={} desired={}",
                    job.job_id, job.kind, job.name, job.desired_state
                );
                heartbeat(&mut db, &config, "busy")?;
                maybe_record_metrics(&mut db, &config, &mut metrics, &mut last_metrics_at);
                maybe_reconcile_deployments(&mut db, &config, &mut last_reconcile_at);
                if let Err(err) = process_job(&mut db, &config, &job) {
                    eprintln!("job {} failed: {err:#}", job.job_id);
                    let error = format!("{err:#}");
                    let logs = json!({
                        "agent": "warren-agent",
                        "error": error,
                        "job": {
                            "id": job.job_id,
                            "kind": job.kind,
                            "name": job.name,
                            "target_selector": job.target_selector,
                        }
                    });
                    fail_job(&mut db, &config, &job, &error, &logs)?;
                    failed_once_job = Some(format!("job {} failed: {err:#}", job.job_id));
                }
                try_record_metrics(&mut db, &config, &mut metrics);
                last_metrics_at = Instant::now();
                if config.once {
                    if let Some(message) = failed_once_job {
                        bail!(message);
                    }
                    break;
                }
            }
            None if config.once => {
                println!("no queued Warren jobs");
                break;
            }
            None => thread::sleep(Duration::from_millis(config.poll_ms)),
        }
    }

    Ok(())
}

impl Config {
    fn from_env_args() -> Result<Self> {
        let mut config = Self {
            dsn: env::var("RVBBIT_DSN")
                .unwrap_or_else(|_| "postgresql://postgres:rvbbit@localhost:55433/bench".into()),
            node_name: env::var("WARREN_NODE").unwrap_or_else(|_| hostname_fallback()),
            work_dir: env::var("WARREN_WORK_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from(".rvbbit/warren")),
            template_dir: env::var("WARREN_TEMPLATE_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from("capabilities/templates")),
            advertise_base_url: env::var("WARREN_ADVERTISE_BASE_URL").ok(),
            docker_network: env::var("RVBBIT_DOCKER_NETWORK")
                .unwrap_or_else(|_| "docker_default".into()),
            poll_ms: env::var("WARREN_POLL_MS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(2_000),
            once: false,
            dry_run: env::var("WARREN_DRY_RUN")
                .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
                .unwrap_or(false),
            labels: env_json_object("WARREN_LABELS", json!({"docker": true, "capability": true}))?,
            capacity: env_json_object("WARREN_CAPACITY", json!({}))?,
            port_base: env::var("WARREN_PORT_BASE")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(8300),
            metrics_ms: env::var("WARREN_METRICS_MS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(10_000),
            reconcile_ms: env::var("WARREN_RECONCILE_MS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(15_000),
            trainer_cmd: env::var("WARREN_TRAINER_CMD")
                .ok()
                .map(|v| v.split_whitespace().map(str::to_string).collect::<Vec<_>>())
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| vec!["rvbbit-trainer".to_string()]),
            trainer_dsn: env::var("WARREN_TRAINER_DSN").ok(),
            trainer_serve_host: env::var("WARREN_TRAINER_SERVE_HOST").ok(),
            trainer_output_root: env::var("WARREN_TRAINER_OUTPUT_ROOT").ok(),
        };

        let mut args = env::args().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--dsn" => config.dsn = take_arg(&mut args, "--dsn")?,
                "--node" | "--node-name" => config.node_name = take_arg(&mut args, "--node")?,
                "--work-dir" => config.work_dir = PathBuf::from(take_arg(&mut args, "--work-dir")?),
                "--template-dir" => {
                    config.template_dir = PathBuf::from(take_arg(&mut args, "--template-dir")?)
                }
                "--advertise-base-url" => {
                    config.advertise_base_url = Some(take_arg(&mut args, "--advertise-base-url")?)
                }
                "--docker-network" => {
                    config.docker_network = take_arg(&mut args, "--docker-network")?
                }
                "--poll-ms" => config.poll_ms = take_arg(&mut args, "--poll-ms")?.parse()?,
                "--port-base" => config.port_base = take_arg(&mut args, "--port-base")?.parse()?,
                "--metrics-ms" => {
                    config.metrics_ms = take_arg(&mut args, "--metrics-ms")?.parse()?
                }
                "--reconcile-ms" => {
                    config.reconcile_ms = take_arg(&mut args, "--reconcile-ms")?.parse()?
                }
                "--labels" => {
                    config.labels = serde_json::from_str(&take_arg(&mut args, "--labels")?)?
                }
                "--capacity" => {
                    config.capacity = serde_json::from_str(&take_arg(&mut args, "--capacity")?)?
                }
                "--trainer-cmd" => {
                    config.trainer_cmd = take_arg(&mut args, "--trainer-cmd")?
                        .split_whitespace()
                        .map(str::to_string)
                        .collect();
                }
                "--trainer-dsn" => config.trainer_dsn = Some(take_arg(&mut args, "--trainer-dsn")?),
                "--trainer-serve-host" => {
                    config.trainer_serve_host = Some(take_arg(&mut args, "--trainer-serve-host")?)
                }
                "--trainer-output-root" => {
                    config.trainer_output_root = Some(take_arg(&mut args, "--trainer-output-root")?)
                }
                "--once" => config.once = true,
                "--dry-run" => config.dry_run = true,
                "--help" | "-h" => {
                    print_help();
                    std::process::exit(0);
                }
                other => bail!("unknown argument {other:?}; pass --help"),
            }
        }

        if config.trainer_cmd.is_empty() {
            config.trainer_cmd = vec!["rvbbit-trainer".to_string()];
        }

        if !config.labels.is_object() {
            bail!("--labels must be a JSON object");
        }
        if !config.capacity.is_object() {
            bail!("--capacity must be a JSON object");
        }
        Ok(config)
    }
}

fn env_json_object(name: &str, default: Value) -> Result<Value> {
    let Ok(raw) = env::var(name) else {
        return Ok(default);
    };
    let value: Value =
        serde_json::from_str(&raw).with_context(|| format!("parsing {name} as JSON"))?;
    if !value.is_object() {
        bail!("{name} must be a JSON object");
    }
    Ok(value)
}

fn print_help() {
    println!(
        "warren-agent\n\n\
         Options:\n\
           --dsn <postgres-url>               Postgres DSN (or RVBBIT_DSN)\n\
           --node <name>                      Warren node name (or WARREN_NODE)\n\
           --work-dir <dir>                   Deployment workspace\n\
           --template-dir <dir>               capability template root or template directory\n\
           --advertise-base-url <url>         Remote base URL; default is Docker service URL\n\
           --docker-network <name>            Docker network for generated compose projects\n\
           --labels <json>                    Node labels used by target selectors\n\
           --capacity <json>                  Informational capacity document\n\
           --port-base <port>                 Deterministic port range base\n\
           --metrics-ms <ms>                  Metrics interval, 0 disables\n\
           --reconcile-ms <ms>                Deployment reconciliation interval, 0 disables\n\
           --poll-ms <ms>                     Poll interval\n\
           --trainer-cmd <argv>               Command to run the trainer for model_training jobs\n\
                                              (or WARREN_TRAINER_CMD; default 'rvbbit-trainer')\n\
           --trainer-dsn <postgres-url>       DSN the trainer uses (or WARREN_TRAINER_DSN; default --dsn)\n\
           --trainer-serve-host <host>        Host Postgres uses to reach a served sidecar (default --node)\n\
           --trainer-output-root <dir>        Trainer model-bundle output root (default <work-dir>/trained-models)\n\
           --once                             Claim at most one job\n\
           --dry-run                          Scaffold/register without starting Docker"
    );
}

fn take_arg(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String> {
    args.next()
        .ok_or_else(|| anyhow!("{flag} requires a value"))
}

fn hostname_fallback() -> String {
    env::var("HOSTNAME").unwrap_or_else(|_| "warren-local".into())
}

fn register_node(db: &mut Client, config: &Config) -> Result<()> {
    let labels = config.labels.to_string();
    let capacity = config.capacity.to_string();
    let base_url = config.advertise_base_url.clone();
    db.query_one(
        "SELECT rvbbit.register_warren_node($1, $2, $3::text::jsonb, $4::text::jsonb, $5)",
        &[&config.node_name, &base_url, &labels, &capacity, &VERSION],
    )
    .context("registering Warren node")?;
    Ok(())
}

fn heartbeat(db: &mut Client, config: &Config, status: &str) -> Result<()> {
    let labels = config.labels.to_string();
    let capacity = config.capacity.to_string();
    db.execute(
        "SELECT rvbbit.warren_heartbeat($1, $2, $3::text::jsonb, $4::text::jsonb, NULL::jsonb, $5)",
        &[&config.node_name, &status, &labels, &capacity, &VERSION],
    )
    .context("sending Warren heartbeat")?;
    Ok(())
}

#[derive(Default)]
struct MetricsSampler {
    last_cpu: Option<CpuSample>,
}

#[derive(Clone, Copy)]
struct CpuSample {
    idle: u64,
    total: u64,
}

fn maybe_record_metrics(
    db: &mut Client,
    config: &Config,
    sampler: &mut MetricsSampler,
    last_metrics_at: &mut Instant,
) {
    if config.metrics_ms == 0 {
        return;
    }
    if last_metrics_at.elapsed() >= Duration::from_millis(config.metrics_ms) {
        try_record_metrics(db, config, sampler);
        *last_metrics_at = Instant::now();
    }
}

fn try_record_metrics(db: &mut Client, config: &Config, sampler: &mut MetricsSampler) {
    if config.metrics_ms == 0 {
        return;
    }
    if let Err(err) = record_metrics(db, config, sampler) {
        eprintln!("warren metrics write failed: {err:#}");
    }
}

fn record_metrics(db: &mut Client, config: &Config, sampler: &mut MetricsSampler) -> Result<()> {
    let metrics = collect_metrics(config, sampler);
    let metrics_json = metrics.to_string();
    db.execute(
        "SELECT rvbbit.record_warren_metrics($1, $2::text::jsonb)",
        &[&config.node_name, &metrics_json],
    )
    .context("recording Warren metrics")?;
    Ok(())
}

fn collect_metrics(config: &Config, sampler: &mut MetricsSampler) -> Value {
    let (load1, load5, load15) = read_loadavg();
    let cpu_pct = read_cpu_pct(sampler);
    let memory = read_memory_metrics();
    let disk = read_disk_metrics(&config.work_dir);
    let uptime_secs = read_uptime_secs();
    let (gpus, gpu_probe) = read_gpu_metrics();
    let summary = summarize_gpus(&gpus);
    json!({
        "agent": {
            "name": "warren-agent",
            "version": VERSION
        },
        "system": {
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "uptime_secs": uptime_secs,
            "cpu": {
                "usage_pct": cpu_pct,
                "logical_cores": std::thread::available_parallelism().ok().map(|n| n.get())
            },
            "memory": memory,
            "disk": disk
        },
        "gpus": gpus,
        "gpu_probe": gpu_probe,
        "summary": summary
    })
}

fn read_loadavg() -> (Option<f64>, Option<f64>, Option<f64>) {
    let Ok(contents) = fs::read_to_string("/proc/loadavg") else {
        return (None, None, None);
    };
    let mut parts = contents.split_whitespace();
    (
        parts.next().and_then(parse_f64),
        parts.next().and_then(parse_f64),
        parts.next().and_then(parse_f64),
    )
}

fn read_uptime_secs() -> Option<f64> {
    let contents = fs::read_to_string("/proc/uptime").ok()?;
    contents.split_whitespace().next().and_then(parse_f64)
}

fn read_cpu_pct(sampler: &mut MetricsSampler) -> Option<f64> {
    let current = read_cpu_sample()?;
    let pct = sampler.last_cpu.and_then(|previous| {
        let total_delta = current.total.saturating_sub(previous.total);
        let idle_delta = current.idle.saturating_sub(previous.idle);
        if total_delta == 0 {
            None
        } else {
            Some(((total_delta - idle_delta) as f64 / total_delta as f64) * 100.0)
        }
    });
    sampler.last_cpu = Some(current);
    pct
}

fn read_cpu_sample() -> Option<CpuSample> {
    let contents = fs::read_to_string("/proc/stat").ok()?;
    let line = contents.lines().next()?;
    let mut parts = line.split_whitespace();
    if parts.next()? != "cpu" {
        return None;
    }
    let values = parts
        .filter_map(|part| part.parse::<u64>().ok())
        .collect::<Vec<_>>();
    if values.len() < 4 {
        return None;
    }
    let idle = values.get(3).copied().unwrap_or(0) + values.get(4).copied().unwrap_or(0);
    let total = values.iter().copied().sum();
    Some(CpuSample { idle, total })
}

fn read_memory_metrics() -> Value {
    let Ok(contents) = fs::read_to_string("/proc/meminfo") else {
        return json!({"ok": false});
    };
    let total = meminfo_kib(&contents, "MemTotal").map(kib_to_bytes);
    let available = meminfo_kib(&contents, "MemAvailable").map(kib_to_bytes);
    let free = meminfo_kib(&contents, "MemFree").map(kib_to_bytes);
    let used = match (total, available) {
        (Some(total), Some(available)) => total.checked_sub(available),
        _ => None,
    };
    let used_pct = match (used, total) {
        (Some(used), Some(total)) if total > 0 => Some((used as f64 / total as f64) * 100.0),
        _ => None,
    };
    json!({
        "ok": true,
        "total_bytes": total,
        "available_bytes": available,
        "free_bytes": free,
        "used_bytes": used,
        "used_pct": used_pct
    })
}

fn meminfo_kib(contents: &str, key: &str) -> Option<u64> {
    for line in contents.lines() {
        let mut parts = line.split_whitespace();
        let name = parts.next()?.trim_end_matches(':');
        if name == key {
            return parts.next()?.parse().ok();
        }
    }
    None
}

fn read_disk_metrics(path: &Path) -> Value {
    let output = Command::new("df").arg("-Pk").arg(path).output();
    let Ok(output) = output else {
        return json!({"ok": false});
    };
    if !output.status.success() {
        return json!({
            "ok": false,
            "error": String::from_utf8_lossy(&output.stderr).trim()
        });
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let Some(line) = stdout.lines().nth(1) else {
        return json!({"ok": false, "error": "df output missing data row"});
    };
    let parts = line.split_whitespace().collect::<Vec<_>>();
    if parts.len() < 6 {
        return json!({"ok": false, "error": "df output row has too few columns"});
    }
    let total = parts
        .get(1)
        .and_then(|v| v.parse::<u64>().ok())
        .map(kib_to_bytes);
    let used = parts
        .get(2)
        .and_then(|v| v.parse::<u64>().ok())
        .map(kib_to_bytes);
    let available = parts
        .get(3)
        .and_then(|v| v.parse::<u64>().ok())
        .map(kib_to_bytes);
    json!({
        "ok": true,
        "path": path.display().to_string(),
        "filesystem": parts[0],
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "mount": parts[5]
    })
}

fn read_gpu_metrics() -> (Vec<Value>, Value) {
    let output = Command::new("nvidia-smi")
        .arg("--query-gpu=index,name,uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit")
        .arg("--format=csv,noheader,nounits")
        .output();
    let Ok(output) = output else {
        return (
            Vec::new(),
            json!({"available": false, "source": "nvidia-smi"}),
        );
    };
    if !output.status.success() {
        return (
            Vec::new(),
            json!({
                "available": false,
                "source": "nvidia-smi",
                "error": String::from_utf8_lossy(&output.stderr).trim()
            }),
        );
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut gpus = Vec::new();
    for line in stdout.lines().filter(|line| !line.trim().is_empty()) {
        let fields = line.split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() < 9 {
            continue;
        }
        let memory_used_bytes = parse_f64(fields[4]).map(mib_to_bytes_f64);
        let memory_total_bytes = parse_f64(fields[5]).map(mib_to_bytes_f64);
        gpus.push(json!({
            "index": parse_i64(fields[0]),
            "name": fields[1],
            "uuid": fields[2],
            "utilization_pct": parse_f64(fields[3]),
            "memory_used_bytes": memory_used_bytes,
            "memory_total_bytes": memory_total_bytes,
            "temperature_c": parse_f64(fields[6]),
            "power_draw_watts": parse_f64(fields[7]),
            "power_limit_watts": parse_f64(fields[8])
        }));
    }
    (
        gpus,
        json!({
            "available": true,
            "source": "nvidia-smi"
        }),
    )
}

fn summarize_gpus(gpus: &[Value]) -> Value {
    let gpu_count = gpus.len();
    let mut util_sum = 0.0;
    let mut util_count = 0usize;
    let mut mem_used = 0u64;
    let mut mem_total = 0u64;
    for gpu in gpus {
        if let Some(util) = gpu.get("utilization_pct").and_then(Value::as_f64) {
            util_sum += util;
            util_count += 1;
        }
        mem_used += gpu
            .get("memory_used_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        mem_total += gpu
            .get("memory_total_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
    }
    let gpu_util_pct = if util_count > 0 {
        Some(util_sum / util_count as f64)
    } else {
        None
    };
    json!({
        "gpu_count": gpu_count,
        "gpu_util_pct": gpu_util_pct,
        "gpu_mem_used_bytes": mem_used,
        "gpu_mem_total_bytes": mem_total
    })
}

fn parse_f64(value: &str) -> Option<f64> {
    let value = value.trim();
    if value.is_empty() || value.eq_ignore_ascii_case("N/A") || value == "[Not Supported]" {
        None
    } else {
        value.parse().ok()
    }
}

fn parse_i64(value: &str) -> Option<i64> {
    let value = value.trim();
    if value.is_empty() || value.eq_ignore_ascii_case("N/A") {
        None
    } else {
        value.parse().ok()
    }
}

fn kib_to_bytes(value: u64) -> u64 {
    value.saturating_mul(1024)
}

fn mib_to_bytes_f64(value: f64) -> u64 {
    if value <= 0.0 {
        0
    } else {
        (value * 1024.0 * 1024.0).round() as u64
    }
}

fn claim_job(db: &mut Client, config: &Config) -> Result<Option<WarrenJob>> {
    let row = db
        .query_opt(
            "SELECT job_id::text, kind, desired_state, name, manifest::text, target_selector::text \
             FROM rvbbit.claim_warren_job($1)",
            &[&config.node_name],
        )
        .context("claiming Warren job")?;

    let Some(row) = row else {
        return Ok(None);
    };

    let manifest: String = row.get(4);
    let selector: String = row.get(5);
    Ok(Some(WarrenJob {
        job_id: row.get(0),
        kind: row.get(1),
        desired_state: row.get(2),
        name: row.get(3),
        manifest: serde_json::from_str(&manifest).context("parsing job manifest")?,
        target_selector: serde_json::from_str(&selector).context("parsing job target selector")?,
    }))
}

/// Claim a queued `model_training` job this node is eligible for (target_selector
/// matched against the node's labels). Mapped into the same `WarrenJob` shape so
/// the main loop and the complete/fail helpers work uniformly; run_id, model, and
/// the deploy flag travel in the manifest. This is what makes the standing agent
/// the training executor -- the user only runs SQL (`train_model_managed`).
fn claim_training_job(db: &mut Client, config: &Config) -> Result<Option<WarrenJob>> {
    let row = db
        .query_opt(
            "SELECT job_id::text, run_id::text, model_name, task, deploy, target_selector::text \
             FROM rvbbit.claim_model_training_job_for_node($1)",
            &[&config.node_name],
        )
        .context("claiming model_training job")?;

    let Some(row) = row else {
        return Ok(None);
    };

    let job_id: String = row.get(0);
    let run_id: String = row.get(1);
    let model_name: String = row.get(2);
    let task: String = row.get(3);
    let deploy: bool = row.get(4);
    let selector: String = row.get(5);
    Ok(Some(WarrenJob {
        job_id,
        kind: "model_training".to_string(),
        desired_state: "running".to_string(),
        name: format!("train:{model_name}"),
        manifest: json!({
            "subkind": "model_training",
            "run_id": run_id,
            "model_name": model_name,
            "task": task,
            "deploy": deploy,
        }),
        target_selector: serde_json::from_str(&selector)
            .context("parsing training target selector")?,
    }))
}

/// One claim entry point for the main loop: a deploy job first, then a training
/// job, so both families are drained by the same standing agent.
fn claim_next(db: &mut Client, config: &Config) -> Result<Option<WarrenJob>> {
    if let Some(job) = claim_job(db, config)? {
        return Ok(Some(job));
    }
    claim_training_job(db, config)
}

fn maybe_reconcile_deployments(db: &mut Client, config: &Config, last_reconcile_at: &mut Instant) {
    if config.reconcile_ms == 0 {
        return;
    }
    if last_reconcile_at.elapsed() < Duration::from_millis(config.reconcile_ms) {
        return;
    }
    *last_reconcile_at = Instant::now();
    if let Err(err) = reconcile_deployments(db, config) {
        eprintln!("warning: Warren deployment reconciliation failed: {err:#}");
    }
}

fn reconcile_deployments(db: &mut Client, config: &Config) -> Result<()> {
    if config.dry_run {
        return Ok(());
    }
    let rows = db
        .query(
            "SELECT deployment_id::text, name, status, compose_project, work_dir \
             FROM rvbbit.warren_deployments \
             WHERE node_name = $1 \
               AND status IN ('starting', 'running', 'stopping', 'stopped', \
                              'removed', 'drifted', 'orphaned') \
             ORDER BY created_at",
            &[&config.node_name],
        )
        .context("loading Warren deployments for reconciliation")?;

    for row in rows {
        let target = DeploymentObservationTarget {
            deployment_id: row.get(0),
            name: row.get(1),
            status: row.get(2),
            compose_project: row.get(3),
            work_dir: row.get::<_, Option<String>>(4).map(PathBuf::from),
        };
        let project = target
            .compose_project
            .as_deref()
            .map(str::to_string)
            .unwrap_or_else(|| slugify(&target.name));
        let container = container_name(&project);
        let observed = docker_observation(&container);
        let observation = merge_observation_context(&target, &container, observed.observation);
        let observation_json = observation.to_string();
        let next_status: String = db
            .query_one(
                "SELECT rvbbit.report_warren_deployment_observation(\
                 $1::text::uuid, $2, $3, $4::text::jsonb, $5)",
                &[
                    &target.deployment_id,
                    &config.node_name,
                    &observed.observed_state,
                    &observation_json,
                    &observed.error,
                ],
            )
            .with_context(|| {
                format!(
                    "reporting Warren deployment observation for {}",
                    target.deployment_id
                )
            })?
            .get(0);
        if next_status != target.status {
            println!(
                "reconciled deployment={} {} -> {} observed={}",
                target.deployment_id, target.status, next_status, observed.observed_state
            );
        }
    }
    Ok(())
}

fn merge_observation_context(
    target: &DeploymentObservationTarget,
    container_name: &str,
    observation: Value,
) -> Value {
    let mut doc = match observation {
        Value::Object(obj) => obj,
        other => {
            let mut obj = Map::new();
            obj.insert("raw".into(), other);
            obj
        }
    };
    doc.insert("container_name".into(), json!(container_name));
    doc.insert("deployment_name".into(), json!(target.name));
    doc.insert("deployment_status".into(), json!(target.status));
    if let Some(compose_project) = &target.compose_project {
        doc.insert("compose_project".into(), json!(compose_project));
    }
    if let Some(work_dir) = &target.work_dir {
        doc.insert("work_dir".into(), json!(work_dir.display().to_string()));
    }
    Value::Object(doc)
}

fn docker_observation(container_name: &str) -> DockerObservation {
    let output = match Command::new("docker")
        .arg("inspect")
        .arg("--format")
        .arg("{{json .State}}")
        .arg(container_name)
        .output()
    {
        Ok(output) => output,
        Err(err) => {
            let error = format!("docker inspect failed: {err}");
            return DockerObservation {
                observed_state: "unknown".into(),
                observation: json!({"ok": false, "error": error}),
                error: Some(error),
            };
        }
    };

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let observed_state = if stderr.contains("No such object") {
            "missing"
        } else {
            "unknown"
        };
        return DockerObservation {
            observed_state: observed_state.into(),
            observation: json!({
                "ok": observed_state == "missing",
                "stderr": stderr,
            }),
            error: if observed_state == "missing" {
                None
            } else {
                Some(stderr)
            },
        };
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let state = match serde_json::from_str::<Value>(stdout.trim()) {
        Ok(state) => state,
        Err(err) => {
            let error = format!("docker inspect returned invalid state JSON: {err}");
            return DockerObservation {
                observed_state: "unknown".into(),
                observation: json!({"ok": false, "error": error}),
                error: Some(error),
            };
        }
    };
    let status = state
        .get("Status")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let health = state
        .pointer("/Health/Status")
        .and_then(Value::as_str)
        .unwrap_or("");
    let observed_state = if health == "healthy" {
        "healthy"
    } else if matches!(status, "running" | "exited" | "dead") {
        status
    } else {
        "unknown"
    };
    let error = if observed_state == "unknown" {
        Some(format!("unexpected Docker state {status:?}"))
    } else {
        None
    };
    DockerObservation {
        observed_state: observed_state.into(),
        observation: json!({
            "ok": matches!(observed_state, "healthy" | "running"),
            "docker_state": state,
        }),
        error,
    }
}

fn process_job(db: &mut Client, config: &Config, job: &WarrenJob) -> Result<()> {
    if matches!(job.desired_state.as_str(), "stopped" | "removed") {
        return stop_or_remove_deployment(db, config, job);
    }
    if job.desired_state != "running" {
        bail!("desired_state {:?} is not supported", job.desired_state);
    }

    if job.kind == "model_training" {
        return process_training_job(db, config, job);
    }

    let mut result = match job.kind.as_str() {
        "capability" | "trained_model" => deploy_capability(db, config, job)?,
        other => bail!("Warren job kind {other:?} is not implemented yet"),
    };

    if is_runtime_sidecar(&job.manifest) {
        if !config.dry_run {
            try_update_job_progress(
                db,
                config,
                job,
                "probing_runtime",
                json!({"probe_url": result.probe_url}),
            );
            let probe = probe_runtime(&job.manifest, &result)?;
            if !probe.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                bail!("runtime probe failed after deployment: {probe}");
            }
            if let Some(obj) = result.health.as_object_mut() {
                obj.insert("runtime_probe".into(), probe);
            }
        }
        try_update_job_progress(
            db,
            config,
            job,
            "registering_runtime",
            json!({"runtime_name": result.runtime_name}),
        );
        register_runtime(db, &job.manifest, &result)?;
    } else {
        try_update_job_progress(
            db,
            config,
            job,
            "registering_backend",
            json!({
                "backend_name": result.backend_name,
                "operator_name": result.operator_name,
                "endpoint_url": result.endpoint_url,
            }),
        );
        register_backend_and_operators(db, &job.manifest, &result)?;
        if !config.dry_run {
            try_update_job_progress(
                db,
                config,
                job,
                "probing_backend",
                json!({"backend_name": result.backend_name}),
            );
            let probe = probe_backend(db, &job.manifest)?;
            if !probe.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                bail!("backend probe failed after deployment: {probe}");
            }
            if let Some(obj) = result.health.as_object_mut() {
                obj.insert("backend_probe".into(), probe);
            }
        }
    }
    complete_job(db, config, job, &result)?;
    println!(
        "completed job={} endpoint={} backend={:?} operator={:?} runtime={:?}",
        job.job_id,
        result.endpoint_url,
        result.backend_name,
        result.operator_name,
        result.runtime_name
    );
    Ok(())
}

/// Execute a `model_training` job. The run row is already 'running' (flipped
/// atomically by claim_model_training_job_for_node). We shell out to the trainer
/// exactly like `docker compose` for deploys; the trainer fits the model and
/// registers the backend + `predict_<model>` operator via complete_model_training.
/// We then mark the Warren job complete. The user only ever ran SQL.
fn process_training_job(db: &mut Client, config: &Config, job: &WarrenJob) -> Result<()> {
    let run_id = job
        .manifest
        .get("run_id")
        .and_then(Value::as_str)
        .context("model_training job manifest missing run_id")?
        .to_string();
    let model_name = job
        .manifest
        .get("model_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let deploy = job
        .manifest
        .get("deploy")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    try_update_job_progress(
        db,
        config,
        job,
        "training",
        json!({"run_id": run_id, "model_name": model_name, "deploy": deploy}),
    );

    if config.dry_run {
        println!("dry-run: not training run {run_id} for model {model_name}");
        // The claim already flipped the run/model to 'running'; since no training
        // happens, cancel the run so the model lands in a terminal state
        // ('registered') instead of being stuck 'running'.
        if let Err(e) = cancel_model_training(db, &run_id) {
            eprintln!("warning: dry-run could not cancel run {run_id}: {e:#}");
        }
        let result = training_dry_run_result(config, &model_name);
        complete_job(db, config, job, &result)?;
        return Ok(());
    }

    let output_root = config
        .trainer_output_root
        .clone()
        .unwrap_or_else(|| config.work_dir.join("trained-models").display().to_string());
    let serve_host = config
        .trainer_serve_host
        .clone()
        .unwrap_or_else(|| config.node_name.clone());
    let trainer_dsn = config.trainer_dsn.as_deref().unwrap_or(&config.dsn);

    let (program, base_args) = config
        .trainer_cmd
        .split_first()
        .context("trainer_cmd is empty")?;
    let mut command = Command::new(program);
    command.args(base_args);
    command
        .arg("train-run")
        .arg(&run_id)
        .arg("--dsn")
        .arg(trainer_dsn)
        .arg("--output-root")
        .arg(&output_root)
        .arg("--docker-network")
        .arg(&config.docker_network)
        .arg("--port-base")
        .arg(config.port_base.to_string())
        .arg("--force");
    if deploy {
        command
            .arg("--serve-local")
            .arg("--serve-host")
            .arg(&serve_host);
    }

    println!(
        "training run {run_id} for model {model_name} via {:?} (deploy={deploy})",
        config.trainer_cmd
    );
    let output = command
        .output()
        .with_context(|| format!("running trainer {:?}", config.trainer_cmd))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let detail = format!(
            "trainer exited {}: {}",
            output.status,
            if stderr.trim().is_empty() {
                stdout.trim()
            } else {
                stderr.trim()
            }
        );
        if let Err(e) = fail_model_training(db, &run_id, &detail) {
            eprintln!("warning: could not mark training run {run_id} failed: {e:#}");
        }
        bail!(detail);
    }

    // The trainer registered the model via complete_model_training; read back the
    // serving details to complete the Warren job (and confirm it activated).
    let row = db
        .query_opt(
            "SELECT m.backend_name, m.operator_name, \
                    COALESCE(b.endpoint_url, '') AS endpoint, m.status \
             FROM rvbbit.ml_models m \
             LEFT JOIN rvbbit.backends b ON b.name = m.backend_name \
             WHERE m.name = $1",
            &[&model_name],
        )
        .context("reading trained model registration")?;
    let Some(row) = row else {
        let detail = format!("model {model_name} not found after training run {run_id}");
        if let Err(e) = fail_model_training(db, &run_id, &detail) {
            eprintln!("warning: could not mark training run {run_id} failed: {e:#}");
        }
        bail!(detail);
    };
    let backend_name: Option<String> = row.get(0);
    let operator_name: Option<String> = row.get(1);
    let endpoint_url: String = row.get(2);
    let status: String = row.get(3);
    if status != "active" {
        let detail =
            format!("model {model_name} status is {status:?} (expected active) after training");
        if let Err(e) = fail_model_training(db, &run_id, &detail) {
            eprintln!("warning: could not mark training run {run_id} failed: {e:#}");
        }
        bail!(detail);
    }

    let stdout_tail = String::from_utf8_lossy(&output.stdout)
        .lines()
        .last()
        .unwrap_or("")
        .to_string();
    let result = DeploymentResult {
        endpoint_url,
        probe_url: String::new(),
        container_name: String::new(),
        published_host_port: false,
        backend_name,
        operator_name,
        runtime_name: None,
        compose_project: String::new(),
        work_dir: PathBuf::from(&output_root),
        health: json!({
            "trained": true,
            "deploy": deploy,
            "run_id": run_id,
            "stdout_tail": stdout_tail,
        }),
    };
    complete_job(db, config, job, &result)?;
    println!(
        "completed training job={} model={} run={} backend={:?} operator={:?}",
        job.job_id, model_name, run_id, result.backend_name, result.operator_name
    );
    Ok(())
}

fn training_dry_run_result(config: &Config, model_name: &str) -> DeploymentResult {
    DeploymentResult {
        endpoint_url: String::new(),
        probe_url: String::new(),
        container_name: String::new(),
        published_host_port: false,
        backend_name: Some(format!("predict_{model_name}_backend")),
        operator_name: Some(format!("predict_{model_name}")),
        runtime_name: None,
        compose_project: String::new(),
        work_dir: config.work_dir.clone(),
        health: json!({"trained": false, "dry_run": true}),
    }
}

fn fail_model_training(db: &mut Client, run_id: &str, error: &str) -> Result<()> {
    db.execute(
        "SELECT rvbbit.fail_model_training($1::text::uuid, $2)",
        &[&run_id, &error],
    )
    .context("marking model training run failed")?;
    Ok(())
}

fn cancel_model_training(db: &mut Client, run_id: &str) -> Result<()> {
    db.execute(
        "SELECT rvbbit.cancel_model_training($1::text::uuid)",
        &[&run_id],
    )
    .context("cancelling model training run")?;
    Ok(())
}

fn stop_or_remove_deployment(db: &mut Client, config: &Config, job: &WarrenJob) -> Result<()> {
    let metadata = lifecycle_deployment_metadata(config, job)?;
    if metadata.node_name != config.node_name {
        bail!(
            "lifecycle job targets node {:?}, but this agent is {:?}",
            metadata.node_name,
            config.node_name
        );
    }

    let remove_work_dir = job.desired_state == "removed";
    try_update_job_progress(
        db,
        config,
        job,
        "stopping",
        json!({
            "deployment_id": metadata.deployment_id,
            "compose_project": metadata.compose_project,
            "work_dir": metadata.work_dir.display().to_string(),
            "remove_work_dir": remove_work_dir,
        }),
    );

    let compose_file = metadata.work_dir.join("compose.yaml");
    let mut compose_down = false;
    let mut removed_work_dir = false;
    if config.dry_run {
        println!(
            "dry-run: not stopping Docker project {}",
            metadata
                .compose_project
                .as_deref()
                .unwrap_or(metadata.name.as_str())
        );
    } else if compose_file.exists() {
        docker_compose_down(&metadata.work_dir)?;
        compose_down = true;
    }

    if remove_work_dir && !config.dry_run && metadata.work_dir.exists() {
        fs::remove_dir_all(&metadata.work_dir)
            .with_context(|| format!("removing {}", metadata.work_dir.display()))?;
        removed_work_dir = true;
    }

    let health = json!({
        "ok": !config.dry_run,
        "dry_run": config.dry_run,
        "action": job.desired_state,
        "compose_down": compose_down,
        "compose_file": compose_file.display().to_string(),
        "work_dir_removed": removed_work_dir,
    });
    complete_lifecycle_job(db, config, job, &metadata, &health)?;
    println!(
        "completed lifecycle job={} deployment={} desired={}",
        job.job_id, metadata.deployment_id, job.desired_state
    );
    Ok(())
}

fn lifecycle_deployment_metadata(
    config: &Config,
    job: &WarrenJob,
) -> Result<LifecycleDeploymentMetadata> {
    let deployment = job
        .manifest
        .pointer("/warren_deployment")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("lifecycle job manifest missing warren_deployment object"))?;
    let deployment_id = deployment
        .get("deployment_id")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("lifecycle job manifest missing warren_deployment.deployment_id"))?
        .to_string();
    let node_name = deployment
        .get("node_name")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("lifecycle job manifest missing warren_deployment.node_name"))?
        .to_string();
    let name = deployment
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(&job.name)
        .to_string();
    let status = deployment
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string();
    let compose_project = deployment
        .get("compose_project")
        .and_then(Value::as_str)
        .filter(|v| !v.trim().is_empty())
        .map(str::to_string);
    let work_dir = deployment
        .get("work_dir")
        .and_then(Value::as_str)
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            config.work_dir.join(
                compose_project
                    .as_deref()
                    .map(str::to_string)
                    .unwrap_or_else(|| slugify(&name)),
            )
        });

    Ok(LifecycleDeploymentMetadata {
        deployment_id,
        node_name,
        name,
        status,
        endpoint_url: deployment
            .get("endpoint_url")
            .and_then(Value::as_str)
            .map(str::to_string),
        backend_name: deployment
            .get("backend_name")
            .and_then(Value::as_str)
            .map(str::to_string),
        operator_name: deployment
            .get("operator_name")
            .and_then(Value::as_str)
            .map(str::to_string),
        runtime_name: deployment
            .get("runtime_name")
            .and_then(Value::as_str)
            .map(str::to_string),
        compose_project,
        work_dir,
    })
}

fn deploy_capability(
    db: &mut Client,
    config: &Config,
    job: &WarrenJob,
) -> Result<DeploymentResult> {
    let manifest = &job.manifest;
    let pack_name = manifest
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(&job.name);
    let safe_name = slugify(pack_name);
    let project_dir = config.work_dir.join(&safe_name);
    try_update_job_progress(
        db,
        config,
        job,
        "preparing",
        json!({"compose_project": safe_name, "work_dir": project_dir.display().to_string()}),
    );
    if project_dir.exists() {
        if !config.dry_run && project_dir.join("compose.yaml").exists() {
            docker_compose_down(&project_dir)?;
        }
        fs::remove_dir_all(&project_dir)
            .with_context(|| format!("clearing {}", project_dir.display()))?;
    }
    fs::create_dir_all(&project_dir)
        .with_context(|| format!("creating {}", project_dir.display()))?;

    try_update_job_progress(
        db,
        config,
        job,
        "scaffolding",
        json!({"work_dir": project_dir.display().to_string()}),
    );
    scaffold_project(config, manifest, &project_dir, &safe_name)?;

    let publish_host_port = should_publish_host_port(config, manifest);
    let port = manifest
        .pointer("/warren/port")
        .and_then(Value::as_u64)
        .map(|v| v as u16)
        .unwrap_or_else(|| deterministic_port(&job.job_id, config.port_base));
    let endpoint_url = endpoint_for(config, manifest, port, &safe_name);
    let probe_url = if is_runtime_sidecar(manifest) {
        local_runtime_probe_endpoint_for(manifest, port)
    } else {
        local_probe_endpoint_for(manifest, port)
    };

    if config.dry_run {
        println!(
            "dry-run: not starting Docker project {} on port {}",
            safe_name, port
        );
    } else {
        let should_build = manifest.get("runtime").and_then(runtime_image).is_none();
        // Resolve "auto" device server-side: apply the GPU overlay only when the
        // manifest wants the GPU AND this host actually has one.
        let use_gpu = resolve_gpu_overlay(manifest, &project_dir);
        try_update_job_progress(
            db,
            config,
            job,
            "starting",
            json!({
                "compose_project": safe_name,
                "container_name": container_name(&safe_name),
                "port": port,
                "published_host_port": publish_host_port,
                "build": should_build,
                "gpu": use_gpu,
            }),
        );
        docker_compose_up(
            &project_dir,
            port,
            &config.docker_network,
            should_build,
            publish_host_port,
            use_gpu,
        )?;
        if publish_host_port {
            try_update_job_progress(
                db,
                config,
                job,
                "waiting_health",
                json!({"health_url": format!("http://127.0.0.1:{port}{}", runtime_health_path(manifest))}),
            );
            wait_for_health(
                port,
                runtime_health_path(manifest),
                Duration::from_secs(180),
            )?;
        } else {
            let container = container_name(&safe_name);
            try_update_job_progress(
                db,
                config,
                job,
                "waiting_health",
                json!({"container_name": container}),
            );
            wait_for_container_health(&container, Duration::from_secs(180))?;
        }
    }

    let health = json!({
        "ok": !config.dry_run,
        "dry_run": config.dry_run,
        "port": port,
        "published_host_port": publish_host_port,
        "target_selector": job.target_selector,
    });
    Ok(DeploymentResult {
        endpoint_url,
        probe_url,
        container_name: container_name(&safe_name),
        published_host_port: publish_host_port,
        backend_name: manifest
            .pointer("/backend/name")
            .and_then(Value::as_str)
            .map(str::to_string),
        operator_name: manifest
            .get("operators")
            .and_then(Value::as_array)
            .and_then(|arr| arr.first())
            .and_then(|op| op.get("name"))
            .and_then(Value::as_str)
            .map(str::to_string),
        runtime_name: if is_runtime_sidecar(manifest) {
            runtime_registration_name(manifest).ok()
        } else {
            None
        },
        compose_project: safe_name,
        work_dir: project_dir,
        health,
    })
}

fn scaffold_project(
    config: &Config,
    manifest: &Value,
    out_dir: &Path,
    safe_name: &str,
) -> Result<()> {
    let runtime = manifest.get("runtime").unwrap_or(&Value::Null);
    if runtime_image(runtime).is_some() {
        fs::write(
            out_dir.join("rvbbit.backend.json"),
            serde_json::to_string_pretty(manifest)?,
        )?;
        fs::write(
            out_dir.join("compose.yaml"),
            render_compose(manifest, safe_name)?,
        )?;
        fs::write(
            out_dir.join("compose.host-ports.yaml"),
            render_host_ports_compose(manifest, safe_name)?,
        )?;
        fs::write(
            out_dir.join("compose.gpu.yaml"),
            render_gpu_compose(safe_name)?,
        )?;
        return Ok(());
    }

    let source = manifest.get("source").unwrap_or(&Value::Null);
    let template = runtime
        .get("template")
        .and_then(Value::as_str)
        .unwrap_or("hf-rvbbit-fastapi");
    let template_dir = resolve_template_dir(config, template);
    let handler = runtime
        .get("handler")
        .and_then(Value::as_str)
        .unwrap_or("custom");
    let model = source.get("model").and_then(Value::as_str).unwrap_or("");
    let revision = source.get("revision").and_then(Value::as_str).unwrap_or("");
    let device = runtime
        .get("device")
        .and_then(Value::as_str)
        .unwrap_or("auto");
    let base_image = runtime
        .get("base_image")
        .and_then(Value::as_str)
        .unwrap_or("pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime");

    let values = [
        ("base_image", base_image),
        ("model", model),
        ("revision", revision),
        ("handler", handler),
        ("device", device),
    ];
    copy_template_files(&template_dir, &template_dir, out_dir, &values)?;
    let requirements_path = out_dir.join("requirements.txt");
    if requirements_path.exists() {
        let requirements = render_requirements(
            &fs::read_to_string(&requirements_path).context("reading rendered requirements")?,
            handler,
            runtime,
        );
        fs::write(requirements_path, requirements)?;
    }
    fs::write(
        out_dir.join("rvbbit.backend.json"),
        serde_json::to_string_pretty(manifest)?,
    )?;
    fs::write(
        out_dir.join("compose.yaml"),
        render_compose(manifest, safe_name)?,
    )?;
    fs::write(
        out_dir.join("compose.host-ports.yaml"),
        render_host_ports_compose(manifest, safe_name)?,
    )?;
    fs::write(
        out_dir.join("compose.gpu.yaml"),
        render_gpu_compose(safe_name)?,
    )?;
    Ok(())
}

fn copy_template_files(
    root: &Path,
    current: &Path,
    out_dir: &Path,
    values: &[(&str, &str)],
) -> Result<()> {
    for entry in fs::read_dir(current)
        .with_context(|| format!("reading template dir {}", current.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            if path.file_name().and_then(|name| name.to_str()) == Some("__pycache__") {
                continue;
            }
            copy_template_files(root, &path, out_dir, values)?;
            continue;
        }
        if !file_type.is_file() {
            continue;
        }
        if matches!(
            path.extension().and_then(|extension| extension.to_str()),
            Some("pyc" | "pyo")
        ) {
            continue;
        }
        let rel = path
            .strip_prefix(root)
            .with_context(|| format!("computing template path for {}", path.display()))?;
        let target = out_dir.join(rel);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
        }
        let text = render_tokens(
            &fs::read_to_string(&path)
                .with_context(|| format!("reading template file {}", path.display()))?,
            values,
        );
        fs::write(&target, text).with_context(|| format!("writing {}", target.display()))?;
    }
    Ok(())
}

fn resolve_template_dir(config: &Config, template: &str) -> PathBuf {
    let direct = config.template_dir.join(template);
    if direct.exists() {
        return direct;
    }
    if config
        .template_dir
        .file_name()
        .and_then(|name| name.to_str())
        == Some(template)
    {
        return config.template_dir.clone();
    }
    config.template_dir.clone()
}

fn render_tokens(input: &str, values: &[(&str, &str)]) -> String {
    let mut out = input.to_string();
    for (key, value) in values {
        out = out.replace(&format!("{{{{ {key} }}}}"), value);
        out = out.replace(&format!("{{{{{key}}}}}"), value);
    }
    out
}

fn render_requirements(base: &str, handler: &str, runtime: &Value) -> String {
    let mut lines: Vec<String> = base.lines().map(str::to_string).collect();
    if matches!(
        handler,
        "embedding"
            | "sequence_classification"
            | "question_answering"
            | "summarization"
            | "token_classification"
            | "table_question_answering"
            | "zero_shot_classification"
            | "gliner"
    ) {
        for dep in ["transformers==4.46.3", "sentencepiece==0.2.0"] {
            if !lines.iter().any(|line| line == dep) {
                lines.push(dep.into());
            }
        }
        if handler == "gliner" && !lines.iter().any(|line| line == "gliner==0.2.16") {
            lines.push("gliner==0.2.16".into());
        }
        if handler == "table_question_answering"
            && !lines.iter().any(|line| line == "pandas==2.2.3")
        {
            lines.push("pandas==2.2.3".into());
        }
    }
    if matches!(
        handler,
        "tabular_classification" | "tabular_regression" | "tabular_foundation"
    ) {
        for dep in [
            "huggingface_hub==0.26.5",
            "joblib==1.4.2",
            "numpy==2.1.3",
            "pandas==2.2.3",
            "scikit-learn==1.5.2",
        ] {
            if !lines.iter().any(|line| line == dep) {
                lines.push(dep.into());
            }
        }
    }
    if let Some(extra) = runtime.get("extra_requirements").and_then(Value::as_array) {
        for dep in extra.iter().filter_map(Value::as_str).map(str::trim) {
            if !dep.is_empty() && !lines.iter().any(|line| line == dep) {
                lines.push(dep.into());
            }
        }
    }
    lines.push(String::new());
    lines.join("\n")
}

fn render_compose(manifest: &Value, safe_name: &str) -> Result<String> {
    let service = safe_name.replace('_', "-");
    let source = manifest.get("source").unwrap_or(&Value::Null);
    let runtime = manifest.get("runtime").unwrap_or(&Value::Null);
    let container_port = runtime_container_port(manifest);
    let health_path = runtime_health_path(manifest);
    let mut envs = Map::new();
    envs.insert(
        "RVBBIT_CAPABILITY_MODEL".into(),
        json!(source.get("model").and_then(Value::as_str).unwrap_or("")),
    );
    envs.insert(
        "RVBBIT_CAPABILITY_REVISION".into(),
        json!(source.get("revision").and_then(Value::as_str).unwrap_or("")),
    );
    envs.insert(
        "RVBBIT_CAPABILITY_HANDLER".into(),
        json!(runtime
            .get("handler")
            .and_then(Value::as_str)
            .unwrap_or("custom")),
    );
    envs.insert(
        "RVBBIT_CAPABILITY_DEVICE".into(),
        json!(runtime
            .get("device")
            .and_then(Value::as_str)
            .unwrap_or("auto")),
    );
    if let Some(extra) = runtime.get("env").and_then(Value::as_object) {
        for (k, v) in extra {
            envs.insert(k.clone(), v.clone());
        }
    }
    let env_yaml = envs
        .iter()
        .map(|(k, v)| {
            let value = v
                .as_str()
                .map(str::to_string)
                .unwrap_or_else(|| v.to_string());
            format!("      {k}: {}", serde_json::to_string(&value).unwrap())
        })
        .collect::<Vec<_>>()
        .join("\n");
    let (volume_mounts, volume_defs) = render_runtime_volumes(runtime);
    let runtime_source = render_runtime_source(runtime);
    let runtime_command = render_runtime_command(runtime);
    let runtime_ipc = render_runtime_ipc(runtime);

    Ok(format!(
        "services:\n  {service}:\n{runtime_source}{runtime_command}{runtime_ipc}    container_name: rvbbit-{service}\n    expose:\n      - \"{container_port}\"\n    environment:\n{env_yaml}\n    volumes:\n{volume_mounts}\n    networks:\n      - rvbbit\n    healthcheck:\n      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://localhost:{container_port}{health_path}').read()\"]\n      interval: 10s\n      timeout: 5s\n      retries: 60\n\nnetworks:\n  rvbbit:\n    name: ${{RVBBIT_DOCKER_NETWORK:-docker_default}}\n    external: true\n\nvolumes:\n{volume_defs}\n"
    ))
}

fn render_host_ports_compose(manifest: &Value, safe_name: &str) -> Result<String> {
    let service = safe_name.replace('_', "-");
    let container_port = runtime_container_port(manifest);
    Ok(format!(
        "services:\n  {service}:\n    ports:\n      - \"${{RVBBIT_CAPABILITY_PORT:-0}}:{container_port}\"\n"
    ))
}

fn render_gpu_compose(safe_name: &str) -> Result<String> {
    let service = safe_name.replace('_', "-");
    Ok(format!(
        "services:\n  {service}:\n    gpus: all\n    environment:\n      RVBBIT_CAPABILITY_DEVICE: \"cuda\"\n"
    ))
}

fn runtime_container_port(manifest: &Value) -> u16 {
    manifest
        .pointer("/warren/container_port")
        .or_else(|| manifest.pointer("/runtime/port"))
        .and_then(Value::as_u64)
        .filter(|port| (1..=u16::MAX as u64).contains(port))
        .map(|port| port as u16)
        .unwrap_or(8080)
}

fn runtime_health_path(manifest: &Value) -> &str {
    manifest
        .pointer("/warren/health_path")
        .or_else(|| manifest.pointer("/runtime/health_path"))
        .and_then(Value::as_str)
        .unwrap_or("/health")
}

fn runtime_image(runtime: &Value) -> Option<String> {
    let image = runtime.get("image").and_then(Value::as_str)?.trim();
    if image.is_empty() {
        return None;
    }
    let digest = runtime
        .get("image_digest")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|v| !v.is_empty());
    match digest {
        Some(digest) if !image.contains('@') => Some(format!("{image}@{digest}")),
        _ => Some(image.to_string()),
    }
}

fn render_runtime_source(runtime: &Value) -> String {
    if let Some(image) = runtime_image(runtime) {
        let pull_policy = runtime
            .get("pull_policy")
            .and_then(Value::as_str)
            .unwrap_or("missing");
        return format!(
            "    image: {}\n    pull_policy: {}\n",
            serde_json::to_string(&image).unwrap(),
            serde_json::to_string(pull_policy).unwrap()
        );
    }
    "    build: .\n".into()
}

fn runtime_argv(runtime: &Value) -> Vec<String> {
    let mut argv = Vec::new();
    match runtime.get("command") {
        Some(Value::Array(items)) => {
            argv.extend(items.iter().filter_map(Value::as_str).map(str::to_string));
        }
        Some(Value::String(value)) if !value.trim().is_empty() => argv.push(value.to_string()),
        _ => {}
    }
    match runtime.get("args") {
        Some(Value::Array(items)) => {
            argv.extend(items.iter().filter_map(Value::as_str).map(str::to_string));
        }
        Some(Value::String(value)) if !value.trim().is_empty() => argv.push(value.to_string()),
        _ => {}
    }
    argv
}

fn render_runtime_command(runtime: &Value) -> String {
    let argv = runtime_argv(runtime);
    if argv.is_empty() {
        return String::new();
    }
    let mut lines = vec!["    command:".to_string()];
    lines.extend(
        argv.iter()
            .map(|arg| format!("      - {}", serde_json::to_string(arg).unwrap())),
    );
    format!("{}\n", lines.join("\n"))
}

fn render_runtime_ipc(runtime: &Value) -> String {
    runtime
        .get("ipc")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(|value| format!("    ipc: {}\n", serde_json::to_string(value).unwrap()))
        .unwrap_or_default()
}

fn render_runtime_volumes(runtime: &Value) -> (String, String) {
    let specs = runtime
        .get("volumes")
        .and_then(Value::as_array)
        .filter(|arr| !arr.is_empty())
        .cloned()
        .unwrap_or_else(|| vec![json!({"name": "hf_cache", "mount": "/root/.cache/huggingface"})]);
    let mut mounts = Vec::new();
    let mut defs = Vec::new();
    for spec in specs {
        let Some(name) = spec.get("name").and_then(Value::as_str) else {
            continue;
        };
        let Some(mount) = spec.get("mount").and_then(Value::as_str) else {
            continue;
        };
        mounts.push(format!("      - {name}:{mount}"));
        defs.push(format!("  {name}:"));
    }
    if mounts.is_empty() {
        mounts.push("      - hf_cache:/root/.cache/huggingface".into());
        defs.push("  hf_cache:".into());
    }
    (mounts.join("\n"), defs.join("\n"))
}

/// True when this host has a usable NVIDIA GPU (driver + `nvidia-smi`). This is
/// the signal that resolves a capability's "auto" device safely: `gpus: all`
/// hard-fails on a host without the GPU/runtime, so we only apply the GPU
/// overlay when the card is actually present.
fn host_has_gpu() -> bool {
    Command::new("nvidia-smi")
        .arg("-L")
        .output()
        .map(|o| o.status.success() && !String::from_utf8_lossy(&o.stdout).trim().is_empty())
        .unwrap_or(false)
}

/// Whether a capability manifest wants the GPU. `device: cpu` never; `cuda`
/// always; `auto`/unset only when the pack declares a GPU placement or
/// requirement (so plain CPU models aren't forced onto the card).
fn manifest_wants_gpu(manifest: &Value) -> bool {
    let device = manifest
        .get("runtime")
        .and_then(|r| r.get("device"))
        .and_then(Value::as_str)
        .unwrap_or("auto")
        .to_ascii_lowercase();
    if device == "cpu" {
        return false;
    }
    if device == "cuda" {
        return true;
    }
    // device == "auto" (or unset): opt in only when the pack declares GPU intent.
    let gpu = manifest.get("resources").and_then(|r| r.get("gpu"));
    let required = gpu
        .and_then(|g| g.get("required"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let placement = gpu
        .and_then(|g| g.get("placement"))
        .and_then(Value::as_str)
        .map(|p| !p.trim().is_empty())
        .unwrap_or(false);
    required || placement
}

/// Resolve whether to apply the GPU compose overlay for a deploy: the manifest
/// must want the GPU, the host must have one, and the overlay file must exist.
fn resolve_gpu_overlay(manifest: &Value, project_dir: &Path) -> bool {
    if !manifest_wants_gpu(manifest) {
        return false;
    }
    if !project_dir.join("compose.gpu.yaml").exists() {
        return false;
    }
    if !host_has_gpu() {
        eprintln!(
            "warren-agent: capability wants the GPU but no NVIDIA GPU was detected on this \
             host (nvidia-smi) — deploying on CPU. Set device=cpu in the manifest to silence."
        );
        return false;
    }
    true
}

fn docker_compose_up(
    project_dir: &Path,
    port: u16,
    network: &str,
    build: bool,
    publish_host_port: bool,
    gpu: bool,
) -> Result<()> {
    let mut command = Command::new("docker");
    command.arg("compose").arg("-f").arg("compose.yaml");
    if publish_host_port {
        command.arg("-f").arg("compose.host-ports.yaml");
    }
    // GPU overlay last so its `gpus: all` + device=cuda win. Only applied when
    // the caller resolved Auto to "use the GPU" AND the overlay exists, so a
    // GPU-less host (where `gpus: all` would hard-fail) stays on the CPU base.
    if gpu {
        command.arg("-f").arg("compose.gpu.yaml");
    }
    command.arg("up").arg("-d");
    if build {
        command.arg("--build");
    }
    let status = command
        .current_dir(project_dir)
        .env("RVBBIT_CAPABILITY_PORT", port.to_string())
        .env("RVBBIT_DOCKER_NETWORK", network)
        .status()
        .context("running docker compose up")?;
    if !status.success() {
        bail!("docker compose up failed with status {status}");
    }
    Ok(())
}

fn wait_for_container_health(container_name: &str, timeout: Duration) -> Result<()> {
    let start = Instant::now();
    let mut last_status = String::new();
    while start.elapsed() < timeout {
        let output = Command::new("docker")
            .arg("inspect")
            .arg("--format")
            .arg("{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}")
            .arg(container_name)
            .output()
            .with_context(|| format!("inspecting container {container_name}"))?;
        if output.status.success() {
            last_status = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if last_status == "healthy" || last_status == "running" {
                return Ok(());
            }
        } else {
            last_status = String::from_utf8_lossy(&output.stderr).trim().to_string();
        }
        thread::sleep(Duration::from_secs(1));
    }
    bail!("container {container_name} did not become healthy: {last_status}");
}

fn docker_compose_down(project_dir: &Path) -> Result<()> {
    let status = Command::new("docker")
        .arg("compose")
        .arg("down")
        .current_dir(project_dir)
        .status()
        .context("running docker compose down")?;
    if !status.success() {
        bail!("docker compose down failed with status {status}");
    }
    Ok(())
}

fn wait_for_health(port: u16, health_path: &str, timeout: Duration) -> Result<()> {
    let url = format!(
        "http://127.0.0.1:{port}{}",
        normalized_endpoint_path(health_path)
    );
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()?;
    let start = Instant::now();
    let mut last_error = None;
    while start.elapsed() < timeout {
        match client.get(&url).send() {
            Ok(resp) if resp.status().is_success() => return Ok(()),
            Ok(resp) => last_error = Some(anyhow!("health status {}", resp.status())),
            Err(err) => last_error = Some(err.into()),
        }
        thread::sleep(Duration::from_secs(1));
    }
    Err(last_error.unwrap_or_else(|| anyhow!("health check timed out for {url}")))
}

fn register_backend_and_operators(
    db: &mut Client,
    manifest: &Value,
    result: &DeploymentResult,
) -> Result<()> {
    let backend = manifest
        .get("backend")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("manifest missing backend object"))?;
    let backend_name = backend
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("manifest backend.name is required"))?;
    let transport = backend
        .get("transport")
        .and_then(Value::as_str)
        .unwrap_or("rvbbit");
    let batch_size = backend
        .get("batch_size")
        .and_then(Value::as_i64)
        .unwrap_or(32) as i32;
    let max_concur = backend
        .get("max_concurrent")
        .and_then(Value::as_i64)
        .unwrap_or(4) as i32;
    let timeout_ms = backend
        .get("timeout_ms")
        .and_then(Value::as_i64)
        .unwrap_or(30_000) as i32;
    let auth_env: Option<String> = backend
        .get("auth_env")
        .and_then(Value::as_str)
        .map(str::to_string);
    let backend_opts = backend
        .get("opts")
        .cloned()
        .unwrap_or_else(|| json!({}))
        .to_string();
    let description: Option<String> = backend
        .get("description")
        .and_then(Value::as_str)
        .map(str::to_string);
    let source = manifest.get("source").unwrap_or(&Value::Null);
    let source_provider: Option<String> = source
        .get("provider")
        .and_then(Value::as_str)
        .map(str::to_string);
    let source_model: Option<String> = source
        .get("model")
        .and_then(Value::as_str)
        .map(str::to_string);
    let source_revision: Option<String> = source
        .get("revision")
        .and_then(Value::as_str)
        .map(str::to_string);
    let install_manifest = manifest.to_string();

    db.execute(
        "SELECT rvbbit.register_backend(\
         backend_name => $1, backend_endpoint => $2, backend_transport => $3, \
         backend_batch_size => $4, backend_max_concur => $5, backend_timeout_ms => $6, \
         backend_auth_env => $7, backend_opts => $8::text::jsonb, backend_description => $9, \
         backend_source_provider => $10, backend_source_model => $11, \
         backend_source_revision => $12, backend_install_manifest => $13::text::jsonb)",
        &[
            &backend_name,
            &result.endpoint_url,
            &transport,
            &batch_size,
            &max_concur,
            &timeout_ms,
            &auth_env,
            &backend_opts,
            &description,
            &source_provider,
            &source_model,
            &source_revision,
            &install_manifest,
        ],
    )
    .context("registering backend")?;

    if manifest.get("kind").and_then(Value::as_str) == Some("llm_provider") {
        register_self_hosted_provider(db, manifest, backend_name)?;
    }

    if let Some(operators) = manifest.get("operators").and_then(Value::as_array) {
        for op in operators {
            register_operator(db, manifest, backend_name, op)?;
        }
    }

    db.execute("SELECT rvbbit.reload_backends()", &[])
        .context("reloading backend cache")?;
    Ok(())
}

fn register_self_hosted_provider(
    db: &mut Client,
    manifest: &Value,
    backend_name: &str,
) -> Result<()> {
    let registration = manifest
        .get("provider_registration")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("llm_provider manifest missing provider_registration object"))?;
    let provider = registration
        .get("provider")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("provider_registration.provider is required"))?;
    let source = manifest.get("source").unwrap_or(&Value::Null);
    let model = registration
        .get("model")
        .and_then(Value::as_str)
        .or_else(|| source.get("model").and_then(Value::as_str))
        .ok_or_else(|| anyhow!("provider_registration.model is required"))?;
    let display_name = registration
        .get("display_name")
        .and_then(Value::as_str)
        .map(str::to_string);
    let family = registration
        .get("family")
        .and_then(Value::as_str)
        .map(str::to_string);
    let capabilities = registration
        .get("capabilities")
        .cloned()
        .unwrap_or_else(|| json!(["chat"]))
        .to_string();
    let context_window = registration.get("context_window").and_then(Value::as_i64);
    let output_token_limit = registration
        .get("output_token_limit")
        .and_then(Value::as_i64);
    let input_per_mtok = registration.get("input_per_mtok").and_then(|v| {
        v.as_f64()
            .map(|n| n.to_string())
            .or_else(|| v.as_str().map(str::to_string))
    });
    let output_per_mtok = registration.get("output_per_mtok").and_then(|v| {
        v.as_f64()
            .map(|n| n.to_string())
            .or_else(|| v.as_str().map(str::to_string))
    });
    let currency = registration
        .get("currency")
        .and_then(Value::as_str)
        .unwrap_or("USD")
        .to_string();
    let cost_policy = registration
        .get("cost_policy")
        .and_then(Value::as_str)
        .unwrap_or("free")
        .to_string();
    let mut raw = registration
        .get("raw")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    raw.entry("capability")
        .or_insert_with(|| json!(manifest.get("name").and_then(Value::as_str).unwrap_or("")));
    raw.entry("source_model")
        .or_insert_with(|| json!(source.get("model").and_then(Value::as_str).unwrap_or("")));
    raw.entry("source_url")
        .or_insert_with(|| json!(source.get("url").and_then(Value::as_str).unwrap_or("")));
    let raw = Value::Object(raw).to_string();

    db.execute(
        "SELECT rvbbit.register_self_hosted_model(\
         provider => $1, model => $2, backend_name => $3, display_name => $4, \
         family => $5, capabilities => $6::text::jsonb, context_window => $7::bigint, \
         output_token_limit => $8::bigint, input_per_mtok => ($9::text)::numeric, \
         output_per_mtok => ($10::text)::numeric, currency => $11, cost_policy => $12, \
         raw => $13::text::jsonb)",
        &[
            &provider,
            &model,
            &backend_name,
            &display_name,
            &family,
            &capabilities,
            &context_window,
            &output_token_limit,
            &input_per_mtok,
            &output_per_mtok,
            &currency,
            &cost_policy,
            &raw,
        ],
    )
    .context("registering self-hosted LLM provider")?;

    if registration
        .get("set_default")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        db.execute("SELECT rvbbit.set_default_provider($1)", &[&backend_name])
            .context("setting default LLM provider")?;
    }
    Ok(())
}

fn probe_backend(db: &mut Client, manifest: &Value) -> Result<Value> {
    let backend_name = manifest
        .pointer("/backend/name")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("manifest backend.name is required"))?;
    let sample = manifest
        .pointer("/smoke/inputs/0")
        .cloned()
        .unwrap_or_else(|| {
            json!({
                "text": "Rvbbit Warren backend probe",
                "query": "backend health",
                "labels": ["entity", "topic"],
                "categories": "entity,topic"
            })
        })
        .to_string();
    let row = db
        .query_one(
            "SELECT rvbbit.backend_probe_with_input($1, $2::text::jsonb)::text",
            &[&backend_name, &sample],
        )
        .context("probing deployed backend")?;
    let probe_text: String = row.get(0);
    serde_json::from_str(&probe_text).context("parsing backend probe output")
}

fn is_runtime_sidecar(manifest: &Value) -> bool {
    manifest.get("kind").and_then(Value::as_str) == Some("runtime_sidecar")
}

fn runtime_registration_name(manifest: &Value) -> Result<String> {
    manifest
        .pointer("/runtime_registration/name")
        .and_then(Value::as_str)
        .or_else(|| manifest.get("name").and_then(Value::as_str))
        .map(str::to_string)
        .ok_or_else(|| anyhow!("runtime sidecar manifest needs runtime_registration.name or name"))
}

fn runtime_language(manifest: &Value) -> String {
    manifest
        .pointer("/runtime_registration/language")
        .or_else(|| manifest.pointer("/runtime/language"))
        .and_then(Value::as_str)
        .unwrap_or("python")
        .to_string()
}

fn register_runtime(db: &mut Client, manifest: &Value, result: &DeploymentResult) -> Result<()> {
    let language = runtime_language(manifest);
    let runtime_name = runtime_registration_name(manifest)?;
    let runtime_registration = manifest.get("runtime_registration").unwrap_or(&Value::Null);
    let labels = runtime_registration
        .get("labels")
        .cloned()
        .unwrap_or_else(|| {
            json!({
                "language": language,
                "capability_kind": "runtime_sidecar",
            })
        })
        .to_string();
    let set_default = runtime_registration
        .get("set_default")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let install_manifest = manifest.to_string();
    let health = result.health.to_string();
    match language.as_str() {
        "python" => {
            db.execute(
                "SELECT rvbbit.register_python_runtime(\
                 runtime_name => $1, endpoint_url => $2, runtime_status => 'ready', \
                 runtime_labels => $3::text::jsonb, runtime_source => 'warren', \
                 warren_deployment_id => NULL, install_manifest => $4::text::jsonb, \
                 health => $5::text::jsonb, set_default => $6)",
                &[
                    &runtime_name,
                    &result.endpoint_url,
                    &labels,
                    &install_manifest,
                    &health,
                    &set_default,
                ],
            )
            .context("registering Python runtime")?;
        }
        "mcp" | "mcp_gateway" => {
            db.execute(
                "SELECT rvbbit.register_mcp_gateway(\
                 gateway_name => $1, endpoint_url => $2, gateway_status => 'ready', \
                 gateway_labels => $3::text::jsonb, gateway_source => 'warren', \
                 warren_deployment_id => NULL, install_manifest => $4::text::jsonb, \
                 health => $5::text::jsonb, set_default => $6)",
                &[
                    &runtime_name,
                    &result.endpoint_url,
                    &labels,
                    &install_manifest,
                    &health,
                    &set_default,
                ],
            )
            .context("registering MCP gateway runtime")?;
        }
        _ => bail!("runtime sidecar language {language:?} is not supported by this agent yet"),
    }
    Ok(())
}

fn probe_runtime(manifest: &Value, result: &DeploymentResult) -> Result<Value> {
    let language = runtime_language(manifest);
    if matches!(language.as_str(), "mcp" | "mcp_gateway") {
        let parsed = if result.published_host_port {
            http_get_json(&result.probe_url)
                .with_context(|| format!("probing MCP gateway {}", result.probe_url))?
        } else {
            let url = format!(
                "http://127.0.0.1:{}{}",
                runtime_container_port(manifest),
                normalized_endpoint_path(runtime_health_path(manifest))
            );
            container_http_get_json(&result.container_name, &url)
                .with_context(|| format!("probing MCP gateway in {}", result.container_name))?
        };
        return Ok(json!({
            "ok": parsed.get("status").and_then(Value::as_str) == Some("ok")
                || parsed.get("ok").and_then(Value::as_bool).unwrap_or(false)
                || parsed.get("body").is_some(),
            "runtime": "mcp",
            "health": parsed,
        }));
    }
    if language != "python" {
        bail!("runtime probe for language {language:?} is not implemented");
    }
    let python_version = manifest
        .pointer("/runtime/python_version")
        .and_then(Value::as_str)
        .unwrap_or("3.12");
    let payload = json!({
        "env": {
            "name": "warren_probe",
            "python_version": python_version,
            "requirements": [],
            "env_hash": "00000000000000000000000000000000"
        },
        "handler": {
            "name": "warren_probe",
            "code_hash": "11111111111111111111111111111111",
            "entrypoint": "run",
            "code": "def run(inputs):\n    return {'ok': bool(inputs.get('ok')), 'runtime': 'python'}\n"
        },
        "inputs": {"ok": true},
        "timeout_ms": 5000
    });
    if result.published_host_port {
        return http_post_json(&result.probe_url, &payload)
            .with_context(|| format!("probing runtime {}", result.probe_url));
    }
    let url = format!(
        "http://127.0.0.1:{}{}",
        runtime_container_port(manifest),
        normalized_endpoint_path(
            manifest
                .pointer("/warren/endpoint_path")
                .or_else(|| manifest.pointer("/runtime_registration/endpoint_path"))
                .and_then(Value::as_str)
                .unwrap_or("/run")
        )
    );
    container_http_post_json(&result.container_name, &url, &payload)
        .with_context(|| format!("probing runtime in {}", result.container_name))
}

fn http_get_json(url: &str) -> Result<Value> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;
    let resp = client.get(url).send()?;
    let status = resp.status();
    let body = resp.text().context("reading HTTP response")?;
    if !status.is_success() {
        bail!("HTTP GET returned {}: {}", status.as_u16(), body);
    }
    serde_json::from_str(&body).or_else(|_| Ok(json!({ "body": body })))
}

fn http_post_json(url: &str, payload: &Value) -> Result<Value> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;
    let resp = client.post(url).json(payload).send()?;
    let status = resp.status();
    let body = resp.text().context("reading HTTP response")?;
    if !status.is_success() {
        bail!("HTTP POST returned {}: {}", status.as_u16(), body);
    }
    serde_json::from_str(&body).context("parsing HTTP JSON response")
}

fn container_http_get_json(container_name: &str, url: &str) -> Result<Value> {
    let input = json!({ "url": url }).to_string();
    let script = r#"
import json, sys, urllib.request
doc = json.load(sys.stdin)
with urllib.request.urlopen(doc["url"], timeout=10) as response:
    sys.stdout.write(response.read().decode())
"#;
    let body = docker_exec_python(container_name, script, &input)?;
    serde_json::from_str(&body).or_else(|_| Ok(json!({ "body": body })))
}

fn container_http_post_json(container_name: &str, url: &str, payload: &Value) -> Result<Value> {
    let input = json!({ "url": url, "payload": payload }).to_string();
    let script = r#"
import json, sys, urllib.request
doc = json.load(sys.stdin)
data = json.dumps(doc["payload"]).encode()
request = urllib.request.Request(
    doc["url"],
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    sys.stdout.write(response.read().decode())
"#;
    let body = docker_exec_python(container_name, script, &input)?;
    serde_json::from_str(&body).context("parsing container HTTP JSON response")
}

fn docker_exec_python(container_name: &str, script: &str, stdin: &str) -> Result<String> {
    let mut child = Command::new("docker")
        .arg("exec")
        .arg("-i")
        .arg(container_name)
        .arg("python")
        .arg("-c")
        .arg(script)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("starting docker exec in {container_name}"))?;
    child
        .stdin
        .as_mut()
        .ok_or_else(|| anyhow!("docker exec stdin unavailable"))?
        .write_all(stdin.as_bytes())
        .context("writing docker exec stdin")?;
    let output = child
        .wait_with_output()
        .context("waiting for docker exec")?;
    if !output.status.success() {
        bail!(
            "docker exec in {} failed: {}",
            container_name,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn register_operator(
    db: &mut Client,
    manifest: &Value,
    backend_name: &str,
    op: &Value,
) -> Result<()> {
    let op_name = op
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("operator missing name"))?;
    let arg_names = op
        .get("arg_names")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("operator {op_name} missing arg_names"))?
        .iter()
        .map(|v| v.as_str().unwrap_or("").to_string())
        .collect::<Vec<_>>();
    let arg_types = op
        .get("arg_types")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|v| v.as_str().unwrap_or("text").to_string())
                .collect::<Vec<_>>()
        })
        .unwrap_or_else(|| vec!["text".into(); arg_names.len()]);
    let return_type = op
        .get("return_type")
        .and_then(Value::as_str)
        .unwrap_or("jsonb");
    let parser = op
        .get("parser")
        .and_then(Value::as_str)
        .unwrap_or(if return_type == "jsonb" {
            "json"
        } else {
            "strip"
        });
    let shape = op.get("shape").and_then(Value::as_str).unwrap_or("scalar");
    let description = op
        .get("description")
        .and_then(Value::as_str)
        .or_else(|| manifest.get("description").and_then(Value::as_str));
    let infix_symbol: Option<String> = op
        .get("infix_symbol")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    let infix_word: Option<String> = op
        .get("infix_word")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    let inputs = op
        .get("inputs")
        .cloned()
        .unwrap_or_else(|| default_step_inputs(&arg_names));
    let steps = op.get("steps").cloned().unwrap_or_else(|| {
        json!([{
            "name": backend_name,
            "kind": "specialist",
            "specialist": backend_name,
            "inputs": inputs
        }])
    });
    let steps_json = steps.to_string();
    let tests = op.get("tests").cloned().unwrap_or(Value::Null);
    let tests_json = if tests.is_null() {
        None
    } else {
        Some(tests.to_string())
    };

    let infix_exists = if let Some(symbol) = infix_symbol.as_deref() {
        arg_types.len() == 2 && infix_operator_exists(db, symbol, &arg_types[0], &arg_types[1])?
    } else {
        false
    };
    let create_infix_symbol = if infix_exists {
        None
    } else {
        infix_symbol.clone()
    };

    db.execute(
        "SELECT rvbbit.create_operator(\
         op_name => $1, op_arg_names => $2, op_arg_types => $3, \
         op_return_type => $4, op_parser => $5, op_shape => $6, \
         op_description => $7, op_infix_symbol => $8, op_infix_word => $9, \
         op_tests => $10::text::jsonb, op_steps => $11::text::jsonb)",
        &[
            &op_name,
            &arg_names,
            &arg_types,
            &return_type,
            &parser,
            &shape,
            &description,
            &create_infix_symbol,
            &infix_word,
            &tests_json,
            &steps_json,
        ],
    )
    .with_context(|| format!("registering operator {op_name}"))?;
    if infix_exists {
        db.execute(
            "UPDATE rvbbit.operators SET infix_symbol = $1, infix_word = $2 WHERE name = $3",
            &[&infix_symbol, &infix_word, &op_name],
        )
        .with_context(|| format!("preserving infix metadata for operator {op_name}"))?;
    }
    Ok(())
}

fn infix_operator_exists(
    db: &mut Client,
    symbol: &str,
    left_type: &str,
    right_type: &str,
) -> Result<bool> {
    let row = db
        .query_opt(
            "SELECT 1 FROM pg_operator op \
         WHERE op.oprnamespace = 'rvbbit'::regnamespace \
           AND op.oprname = $1 \
           AND op.oprleft = $2::text::regtype \
           AND op.oprright = $3::text::regtype",
            &[&symbol, &left_type, &right_type],
        )
        .with_context(|| format!("checking existing infix operator {symbol}"))?;
    Ok(row.is_some())
}

fn default_step_inputs(arg_names: &[String]) -> Value {
    let mut obj = Map::new();
    for name in arg_names {
        obj.insert(
            name.clone(),
            Value::String(format!("{{{{ inputs.{name} }}}}")),
        );
    }
    Value::Object(obj)
}

fn complete_job(
    db: &mut Client,
    config: &Config,
    job: &WarrenJob,
    result: &DeploymentResult,
) -> Result<()> {
    let manifest = job.manifest.to_string();
    let health = result.health.to_string();
    let logs = json!({
        "agent": "warren-agent",
        "compose_project": result.compose_project,
        "work_dir": result.work_dir.display().to_string(),
        "container_name": result.container_name,
        "endpoint_url": result.endpoint_url,
        "probe_url": result.probe_url,
        "published_host_port": result.published_host_port,
        "backend_name": result.backend_name,
        "operator_name": result.operator_name,
        "runtime_name": result.runtime_name,
    })
    .to_string();
    db.execute(
        "SELECT rvbbit.complete_warren_job(\
         job_id => $1::text::uuid, node_name => $2, deployment_status => 'running', \
         endpoint_url => $3, backend_name => $4, operator_name => $5, \
         deploy_manifest => $6::text::jsonb, compose_project => $7, work_dir => $8, \
         health => $9::text::jsonb, logs => $10::text::jsonb, runtime_name => $11)",
        &[
            &job.job_id,
            &config.node_name,
            &result.endpoint_url,
            &result.backend_name,
            &result.operator_name,
            &manifest,
            &result.compose_project,
            &result.work_dir.display().to_string(),
            &health,
            &logs,
            &result.runtime_name,
        ],
    )
    .context("marking Warren job complete")?;
    Ok(())
}

fn complete_lifecycle_job(
    db: &mut Client,
    config: &Config,
    job: &WarrenJob,
    metadata: &LifecycleDeploymentMetadata,
    health: &Value,
) -> Result<()> {
    let manifest = job.manifest.to_string();
    let health = health.to_string();
    let work_dir = metadata.work_dir.display().to_string();
    let logs = json!({
        "agent": "warren-agent",
        "action": job.desired_state,
        "deployment_id": metadata.deployment_id,
        "previous_status": metadata.status,
        "compose_project": metadata.compose_project,
        "work_dir": work_dir,
        "endpoint_url": metadata.endpoint_url,
        "backend_name": metadata.backend_name,
        "operator_name": metadata.operator_name,
        "runtime_name": metadata.runtime_name,
    })
    .to_string();
    db.execute(
        "SELECT rvbbit.complete_warren_job(\
         job_id => $1::text::uuid, node_name => $2, deployment_status => $3, \
         endpoint_url => $4, backend_name => $5, operator_name => $6, \
         deploy_manifest => $7::text::jsonb, compose_project => $8, work_dir => $9, \
         health => $10::text::jsonb, logs => $11::text::jsonb, runtime_name => $12)",
        &[
            &job.job_id,
            &config.node_name,
            &job.desired_state,
            &metadata.endpoint_url,
            &metadata.backend_name,
            &metadata.operator_name,
            &manifest,
            &metadata.compose_project,
            &work_dir,
            &health,
            &logs,
            &metadata.runtime_name,
        ],
    )
    .context("marking Warren lifecycle job complete")?;
    Ok(())
}

fn try_update_job_progress(
    db: &mut Client,
    config: &Config,
    job: &WarrenJob,
    phase: &str,
    progress: Value,
) {
    if let Err(err) = update_job_progress(db, config, job, phase, &progress) {
        eprintln!(
            "warning: failed to update Warren job progress job={} phase={}: {err:#}",
            job.job_id, phase
        );
    }
}

fn update_job_progress(
    db: &mut Client,
    config: &Config,
    job: &WarrenJob,
    phase: &str,
    progress: &Value,
) -> Result<()> {
    let progress = progress.to_string();
    db.execute(
        "SELECT rvbbit.update_warren_job_progress($1::text::uuid, $2, $3, $4::text::jsonb)",
        &[&job.job_id, &config.node_name, &phase, &progress],
    )
    .with_context(|| format!("updating Warren job progress phase={phase}"))?;
    Ok(())
}

fn fail_job(
    db: &mut Client,
    config: &Config,
    job: &WarrenJob,
    error: &str,
    logs: &Value,
) -> Result<()> {
    let logs = logs.to_string();
    db.execute(
        "SELECT rvbbit.fail_warren_job($1::text::uuid, $2, $3, $4::text::jsonb)",
        &[&job.job_id, &config.node_name, &error, &logs],
    )
    .context("marking Warren job failed")?;
    Ok(())
}

fn slugify(value: &str) -> String {
    let mut out = String::new();
    let mut prev_underscore = false;
    for ch in value.chars().flat_map(|c| c.to_lowercase()) {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
            prev_underscore = false;
        } else if !prev_underscore {
            out.push('_');
            prev_underscore = true;
        }
    }
    let out = out.trim_matches('_').to_string();
    if out.is_empty() {
        "capability".into()
    } else {
        out
    }
}

fn deterministic_port(seed: &str, base: u16) -> u16 {
    let mut hasher = Sha256::new();
    hasher.update(seed.as_bytes());
    let digest = hasher.finalize();
    let offset = u16::from_be_bytes([digest[0], digest[1]]) % 1000;
    base + offset
}

fn container_name(safe_name: &str) -> String {
    let service = safe_name.replace('_', "-");
    format!("rvbbit-{}", service.trim_matches('-'))
}

fn should_publish_host_port(config: &Config, manifest: &Value) -> bool {
    config.advertise_base_url.is_some() || manifest.pointer("/warren/port").is_some()
}

fn endpoint_for(config: &Config, manifest: &Value, port: u16, safe_name: &str) -> String {
    if let Some(endpoint) = manifest
        .pointer("/warren/endpoint_url")
        .and_then(Value::as_str)
    {
        return endpoint.to_string();
    }
    let endpoint_path = manifest
        .pointer("/warren/endpoint_path")
        .or_else(|| manifest.pointer("/runtime_registration/endpoint_path"))
        .and_then(Value::as_str)
        .unwrap_or("/predict");
    if let Some(base_url) = config.advertise_base_url.as_deref() {
        return format!(
            "{}:{}{}",
            base_url.trim_end_matches('/'),
            port,
            normalized_endpoint_path(endpoint_path)
        );
    }
    let service = safe_name.replace('_', "-");
    let container_port = runtime_container_port(manifest);
    format!(
        "http://rvbbit-{}:{}{}",
        service.trim_matches('-'),
        container_port,
        normalized_endpoint_path(endpoint_path)
    )
}

fn local_probe_endpoint_for(manifest: &Value, port: u16) -> String {
    let endpoint_path = manifest
        .pointer("/warren/endpoint_path")
        .or_else(|| manifest.pointer("/runtime_registration/endpoint_path"))
        .and_then(Value::as_str)
        .unwrap_or("/predict");
    format!(
        "http://127.0.0.1:{port}{}",
        normalized_endpoint_path(endpoint_path)
    )
}

fn local_runtime_probe_endpoint_for(manifest: &Value, port: u16) -> String {
    let probe_path = match runtime_language(manifest).as_str() {
        "python" => manifest
            .pointer("/warren/endpoint_path")
            .or_else(|| manifest.pointer("/runtime_registration/endpoint_path"))
            .and_then(Value::as_str)
            .unwrap_or("/run"),
        _ => runtime_health_path(manifest),
    };
    format!(
        "http://127.0.0.1:{port}{}",
        normalized_endpoint_path(probe_path)
    )
}

fn normalized_endpoint_path(path: &str) -> String {
    let trimmed = path.trim();
    if trimmed.starts_with('/') {
        trimmed.to_string()
    } else {
        format!("/{trimmed}")
    }
}

#[cfg(test)]
mod gpu_tests {
    use super::manifest_wants_gpu;
    use serde_json::json;

    #[test]
    fn wants_gpu_resolution() {
        // bge-reranker-v2-m3 shape: device auto + gpu placement declared -> wants GPU.
        assert!(manifest_wants_gpu(&json!({
            "runtime": {"device": "auto"},
            "resources": {"gpu": {"required": false, "placement": "single_gpu"}}
        })));
        // Explicit cuda -> wants GPU regardless of resources.
        assert!(manifest_wants_gpu(&json!({"runtime": {"device": "cuda"}})));
        // Explicit cpu -> never, even with a placement.
        assert!(!manifest_wants_gpu(&json!({
            "runtime": {"device": "cpu"},
            "resources": {"gpu": {"placement": "single_gpu"}}
        })));
        // auto with no GPU intent declared -> CPU model, don't force the card.
        assert!(!manifest_wants_gpu(&json!({"runtime": {"device": "auto"}})));
        assert!(!manifest_wants_gpu(&json!({})));
        // required: true alone is enough.
        assert!(manifest_wants_gpu(&json!({
            "resources": {"gpu": {"required": true}}
        })));
    }
}
