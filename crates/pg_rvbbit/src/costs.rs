//! Cost ledger + delayed receipt queue.
//!
//! `rvbbit.receipts` remains the semantic cache and primary audit artifact.
//! This module adds two production-shaped pieces around it:
//!   1. append-only `rvbbit.cost_events`, where actual/estimated/pending
//!      provider costs can settle over time without mutating receipts; and
//!   2. a small file-backed receipt queue for contexts where Postgres forbids
//!      SPI INSERTs (parallel workers / flow pool threads).

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use pgrx::extension_sql;
use pgrx::prelude::*;
use pgrx::Spi;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::unit_of_work::{OpDef, WorkResult};

extension_sql!(
    r#"
-- Append-only cost ledger. A single provider call may produce multiple
-- events over time: pending -> settled, or estimated -> corrected. Views
-- select the latest event per cost_request_id.
CREATE TABLE rvbbit.cost_events (
    event_id               bigserial PRIMARY KEY,
    cost_request_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    query_id               uuid,
    receipt_id             uuid,
    sub_call_index         int,
    source                 text NOT NULL,
    backend                text,
    transport              text,
    model                  text,
    tool                   text,
    provider_request_id    text,
    provider_generation_id text,
    upstream_id            text,
    status                 text NOT NULL,
    cost_source            text NOT NULL,
    tokens_in              int,
    tokens_out             int,
    native_tokens_in       int,
    native_tokens_out      int,
    reasoning_tokens       int,
    cached_tokens          int,
    cost_usd               numeric(18, 9),
    currency               text NOT NULL DEFAULT 'USD',
    raw                    jsonb,
    created_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT cost_events_status_check
        CHECK (status IN ('pending', 'settled', 'estimated', 'free', 'uncosted', 'error')),
    CONSTRAINT cost_events_source_check
        CHECK (source IN ('operator', 'mcp', 'specialist', 'prewarm', 'manual'))
);

CREATE INDEX cost_events_request_idx
    ON rvbbit.cost_events (cost_request_id, event_id DESC);
CREATE INDEX cost_events_query_idx
    ON rvbbit.cost_events (query_id, event_id DESC);
CREATE INDEX cost_events_receipt_idx
    ON rvbbit.cost_events (receipt_id, sub_call_index);
CREATE INDEX cost_events_generation_idx
    ON rvbbit.cost_events (provider_generation_id)
    WHERE provider_generation_id IS NOT NULL;

-- Explicit costing rules for things that do not return an actual provider
-- bill. Specificity is: mcp_tool > backend > model.
CREATE TABLE rvbbit.cost_policies (
    target_kind      text NOT NULL,
    target_name      text NOT NULL,
    policy           text NOT NULL,
    model            text,
    fixed_cost_usd   numeric(18, 9),
    input_per_mtok   numeric(18, 9),
    output_per_mtok  numeric(18, 9),
    currency         text NOT NULL DEFAULT 'USD',
    notes            text,
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (target_kind, target_name),
    CONSTRAINT cost_policies_target_kind_check
        CHECK (target_kind IN ('backend', 'mcp_tool', 'model')),
    CONSTRAINT cost_policies_policy_check
        CHECK (policy IN ('free', 'fixed', 'model_rate', 'provider_settled', 'unknown'))
);

CREATE OR REPLACE FUNCTION rvbbit.set_cost_policy(
    target_kind text,
    target_name text,
    policy text,
    fixed_cost_usd numeric DEFAULT NULL,
    input_per_mtok numeric DEFAULT NULL,
    output_per_mtok numeric DEFAULT NULL,
    model text DEFAULT NULL,
    notes text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    row_doc jsonb;
BEGIN
    INSERT INTO rvbbit.cost_policies
        (target_kind, target_name, policy, fixed_cost_usd, input_per_mtok,
         output_per_mtok, model, notes, updated_at)
    VALUES
        (target_kind, target_name, policy, fixed_cost_usd, input_per_mtok,
         output_per_mtok, model, notes, clock_timestamp())
    ON CONFLICT ON CONSTRAINT cost_policies_pkey
    DO UPDATE SET
        policy = EXCLUDED.policy,
        fixed_cost_usd = EXCLUDED.fixed_cost_usd,
        input_per_mtok = EXCLUDED.input_per_mtok,
        output_per_mtok = EXCLUDED.output_per_mtok,
        model = EXCLUDED.model,
        notes = EXCLUDED.notes,
        updated_at = clock_timestamp()
    RETURNING to_jsonb(rvbbit.cost_policies.*) INTO row_doc;
    RETURN row_doc;
END $$;

INSERT INTO rvbbit.cost_policies (target_kind, target_name, policy, notes)
VALUES
    ('backend', 'embed', 'free', 'Default local CPU embedding backend.'),
    ('backend', 'openrouter', 'provider_settled', 'OpenRouter costs settle by generation id.')
ON CONFLICT (target_kind, target_name) DO NOTHING;

CREATE OR REPLACE VIEW rvbbit.cost_latest AS
SELECT DISTINCT ON (cost_request_id)
    *
FROM rvbbit.cost_events
ORDER BY cost_request_id, event_id DESC;

CREATE OR REPLACE VIEW rvbbit.cost_pending AS
SELECT *
FROM rvbbit.cost_latest
WHERE status = 'pending';

CREATE OR REPLACE VIEW rvbbit.query_costs AS
SELECT
    query_id,
    count(*)                                                    AS costed_calls,
    count(*) FILTER (WHERE status = 'pending')                 AS pending_calls,
    count(*) FILTER (WHERE status = 'estimated')               AS estimated_calls,
    count(*) FILTER (WHERE status = 'uncosted')                AS uncosted_calls,
    count(*) FILTER (WHERE status = 'error')                   AS error_calls,
    coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
                                                                AS total_cost_usd,
    min(created_at)                                             AS first_event_at,
    max(created_at)                                             AS last_event_at
FROM rvbbit.cost_latest
WHERE query_id IS NOT NULL
GROUP BY query_id;

CREATE OR REPLACE VIEW rvbbit.receipt_costs AS
SELECT
    receipt_id,
    count(*)                                                    AS costed_calls,
    count(*) FILTER (WHERE status = 'pending')                 AS pending_calls,
    count(*) FILTER (WHERE status = 'estimated')               AS estimated_calls,
    count(*) FILTER (WHERE status = 'uncosted')                AS uncosted_calls,
    count(*) FILTER (WHERE status = 'error')                   AS error_calls,
    coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
                                                                AS total_cost_usd,
    min(created_at)                                             AS first_event_at,
    max(created_at)                                             AS last_event_at
FROM rvbbit.cost_latest
WHERE receipt_id IS NOT NULL
GROUP BY receipt_id;

CREATE OR REPLACE VIEW rvbbit.receipt_cost_audit AS
WITH receipt_subcalls AS (
    SELECT
        r.receipt_id,
        (sub.ord - 1)::int AS sub_call_index
    FROM rvbbit.receipts r
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(coalesce(r.sub_calls, '[]'::jsonb)) = 'array'
            THEN coalesce(r.sub_calls, '[]'::jsonb)
            ELSE '[]'::jsonb
        END
    ) WITH ORDINALITY AS sub(value, ord)
    WHERE coalesce(sub.value->>'kind', '') IN ('llm', 'specialist', 'mcp')
),
receipt_rollup AS (
    SELECT
        r.receipt_id,
        r.operator,
        r.query_id,
        r.invocation_at,
        count(s.sub_call_index) AS chargeable_sub_calls
    FROM rvbbit.receipts r
    LEFT JOIN receipt_subcalls s ON s.receipt_id = r.receipt_id
    GROUP BY r.receipt_id, r.operator, r.query_id, r.invocation_at
),
cost_rollup AS (
    SELECT
        receipt_id,
        count(DISTINCT sub_call_index) FILTER (WHERE sub_call_index IS NOT NULL)
            AS cost_event_sub_calls,
        count(*) FILTER (WHERE status = 'pending')   AS pending_calls,
        count(*) FILTER (WHERE status = 'estimated') AS estimated_calls,
        count(*) FILTER (WHERE status = 'uncosted')  AS uncosted_calls,
        count(*) FILTER (WHERE status = 'error')     AS error_calls,
        min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending_at,
        coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
            AS total_cost_usd
    FROM rvbbit.cost_latest
    WHERE receipt_id IS NOT NULL
    GROUP BY receipt_id
)
SELECT
    r.receipt_id,
    r.operator,
    r.query_id,
    r.invocation_at,
    r.chargeable_sub_calls,
    coalesce(c.cost_event_sub_calls, 0) AS cost_event_sub_calls,
    greatest(r.chargeable_sub_calls - coalesce(c.cost_event_sub_calls, 0), 0)
        AS missing_cost_events,
    coalesce(c.pending_calls, 0) AS pending_calls,
    coalesce(c.estimated_calls, 0) AS estimated_calls,
    coalesce(c.uncosted_calls, 0) AS uncosted_calls,
    coalesce(c.error_calls, 0) AS error_calls,
    c.oldest_pending_at,
    CASE
        WHEN r.chargeable_sub_calls = 0 THEN 'no_chargeable_sub_calls'
        WHEN greatest(r.chargeable_sub_calls - coalesce(c.cost_event_sub_calls, 0), 0) > 0
            THEN 'missing_cost_events'
        WHEN coalesce(c.pending_calls, 0) > 0
             AND c.oldest_pending_at < clock_timestamp() - interval '15 minutes'
            THEN 'stale_pending'
        WHEN coalesce(c.pending_calls, 0) > 0 THEN 'pending'
        WHEN coalesce(c.uncosted_calls, 0) > 0 THEN 'uncosted'
        WHEN coalesce(c.error_calls, 0) > 0 THEN 'errors'
        ELSE 'ok'
    END AS audit_status,
    coalesce(c.total_cost_usd, 0)::numeric(18,9) AS total_cost_usd
FROM receipt_rollup r
LEFT JOIN cost_rollup c ON c.receipt_id = r.receipt_id;

CREATE OR REPLACE VIEW rvbbit.cost_audit_gaps AS
SELECT *
FROM rvbbit.receipt_cost_audit
WHERE audit_status NOT IN ('ok', 'no_chargeable_sub_calls');
"#,
    name = "rvbbit_cost_catalog",
    requires = ["rvbbit_bootstrap"],
);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct ReceiptRecord {
    pub operator: String,
    pub inputs_hash_hex: String,
    pub model: String,
    pub inputs: Value,
    pub output: Option<String>,
    pub sub_calls: Value,
    pub n_tokens_in: i32,
    pub n_tokens_out: i32,
    pub latency_ms: i32,
    pub error: Option<String>,
    pub cost_usd: Option<f64>,
    pub query_id: Option<String>,
    pub queue_reason: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum MissingQueryId {
    Generate,
    Null,
}

static QUEUE_COUNTER: AtomicU64 = AtomicU64::new(0);

#[pg_extern]
fn flush_receipt_queue(limit: default!(i64, 1000)) -> i64 {
    flush_receipt_queue_impl(limit.max(0) as usize)
}

#[pg_extern]
fn receipt_queue_pending() -> i64 {
    let Ok(entries) = fs::read_dir(queue_dir()) else {
        return 0;
    };
    entries
        .filter_map(Result::ok)
        .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("json"))
        .count() as i64
}

