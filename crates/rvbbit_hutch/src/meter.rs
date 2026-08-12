//! Metering — the receipt side of the house.
//!
//! HOUSE RULE, enforced here by construction: nothing a customer made is
//! ever persisted. The ledger records WHO/WHAT/HOW MANY/HOW LONG — never
//! payload content. There is deliberately no column that could hold an
//! input or output.
//!
//! Two sinks, one call:
//!   - SQLite (WAL) append-only ledger — the queryable truth for usage
//!     pages and Polar event export later.
//!   - In-memory counters rendered as Prometheus text at /metrics.

use rusqlite::Connection;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub struct MeterRow<'a> {
    /// Private correlation key joining one client invocation to any paid
    /// upstream attempts. It is deliberately never a Prometheus label.
    pub invocation_id: Option<&'a str>,
    pub tenant: &'a str,
    pub backend: &'a str,
    /// True only for OpenAI-compatible streaming LLM requests. Specialist
    /// calls and ordinary non-streaming LLM responses leave this false.
    pub stream: bool,
    pub n_inputs: usize,
    /// HTTP status answered to the client.
    pub status: u16,
    /// Stable machine code for non-200s ("lanes_saturated", "invalid_key"…).
    pub error_code: Option<&'a str>,
    pub duration_ms: f64,
    pub upstream_ms: Option<f64>,
    pub model_version: &'a str,
    /// Provider-neutral service tariff. nano-USD avoids rounding every small
    /// request; the legacy micro-USD metric remains as a compatibility view.
    pub reference_cost_nanousd: i64,
    /// LLM calls only — parsed from the upstream's usage block.
    pub prompt_tokens: Option<i64>,
    pub completion_tokens: Option<i64>,
}

pub struct ProviderAttempt<'a> {
    pub invocation_id: &'a str,
    pub tenant: &'a str,
    pub canonical_model: &'a str,
    pub provider: &'a str,
    pub upstream_model: &'a str,
    pub provider_request_id: Option<&'a str>,
    pub attempt_no: usize,
    pub mode: &'a str,
    pub status: u16,
    pub prompt_tokens: Option<i64>,
    pub completion_tokens: Option<i64>,
    /// Actual provider charge in nano-USD, when reported inline.
    pub provider_cost_nanousd: Option<i64>,
    pub cost_source: &'a str,
    /// True only when Hutch has a provider adapter capable of resolving a
    /// missing cost from provider_request_id.
    pub reconcile: bool,
}

#[derive(Debug, Clone)]
pub struct PendingProviderCost {
    pub row_id: i64,
    pub tenant: String,
    pub canonical_model: String,
    pub provider: String,
    pub upstream_model: String,
    pub provider_request_id: String,
}

pub struct ProviderPriceSnapshot<'a> {
    pub provider: &'a str,
    pub upstream_model: &'a str,
    pub prompt_usd_per_token: f64,
    pub completion_usd_per_token: f64,
    pub source: &'a str,
}

#[derive(Default)]
struct Counts {
    n: u64,
    duration_ms_sum: f64,
}

#[derive(Default)]
struct LlmCounts {
    n: u64,
    duration_ms_sum: f64,
    upstream_n: u64,
    upstream_ms_sum: f64,
    prompt_tokens: u64,
    completion_tokens: u64,
    reference_cost_nanousd: u64,
}

#[derive(Default)]
struct ProviderCostCounts {
    observations: u64,
    cost_nanousd: u64,
}

#[derive(Debug, Clone)]
struct PriceGauge {
    prompt_usd_per_token: f64,
    completion_usd_per_token: f64,
    source: String,
}

struct ProviderCostCoverage {
    tenant: String,
    canonical_model: String,
    provider: String,
    eligible_attempts: i64,
    final_attempts: i64,
    pending_attempts: i64,
}

pub struct Meter {
    db: Mutex<Connection>,
    // (tenant, backend, code) → counts; code = "ok" or the error_code.
    counters: Mutex<HashMap<(String, String, String), Counts>>,
    // (client id, canonical model, code, response mode) → LLM-only counters.
    // Every dimension is bounded by authenticated tenants or server config;
    // no caller-supplied model id reaches Prometheus labels.
    llm_counters: Mutex<HashMap<(String, String, String, String), LlmCounts>>,
    // (client id, canonical model, provider, source) → reported actual cost.
    provider_cost_counters: Mutex<HashMap<(String, String, String, String), ProviderCostCounts>>,
    // (provider, upstream model) → last-known advisory catalog price.
    provider_prices: Mutex<HashMap<(String, String), PriceGauge>>,
}

