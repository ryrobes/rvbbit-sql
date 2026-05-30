use anyhow::{anyhow, bail, Context, Result};
use postgres::{Client, NoTls};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
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
    backend_name: Option<String>,
    operator_name: Option<String>,
    runtime_name: Option<String>,
    compose_project: String,
    work_dir: PathBuf,
    health: Value,
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
    println!(
        "warren-agent {} registered node={} work_dir={}",
        VERSION,
        config.node_name,
        config.work_dir.display()
    );

    loop {
        heartbeat(&mut db, &config, "ready")?;
        maybe_record_metrics(&mut db, &config, &mut metrics, &mut last_metrics_at);
        match claim_job(&mut db, &config)? {
            Some(job) => {
                println!(
                    "claimed job={} kind={} name={} desired={}",
                    job.job_id, job.kind, job.name, job.desired_state
                );
                heartbeat(&mut db, &config, "busy")?;
                maybe_record_metrics(&mut db, &config, &mut metrics, &mut last_metrics_at);
                if let Err(err) = process_job(&mut db, &config, &job) {
                    eprintln!("job {} failed: {err:#}", job.job_id);
                    let logs = json!({"error": err.to_string(), "agent": "warren-agent"});
                    fail_job(&mut db, &config, &job, &err.to_string(), &logs)?;
                }
                try_record_metrics(&mut db, &config, &mut metrics);
                last_metrics_at = Instant::now();
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
            labels: json!({"docker": true, "capability": true}),
            capacity: json!({}),
            port_base: env::var("WARREN_PORT_BASE")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(8300),
            metrics_ms: env::var("WARREN_METRICS_MS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(10_000),
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
                "--labels" => {
                    config.labels = serde_json::from_str(&take_arg(&mut args, "--labels")?)?
                }
                "--capacity" => {
                    config.capacity = serde_json::from_str(&take_arg(&mut args, "--capacity")?)?
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

        if !config.labels.is_object() {
            bail!("--labels must be a JSON object");
        }
        if !config.capacity.is_object() {
            bail!("--capacity must be a JSON object");
        }
        Ok(config)
    }
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
           --poll-ms <ms>                     Poll interval\n\
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

fn process_job(db: &mut Client, config: &Config, job: &WarrenJob) -> Result<()> {
    if job.desired_state != "running" {
        bail!(
            "desired_state {:?} is not implemented yet",
            job.desired_state
        );
    }

    let mut result = match job.kind.as_str() {
        "capability" | "trained_model" => deploy_capability(config, job)?,
        other => bail!("Warren job kind {other:?} is not implemented yet"),
    };

    if is_runtime_sidecar(&job.manifest) {
        if !config.dry_run {
            let probe = probe_runtime(&job.manifest, &result)?;
            if !probe.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                bail!("runtime probe failed after deployment: {probe}");
            }
            if let Some(obj) = result.health.as_object_mut() {
                obj.insert("runtime_probe".into(), probe);
            }
        }
        register_runtime(db, &job.manifest, &result)?;
    } else {
        register_backend_and_operators(db, &job.manifest, &result)?;
        if !config.dry_run {
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

fn deploy_capability(config: &Config, job: &WarrenJob) -> Result<DeploymentResult> {
    let manifest = &job.manifest;
    let pack_name = manifest
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(&job.name);
    let safe_name = slugify(pack_name);
    let project_dir = config.work_dir.join(&safe_name);
    if project_dir.exists() {
        if !config.dry_run && project_dir.join("compose.yaml").exists() {
            docker_compose_down(&project_dir)?;
        }
        fs::remove_dir_all(&project_dir)
            .with_context(|| format!("clearing {}", project_dir.display()))?;
    }
    fs::create_dir_all(&project_dir)
        .with_context(|| format!("creating {}", project_dir.display()))?;

    scaffold_project(config, manifest, &project_dir, &safe_name)?;

    let port = manifest
        .pointer("/warren/port")
        .and_then(Value::as_u64)
        .map(|v| v as u16)
        .unwrap_or_else(|| deterministic_port(&job.job_id, config.port_base));
    let endpoint_url = endpoint_for(config, manifest, port, &safe_name);
    let probe_url = local_probe_endpoint_for(manifest, port);

    if config.dry_run {
        println!(
            "dry-run: not starting Docker project {} on port {}",
            safe_name, port
        );
    } else {
        docker_compose_up(&project_dir, port, &config.docker_network)?;
        wait_for_health(port, Duration::from_secs(180))?;
    }

    let health = json!({
        "ok": !config.dry_run,
        "dry_run": config.dry_run,
        "port": port,
        "target_selector": job.target_selector,
    });
    Ok(DeploymentResult {
        endpoint_url,
        probe_url,
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
    for entry in fs::read_dir(&template_dir)
        .with_context(|| format!("reading template dir {}", template_dir.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let text = render_tokens(
            &fs::read_to_string(&path)
                .with_context(|| format!("reading template file {}", path.display()))?,
            &values,
        );
        fs::write(out_dir.join(entry.file_name()), text)?;
    }
    let requirements_path = out_dir.join("requirements.txt");
    if requirements_path.exists() {
        let requirements = render_requirements(
            &fs::read_to_string(&requirements_path).context("reading rendered requirements")?,
            handler,
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

fn render_requirements(base: &str, handler: &str) -> String {
    let mut lines: Vec<String> = base.lines().map(str::to_string).collect();
    if matches!(
        handler,
        "embedding" | "sequence_classification" | "zero_shot_classification" | "gliner"
    ) {
        for dep in ["transformers==4.46.3", "sentencepiece==0.2.0"] {
            if !lines.iter().any(|line| line == dep) {
                lines.push(dep.into());
            }
        }
    }
    if matches!(handler, "tabular_classification" | "tabular_regression") {
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
    lines.push(String::new());
    lines.join("\n")
}

fn render_compose(manifest: &Value, safe_name: &str) -> Result<String> {
    let service = safe_name.replace('_', "-");
    let source = manifest.get("source").unwrap_or(&Value::Null);
    let runtime = manifest.get("runtime").unwrap_or(&Value::Null);
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

    Ok(format!(
        "services:\n  {service}:\n    build: .\n    container_name: rvbbit-{service}\n    ports:\n      - \"${{RVBBIT_CAPABILITY_PORT:-8080}}:8080\"\n    environment:\n{env_yaml}\n    volumes:\n{volume_mounts}\n    networks:\n      - rvbbit\n    healthcheck:\n      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health').read()\"]\n      interval: 10s\n      timeout: 5s\n      retries: 60\n\nnetworks:\n  rvbbit:\n    name: ${{RVBBIT_DOCKER_NETWORK:-docker_default}}\n    external: true\n\nvolumes:\n{volume_defs}\n"
    ))
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

fn docker_compose_up(project_dir: &Path, port: u16, network: &str) -> Result<()> {
    let status = Command::new("docker")
        .arg("compose")
        .arg("up")
        .arg("-d")
        .arg("--build")
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

fn wait_for_health(port: u16, timeout: Duration) -> Result<()> {
    let url = format!("http://127.0.0.1:{port}/health");
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

    if let Some(operators) = manifest.get("operators").and_then(Value::as_array) {
        for op in operators {
            register_operator(db, manifest, backend_name, op)?;
        }
    }

    db.execute("SELECT rvbbit.reload_backends()", &[])
        .context("reloading backend cache")?;
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
    if language != "python" {
        bail!("runtime sidecar language {language:?} is not supported by this agent yet");
    }
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
    Ok(())
}

fn probe_runtime(manifest: &Value, result: &DeploymentResult) -> Result<Value> {
    let language = runtime_language(manifest);
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
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;
    let resp = client
        .post(&result.probe_url)
        .json(&payload)
        .send()
        .with_context(|| format!("probing runtime {}", result.probe_url))?;
    let status = resp.status();
    let body = resp.text().context("reading runtime probe response")?;
    if !status.is_success() {
        bail!("runtime probe returned HTTP {}: {}", status.as_u16(), body);
    }
    serde_json::from_str(&body).context("parsing runtime probe response")
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

    db.execute(
        "SELECT rvbbit.create_operator(\
         op_name => $1, op_arg_names => $2, op_arg_types => $3, \
         op_return_type => $4, op_parser => $5, op_shape => $6, \
         op_description => $7, op_tests => $8::text::jsonb, op_steps => $9::text::jsonb)",
        &[
            &op_name,
            &arg_names,
            &arg_types,
            &return_type,
            &parser,
            &shape,
            &description,
            &tests_json,
            &steps_json,
        ],
    )
    .with_context(|| format!("registering operator {op_name}"))?;
    Ok(())
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
    let logs = json!({"agent": "warren-agent"}).to_string();
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
    format!(
        "http://rvbbit-{}:8080{}",
        service.trim_matches('-'),
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

fn normalized_endpoint_path(path: &str) -> String {
    let trimmed = path.trim();
    if trimmed.starts_with('/') {
        trimmed.to_string()
    } else {
        format!("/{trimmed}")
    }
}