#[pg_extern]
fn reconcile_openrouter_costs(limit: default!(i64, 100)) -> i64 {
    let limit = limit.max(0) as usize;
    if limit == 0 {
        return 0;
    }
    let token = match openrouter_token() {
        Some(t) => t,
        None => {
            pgrx::warning!("rvbbit.reconcile_openrouter_costs: OPENROUTER_API_KEY is not set");
            return 0;
        }
    };

    let pending = load_openrouter_pending(limit);
    if pending.is_empty() {
        return 0;
    }

    let endpoint = openrouter_generation_endpoint();
    let client = reqwest::blocking::Client::new();
    let mut settled = 0_i64;
    for p in pending {
        let resp = client
            .get(&endpoint)
            .bearer_auth(&token)
            .query(&[("id", p.generation_id.as_str())])
            .send();

        let value = match resp {
            Ok(r) if r.status().is_success() => match r.json::<Value>() {
                Ok(v) => v,
                Err(e) => {
                    append_reconcile_error(&p, &format!("bad OpenRouter generation JSON: {e}"));
                    continue;
                }
            },
            Ok(r) => {
                append_reconcile_error(
                    &p,
                    &format!(
                        "OpenRouter generation status {}: {}",
                        r.status().as_u16(),
                        r.text().unwrap_or_default()
                    ),
                );
                continue;
            }
            Err(e) => {
                append_reconcile_error(&p, &e.to_string());
                continue;
            }
        };

        match append_openrouter_settlement(&p, &value) {
            Ok(true) => settled += 1,
            Ok(false) => {}
            Err(e) => append_reconcile_error(&p, &e),
        }
    }
    settled
}