impl Meter {
    pub fn open(path: &str) -> Result<Self, String> {
        let conn = Connection::open(path).map_err(|e| format!("meter db '{path}': {e}"))?;
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| format!("meter WAL: {e}"))?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS invocations (
                id            INTEGER PRIMARY KEY,
                ts            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                tenant        TEXT NOT NULL,
                backend       TEXT NOT NULL,
                n_inputs      INTEGER NOT NULL,
                status        INTEGER NOT NULL,
                error_code    TEXT,
                duration_ms   REAL NOT NULL,
                upstream_ms   REAL,
                model_version TEXT NOT NULL,
                would_be_cost_microusd INTEGER NOT NULL DEFAULT 0,
                invocation_id TEXT,
                reference_cost_nanousd INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS invocations_tenant_ts
                ON invocations (tenant, ts);
            CREATE TABLE IF NOT EXISTS upstream_attempts (
                id                       INTEGER PRIMARY KEY,
                ts                       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                invocation_id            TEXT NOT NULL,
                tenant                   TEXT NOT NULL,
                canonical_model          TEXT NOT NULL,
                provider                 TEXT NOT NULL,
                upstream_model           TEXT NOT NULL,
                provider_request_id      TEXT,
                attempt_no               INTEGER NOT NULL,
                mode                     TEXT NOT NULL,
                status                   INTEGER NOT NULL,
                prompt_tokens            INTEGER,
                completion_tokens        INTEGER,
                provider_cost_nanousd    INTEGER,
                cost_source              TEXT NOT NULL,
                cost_status              TEXT NOT NULL,
                reconcile_attempts       INTEGER NOT NULL DEFAULT 0,
                next_reconcile_unix      INTEGER NOT NULL DEFAULT 0,
                reconcile_error          TEXT,
                reconciled_at            TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS upstream_attempts_invocation_attempt
                ON upstream_attempts (invocation_id, attempt_no);
            CREATE INDEX IF NOT EXISTS upstream_attempts_pending
                ON upstream_attempts (cost_status, next_reconcile_unix);
            CREATE INDEX IF NOT EXISTS upstream_attempts_provider_request
                ON upstream_attempts (provider, provider_request_id);
            CREATE TABLE IF NOT EXISTS provider_price_snapshots (
                id                       INTEGER PRIMARY KEY,
                captured_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                provider                 TEXT NOT NULL,
                upstream_model           TEXT NOT NULL,
                prompt_usd_per_token     REAL NOT NULL,
                completion_usd_per_token REAL NOT NULL,
                source                   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS provider_price_snapshots_model_ts
                ON provider_price_snapshots (provider, upstream_model, captured_at);",
        )
        .map_err(|e| format!("meter schema: {e}"))?;
        // Idempotent column adds for ledgers created before the LLM/cost
        // surfaces. SQLite reports duplicate-column errors; intentionally
        // ignore only those best-effort ALTER results.
        for (col, declaration) in [
            ("prompt_tokens", "INTEGER"),
            ("completion_tokens", "INTEGER"),
            ("invocation_id", "TEXT"),
            ("reference_cost_nanousd", "INTEGER NOT NULL DEFAULT 0"),
        ] {
            let _ = conn.execute(
                &format!("ALTER TABLE invocations ADD COLUMN {col} {declaration}"),
                [],
            );
        }
        let _ = conn.execute(
            "UPDATE invocations
             SET reference_cost_nanousd = would_be_cost_microusd * 1000
             WHERE reference_cost_nanousd = 0 AND would_be_cost_microusd <> 0",
            [],
        );
        conn.execute(
            "CREATE INDEX IF NOT EXISTS invocations_invocation_id
             ON invocations (invocation_id)",
            [],
        )
        .map_err(|e| format!("meter invocation index: {e}"))?;
        let mut provider_prices = HashMap::new();
        {
            let mut statement = conn
                .prepare(
                    "SELECT p.provider, p.upstream_model,
                            p.prompt_usd_per_token,
                            p.completion_usd_per_token, p.source
                     FROM provider_price_snapshots p
                     JOIN (
                         SELECT provider, upstream_model, max(id) AS id
                         FROM provider_price_snapshots
                         GROUP BY provider, upstream_model
                     ) latest ON latest.id = p.id",
                )
                .map_err(|e| format!("meter price cache query: {e}"))?;
            let rows = statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        PriceGauge {
                            prompt_usd_per_token: row.get(2)?,
                            completion_usd_per_token: row.get(3)?,
                            source: row.get(4)?,
                        },
                    ))
                })
                .map_err(|e| format!("meter price cache query: {e}"))?;
            for row in rows {
                let (provider, model, price) =
                    row.map_err(|e| format!("meter price cache row: {e}"))?;
                provider_prices.insert((provider, model), price);
            }
        }
        Ok(Self {
            db: Mutex::new(conn),
            counters: Mutex::new(HashMap::new()),
            llm_counters: Mutex::new(HashMap::new()),
            provider_cost_counters: Mutex::new(HashMap::new()),
            provider_prices: Mutex::new(provider_prices),
        })
    }

    /// Best-effort: metering must never fail a request.
    pub fn record(&self, row: MeterRow<'_>) {
        let code = row.error_code.unwrap_or("ok");
        {
            let mut c = self.counters.lock().expect("meter counters poisoned");
            let e = c
                .entry((
                    row.tenant.to_string(),
                    row.backend.to_string(),
                    code.to_string(),
                ))
                .or_default();
            e.n += 1;
            e.duration_ms_sum += row.duration_ms;
        }
        if let Some(model) = row.backend.strip_prefix("llm:") {
            let mode = if row.stream { "stream" } else { "response" };
            let mut counters = self
                .llm_counters
                .lock()
                .expect("LLM meter counters poisoned");
            let entry = counters
                .entry((
                    row.tenant.to_string(),
                    model.to_string(),
                    code.to_string(),
                    mode.to_string(),
                ))
                .or_default();
            entry.n = entry.n.saturating_add(1);
            entry.duration_ms_sum += row.duration_ms;
            if let Some(upstream_ms) = row.upstream_ms {
                entry.upstream_n = entry.upstream_n.saturating_add(1);
                entry.upstream_ms_sum += upstream_ms;
            }
            entry.prompt_tokens = entry
                .prompt_tokens
                .saturating_add(row.prompt_tokens.unwrap_or_default().max(0) as u64);
            entry.completion_tokens = entry
                .completion_tokens
                .saturating_add(row.completion_tokens.unwrap_or_default().max(0) as u64);
            entry.reference_cost_nanousd = entry
                .reference_cost_nanousd
                .saturating_add(row.reference_cost_nanousd.max(0) as u64);
        }
        let would_be_cost_microusd = row.reference_cost_nanousd.max(0) / 1000;
        let res = {
            let db = self.db.lock().expect("meter db poisoned");
            db.execute(
                "INSERT INTO invocations
                   (tenant, backend, n_inputs, status, error_code, duration_ms,
                    upstream_ms, model_version, would_be_cost_microusd,
                    prompt_tokens, completion_tokens, invocation_id,
                    reference_cost_nanousd)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                rusqlite::params![
                    row.tenant,
                    row.backend,
                    row.n_inputs as i64,
                    row.status as i64,
                    row.error_code,
                    row.duration_ms,
                    row.upstream_ms,
                    row.model_version,
                    would_be_cost_microusd,
                    row.prompt_tokens,
                    row.completion_tokens,
                    row.invocation_id,
                    row.reference_cost_nanousd,
                ],
            )
        };
        if let Err(e) = res {
            tracing::warn!("meter insert failed (request unaffected): {e}");
        }
    }

    /// Record one upstream attempt independently of the client invocation.
    /// A single Calliope request may incur multiple charges when a provider
    /// response is retried or routed through a fallback.
    pub fn record_provider_attempt(&self, attempt: ProviderAttempt<'_>) {
        let cost_status = if attempt.provider_cost_nanousd.is_some() {
            "final"
        } else if attempt.reconcile && attempt.provider_request_id.is_some() {
            "pending"
        } else {
            "unavailable"
        };
        let next_reconcile_unix = if cost_status == "pending" {
            unix_now().saturating_add(5)
        } else {
            0
        };
        let inserted = {
            let db = self.db.lock().expect("meter db poisoned");
            db.execute(
                "INSERT OR IGNORE INTO upstream_attempts
                   (invocation_id, tenant, canonical_model, provider,
                    upstream_model, provider_request_id, attempt_no, mode,
                    status, prompt_tokens, completion_tokens,
                    provider_cost_nanousd, cost_source, cost_status,
                    next_reconcile_unix)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11,
                         ?12, ?13, ?14, ?15)",
                rusqlite::params![
                    attempt.invocation_id,
                    attempt.tenant,
                    attempt.canonical_model,
                    attempt.provider,
                    attempt.upstream_model,
                    attempt.provider_request_id,
                    attempt.attempt_no as i64,
                    attempt.mode,
                    attempt.status as i64,
                    attempt.prompt_tokens,
                    attempt.completion_tokens,
                    attempt.provider_cost_nanousd,
                    attempt.cost_source,
                    cost_status,
                    next_reconcile_unix,
                ],
            )
        };
        match inserted {
            Ok(1) => {
                if let Some(cost) = attempt.provider_cost_nanousd {
                    self.add_provider_cost_counter(
                        attempt.tenant,
                        attempt.canonical_model,
                        attempt.provider,
                        attempt.cost_source,
                        cost,
                    );
                }
            }
            Ok(_) => {}
            Err(error) => tracing::warn!("provider attempt insert failed: {error}"),
        }
    }

    pub fn pending_provider_costs(&self, limit: usize) -> Vec<PendingProviderCost> {
        let now = unix_now();
        let db = self.db.lock().expect("meter db poisoned");
        let mut statement = match db.prepare(
            "SELECT id, tenant, canonical_model, provider, upstream_model,
                    provider_request_id
             FROM upstream_attempts
             WHERE cost_status = 'pending'
               AND provider_request_id IS NOT NULL
               AND next_reconcile_unix <= ?1
               AND reconcile_attempts < 10
             ORDER BY id
             LIMIT ?2",
        ) {
            Ok(statement) => statement,
            Err(error) => {
                tracing::warn!("provider reconciliation query failed: {error}");
                return Vec::new();
            }
        };
        let rows = match statement.query_map(rusqlite::params![now, limit as i64], |row| {
            Ok(PendingProviderCost {
                row_id: row.get(0)?,
                tenant: row.get(1)?,
                canonical_model: row.get(2)?,
                provider: row.get(3)?,
                upstream_model: row.get(4)?,
                provider_request_id: row.get(5)?,
            })
        }) {
            Ok(rows) => rows,
            Err(error) => {
                tracing::warn!("provider reconciliation query failed: {error}");
                return Vec::new();
            }
        };
        rows.filter_map(Result::ok).collect()
    }

    pub fn reconcile_provider_cost(
        &self,
        pending: &PendingProviderCost,
        prompt_tokens: Option<i64>,
        completion_tokens: Option<i64>,
        provider_cost_nanousd: i64,
    ) {
        let updated = {
            let db = self.db.lock().expect("meter db poisoned");
            db.execute(
                "UPDATE upstream_attempts
                 SET provider_cost_nanousd = ?2,
                     prompt_tokens = COALESCE(?3, prompt_tokens),
                     completion_tokens = COALESCE(?4, completion_tokens),
                     cost_source = 'reconciled', cost_status = 'final',
                     reconciled_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                     reconcile_error = NULL
                 WHERE id = ?1 AND provider_cost_nanousd IS NULL",
                rusqlite::params![
                    pending.row_id,
                    provider_cost_nanousd,
                    prompt_tokens,
                    completion_tokens,
                ],
            )
        };
        match updated {
            Ok(1) => self.add_provider_cost_counter(
                &pending.tenant,
                &pending.canonical_model,
                &pending.provider,
                "reconciled",
                provider_cost_nanousd,
            ),
            Ok(_) => {}
            Err(error) => tracing::warn!("provider reconciliation update failed: {error}"),
        }
    }

    pub fn defer_provider_cost(&self, row_id: i64, detail: &str) {
        let db = self.db.lock().expect("meter db poisoned");
        let attempts: i64 = db
            .query_row(
                "SELECT reconcile_attempts FROM upstream_attempts WHERE id = ?1",
                [row_id],
                |row| row.get(0),
            )
            .unwrap_or(0);
        let delay = (5_i64.saturating_mul(1_i64 << attempts.min(9))).min(3600);
        let status = if attempts >= 9 {
            "unavailable"
        } else {
            "pending"
        };
        let bounded: String = detail.chars().take(300).collect();
        let _ = db.execute(
            "UPDATE upstream_attempts
             SET reconcile_attempts = reconcile_attempts + 1,
                 next_reconcile_unix = ?2,
                 reconcile_error = ?3,
                 cost_status = ?4
             WHERE id = ?1 AND provider_cost_nanousd IS NULL",
            rusqlite::params![row_id, unix_now().saturating_add(delay), bounded, status],
        );
    }

    pub fn record_provider_price(&self, snapshot: ProviderPriceSnapshot<'_>) {
        {
            let mut gauges = self
                .provider_prices
                .lock()
                .expect("provider price gauges poisoned");
            gauges.insert(
                (
                    snapshot.provider.to_string(),
                    snapshot.upstream_model.to_string(),
                ),
                PriceGauge {
                    prompt_usd_per_token: snapshot.prompt_usd_per_token,
                    completion_usd_per_token: snapshot.completion_usd_per_token,
                    source: snapshot.source.to_string(),
                },
            );
        }
        let db = self.db.lock().expect("meter db poisoned");
        if let Err(error) = db.execute(
            "INSERT INTO provider_price_snapshots
               (provider, upstream_model, prompt_usd_per_token,
                completion_usd_per_token, source)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                snapshot.provider,
                snapshot.upstream_model,
                snapshot.prompt_usd_per_token,
                snapshot.completion_usd_per_token,
                snapshot.source,
            ],
        ) {
            tracing::warn!("provider price snapshot insert failed: {error}");
        }
    }

    fn add_provider_cost_counter(
        &self,
        tenant: &str,
        canonical_model: &str,
        provider: &str,
        source: &str,
        cost_nanousd: i64,
    ) {
        let mut counters = self
            .provider_cost_counters
            .lock()
            .expect("provider cost counters poisoned");
        let entry = counters
            .entry((
                tenant.to_string(),
                canonical_model.to_string(),
                provider.to_string(),
                source.to_string(),
            ))
            .or_default();
        entry.observations = entry.observations.saturating_add(1);
        entry.cost_nanousd = entry
            .cost_nanousd
            .saturating_add(cost_nanousd.max(0) as u64);
    }

    /// Read coverage from the durable attempt ledger rather than process-local
    /// counters. This keeps the signal truthful across Hutch restarts and when
    /// a pending request is reconciled by a later process.
    fn provider_cost_coverage(&self) -> Vec<ProviderCostCoverage> {
        let db = self.db.lock().expect("meter db poisoned");
        let mut statement = match db.prepare(
            "SELECT tenant, canonical_model, provider,
                    sum(CASE
                          WHEN provider_cost_nanousd IS NOT NULL
                            OR provider_request_id IS NOT NULL
                            OR status BETWEEN 200 AND 299
                          THEN 1 ELSE 0 END) AS eligible_attempts,
                    sum(CASE WHEN provider_cost_nanousd IS NOT NULL
                             THEN 1 ELSE 0 END) AS final_attempts,
                    sum(CASE WHEN cost_status = 'pending'
                             THEN 1 ELSE 0 END) AS pending_attempts
             FROM upstream_attempts
             GROUP BY tenant, canonical_model, provider",
        ) {
            Ok(statement) => statement,
            Err(error) => {
                tracing::warn!("provider coverage query failed: {error}");
                return Vec::new();
            }
        };
        let rows = match statement.query_map([], |row| {
            Ok(ProviderCostCoverage {
                tenant: row.get(0)?,
                canonical_model: row.get(1)?,
                provider: row.get(2)?,
                eligible_attempts: row.get(3)?,
                final_attempts: row.get(4)?,
                pending_attempts: row.get(5)?,
            })
        }) {
            Ok(rows) => rows,
            Err(error) => {
                tracing::warn!("provider coverage query failed: {error}");
                return Vec::new();
            }
        };
        rows.filter_map(|row| match row {
            Ok(coverage) => Some(coverage),
            Err(error) => {
                tracing::warn!("provider coverage row failed: {error}");
                None
            }
        })
        .collect()
    }

    pub fn render_prometheus(&self, lanes: &[(String, usize, usize)]) -> String {
        let mut out = String::with_capacity(4096);
        out.push_str("# TYPE hutch_requests_total counter\n");
        out.push_str("# TYPE hutch_request_duration_ms_sum counter\n");
        {
            let c = self.counters.lock().expect("meter counters poisoned");
            for ((tenant, backend, code), v) in c.iter() {
                let client_id = prometheus_label(tenant);
                let backend = prometheus_label(backend);
                let code = prometheus_label(code);
                out.push_str(&format!(
                    "hutch_requests_total{{tenant=\"{client_id}\",client_id=\"{client_id}\",backend=\"{backend}\",code=\"{code}\"}} {}\n",
                    v.n
                ));
                out.push_str(&format!(
                    "hutch_request_duration_ms_sum{{tenant=\"{client_id}\",client_id=\"{client_id}\",backend=\"{backend}\",code=\"{code}\"}} {:.1}\n",
                    v.duration_ms_sum
                ));
            }
        }
        out.push_str(
            "# HELP hutch_llm_requests_total Authenticated OpenAI-compatible LLM passthrough requests.\n\
# TYPE hutch_llm_requests_total counter\n\
# HELP hutch_llm_request_duration_seconds Time from accepted client request to upstream response readiness.\n\
# TYPE hutch_llm_request_duration_seconds summary\n\
# HELP hutch_llm_upstream_duration_seconds Measured upstream LLM request time when available.\n\
# TYPE hutch_llm_upstream_duration_seconds summary\n\
# HELP hutch_llm_prompt_tokens_total Prompt tokens reported by non-streaming upstream responses.\n\
# TYPE hutch_llm_prompt_tokens_total counter\n\
# HELP hutch_llm_completion_tokens_total Completion tokens reported by non-streaming upstream responses.\n\
# TYPE hutch_llm_completion_tokens_total counter\n\
# HELP hutch_llm_would_be_cost_microusd_total Configured reference cost in micro-USD for metered tokens.\n\
# TYPE hutch_llm_would_be_cost_microusd_total counter\n\
# HELP hutch_llm_reference_cost_usd_total Provider-neutral reference tariff applied to metered tokens.\n\
# TYPE hutch_llm_reference_cost_usd_total counter\n",
        );
        {
            let counters = self
                .llm_counters
                .lock()
                .expect("LLM meter counters poisoned");
            for ((client_id, model, code, mode), counts) in counters.iter() {
                let labels = format!(
                    "client_id=\"{}\",model=\"{}\",code=\"{}\",mode=\"{}\"",
                    prometheus_label(client_id),
                    prometheus_label(model),
                    prometheus_label(code),
                    prometheus_label(mode),
                );
                out.push_str(&format!(
                    "hutch_llm_requests_total{{{labels}}} {}\n",
                    counts.n
                ));
                out.push_str(&format!(
                    "hutch_llm_request_duration_seconds_sum{{{labels}}} {:.6}\n\
hutch_llm_request_duration_seconds_count{{{labels}}} {}\n",
                    counts.duration_ms_sum / 1000.0,
                    counts.n,
                ));
                out.push_str(&format!(
                    "hutch_llm_upstream_duration_seconds_sum{{{labels}}} {:.6}\n\
hutch_llm_upstream_duration_seconds_count{{{labels}}} {}\n",
                    counts.upstream_ms_sum / 1000.0,
                    counts.upstream_n,
                ));
                out.push_str(&format!(
                    "hutch_llm_prompt_tokens_total{{{labels}}} {}\n\
hutch_llm_completion_tokens_total{{{labels}}} {}\n\
hutch_llm_would_be_cost_microusd_total{{{labels}}} {:.3}\n\
hutch_llm_reference_cost_usd_total{{{labels}}} {:.9}\n",
                    counts.prompt_tokens,
                    counts.completion_tokens,
                    counts.reference_cost_nanousd as f64 / 1_000.0,
                    counts.reference_cost_nanousd as f64 / 1_000_000_000.0,
                ));
            }
        }
        out.push_str(
            "# HELP hutch_llm_provider_cost_usd_total Actual cost reported by the upstream provider.\n\
# TYPE hutch_llm_provider_cost_usd_total counter\n\
# HELP hutch_llm_provider_cost_observations_total Upstream attempts with a final provider cost.\n\
# TYPE hutch_llm_provider_cost_observations_total counter\n",
        );
        {
            let counters = self
                .provider_cost_counters
                .lock()
                .expect("provider cost counters poisoned");
            for ((client_id, model, provider, source), counts) in counters.iter() {
                let labels = format!(
                    "client_id=\"{}\",model=\"{}\",provider=\"{}\",source=\"{}\"",
                    prometheus_label(client_id),
                    prometheus_label(model),
                    prometheus_label(provider),
                    prometheus_label(source),
                );
                out.push_str(&format!(
                    "hutch_llm_provider_cost_usd_total{{{labels}}} {:.9}\n\
hutch_llm_provider_cost_observations_total{{{labels}}} {}\n",
                    counts.cost_nanousd as f64 / 1_000_000_000.0,
                    counts.observations,
                ));
            }
        }
        let pending = {
            let db = self.db.lock().expect("meter db poisoned");
            db.query_row(
                "SELECT count(*) FROM upstream_attempts WHERE cost_status = 'pending'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap_or(0)
        };
        out.push_str(
            "# HELP hutch_llm_provider_cost_pending Upstream attempts awaiting provider cost reconciliation.\n\
# TYPE hutch_llm_provider_cost_pending gauge\n",
        );
        out.push_str(&format!("hutch_llm_provider_cost_pending {pending}\n"));
        out.push_str(
            "# HELP hutch_llm_provider_cost_eligible_attempts Charge-bearing or potentially charge-bearing attempts in the durable ledger.\n\
# TYPE hutch_llm_provider_cost_eligible_attempts gauge\n\
# HELP hutch_llm_provider_cost_final_attempts Eligible attempts with a final provider cost in the durable ledger.\n\
# TYPE hutch_llm_provider_cost_final_attempts gauge\n\
# HELP hutch_llm_provider_cost_pending_attempts Attempts still awaiting cost reconciliation, partitioned by client and model.\n\
# TYPE hutch_llm_provider_cost_pending_attempts gauge\n\
# HELP hutch_llm_provider_cost_coverage_ratio Fraction of eligible attempts with a final provider cost.\n\
# TYPE hutch_llm_provider_cost_coverage_ratio gauge\n",
        );
        for coverage in self.provider_cost_coverage() {
            let labels = format!(
                "client_id=\"{}\",model=\"{}\",provider=\"{}\"",
                prometheus_label(&coverage.tenant),
                prometheus_label(&coverage.canonical_model),
                prometheus_label(&coverage.provider),
            );
            let ratio = if coverage.eligible_attempts > 0 {
                coverage.final_attempts as f64 / coverage.eligible_attempts as f64
            } else {
                1.0
            };
            out.push_str(&format!(
                "hutch_llm_provider_cost_eligible_attempts{{{labels}}} {}\n\
hutch_llm_provider_cost_final_attempts{{{labels}}} {}\n\
hutch_llm_provider_cost_pending_attempts{{{labels}}} {}\n\
hutch_llm_provider_cost_coverage_ratio{{{labels}}} {ratio:.9}\n",
                coverage.eligible_attempts, coverage.final_attempts, coverage.pending_attempts,
            ));
        }
        out.push_str(
            "# HELP hutch_llm_catalog_prompt_usd_per_million Advisory provider catalog prompt price.\n\
# TYPE hutch_llm_catalog_prompt_usd_per_million gauge\n\
# HELP hutch_llm_catalog_completion_usd_per_million Advisory provider catalog completion price.\n\
# TYPE hutch_llm_catalog_completion_usd_per_million gauge\n",
        );
        {
            let prices = self
                .provider_prices
                .lock()
                .expect("provider price gauges poisoned");
            for ((provider, upstream_model), price) in prices.iter() {
                let labels = format!(
                    "provider=\"{}\",upstream_model=\"{}\",source=\"{}\"",
                    prometheus_label(provider),
                    prometheus_label(upstream_model),
                    prometheus_label(&price.source),
                );
                out.push_str(&format!(
                    "hutch_llm_catalog_prompt_usd_per_million{{{labels}}} {:.9}\n\
hutch_llm_catalog_completion_usd_per_million{{{labels}}} {:.9}\n",
                    price.prompt_usd_per_token * 1_000_000.0,
                    price.completion_usd_per_token * 1_000_000.0,
                ));
            }
        }
        out.push_str("# TYPE hutch_lanes_in_flight gauge\n# TYPE hutch_lanes_max gauge\n");
        for (tenant, in_flight, max) in lanes {
            let client_id = prometheus_label(tenant);
            out.push_str(&format!(
                "hutch_lanes_in_flight{{tenant=\"{client_id}\",client_id=\"{client_id}\"}} {in_flight}\nhutch_lanes_max{{tenant=\"{client_id}\",client_id=\"{client_id}\"}} {max}\n"
            ));
        }
        out
    }
}

fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs().min(i64::MAX as u64) as i64)
        .unwrap_or(0)
}

fn prometheus_label(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            other => escaped.push(other),
        }
    }
    escaped
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_client_labeled_llm_passthrough_metrics() {
        let meter = Meter::open(":memory:").expect("in-memory meter");
        meter.record(MeterRow {
            invocation_id: Some("invocation-response"),
            tenant: "client\\\"one",
            backend: "llm:calliope",
            stream: false,
            n_inputs: 1,
            status: 200,
            error_code: None,
            duration_ms: 1250.0,
            upstream_ms: Some(1200.0),
            model_version: "openrouter/openai/gpt-5.6-luna",
            reference_cost_nanousd: 4_321_000,
            prompt_tokens: Some(120),
            completion_tokens: Some(45),
        });
        meter.record(MeterRow {
            invocation_id: Some("invocation-stream"),
            tenant: "client\\\"one",
            backend: "llm:calliope",
            stream: true,
            n_inputs: 1,
            status: 200,
            error_code: None,
            duration_ms: 500.0,
            upstream_ms: Some(500.0),
            model_version: "openrouter/openai/gpt-5.6-luna",
            reference_cost_nanousd: 0,
            prompt_tokens: None,
            completion_tokens: None,
        });
        meter.record_provider_attempt(ProviderAttempt {
            invocation_id: "invocation-response",
            tenant: "client\\\"one",
            canonical_model: "calliope",
            provider: "openrouter",
            upstream_model: "openai/gpt-5.6-luna",
            provider_request_id: Some("gen-inline"),
            attempt_no: 1,
            mode: "response",
            status: 200,
            prompt_tokens: Some(120),
            completion_tokens: Some(45),
            provider_cost_nanousd: Some(1_500_000),
            cost_source: "inline_response",
            reconcile: true,
        });
        meter.record_provider_price(ProviderPriceSnapshot {
            provider: "openrouter",
            upstream_model: "openai/gpt-5.6-luna",
            prompt_usd_per_token: 0.000001,
            completion_usd_per_token: 0.000006,
            source: "openrouter-models",
        });

        let rendered = meter.render_prometheus(&[("client\\\"one".into(), 1, 4)]);
        assert!(rendered.contains(
            "hutch_llm_requests_total{client_id=\"client\\\\\\\"one\",model=\"calliope\",code=\"ok\",mode=\"response\"} 1"
        ));
        assert!(rendered.contains(
            "hutch_llm_request_duration_seconds_sum{client_id=\"client\\\\\\\"one\",model=\"calliope\",code=\"ok\",mode=\"response\"} 1.250000"
        ));
        assert!(rendered.contains(
            "hutch_llm_prompt_tokens_total{client_id=\"client\\\\\\\"one\",model=\"calliope\",code=\"ok\",mode=\"response\"} 120"
        ));
        assert!(rendered.contains(
            "hutch_llm_requests_total{client_id=\"client\\\\\\\"one\",model=\"calliope\",code=\"ok\",mode=\"stream\"} 1"
        ));
        assert!(rendered.contains(
            "hutch_requests_total{tenant=\"client\\\\\\\"one\",client_id=\"client\\\\\\\"one\",backend=\"llm:calliope\",code=\"ok\"} 2"
        ));
        assert!(rendered.contains(
            "hutch_lanes_max{tenant=\"client\\\\\\\"one\",client_id=\"client\\\\\\\"one\"} 4"
        ));
        assert!(rendered.contains(
            "hutch_llm_reference_cost_usd_total{client_id=\"client\\\\\\\"one\",model=\"calliope\",code=\"ok\",mode=\"response\"} 0.004321000"
        ));
        assert!(rendered.contains(
            "hutch_llm_provider_cost_usd_total{client_id=\"client\\\\\\\"one\",model=\"calliope\",provider=\"openrouter\",source=\"inline_response\"} 0.001500000"
        ));
        assert!(rendered.contains(
            "hutch_llm_provider_cost_coverage_ratio{client_id=\"client\\\\\\\"one\",model=\"calliope\",provider=\"openrouter\"} 1.000000000"
        ));
        assert!(rendered.contains(
            "hutch_llm_catalog_completion_usd_per_million{provider=\"openrouter\",upstream_model=\"openai/gpt-5.6-luna\",source=\"openrouter-models\"} 6.000000000"
        ));
    }

    #[test]
    fn pending_provider_cost_reconciles_exactly_once() {
        let meter = Meter::open(":memory:").expect("in-memory meter");
        meter.record_provider_attempt(ProviderAttempt {
            invocation_id: "invocation-pending",
            tenant: "client-one",
            canonical_model: "calliope",
            provider: "openrouter",
            upstream_model: "openai/gpt-5.6-luna",
            provider_request_id: Some("gen-pending"),
            attempt_no: 1,
            mode: "stream",
            status: 499,
            prompt_tokens: None,
            completion_tokens: None,
            provider_cost_nanousd: None,
            cost_source: "pending",
            reconcile: true,
        });
        {
            let db = meter.db.lock().expect("meter db");
            db.execute("UPDATE upstream_attempts SET next_reconcile_unix = 0", [])
                .unwrap();
        }
        let pending = meter.pending_provider_costs(10);
        assert_eq!(pending.len(), 1);
        meter.reconcile_provider_cost(&pending[0], Some(100), Some(20), 777_000);
        meter.reconcile_provider_cost(&pending[0], Some(100), Some(20), 777_000);

        let rendered = meter.render_prometheus(&[]);
        assert!(rendered.contains("hutch_llm_provider_cost_pending 0"));
        assert!(rendered.contains(
            "hutch_llm_provider_cost_usd_total{client_id=\"client-one\",model=\"calliope\",provider=\"openrouter\",source=\"reconciled\"} 0.000777000"
        ));
        assert!(rendered.contains(
            "hutch_llm_provider_cost_observations_total{client_id=\"client-one\",model=\"calliope\",provider=\"openrouter\",source=\"reconciled\"} 1"
        ));
        assert!(rendered.contains(
            "hutch_llm_provider_cost_coverage_ratio{client_id=\"client-one\",model=\"calliope\",provider=\"openrouter\"} 1.000000000"
        ));
    }

    #[test]
    fn existing_meter_ledger_migrates_without_losing_reference_cost() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("hutch-meter-upgrade-{unique}.sqlite"));
        {
            let old = Connection::open(&path).unwrap();
            old.execute_batch(
                "CREATE TABLE invocations (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    tenant TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    n_inputs INTEGER NOT NULL,
                    status INTEGER NOT NULL,
                    error_code TEXT,
                    duration_ms REAL NOT NULL,
                    upstream_ms REAL,
                    model_version TEXT NOT NULL,
                    would_be_cost_microusd INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO invocations
                    (tenant, backend, n_inputs, status, duration_ms,
                     model_version, would_be_cost_microusd)
                VALUES ('old-client', 'llm:clover', 1, 200, 10,
                        'old-model', 12);",
            )
            .unwrap();
        }
        let meter = Meter::open(path.to_str().unwrap()).expect("upgrade old ledger");
        let migrated: i64 = meter
            .db
            .lock()
            .unwrap()
            .query_row(
                "SELECT reference_cost_nanousd FROM invocations WHERE tenant='old-client'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(migrated, 12_000);
        drop(meter);
        let _ = std::fs::remove_file(path);
    }
}
