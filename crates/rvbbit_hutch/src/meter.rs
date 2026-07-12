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

pub struct MeterRow<'a> {
    pub tenant: &'a str,
    pub backend: &'a str,
    pub n_inputs: usize,
    /// HTTP status answered to the client.
    pub status: u16,
    /// Stable machine code for non-200s ("lanes_saturated", "invalid_key"…).
    pub error_code: Option<&'a str>,
    pub duration_ms: f64,
    pub upstream_ms: Option<f64>,
    pub model_version: &'a str,
    pub would_be_cost_microusd: i64,
    /// LLM calls only — parsed from the upstream's usage block.
    pub prompt_tokens: Option<i64>,
    pub completion_tokens: Option<i64>,
}

#[derive(Default)]
struct Counts {
    n: u64,
    duration_ms_sum: f64,
}

pub struct Meter {
    db: Mutex<Connection>,
    // (tenant, backend, code) → counts; code = "ok" or the error_code.
    counters: Mutex<HashMap<(String, String, String), Counts>>,
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
                would_be_cost_microusd INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS invocations_tenant_ts
                ON invocations (tenant, ts);",
        )
        .map_err(|e| format!("meter schema: {e}"))?;
        // Idempotent column adds for ledgers created before the LLM surface.
        for col in ["prompt_tokens", "completion_tokens"] {
            let _ = conn.execute(
                &format!("ALTER TABLE invocations ADD COLUMN {col} INTEGER"),
                [],
            );
        }
        Ok(Self {
            db: Mutex::new(conn),
            counters: Mutex::new(HashMap::new()),
        })
    }

    /// Best-effort: metering must never fail a request.
    pub fn record(&self, row: MeterRow<'_>) {
        {
            let code = row.error_code.unwrap_or("ok").to_string();
            let mut c = self.counters.lock().expect("meter counters poisoned");
            let e = c
                .entry((row.tenant.to_string(), row.backend.to_string(), code))
                .or_default();
            e.n += 1;
            e.duration_ms_sum += row.duration_ms;
        }
        let res = {
            let db = self.db.lock().expect("meter db poisoned");
            db.execute(
                "INSERT INTO invocations
                   (tenant, backend, n_inputs, status, error_code, duration_ms,
                    upstream_ms, model_version, would_be_cost_microusd,
                    prompt_tokens, completion_tokens)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                rusqlite::params![
                    row.tenant,
                    row.backend,
                    row.n_inputs as i64,
                    row.status as i64,
                    row.error_code,
                    row.duration_ms,
                    row.upstream_ms,
                    row.model_version,
                    row.would_be_cost_microusd,
                    row.prompt_tokens,
                    row.completion_tokens,
                ],
            )
        };
        if let Err(e) = res {
            tracing::warn!("meter insert failed (request unaffected): {e}");
        }
    }

    pub fn render_prometheus(&self, lanes: &[(String, usize, usize)]) -> String {
        let mut out = String::with_capacity(4096);
        out.push_str("# TYPE hutch_requests_total counter\n");
        out.push_str("# TYPE hutch_request_duration_ms_sum counter\n");
        {
            let c = self.counters.lock().expect("meter counters poisoned");
            for ((tenant, backend, code), v) in c.iter() {
                out.push_str(&format!(
                    "hutch_requests_total{{tenant=\"{tenant}\",backend=\"{backend}\",code=\"{code}\"}} {}\n",
                    v.n
                ));
                out.push_str(&format!(
                    "hutch_request_duration_ms_sum{{tenant=\"{tenant}\",backend=\"{backend}\",code=\"{code}\"}} {:.1}\n",
                    v.duration_ms_sum
                ));
            }
        }
        out.push_str("# TYPE hutch_lanes_in_flight gauge\n# TYPE hutch_lanes_max gauge\n");
        for (tenant, in_flight, max) in lanes {
            out.push_str(&format!(
                "hutch_lanes_in_flight{{tenant=\"{tenant}\"}} {in_flight}\nhutch_lanes_max{{tenant=\"{tenant}\"}} {max}\n"
            ));
        }
        out
    }
}