#[pg_extern]
fn backfill_cost_events_from_receipts(limit: default!(i64, 1000)) -> i64 {
    backfill_cost_events_from_receipts_impl(limit.max(0) as usize)
}

#[pg_extern]
fn cost_audit_summary() -> pgrx::JsonB {
    let queue_pending = receipt_queue_pending();
    let sql = format!(
        "SELECT jsonb_build_object( \
            'receipt_queue_pending', {queue_pending}, \
            'receipts', coalesce(( \
                SELECT jsonb_build_object( \
                    'total', count(*), \
                    'ok', count(*) FILTER (WHERE audit_status = 'ok'), \
                    'no_chargeable_sub_calls', count(*) FILTER (WHERE audit_status = 'no_chargeable_sub_calls'), \
                    'missing_cost_events', count(*) FILTER (WHERE audit_status = 'missing_cost_events'), \
                    'pending', count(*) FILTER (WHERE audit_status = 'pending'), \
                    'stale_pending', count(*) FILTER (WHERE audit_status = 'stale_pending'), \
                    'uncosted', count(*) FILTER (WHERE audit_status = 'uncosted'), \
                    'errors', count(*) FILTER (WHERE audit_status = 'errors') \
                ) \
                FROM rvbbit.receipt_cost_audit \
            ), '{{}}'::jsonb), \
            'cost_events', coalesce(( \
                SELECT jsonb_build_object( \
                    'latest_calls', count(*), \
                    'pending', count(*) FILTER (WHERE status = 'pending'), \
                    'settled', count(*) FILTER (WHERE status = 'settled'), \
                    'estimated', count(*) FILTER (WHERE status = 'estimated'), \
                    'free', count(*) FILTER (WHERE status = 'free'), \
                    'uncosted', count(*) FILTER (WHERE status = 'uncosted'), \
                    'error', count(*) FILTER (WHERE status = 'error') \
                ) \
                FROM rvbbit.cost_latest \
            ), '{{}}'::jsonb) \
        )"
    );
    Spi::get_one::<pgrx::JsonB>(&sql)
        .ok()
        .flatten()
        .unwrap_or_else(|| pgrx::JsonB(serde_json::json!({"receipt_queue_pending": queue_pending})))
}

#[pg_extern]
fn maintain_cost_audit(
    queue_limit: default!(i64, 10000),
    backfill_limit: default!(i64, 10000),
    reconcile_limit: default!(i64, 1000),
) -> pgrx::JsonB {
    let flushed_receipts = flush_receipt_queue_impl(queue_limit.max(0) as usize);
    let backfilled_receipts =
        backfill_cost_events_from_receipts_impl(backfill_limit.max(0) as usize);
    let reconciled_costs = reconcile_openrouter_costs(reconcile_limit);
    let summary = cost_audit_summary().0;
    pgrx::JsonB(serde_json::json!({
        "flushed_receipts": flushed_receipts,
        "backfilled_receipts": backfilled_receipts,
        "reconciled_costs": reconciled_costs,
        "summary": summary,
    }))
}

pub(crate) fn log_mcp_invocation_cost(
    server: &str,
    tool: &str,
    error: Option<&str>,
    cache_hit: bool,
    output: &Value,
) {
    let tool_name = format!("{server}.{tool}");
    let decision = if cache_hit {
        CostDecision {
            status: "free".into(),
            cost_source: "cache_hit".into(),
            cost_usd: Some(0.0),
        }
    } else {
        decide_cost(
            Some("mcp"),
            Some(&tool_name),
            Some(&tool_name),
            None,
            None,
            None,
            error,
            None,
            None,
        )
    };
    let raw = serde_json::json!({
        "server": server,
        "tool": tool,
        "cache_hit": cache_hit,
        "output": output,
        "error": error,
    });
    let raw = serde_json::to_string(&raw).unwrap_or_else(|_| "null".into());
    let cost_sql = decision
        .cost_usd
        .map(|c| format!("{c:.9}::numeric"))
        .unwrap_or_else(|| "NULL".into());
    let sql = format!(
        "INSERT INTO rvbbit.cost_events \
         (query_id, source, backend, transport, model, tool, status, cost_source, cost_usd, raw) \
         VALUES (rvbbit.current_query_id(), 'mcp', 'mcp', 'mcp', {model}, {tool}, \
                 {status}, {cost_source}, {cost_sql}, {raw}::jsonb)",
        model = sql_lit(&tool_name),
        tool = sql_lit(&tool_name),
        status = sql_lit(&decision.status),
        cost_source = sql_lit(&decision.cost_source),
        raw = sql_lit(&raw),
    );
    let _ = Spi::run(&sql);
}

pub(crate) fn record_from_work(
    op: &OpDef,
    hash: &[u8],
    res: &WorkResult,
    inputs: &Value,
) -> ReceiptRecord {
    let sub_calls = serde_json::to_value(&res.sub_calls).unwrap_or(Value::Array(Vec::new()));
    let cost_usd = sub_calls.as_array().and_then(|arr| {
        let mut total = 0.0_f64;
        let mut any = false;
        for sub in arr {
            if let Some(c) = sub.get("cost_usd").and_then(|v| v.as_f64()) {
                total += c;
                any = true;
            }
        }
        any.then_some(total)
    });
    let model_used = res
        .sub_calls
        .iter()
        .find_map(|s| s.model.clone())
        .unwrap_or_else(|| op.model.clone());
    ReceiptRecord {
        operator: op.name.clone(),
        inputs_hash_hex: bytes_to_hex(hash),
        model: model_used,
        inputs: inputs.clone(),
        output: if res.error.is_some() {
            None
        } else {
            Some(res.output.clone())
        },
        sub_calls,
        n_tokens_in: res.total_tokens_in,
        n_tokens_out: res.total_tokens_out,
        latency_ms: res.total_latency_ms,
        error: res.error.clone(),
        cost_usd,
        query_id: None,
        queue_reason: None,
    }
}

pub(crate) fn write_receipt_now(
    record: &ReceiptRecord,
    missing_query_id: MissingQueryId,
) -> Result<String, String> {
    let inputs_str = serde_json::to_string(&record.inputs).unwrap_or_else(|_| "null".into());
    let sub_calls_str = serde_json::to_string(&record.sub_calls).unwrap_or_else(|_| "[]".into());
    let output_sql = record
        .output
        .as_deref()
        .map(sql_lit)
        .unwrap_or_else(|| "NULL".into());
    let error_sql = record
        .error
        .as_deref()
        .map(sql_lit)
        .unwrap_or_else(|| "NULL".into());
    let cost_sql = record
        .cost_usd
        .map(|c| format!("{c:.9}::numeric"))
        .unwrap_or_else(|| "NULL".into());
    let query_id_sql = query_id_sql(record.query_id.as_deref(), missing_query_id);
    let sql = format!(
        "INSERT INTO rvbbit.receipts \
         (operator, inputs_hash, model, inputs, output, parsed, cost_usd, sub_calls, query_id, \
          n_tokens_in, n_tokens_out, latency_ms, error) \
         VALUES ({op_sql}, '\\x{hex}'::bytea, {model_sql}, {inputs_sql}::jsonb, \
                 {output_sql}, NULL, {cost_sql}, {subcalls_sql}::jsonb, {query_id_sql}, \
                 {ti}, {to_}, {lat}, {error_sql}) \
         RETURNING receipt_id::text",
        op_sql = sql_lit(&record.operator),
        hex = record.inputs_hash_hex,
        model_sql = sql_lit(&record.model),
        inputs_sql = sql_lit(&inputs_str),
        subcalls_sql = sql_lit(&sub_calls_str),
        ti = record.n_tokens_in,
        to_ = record.n_tokens_out,
        lat = record.latency_ms,
    );
    let receipt_id = Spi::get_one::<String>(&sql)
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "receipt insert returned no receipt_id".to_string())?;
    if let Err(e) = append_cost_events_for_receipt(record, &receipt_id) {
        pgrx::warning!("rvbbit: failed to log cost events: {e}");
    }
    Ok(receipt_id)
}

pub(crate) fn enqueue_receipt(record: &ReceiptRecord, reason: &str) -> Result<(), String> {
    let mut queued = record.clone();
    queued.queue_reason = Some(reason.to_string());
    let dir = queue_dir();
    fs::create_dir_all(&dir).map_err(|e| format!("create {}: {e}", dir.display()))?;
    let base = queue_basename();
    let tmp = dir.join(format!("{base}.tmp"));
    let final_path = dir.join(format!("{base}.json"));
    let payload = serde_json::to_vec(&queued).map_err(|e| e.to_string())?;
    fs::write(&tmp, payload).map_err(|e| format!("write {}: {e}", tmp.display()))?;
    fs::rename(&tmp, &final_path)
        .map_err(|e| format!("rename {} -> {}: {e}", tmp.display(), final_path.display()))
}

pub(crate) fn flush_receipt_queue_best_effort(limit: usize) {
    if autodrain_disabled() || limit == 0 {
        return;
    }
    let _ = flush_receipt_queue_impl(limit);
}

/// Number of receipts currently waiting in the on-disk audit queue. This is a
/// LIVE, cross-connection signal of in-flight semantic-operator work: every
/// completed operator call enqueues one file here before it is flushed into
/// rvbbit.receipts. A separate connection can poll this WHILE a query runs to
/// show progress — unlike rvbbit.receipts / rvbbit.cost_events, which are
/// transaction-isolated and invisible until the running query commits.
#[pgrx::pg_extern]
fn receipt_queue_depth() -> i64 {
    match fs::read_dir(queue_dir()) {
        Ok(entries) => entries
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("json"))
            .count() as i64,
        Err(_) => 0,
    }
}

fn flush_receipt_queue_impl(limit: usize) -> i64 {
    let dir = queue_dir();
    let Ok(entries) = fs::read_dir(&dir) else {
        return 0;
    };
    let mut paths: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("json"))
        .collect();
    paths.sort();

    let mut inserted = 0_i64;
    for path in paths.into_iter().take(limit) {
        let processing = path.with_extension("processing");
        if fs::rename(&path, &processing).is_err() {
            continue;
        }
        let payload = match fs::read_to_string(&processing) {
            Ok(s) => s,
            Err(e) => {
                move_queue_error(&processing, &format!("read failed: {e}"));
                continue;
            }
        };
        let record: ReceiptRecord = match serde_json::from_str(&payload) {
            Ok(r) => r,
            Err(e) => {
                move_queue_error(&processing, &format!("decode failed: {e}"));
                continue;
            }
        };
        match write_receipt_now(&record, MissingQueryId::Null) {
            Ok(_) => {
                let _ = fs::remove_file(&processing);
                inserted += 1;
            }
            Err(e) => {
                restore_queue_item(&processing, &format!("insert failed: {e}"));
            }
        }
    }
    inserted
}

fn backfill_cost_events_from_receipts_impl(limit: usize) -> i64 {
    if limit == 0 {
        return 0;
    }
    let sql = format!(
        "SELECT receipt_id::text, operator, encode(inputs_hash, 'hex'), model, \
                coalesce(inputs, 'null'::jsonb), output, coalesce(sub_calls, '[]'::jsonb), \
                coalesce(n_tokens_in, 0), coalesce(n_tokens_out, 0), coalesce(latency_ms, 0), \
                error, cost_usd::float8, query_id::text \
         FROM rvbbit.receipts r \
         WHERE NOT EXISTS (SELECT 1 FROM rvbbit.cost_events c WHERE c.receipt_id = r.receipt_id) \
         ORDER BY invocation_at DESC \
         LIMIT {limit}"
    );
    let mut rows: Vec<(String, ReceiptRecord)> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let receipt_id: Option<String> = row.get(1)?;
            let operator: Option<String> = row.get(2)?;
            let inputs_hash_hex: Option<String> = row.get(3)?;
            let model: Option<String> = row.get(4)?;
            let inputs: Option<pgrx::JsonB> = row.get(5)?;
            let output: Option<String> = row.get(6)?;
            let sub_calls: Option<pgrx::JsonB> = row.get(7)?;
            let n_tokens_in: Option<i32> = row.get(8)?;
            let n_tokens_out: Option<i32> = row.get(9)?;
            let latency_ms: Option<i32> = row.get(10)?;
            let error: Option<String> = row.get(11)?;
            let cost_usd: Option<f64> = row.get(12)?;
            let query_id: Option<String> = row.get(13)?;
            if let (Some(receipt_id), Some(operator), Some(inputs_hash_hex), Some(model)) =
                (receipt_id, operator, inputs_hash_hex, model)
            {
                rows.push((
                    receipt_id,
                    ReceiptRecord {
                        operator,
                        inputs_hash_hex,
                        model,
                        inputs: inputs.map(|j| j.0).unwrap_or(Value::Null),
                        output,
                        sub_calls: sub_calls
                            .map(|j| j.0)
                            .unwrap_or_else(|| Value::Array(vec![])),
                        n_tokens_in: n_tokens_in.unwrap_or(0),
                        n_tokens_out: n_tokens_out.unwrap_or(0),
                        latency_ms: latency_ms.unwrap_or(0),
                        error,
                        cost_usd,
                        query_id,
                        queue_reason: None,
                    },
                ));
            }
        }
        Ok(())
    });

    let mut inserted = 0_i64;
    for (receipt_id, record) in rows {
        if append_cost_events_for_receipt(&record, &receipt_id).is_ok() {
            inserted += 1;
        }
    }
    inserted
}

fn append_cost_events_for_receipt(record: &ReceiptRecord, receipt_id: &str) -> Result<(), String> {
    let Some(arr) = record.sub_calls.as_array() else {
        return Ok(());
    };
    for (idx, sub) in arr.iter().enumerate() {
        let kind = json_str(sub, "kind").unwrap_or_default();
        if !matches!(kind.as_str(), "llm" | "specialist" | "mcp") {
            continue;
        }
        let model = json_str(sub, "model");
        let backend = json_str(sub, "backend")
            .or_else(|| json_str(sub, "provider"))
            .or_else(|| {
                if kind == "mcp" {
                    Some("mcp".to_string())
                } else {
                    None
                }
            });
        let transport = json_str(sub, "transport");
        let generation_id = json_str(sub, "provider_generation_id");
        let request_id = json_str(sub, "provider_request_id");
        let upstream_id = json_str(sub, "upstream_id");
        let sub_cost_source = json_str(sub, "cost_source");
        let cost_inline = sub.get("cost_usd").and_then(|v| v.as_f64());
        let error = sub.get("error").and_then(|v| v.as_str());
        let tokens_in = json_i32(sub, "tokens_in");
        let tokens_out = json_i32(sub, "tokens_out");
        let native_tokens_in = json_i32(sub, "native_tokens_in");
        let native_tokens_out = json_i32(sub, "native_tokens_out");
        let reasoning_tokens = json_i32(sub, "reasoning_tokens");
        let cached_tokens = json_i32(sub, "cached_tokens");

        let source = match kind.as_str() {
            "mcp" => "mcp",
            "specialist" => "specialist",
            _ => "operator",
        };
        let decision = decide_cost(
            backend.as_deref(),
            model.as_deref(),
            if kind == "mcp" {
                model.as_deref()
            } else {
                None
            },
            sub_cost_source.as_deref(),
            generation_id.as_deref(),
            cost_inline,
            error,
            tokens_in,
            tokens_out,
        );
        let cost_expr = decision
            .cost_usd
            .map(|c| format!("{c:.9}::numeric"))
            .unwrap_or_else(|| "NULL".to_string());

        let raw = serde_json::to_string(sub).unwrap_or_else(|_| "null".into());
        let sql = format!(
            "INSERT INTO rvbbit.cost_events \
             (query_id, receipt_id, sub_call_index, source, backend, transport, model, tool, \
              provider_request_id, provider_generation_id, upstream_id, status, cost_source, \
              tokens_in, tokens_out, native_tokens_in, native_tokens_out, reasoning_tokens, \
              cached_tokens, cost_usd, raw) \
             VALUES ((SELECT query_id FROM rvbbit.receipts WHERE receipt_id = {receipt_id}::uuid), \
                     {receipt_id}::uuid, {idx}, {source}, {backend}, {transport}, {model}, {tool}, \
                     {request_id}, {generation_id}, {upstream_id}, {status}, {cost_source}, \
                     {tokens_in}, {tokens_out}, {native_tokens_in}, {native_tokens_out}, \
                     {reasoning_tokens}, {cached_tokens}, {cost_expr}, {raw}::jsonb)",
            receipt_id = sql_lit(receipt_id),
            idx = idx as i32,
            source = sql_lit(source),
            backend = sql_opt_lit(backend.as_deref()),
            transport = sql_opt_lit(transport.as_deref()),
            model = sql_opt_lit(model.as_deref()),
            tool = sql_opt_lit(if kind == "mcp" {
                model.as_deref()
            } else {
                None
            }),
            request_id = sql_opt_lit(request_id.as_deref()),
            generation_id = sql_opt_lit(generation_id.as_deref()),
            upstream_id = sql_opt_lit(upstream_id.as_deref()),
            status = sql_lit(&decision.status),
            cost_source = sql_lit(&decision.cost_source),
            tokens_in = sql_opt_i32(tokens_in),
            tokens_out = sql_opt_i32(tokens_out),
            native_tokens_in = sql_opt_i32(native_tokens_in),
            native_tokens_out = sql_opt_i32(native_tokens_out),
            reasoning_tokens = sql_opt_i32(reasoning_tokens),
            cached_tokens = sql_opt_i32(cached_tokens),
            raw = sql_lit(&raw),
        );
        Spi::run(&sql).map_err(|e| e.to_string())?;
    }
    Ok(())
}

struct CostDecision {
    status: String,
    cost_source: String,
    cost_usd: Option<f64>,
}

struct CostPolicy {
    policy: String,
    model: Option<String>,
    fixed_cost_usd: Option<f64>,
    input_per_mtok: Option<f64>,
    output_per_mtok: Option<f64>,
}

#[allow(clippy::too_many_arguments)]
fn decide_cost(
    backend: Option<&str>,
    model: Option<&str>,
    tool: Option<&str>,
    sub_cost_source: Option<&str>,
    generation_id: Option<&str>,
    inline_cost_usd: Option<f64>,
    error: Option<&str>,
    tokens_in: Option<i32>,
    tokens_out: Option<i32>,
) -> CostDecision {
    if error.is_some() {
        return CostDecision {
            status: "error".into(),
            cost_source: "none".into(),
            cost_usd: None,
        };
    }
    if let Some(cost) = inline_cost_usd {
        return CostDecision {
            status: "settled".into(),
            cost_source: "inline".into(),
            cost_usd: Some(cost),
        };
    }

    let policy = load_cost_policy(backend, model, tool);
    if let Some(policy) = policy {
        match policy.policy.as_str() {
            "free" => {
                return CostDecision {
                    status: "free".into(),
                    cost_source: "policy_free".into(),
                    cost_usd: Some(0.0),
                }
            }
            "fixed" => {
                return CostDecision {
                    status: policy
                        .fixed_cost_usd
                        .map(|_| "estimated")
                        .unwrap_or("uncosted")
                        .into(),
                    cost_source: policy
                        .fixed_cost_usd
                        .map(|_| "policy_fixed")
                        .unwrap_or("none")
                        .into(),
                    cost_usd: policy.fixed_cost_usd,
                }
            }
            "provider_settled" => {
                if generation_id.is_some() {
                    let cost_source = if backend == Some("openrouter")
                        || sub_cost_source == Some("openrouter_generation")
                    {
                        "openrouter_generation"
                    } else {
                        "provider_settled"
                    };
                    return CostDecision {
                        status: "pending".into(),
                        cost_source: cost_source.into(),
                        cost_usd: None,
                    };
                }
                return CostDecision {
                    status: "uncosted".into(),
                    cost_source: "provider_settled".into(),
                    cost_usd: None,
                };
            }
            "model_rate" => {
                let rate_model = policy.model.as_deref().or(model);
                let policy_rate = policy.input_per_mtok.zip(policy.output_per_mtok);
                if let Some(cost) =
                    estimate_token_cost(rate_model, tokens_in, tokens_out, policy_rate)
                {
                    return CostDecision {
                        status: "estimated".into(),
                        cost_source: "policy_model_rate".into(),
                        cost_usd: Some(cost),
                    };
                }
                return CostDecision {
                    status: "uncosted".into(),
                    cost_source: "policy_model_rate".into(),
                    cost_usd: None,
                };
            }
            _ => {
                return CostDecision {
                    status: "uncosted".into(),
                    cost_source: "none".into(),
                    cost_usd: None,
                }
            }
        }
    }

    let openrouter_pending = backend == Some("openrouter")
        || sub_cost_source == Some("openrouter_generation")
        || sub_cost_source == Some("provider_settled");
    if openrouter_pending && generation_id.is_some() {
        return CostDecision {
            status: "pending".into(),
            cost_source: "openrouter_generation".into(),
            cost_usd: None,
        };
    }

    if let Some(cost) = estimate_token_cost(model, tokens_in, tokens_out, None) {
        return CostDecision {
            status: "estimated".into(),
            cost_source: "model_rate".into(),
            cost_usd: Some(cost),
        };
    }

    CostDecision {
        status: "uncosted".into(),
        cost_source: "none".into(),
        cost_usd: None,
    }
}

fn load_cost_policy(
    backend: Option<&str>,
    model: Option<&str>,
    tool: Option<&str>,
) -> Option<CostPolicy> {
    let mut predicates = Vec::new();
    if let Some(tool) = tool.filter(|s| !s.is_empty()) {
        predicates.push(format!(
            "(target_kind = 'mcp_tool' AND target_name = {})",
            sql_lit(tool)
        ));
    }
    if let Some(backend) = backend.filter(|s| !s.is_empty()) {
        predicates.push(format!(
            "(target_kind = 'backend' AND target_name = {})",
            sql_lit(backend)
        ));
    }
    if let Some(model) = model.filter(|s| !s.is_empty()) {
        predicates.push(format!(
            "(target_kind = 'model' AND target_name = {})",
            sql_lit(model)
        ));
    }
    if predicates.is_empty() {
        return None;
    }
    let sql = format!(
        "SELECT policy, model, fixed_cost_usd::float8, input_per_mtok::float8, \
                output_per_mtok::float8 \
         FROM rvbbit.cost_policies \
         WHERE {} \
         ORDER BY CASE target_kind WHEN 'mcp_tool' THEN 1 WHEN 'backend' THEN 2 ELSE 3 END \
         LIMIT 1",
        predicates.join(" OR ")
    );
    let mut out = None;
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let policy: Option<String> = row.get(1)?;
            if let Some(policy) = policy {
                out = Some(CostPolicy {
                    policy,
                    model: row.get(2)?,
                    fixed_cost_usd: row.get(3)?,
                    input_per_mtok: row.get(4)?,
                    output_per_mtok: row.get(5)?,
                });
            }
        }
        Ok(())
    });
    out
}

fn estimate_token_cost(
    model: Option<&str>,
    tokens_in: Option<i32>,
    tokens_out: Option<i32>,
    explicit_rate: Option<(f64, f64)>,
) -> Option<f64> {
    let ti = tokens_in.unwrap_or(0).max(0) as f64;
    let to_ = tokens_out.unwrap_or(0).max(0) as f64;
    if ti == 0.0 && to_ == 0.0 {
        return None;
    }
    let (input_per_mtok, output_per_mtok) = match explicit_rate {
        Some(rate) => rate,
        None => load_model_rate(model?)?,
    };
    Some((ti / 1_000_000.0) * input_per_mtok + (to_ / 1_000_000.0) * output_per_mtok)
}

fn load_model_rate(model: &str) -> Option<(f64, f64)> {
    let sql = format!(
        "SELECT input_per_mtok::float8, output_per_mtok::float8 \
         FROM rvbbit.model_rates WHERE model = {}",
        sql_lit(model)
    );
    let mut out = None;
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let input: Option<f64> = row.get(1)?;
            let output: Option<f64> = row.get(2)?;
            if let (Some(input), Some(output)) = (input, output) {
                out = Some((input, output));
            }
        }
        Ok(())
    });
    out
}

#[derive(Debug)]
struct PendingOpenRouter {
    cost_request_id: String,
    generation_id: String,
}

fn load_openrouter_pending(limit: usize) -> Vec<PendingOpenRouter> {
    let sql = format!(
        "SELECT cost_request_id::text, provider_generation_id \
         FROM rvbbit.cost_pending \
         WHERE provider_generation_id IS NOT NULL \
           AND (backend = 'openrouter' \
                OR cost_source IN ('openrouter_generation', 'provider_settled')) \
         ORDER BY created_at ASC \
         LIMIT {limit}"
    );
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let cost_request_id: Option<String> = row.get(1)?;
            let generation_id: Option<String> = row.get(2)?;
            if let (Some(cost_request_id), Some(generation_id)) = (cost_request_id, generation_id) {
                out.push(PendingOpenRouter {
                    cost_request_id,
                    generation_id,
                });
            }
        }
        Ok(())
    });
    out
}

fn append_openrouter_settlement(p: &PendingOpenRouter, value: &Value) -> Result<bool, String> {
    let data = value.get("data").unwrap_or(value);
    let total_cost = json_f64(data, "total_cost").or_else(|| json_f64(data, "usage"));
    let Some(total_cost) = total_cost else {
        append_reconcile_error(
            p,
            "OpenRouter generation response did not include total_cost or usage",
        );
        return Ok(false);
    };
    let model = json_str(data, "model");
    let request_id = json_str(data, "request_id");
    let upstream_id = json_str(data, "upstream_id");
    let tokens_in = json_i32(data, "tokens_prompt");
    let tokens_out = json_i32(data, "tokens_completion");
    let native_tokens_in = json_i32(data, "native_tokens_prompt");
    let native_tokens_out = json_i32(data, "native_tokens_completion");
    let reasoning_tokens = json_i32(data, "native_tokens_reasoning");
    let cached_tokens = json_i32(data, "native_tokens_cached");
    let raw = serde_json::to_string(data).unwrap_or_else(|_| "null".into());
    let sql = format!(
        "INSERT INTO rvbbit.cost_events \
         (cost_request_id, query_id, receipt_id, sub_call_index, source, backend, transport, \
          model, provider_request_id, provider_generation_id, upstream_id, status, cost_source, \
          tokens_in, tokens_out, native_tokens_in, native_tokens_out, reasoning_tokens, \
          cached_tokens, cost_usd, raw) \
         SELECT {cost_request_id}::uuid, query_id, receipt_id, sub_call_index, source, backend, \
                transport, COALESCE({model}, model), COALESCE({request_id}, provider_request_id), \
                provider_generation_id, COALESCE({upstream_id}, upstream_id), 'settled', \
                'openrouter_generation', COALESCE({tokens_in}, tokens_in), \
                COALESCE({tokens_out}, tokens_out), {native_tokens_in}, {native_tokens_out}, \
                {reasoning_tokens}, {cached_tokens}, {total_cost}, {raw}::jsonb \
         FROM rvbbit.cost_latest \
         WHERE cost_request_id = {cost_request_id}::uuid",
        cost_request_id = sql_lit(&p.cost_request_id),
        model = sql_opt_lit(model.as_deref()),
        request_id = sql_opt_lit(request_id.as_deref()),
        upstream_id = sql_opt_lit(upstream_id.as_deref()),
        tokens_in = sql_opt_i32(tokens_in),
        tokens_out = sql_opt_i32(tokens_out),
        native_tokens_in = sql_opt_i32(native_tokens_in),
        native_tokens_out = sql_opt_i32(native_tokens_out),
        reasoning_tokens = sql_opt_i32(reasoning_tokens),
        cached_tokens = sql_opt_i32(cached_tokens),
        total_cost = format!("{total_cost:.9}::numeric"),
        raw = sql_lit(&raw),
    );
    Spi::run(&sql).map_err(|e| e.to_string())?;
    Ok(true)
}

fn append_reconcile_error(p: &PendingOpenRouter, error: &str) {
    let raw = serde_json::json!({
        "provider_generation_id": p.generation_id,
        "error": error,
    });
    let raw = serde_json::to_string(&raw).unwrap_or_else(|_| "null".into());
    let sql = format!(
        "INSERT INTO rvbbit.cost_events \
         (cost_request_id, query_id, receipt_id, sub_call_index, source, backend, transport, \
          model, provider_generation_id, status, cost_source, raw) \
         SELECT {cost_request_id}::uuid, query_id, receipt_id, sub_call_index, source, backend, \
                transport, model, provider_generation_id, 'pending', 'openrouter_generation', \
                {raw}::jsonb \
         FROM rvbbit.cost_latest \
         WHERE cost_request_id = {cost_request_id}::uuid",
        cost_request_id = sql_lit(&p.cost_request_id),
        raw = sql_lit(&raw),
    );
    let _ = Spi::run(&sql);
}

fn openrouter_token() -> Option<String> {
    let env_name = Spi::get_one::<String>(
        "SELECT auth_header_env FROM rvbbit.backends WHERE name = 'openrouter'",
    )
    .ok()
    .flatten()
    .unwrap_or_else(|| "OPENROUTER_API_KEY".to_string());
    std::env::var(env_name).ok().filter(|s| !s.is_empty())
}

fn openrouter_generation_endpoint() -> String {
    if let Ok(endpoint) = std::env::var("RVBBIT_OPENROUTER_GENERATION_URL") {
        let endpoint = endpoint.trim();
        if !endpoint.is_empty() {
            return endpoint.to_string();
        }
    }
    Spi::get_one::<String>(
        "SELECT transport_opts->>'generation_endpoint' \
         FROM rvbbit.backends WHERE name = 'openrouter'",
    )
    .ok()
    .flatten()
    .map(|s| s.trim().to_string())
    .filter(|s| !s.is_empty())
    .unwrap_or_else(|| "https://openrouter.ai/api/v1/generation".to_string())
}

fn query_id_sql(query_id: Option<&str>, missing: MissingQueryId) -> String {
    match query_id.filter(|s| !s.trim().is_empty()) {
        Some(q) => format!("{}::uuid", sql_lit(q)),
        None => match missing {
            MissingQueryId::Generate => "rvbbit.current_query_id()".into(),
            MissingQueryId::Null => "NULL".into(),
        },
    }
}

fn queue_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("RVBBIT_AUDIT_QUEUE_DIR") {
        let trimmed = dir.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    if let Ok(pgdata) = std::env::var("PGDATA") {
        let trimmed = pgdata.trim();
        if !trimmed.is_empty() {
            return Path::new(trimmed).join("rvbbit_audit_queue");
        }
    }
    PathBuf::from("/tmp/rvbbit_audit_queue")
}

fn queue_basename() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let pid = std::process::id();
    let seq = QUEUE_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("receipt-{now}-{pid}-{seq}")
}

fn move_queue_error(path: &Path, reason: &str) {
    let error_path = path.with_extension("error");
    let _ = fs::rename(path, &error_path);
    let meta_path = error_path.with_extension("error.txt");
    let _ = fs::write(meta_path, reason.as_bytes());
    pgrx::warning!("rvbbit: delayed receipt queue item failed: {reason}");
}

fn restore_queue_item(path: &Path, reason: &str) {
    let retry_path = path.with_extension("json");
    let _ = fs::rename(path, &retry_path);
    pgrx::warning!("rvbbit: delayed receipt queue item will retry: {reason}");
}

fn autodrain_disabled() -> bool {
    std::env::var("RVBBIT_RECEIPT_QUEUE_AUTODRAIN")
        .ok()
        .map(|s| {
            matches!(
                s.trim().to_ascii_lowercase().as_str(),
                "0" | "off" | "false" | "no"
            )
        })
        .unwrap_or(false)
}

fn json_str(v: &Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(|x| x.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

fn json_i32(v: &Value, key: &str) -> Option<i32> {
    v.get(key)
        .and_then(|x| x.as_i64())
        .and_then(|n| i32::try_from(n).ok())
}

fn json_f64(v: &Value, key: &str) -> Option<f64> {
    let value = v.get(key)?;
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse::<f64>().ok())
}

fn sql_opt_lit(v: Option<&str>) -> String {
    v.map(sql_lit).unwrap_or_else(|| "NULL".into())
}

fn sql_opt_i32(v: Option<i32>) -> String {
    v.map(|n| n.to_string()).unwrap_or_else(|| "NULL".into())
}

pub(crate) fn sql_lit(s: &str) -> String {
    let escaped = s.replace('\'', "''");
    format!("'{escaped}'")
}

fn bytes_to_hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for byte in b {
        s.push_str(&format!("{:02x}", byte));
    }
    s
}
