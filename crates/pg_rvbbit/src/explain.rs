//! EXPLAIN SEMANTIC — the semantic execution graph (RYR-290).
//!
//! Postgres `EXPLAIN` already describes the *relational* runstream: a
//! tree of plan nodes with rows flowing up. `EXPLAIN SEMANTIC` describes
//! a *different* runstream that lives underneath it — the external-call
//! graph. When a semantic query runs, each `rvbbit.<op>(...)` call site
//! fans out through a cache cascade (L1 in-memory → L2 receipts → fresh)
//! and, on a miss, to one or more endpoints:
//!
//!   - LLM      — a model call to a provider. Billable (tokens × rate).
//!   - SIDECAR  — a local specialist (embeddings, rerank, …). Latency only.
//!   - CODE     — an in-process function. Local, free, fast.
//!
//! Two modes, mirroring EXPLAIN / EXPLAIN ANALYZE:
//!
//!   rvbbit.explain_semantic(q)          ESTIMATE — projected, q not run.
//!                                       Uses EXPLAIN's row estimates +
//!                                       receipt history to sketch cost.
//!   rvbbit.explain_semantic_analyze(q)  ANALYZE  — q executed once, the
//!                                       measured graph with real cost.
//!
//! Dollar cost is LLM-only; sidecars and code run locally. Rates live in
//! `rvbbit.model_rates` — seed them from OpenRouter via
//! `rvbbit.refresh_model_rates()`.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::panic::AssertUnwindSafe;
use std::time::Instant;

use pgrx::extension_sql;
use pgrx::prelude::*;
use serde_json::Value;
use tiktoken_rs::cl100k_base;

use crate::probe::OpTally;

// ---------------------------------------------------------------------------
// Model rate table — turns token counts into dollars. User-maintained;
// seed/refresh it from live OpenRouter pricing via rvbbit.refresh_model_rates().

extension_sql!(
    r#"
CREATE TABLE rvbbit.model_rates (
    model            text PRIMARY KEY,        -- matches operators.model / sub_calls.model
    input_per_mtok   numeric(12, 6) NOT NULL, -- USD per 1,000,000 input tokens
    output_per_mtok  numeric(12, 6) NOT NULL, -- USD per 1,000,000 output tokens
    currency         text NOT NULL DEFAULT 'USD',
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Upsert helper, same house style as create_operator / register_specialist.
CREATE OR REPLACE FUNCTION rvbbit.set_model_rate(
    p_model           text,
    p_input_per_mtok  numeric,
    p_output_per_mtok numeric,
    p_currency        text DEFAULT 'USD'
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO rvbbit.model_rates
        (model, input_per_mtok, output_per_mtok, currency, updated_at)
    VALUES (p_model, p_input_per_mtok, p_output_per_mtok, p_currency, now())
    ON CONFLICT (model) DO UPDATE SET
        input_per_mtok  = EXCLUDED.input_per_mtok,
        output_per_mtok = EXCLUDED.output_per_mtok,
        currency        = EXCLUDED.currency,
        updated_at      = now();
$$;

-- Cold-start fallback so a freshly created extension shows *a* number
-- before rvbbit.refresh_model_rates() has run. Overwritten on refresh.
SELECT rvbbit.set_model_rate('openai/gpt-5.4-mini', 1.000000, 5.000000);

-- Direct OpenAI standard token pricing seeds. These keep direct OpenAI
-- providers auditable even before a user runs provider-specific refreshes.
-- OpenRouter refreshes may overwrite OpenRouter-namespaced models; these
-- direct ids intentionally match OpenAI API model ids.
SELECT rvbbit.set_model_rate('gpt-5.5', 5.000000, 30.000000);
SELECT rvbbit.set_model_rate('gpt-5.5-pro', 30.000000, 180.000000);
SELECT rvbbit.set_model_rate('gpt-5.4', 2.500000, 15.000000);
SELECT rvbbit.set_model_rate('gpt-5.4-mini', 0.750000, 4.500000);
SELECT rvbbit.set_model_rate('gpt-5.4-nano', 0.200000, 1.250000);
SELECT rvbbit.set_model_rate('gpt-5.4-pro', 30.000000, 180.000000);
SELECT rvbbit.set_model_rate('gpt-5.3-codex', 1.750000, 14.000000);
SELECT rvbbit.set_model_rate('gpt-4.1', 2.000000, 8.000000);
SELECT rvbbit.set_model_rate('gpt-4.1-mini', 0.400000, 1.600000);
SELECT rvbbit.set_model_rate('gpt-4.1-nano', 0.100000, 0.400000);
SELECT rvbbit.set_model_rate('gpt-4o', 2.500000, 10.000000);
SELECT rvbbit.set_model_rate('gpt-4o-mini', 0.150000, 0.600000);

-- Direct Anthropic standard token pricing seeds. Cache read/write pricing
-- and data residency multipliers are intentionally left to explicit policies.
SELECT rvbbit.set_model_rate('claude-opus-4-7', 5.000000, 25.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-6', 5.000000, 25.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-5-20251101', 5.000000, 25.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-1-20250805', 15.000000, 75.000000);
SELECT rvbbit.set_model_rate('claude-opus-4-20250514', 15.000000, 75.000000);
SELECT rvbbit.set_model_rate('claude-sonnet-4-6', 3.000000, 15.000000);
SELECT rvbbit.set_model_rate('claude-sonnet-4-5-20250929', 3.000000, 15.000000);
SELECT rvbbit.set_model_rate('claude-sonnet-4-20250514', 3.000000, 15.000000);
SELECT rvbbit.set_model_rate('claude-haiku-4-5-20251001', 1.000000, 5.000000);

-- Direct Gemini text model pricing seeds. Long-context, cache, batch, flex,
-- priority, image, audio, and grounding SKUs need explicit policy overrides.
SELECT rvbbit.set_model_rate('gemini-3.5-flash', 1.500000, 9.000000);
SELECT rvbbit.set_model_rate('gemini-3-flash-preview', 0.500000, 3.000000);
SELECT rvbbit.set_model_rate('gemini-3.1-flash-lite', 0.250000, 1.500000);
SELECT rvbbit.set_model_rate('gemini-3.1-flash-lite-preview', 0.250000, 1.500000);
SELECT rvbbit.set_model_rate('gemini-3.1-pro-preview', 2.000000, 12.000000);
SELECT rvbbit.set_model_rate('gemini-2.5-pro', 1.250000, 10.000000);
SELECT rvbbit.set_model_rate('gemini-2.5-flash', 0.300000, 2.500000);
SELECT rvbbit.set_model_rate('gemini-2.5-flash-lite', 0.100000, 0.400000);
SELECT rvbbit.set_model_rate('gemini-2.0-flash', 0.100000, 0.400000);
"#,
    name = "create_model_rates",
    requires = ["rvbbit_bootstrap"]
);

// ===========================================================================
// SQL-facing entry points
// ===========================================================================

/// EXPLAIN SEMANTIC — projected semantic execution graph. Does NOT run
/// the query; uses Postgres's own row estimates (via EXPLAIN) plus
/// receipt history to sketch the external calls and their cost.
///
///   SELECT * FROM rvbbit.explain_semantic($q$ ... $q$);
#[pg_extern]
fn explain_semantic(query: &str) -> TableIterator<'static, (name!(line, String),)> {
    let graph = build_estimate_graph(query);
    TableIterator::new(render_graph(&graph).into_iter().map(|s| (s,)))
}

/// EXPLAIN SEMANTIC ANALYZE — measured semantic execution graph. Runs
/// the query once and reports the actual cache cascade, external calls,
/// tokens, latency, and dollar cost.
///
///   SELECT * FROM rvbbit.explain_semantic_analyze($q$ ... $q$);
#[pg_extern(volatile)]
fn explain_semantic_analyze(query: &str) -> TableIterator<'static, (name!(line, String),)> {
    let graph = build_analyze_graph(query);
    TableIterator::new(render_graph(&graph).into_iter().map(|s| (s,)))
}

/// Refresh rvbbit.model_rates from live OpenRouter pricing. Fetches the
/// public model catalogue, converts per-token prices to per-Mtok, and
/// upserts every priced model. Returns the number of models loaded.
///
///   SELECT rvbbit.refresh_model_rates();
#[pg_extern(volatile)]
fn refresh_model_rates() -> i64 {
    match fetch_openrouter_rates() {
        Ok(n) => n,
        Err(e) => {
            pgrx::warning!("rvbbit.refresh_model_rates: {e}");
            0
        }
    }
}

// ===========================================================================
// The semantic execution graph — one shape, two builders.
// ===========================================================================

#[derive(Clone, Copy, PartialEq, Eq)]
enum EndpointKind {
    Llm,
    Sidecar,
    Code,
}

impl EndpointKind {
    fn from_subcall_kind(k: &str) -> EndpointKind {
        match k {
            "specialist" => EndpointKind::Sidecar,
            "code" => EndpointKind::Code,
            _ => EndpointKind::Llm,
        }
    }
    fn label(self) -> &'static str {
        match self {
            EndpointKind::Llm => "LLM",
            EndpointKind::Sidecar => "SIDECAR",
            EndpointKind::Code => "CODE",
        }
    }
    fn priority(self) -> u8 {
        match self {
            EndpointKind::Llm => 0,
            EndpointKind::Sidecar => 1,
            EndpointKind::Code => 2,
        }
    }
}

/// Cost of one endpoint's calls.
#[derive(Clone, Copy)]
enum EndpointCost {
    /// LLM endpoint with a known rate.
    Billable(f64),
    /// LLM endpoint with no rvbbit.model_rates row.
    Uncosted,
    /// Sidecar or code — runs locally, no dollar cost.
    Local,
}

/// One external endpoint a call site hits on its fresh path.
struct EndpointLine {
    kind: EndpointKind,
    name: String,
    calls: u64,
    tokens_in: u64,
    tokens_out: u64,
    /// Measured wall time, ms. 0 in ESTIMATE mode (not run).
    latency_ms: u64,
    errors: u64,
    cost: EndpointCost,
}

/// One semantic operator call site in the query. A call site is a
/// distinct (operator, criterion) pair — two `rvbbit.extract(...)` calls
/// with different criteria are two call sites.
struct CallSite {
    operator: String,
    /// The call-site-defining arguments (every input but the `text`
    /// subject). Empty for criterion-free operators like `sentiment`.
    criterion: String,
    shape: String,
    return_type: String,
    /// Cache cascade. In ESTIMATE mode l1 = l2 = 0 (assumed cold).
    l1: u64,
    l2: u64,
    fresh: u64,
    errors: u64,
    invocations: u64,
    endpoints: Vec<EndpointLine>,
    /// Provenance line, e.g. the EXPLAIN node the estimate came from.
    annotation: Option<String>,
}

enum GraphMode {
    Analyze {
        /// None when measured inside `EXPLAIN (SEMANTIC, ANALYZE)` — the
        /// relational plan above already carries row counts and timing.
        result_rows: Option<i64>,
        wall_ms: Option<u128>,
        failed: Option<String>,
    },
    Estimate,
}

struct SemanticGraph {
    query: String,
    mode: GraphMode,
    call_sites: Vec<CallSite>,
    notes: Vec<String>,
}

impl SemanticGraph {
    fn is_estimate(&self) -> bool {
        matches!(self.mode, GraphMode::Estimate)
    }
}

// ===========================================================================
// ANALYZE — arm the probe, run the query once, build the measured graph.
// ===========================================================================

enum MeasuredRun {
    Ok { rows: i64 },
    Failed { error: String },
}

fn build_analyze_graph(query: &str) -> SemanticGraph {
    let trimmed = query.trim().trim_end_matches(';').trim();
    if trimmed.is_empty() {
        return SemanticGraph {
            query: query.to_string(),
            mode: GraphMode::Analyze {
                result_rows: None,
                wall_ms: None,
                failed: Some("empty query".to_string()),
            },
            call_sites: Vec::new(),
            notes: Vec::new(),
        };
    }

    crate::probe::arm();
    let t0 = Instant::now();
    let exec = execute_for_measurement(trimmed);
    let wall_ms = t0.elapsed().as_millis();
    let tallies = crate::probe::disarm();

    let (result_rows, failed) = match exec {
        MeasuredRun::Ok { rows } => (Some(rows), None),
        MeasuredRun::Failed { error } => (None, Some(error)),
    };
    analyze_graph_from_parts(query, &tallies, result_rows, Some(wall_ms), failed)
}

/// Build the measured graph from probe tallies a caller already collected.
/// Used by `build_analyze_graph` (runs the query itself) and by the
/// `EXPLAIN (SEMANTIC, ANALYZE)` hook (EXPLAIN already ran it). `result_rows`
/// / `wall_ms` are None when the relational plan above already reports them.
fn analyze_graph_from_parts(
    query: &str,
    tallies: &BTreeMap<String, OpTally>,
    result_rows: Option<i64>,
    wall_ms: Option<u128>,
    failed: Option<String>,
) -> SemanticGraph {
    let rates = load_model_rates();
    let mut call_sites = Vec::new();
    for tally in tallies.values() {
        call_sites.push(analyze_call_site(tally, &rates));
    }

    let mut notes = vec![
        "This is the semantic runstream — external calls and cost. For the \
         relational plan (scans, joins, row counts) use plain EXPLAIN."
            .to_string(),
        "Cache hits (L1/L2) make no external call and cost $0; re-running a \
         warm query moves invocations up the cascade."
            .to_string(),
    ];
    let uncosted = uncosted_models(&call_sites);
    if !uncosted.is_empty() {
        notes.push(format!(
            "Uncosted LLM models (no rvbbit.model_rates row): {}. Run \
             rvbbit.refresh_model_rates() or rvbbit.set_model_rate(...).",
            uncosted.join(", ")
        ));
    }

    SemanticGraph {
        query: query.to_string(),
        mode: GraphMode::Analyze {
            result_rows,
            wall_ms,
            failed,
        },
        call_sites,
        notes,
    }
}

fn analyze_call_site(tally: &OpTally, rates: &HashMapRates) -> CallSite {
    let meta = lookup_operator_meta(&tally.operator);
    let (shape, return_type) = match &meta {
        Some(m) => (m.shape.clone(), m.return_type.clone()),
        None => ("?".to_string(), "?".to_string()),
    };

    let mut endpoints: Vec<EndpointLine> = Vec::new();
    for ((kind_str, name), stat) in &tally.endpoints {
        let kind = EndpointKind::from_subcall_kind(kind_str);
        let cost = endpoint_cost(kind, name, stat.tokens_in, stat.tokens_out, rates);
        endpoints.push(EndpointLine {
            kind,
            name: name.clone(),
            calls: stat.calls,
            tokens_in: stat.tokens_in,
            tokens_out: stat.tokens_out,
            latency_ms: stat.latency_ms,
            errors: stat.errors,
            cost,
        });
    }
    sort_endpoints(&mut endpoints);

    CallSite {
        operator: tally.operator.clone(),
        criterion: tally.criterion.clone(),
        shape,
        return_type,
        l1: tally.l1_hits,
        l2: tally.l2_hits,
        fresh: tally.fresh,
        errors: tally.errors,
        invocations: tally.invocations(),
        endpoints,
        annotation: None,
    }
}

/// Execute the query so its semantic operators fire, without shipping
/// result rows back. Wrapping in `count(*)` forces full execution
/// (volatile operators are never pruned from a subquery target list).
/// Parallelism is disabled so the leader backend records every cascade
/// event — the probe is thread-local and parallel workers are separate
/// processes.
fn execute_for_measurement(query: &str) -> MeasuredRun {
    let prev_parallel: Option<String> =
        Spi::get_one::<String>("SHOW max_parallel_workers_per_gather")
            .ok()
            .flatten();
    let _ = Spi::run("SET max_parallel_workers_per_gather = 0");

    let wrapped = format!("SELECT count(*)::bigint FROM ({query}) AS _rvbbit_explain_analyze");
    let run = Spi::get_one::<i64>(&wrapped);

    if let Some(prev) = prev_parallel {
        let _ = Spi::run(&format!("SET max_parallel_workers_per_gather = {prev}"));
    }

    match run {
        Ok(Some(rows)) => MeasuredRun::Ok { rows },
        Ok(None) => MeasuredRun::Ok { rows: 0 },
        Err(e) => MeasuredRun::Failed {
            error: e.to_string(),
        },
    }
}

// ===========================================================================
// ESTIMATE — project the graph from EXPLAIN row estimates + receipt history.
// Does not execute the query (and so makes no external calls / spends no $).
// ===========================================================================

fn build_estimate_graph(query: &str) -> SemanticGraph {
    let trimmed = query.trim().trim_end_matches(';').trim();
    let rates = load_model_rates();

    // Per-operator row estimates from the EXPLAIN-plan walk.
    let per_op = estimate_invocations(trimmed);
    // Distinct call sites from the query text — each textual
    // `rvbbit.<op>(...)` is its own call site, keyed by (op, criterion).
    let calls = scan_rvbbit_calls(trimmed);

    let mut call_sites = Vec::new();
    let mut seen: BTreeSet<(String, String)> = BTreeSet::new();
    for call in &calls {
        let criterion = criterion_from_args(&call.args);
        if !seen.insert((call.name.clone(), criterion.clone())) {
            continue; // identical textual call — count it once
        }
        let (count, source) = per_op.get(&call.name).cloned().unwrap_or((None, None));
        call_sites.push(estimate_call_site(
            &call.name, &criterion, count, source, &rates,
        ));
    }
    // Operators the plan walk found but the text scan did not (e.g. infix
    // operator calls) — list them with an unknown criterion.
    for (op, (count, source)) in &per_op {
        if !calls.iter().any(|c| &c.name == op) {
            call_sites.push(estimate_call_site(op, "", *count, source.clone(), &rates));
        }
    }
    call_sites.sort_by(|a, b| {
        a.operator
            .cmp(&b.operator)
            .then_with(|| a.criterion.cmp(&b.criterion))
    });

    let mut notes = vec![
        "ESTIMATE projects cost from Postgres's own row estimates and your \
         receipt history — the query was NOT executed, no external calls \
         were made."
            .to_string(),
        "Invocation counts are a cold-cache upper bound; real cost is lower \
         by your cache hit rate. Run EXPLAIN SEMANTIC ANALYZE to measure."
            .to_string(),
    ];
    let uncosted = uncosted_models(&call_sites);
    if !uncosted.is_empty() {
        notes.push(format!(
            "Uncosted LLM models (no rvbbit.model_rates row): {}. Run \
             rvbbit.refresh_model_rates().",
            uncosted.join(", ")
        ));
    }

    SemanticGraph {
        query: query.to_string(),
        mode: GraphMode::Estimate,
        call_sites,
        notes,
    }
}

fn estimate_call_site(
    op: &str,
    criterion: &str,
    count: Option<u64>,
    source: Option<String>,
    rates: &HashMapRates,
) -> CallSite {
    let meta = lookup_operator_meta(op);
    let (shape, return_type, model) = match &meta {
        Some(m) => (m.shape.clone(), m.return_type.clone(), m.model.clone()),
        None => ("?".to_string(), "?".to_string(), String::new()),
    };
    let invocations = count.unwrap_or(0);

    // Endpoint shape + per-call tokens come from receipt history — the
    // record of past fresh executions of this operator.
    let history = op_endpoint_history(op);
    let mut endpoints: Vec<EndpointLine> = Vec::new();
    let mut annotation = source.map(|s| format!("planner estimate via EXPLAIN: {s}"));

    if !history.is_empty() {
        for h in &history {
            let kind = EndpointKind::from_subcall_kind(&h.kind);
            let tin = (invocations as f64 * h.avg_tokens_in).round() as u64;
            let tout = (invocations as f64 * h.avg_tokens_out).round() as u64;
            let cost = endpoint_cost(kind, &h.name, tin, tout, rates);
            endpoints.push(EndpointLine {
                kind,
                name: h.name.clone(),
                calls: invocations,
                tokens_in: tin,
                tokens_out: tout,
                latency_ms: 0,
                errors: 0,
                cost,
            });
        }
        let total: u64 = history.iter().map(|h| h.n).sum();
        annotation = Some(match annotation {
            Some(a) => format!("{a}; tokens/call from {total} historical fresh executions"),
            None => format!("tokens/call from {total} historical fresh executions"),
        });
    } else if !model.is_empty() {
        // No history yet — single-LLM fallback with a literal-token guess.
        let lit = literal_tokens_for(op).max(0) as u64;
        let tin = invocations.saturating_mul(lit);
        let cost = endpoint_cost(EndpointKind::Llm, &model, tin, 0, rates);
        endpoints.push(EndpointLine {
            kind: EndpointKind::Llm,
            name: model,
            calls: invocations,
            tokens_in: tin,
            tokens_out: 0,
            latency_ms: 0,
            errors: 0,
            cost,
        });
        annotation = Some(match annotation {
            Some(a) => format!("{a}; no receipt history — token estimate is rough"),
            None => "no receipt history — token estimate is rough".to_string(),
        });
    }
    sort_endpoints(&mut endpoints);

    CallSite {
        operator: op.to_string(),
        criterion: criterion.to_string(),
        shape,
        return_type,
        l1: 0,
        l2: 0,
        fresh: invocations,
        errors: 0,
        invocations,
        endpoints,
        annotation,
    }
}

/// Walk `EXPLAIN (VERBOSE, FORMAT JSON) <query>` and attribute the
/// planner's row estimates to each semantic operator call site. Does not
/// execute the query — EXPLAIN without ANALYZE only plans.
fn estimate_invocations(query: &str) -> BTreeMap<String, (Option<u64>, Option<String>)> {
    let mut out: BTreeMap<String, (Option<u64>, Option<String>)> = BTreeMap::new();
    let ops = load_operator_signatures();
    if ops.is_empty() {
        return out;
    }
    let explain_sql = format!("EXPLAIN (VERBOSE, FORMAT JSON) {query}");
    let json = match PgTryBuilder::new(AssertUnwindSafe(|| {
        Spi::get_one::<pgrx::Json>(&explain_sql)
    }))
    .catch_others(|_| Ok(None))
    .catch_rust_panic(|_| Ok(None))
    .execute()
    {
        Ok(Some(j)) => j.0,
        _ => return out,
    };
    let mut acc: BTreeMap<String, OpAccum> = BTreeMap::new();
    if let Some(arr) = json.as_array() {
        for item in arr {
            if let Some(plan) = item.get("Plan") {
                walk_plan(plan, &ops, &mut acc, &|rel| reltuples_of(rel));
            }
        }
    }
    for (name, a) in acc {
        // A filter-position operator runs on the rows feeding its node — an
        // upper bound, so take the max. An output-position operator runs on
        // the rows its node emits; carried up through a LIMIT that shrinks,
        // so take the min. Filter wins when an op appears as both.
        let count = a.filter_rows.or(a.output_rows);
        out.insert(name, (count, Some(a.source)));
    }
    out
}

/// Operator-detection sentinels. `fn_form_quoted` catches reserved-word
/// operator names — EXPLAIN renders e.g. `rvbbit.extract` as
/// `rvbbit."extract"`.
struct OpSig {
    name: String,
    fn_form: String,
    fn_form_quoted: String,
    infix: Option<String>,
}

fn load_operator_signatures() -> Vec<OpSig> {
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select("SELECT name, infix_symbol FROM rvbbit.operators", None, &[])?;
        for row in table {
            if let Some(name) = row.get::<String>(1)? {
                let infix: Option<String> = row.get(2)?;
                out.push(OpSig {
                    fn_form: format!("rvbbit.{name}("),
                    fn_form_quoted: format!("rvbbit.\"{name}\"("),
                    infix: infix.filter(|s| !s.is_empty()),
                    name,
                });
            }
        }
        Ok(())
    });
    out
}

/// Per-operator row estimate accumulated across the plan tree.
#[derive(Default)]
struct OpAccum {
    /// Max input-row count over filter-position appearances.
    filter_rows: Option<u64>,
    /// Min output-row count over projection-position appearances.
    output_rows: Option<u64>,
    source: String,
}

fn walk_plan(
    node: &Value,
    ops: &[OpSig],
    acc: &mut BTreeMap<String, OpAccum>,
    leaf_rows: &dyn Fn(&str) -> Option<u64>,
) {
    // Condition text — operators here run on the node's *input* rows.
    let mut filter_text = String::new();
    for key in [
        "Filter",
        "Index Cond",
        "Recheck Cond",
        "Hash Cond",
        "Join Filter",
        "One-Time Filter",
        "Merge Cond",
    ] {
        if let Some(s) = node.get(key).and_then(|v| v.as_str()) {
            filter_text.push_str(s);
            filter_text.push(' ');
        }
    }
    // Projection text — operators here run on the node's *output* rows.
    let mut output_text = String::new();
    if let Some(out_arr) = node.get("Output").and_then(|v| v.as_array()) {
        for o in out_arr {
            if let Some(s) = o.as_str() {
                output_text.push_str(s);
                output_text.push(' ');
            }
        }
    }

    let children = node.get("Plans").and_then(|v| v.as_array());
    let node_rows = node
        .get("Plan Rows")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0)
        .max(0.0) as u64;
    let input_rows: u64 = match children {
        Some(ch) if !ch.is_empty() => ch
            .iter()
            .filter_map(|c| c.get("Plan Rows").and_then(|v| v.as_f64()))
            .sum::<f64>()
            .max(0.0) as u64,
        _ => {
            // Leaf: Plan Rows is post-filter. Use the relation's live
            // estimate so a selective filter doesn't undercount the calls.
            node.get("Relation Name")
                .and_then(|v| v.as_str())
                .and_then(leaf_rows)
                .unwrap_or(node_rows)
        }
    };

    let node_type = node
        .get("Node Type")
        .and_then(|v| v.as_str())
        .unwrap_or("node");
    let source = match node.get("Relation Name").and_then(|v| v.as_str()) {
        Some(r) => format!("{node_type} on {r}"),
        None => node_type.to_string(),
    };

    for op in ops {
        let mentions = |text: &str| -> bool {
            contains_outside_single_quotes(text, &op.fn_form)
                || contains_outside_single_quotes(text, &op.fn_form_quoted)
                || op
                    .infix
                    .as_deref()
                    .map(|s| contains_outside_single_quotes(text, s))
                    .unwrap_or(false)
        };
        let in_filter = mentions(&filter_text);
        let in_output = mentions(&output_text);
        if !in_filter && !in_output {
            continue;
        }
        let e = acc.entry(op.name.clone()).or_default();
        if e.source.is_empty() {
            e.source = source.clone();
        }
        if in_filter {
            e.filter_rows = Some(e.filter_rows.map_or(input_rows, |m| m.max(input_rows)));
            e.source = source.clone(); // a filter node is the better source
        }
        if in_output {
            e.output_rows = Some(e.output_rows.map_or(node_rows, |m| m.min(node_rows)));
        }
    }

    if let Some(ch) = children {
        for c in ch {
            walk_plan(c, ops, acc, leaf_rows);
        }
    }
}

fn reltuples_of(relname: &str) -> Option<u64> {
    let esc = relname.replace('\'', "''");
    let sql = format!(
        "SELECT reltuples::float8 FROM pg_class \
         WHERE relname = '{esc}' AND relkind IN ('r','m','p','f') \
         ORDER BY relkind LIMIT 1"
    );
    let n = Spi::get_one::<f64>(&sql).ok().flatten()?;
    if n < 1.0 {
        None // -1 = never analyzed, 0 = empty — let the caller fall back
    } else {
        Some(n as u64)
    }
}

/// One endpoint's history, averaged over past fresh executions (receipts).
struct EndpointHistory {
    kind: String,
    name: String,
    avg_tokens_in: f64,
    avg_tokens_out: f64,
    n: u64,
}

fn op_endpoint_history(op: &str) -> Vec<EndpointHistory> {
    let esc = op.replace('\'', "''");
    let sql = format!(
        "SELECT sub->>'kind' AS kind, \
                coalesce(sub->>'model', '(unknown)') AS name, \
                avg(coalesce((sub->>'tokens_in')::float8, 0)) AS avg_in, \
                avg(coalesce((sub->>'tokens_out')::float8, 0)) AS avg_out, \
                count(*)::bigint AS n \
         FROM rvbbit.receipts r, jsonb_array_elements(r.sub_calls) AS sub \
         WHERE r.operator = '{esc}' AND r.error IS NULL \
         GROUP BY 1, 2"
    );
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let kind: Option<String> = row.get(1)?;
            let name: Option<String> = row.get(2)?;
            let avg_in: Option<f64> = row.get(3)?;
            let avg_out: Option<f64> = row.get(4)?;
            let n: Option<i64> = row.get(5)?;
            if let (Some(k), Some(nm)) = (kind, name) {
                out.push(EndpointHistory {
                    kind: k,
                    name: nm,
                    avg_tokens_in: avg_in.unwrap_or(0.0),
                    avg_tokens_out: avg_out.unwrap_or(0.0),
                    n: n.unwrap_or(0).max(0) as u64,
                });
            }
        }
        Ok(())
    });
    out
}

/// Rough literal-token count for an operator's prompt template — the
/// no-history fallback. Counts the operator's own system+user prompt
/// plus a flat allowance for the dynamic input column.
fn literal_tokens_for(op: &str) -> i32 {
    let esc = op.replace('\'', "''");
    let sql = format!(
        "SELECT coalesce(system_prompt,'') || ' ' || coalesce(user_prompt,'') \
         FROM rvbbit.operators WHERE name = '{esc}'"
    );
    let prompt = Spi::get_one::<String>(&sql)
        .ok()
        .flatten()
        .unwrap_or_default();
    let Ok(bpe) = cl100k_base() else { return 0 };
    // Prompt template tokens + a flat 250-token allowance for the row's
    // text column (which we cannot see without running the query).
    bpe.encode_with_special_tokens(&prompt).len() as i32 + 250
}

// ===========================================================================
// Renderer — one function, both modes.
// ===========================================================================

fn render_graph(g: &SemanticGraph) -> Vec<String> {
    let estimate = g.is_estimate();
    let mut out = Vec::new();
    out.push("Semantic Execution Graph".to_string());
    out.push("========================".to_string());

    match &g.mode {
        GraphMode::Estimate => {
            out.push("Mode:   ESTIMATE  (projected — query was NOT executed)".to_string());
        }
        GraphMode::Analyze {
            result_rows,
            wall_ms,
            failed,
        } => {
            out.push("Mode:   ANALYZE   (query executed once)".to_string());
            if let Some(err) = failed {
                out.push(format!("Result: FAILED — {err}"));
            } else if let (Some(r), Some(ms)) = (result_rows, wall_ms) {
                out.push(format!("Result: {r} row(s) in {ms} ms"));
            } else {
                out.push(
                    "Result: measured during EXPLAIN ANALYZE — see the plan above \
                     for row counts and timing."
                        .to_string(),
                );
            }
        }
    }

    out.push("Query:".to_string());
    for line in g.query.trim().lines() {
        out.push(format!("  {line}"));
    }
    out.push(String::new());

    if g.call_sites.is_empty() {
        out.push("No semantic operators in this query.".to_string());
        return out;
    }

    out.push("Call sites".to_string());
    out.push("----------".to_string());
    for cs in &g.call_sites {
        render_call_site(cs, estimate, &mut out);
    }

    render_summary(g, estimate, &mut out);

    out.push(String::new());
    out.push("Notes".to_string());
    for n in &g.notes {
        out.push(format!("  - {n}"));
    }
    out
}

fn render_call_site(cs: &CallSite, estimate: bool, out: &mut Vec<String>) {
    out.push(format!(
        "  rvbbit.{}  [{} -> {}]",
        cs.operator, cs.shape, cs.return_type
    ));
    if !cs.criterion.is_empty() {
        out.push(format!("    criterion    '{}'", cs.criterion));
    }
    if let Some(a) = &cs.annotation {
        out.push(format!("    note: {a}"));
    }

    if estimate {
        out.push(format!(
            "    invocations  ~{} (cold-cache upper bound)",
            cs.invocations
        ));
        out.push("    cascade      assumed cold — all invocations counted as fresh".to_string());
    } else {
        let inv = cs.invocations;
        out.push(format!(
            "    invocations  {inv}   cascade: L1 {} ({})  L2 {} ({})  fresh {} ({})",
            cs.l1,
            pct(cs.l1, inv),
            cs.l2,
            pct(cs.l2, inv),
            cs.fresh,
            pct(cs.fresh, inv),
        ));
        if cs.errors > 0 {
            out.push(format!(
                "    errors       {} fresh execution(s) failed",
                cs.errors
            ));
        }
    }

    if cs.endpoints.is_empty() {
        if !estimate && cs.fresh == 0 && cs.invocations > 0 {
            out.push("    -> served entirely from cache — no external calls".to_string());
        } else {
            out.push("    -> (no external endpoints recorded)".to_string());
        }
    }
    for ep in &cs.endpoints {
        render_endpoint(ep, estimate, out);
    }
    out.push(String::new());
}

fn render_endpoint(ep: &EndpointLine, estimate: bool, out: &mut Vec<String>) {
    let tilde = if estimate { "~" } else { "" };
    let mut parts: Vec<String> = Vec::new();
    parts.push(format!("{tilde}{} calls", ep.calls));

    match ep.kind {
        EndpointKind::Llm => {
            parts.push(format!(
                "{tilde}{} in / {tilde}{} out tok",
                ep.tokens_in, ep.tokens_out
            ));
            if !estimate {
                parts.push(format!("{} ms", ep.latency_ms));
            }
            match ep.cost {
                EndpointCost::Billable(v) => parts.push(format!(
                    "{}{}",
                    if estimate { "est. " } else { "" },
                    fmt_usd(v)
                )),
                EndpointCost::Uncosted => parts.push("cost: uncosted (no rate)".to_string()),
                EndpointCost::Local => {}
            }
        }
        EndpointKind::Sidecar | EndpointKind::Code => {
            if !estimate {
                parts.push(format!("{} ms", ep.latency_ms));
            }
            parts.push("local ($0)".to_string());
        }
    }
    if ep.errors > 0 {
        parts.push(format!("{} errored", ep.errors));
    }

    out.push(format!(
        "    -> {:<8} {:<32} {}",
        ep.kind.label(),
        ep.name,
        parts.join(" · ")
    ));
}

/// Cross-cut totals over a graph's call sites.
struct GraphSummary {
    llm_calls: u64,
    sidecar_calls: u64,
    code_calls: u64,
    cache_served: u64,
    total_cost: f64,
    any_uncosted: bool,
}

fn compute_summary(call_sites: &[CallSite]) -> GraphSummary {
    let mut s = GraphSummary {
        llm_calls: 0,
        sidecar_calls: 0,
        code_calls: 0,
        cache_served: 0,
        total_cost: 0.0,
        any_uncosted: false,
    };
    for cs in call_sites {
        s.cache_served += cs.l1 + cs.l2;
        for ep in &cs.endpoints {
            match ep.kind {
                EndpointKind::Llm => s.llm_calls += ep.calls,
                EndpointKind::Sidecar => s.sidecar_calls += ep.calls,
                EndpointKind::Code => s.code_calls += ep.calls,
            }
            match ep.cost {
                EndpointCost::Billable(v) => s.total_cost += v,
                EndpointCost::Uncosted => s.any_uncosted = true,
                EndpointCost::Local => {}
            }
        }
    }
    s
}

fn render_summary(g: &SemanticGraph, estimate: bool, out: &mut Vec<String>) {
    let GraphSummary {
        llm_calls,
        sidecar_calls,
        code_calls,
        cache_served,
        total_cost,
        any_uncosted,
    } = compute_summary(&g.call_sites);

    out.push("External call summary".to_string());
    out.push("---------------------".to_string());
    let cost_label = if estimate { "est. " } else { "" };
    out.push(format!(
        "  LLM      {:>8} calls   billable   {}{}{}",
        llm_calls,
        cost_label,
        fmt_usd(total_cost),
        if any_uncosted { "  (+ uncosted)" } else { "" },
    ));
    out.push(format!("  SIDECAR  {sidecar_calls:>8} calls   local ($0)"));
    out.push(format!("  CODE     {code_calls:>8} calls   local ($0)"));
    if !estimate {
        out.push(format!(
            "  cache    {cache_served:>8} served  no external call"
        ));
    }
    out.push(String::new());

    if estimate {
        out.push(format!(
            "Projected cost (cold-cache upper bound): {}{}",
            fmt_usd(total_cost),
            if any_uncosted {
                "  + uncosted models"
            } else {
                ""
            }
        ));
    } else {
        out.push(format!(
            "Total semantic cost: {}   (LLM calls only — sidecars and code run locally)",
            fmt_usd(total_cost)
        ));
    }
}

// ===========================================================================
// Native `EXPLAIN (SEMANTIC)` — PG18 extensible-EXPLAIN integration.
//
// Registers a custom EXPLAIN option so the semantic execution graph can be
// requested as `EXPLAIN (SEMANTIC) <query>` / `EXPLAIN (SEMANTIC, ANALYZE)
// <query>`. The graph is *appended* after the normal relational plan as its
// own section — never merged into the plan tree. Two separate runstreams,
// two separate outputs (in FORMAT JSON it is a sibling key of "Plan").
// ===========================================================================

static mut PREV_EXPLAIN_ONE_QUERY_HOOK: pg_sys::ExplainOneQuery_hook_type = None;
static mut PREV_EXPLAIN_PER_PLAN_HOOK: pg_sys::explain_per_plan_hook_type = None;
static mut SEMANTIC_EXT_ID: c_int = -1;

/// Install the `SEMANTIC` EXPLAIN option and the supporting hooks. Called
/// once per backend from `_PG_init`.
pub unsafe fn register_explain_semantic() {
    SEMANTIC_EXT_ID = pg_sys::GetExplainExtensionId(c"pg_rvbbit".as_ptr());
    pg_sys::RegisterExtensionExplainOption(c"semantic".as_ptr(), Some(semantic_option_handler));

    PREV_EXPLAIN_ONE_QUERY_HOOK = pg_sys::ExplainOneQuery_hook;
    pg_sys::ExplainOneQuery_hook = Some(rvbbit_explain_one_query);

    PREV_EXPLAIN_PER_PLAN_HOOK = pg_sys::explain_per_plan_hook;
    pg_sys::explain_per_plan_hook = Some(rvbbit_explain_per_plan);
}

/// True when `(SEMANTIC)` was requested on this EXPLAIN.
unsafe fn semantic_requested(es: *mut pg_sys::ExplainState) -> bool {
    SEMANTIC_EXT_ID >= 0 && !pg_sys::GetExplainExtensionState(es, SEMANTIC_EXT_ID).is_null()
}

/// Handles `(SEMANTIC [true|false])` in the EXPLAIN option list.
#[pg_guard]
unsafe extern "C-unwind" fn semantic_option_handler(
    es: *mut pg_sys::ExplainState,
    opt: *mut pg_sys::DefElem,
    _pstate: *mut pg_sys::ParseState,
) {
    if pg_sys::defGetBoolean(opt) {
        // Non-null sentinel — we only ever test presence, never deref.
        pg_sys::SetExplainExtensionState(es, SEMANTIC_EXT_ID, 1usize as *mut c_void);
    }
}

/// Wraps `ExplainOneQuery` so the probe can be armed *before* EXPLAIN
/// ANALYZE executes the query — the probe is thread-local and must see the
/// leader backend's operator calls, so parallelism is pinned off too.
#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_explain_one_query(
    query: *mut pg_sys::Query,
    cursor_options: c_int,
    into: *mut pg_sys::IntoClause,
    es: *mut pg_sys::ExplainState,
    query_string: *const c_char,
    params: pg_sys::ParamListInfo,
    query_env: *mut pg_sys::QueryEnvironment,
) {
    let measure = semantic_requested(es) && (*es).analyze;
    let mut saved_parallel: Option<String> = None;
    if measure {
        saved_parallel = guc_get("max_parallel_workers_per_gather");
        guc_set("max_parallel_workers_per_gather", "0");
        crate::probe::arm();
    }

    if let Some(prev) = PREV_EXPLAIN_ONE_QUERY_HOOK {
        prev(
            query,
            cursor_options,
            into,
            es,
            query_string,
            params,
            query_env,
        );
    } else {
        pg_sys::standard_ExplainOneQuery(
            query,
            cursor_options,
            into,
            es,
            query_string,
            params,
            query_env,
        );
    }

    if measure {
        if let Some(prev) = saved_parallel {
            guc_set("max_parallel_workers_per_gather", &prev);
        }
    }
}

/// Appends the semantic execution graph after the relational plan. Fires
/// once per plan, inside `ExplainOnePlan` — the right grouping level for a
/// sibling section.
#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_explain_per_plan(
    plannedstmt: *mut pg_sys::PlannedStmt,
    into: *mut pg_sys::IntoClause,
    es: *mut pg_sys::ExplainState,
    query_string: *const c_char,
    params: pg_sys::ParamListInfo,
    query_env: *mut pg_sys::QueryEnvironment,
) {
    if let Some(prev) = PREV_EXPLAIN_PER_PLAN_HOOK {
        prev(plannedstmt, into, es, query_string, params, query_env);
    }
    if !semantic_requested(es) {
        return;
    }

    let full = if query_string.is_null() {
        String::new()
    } else {
        CStr::from_ptr(query_string).to_string_lossy().into_owned()
    };
    let inner = strip_explain_prefix(&full);

    let graph = if (*es).analyze {
        let tallies = crate::probe::disarm();
        analyze_graph_from_parts(&inner, &tallies, None, None, None)
    } else {
        build_estimate_graph(&inner)
    };

    emit_graph_section(es, &graph);
}

/// Render the graph into the EXPLAIN output. Text format: appended verbatim
/// (keeps its column-0 layout, visually distinct from the indented plan).
/// JSON/YAML/XML: a single sibling property next to "Plan".
unsafe fn emit_graph_section(es: *mut pg_sys::ExplainState, graph: &SemanticGraph) {
    if (*es).format == pg_sys::ExplainFormat::EXPLAIN_FORMAT_TEXT {
        // Text: append the hand-rendered graph verbatim. Leading + trailing
        // blank line so the section is clearly delimited and EXPLAIN's own
        // trailing lines (e.g. "Execution Time:") don't run onto it.
        let text = render_graph(graph).join("\n");
        if let Ok(c_text) = CString::new(text) {
            pg_sys::appendStringInfoString((*es).str_, c"\n".as_ptr());
            pg_sys::appendStringInfoString((*es).str_, c_text.as_ptr());
            pg_sys::appendStringInfoString((*es).str_, c"\n".as_ptr());
        }
    } else {
        // JSON / YAML / XML: a real structured object (sibling of "Plan").
        emit_graph_structured(es, graph);
    }
}

// --- Structured emit via the EXPLAIN property API (non-text formats) -------

unsafe fn open_group(
    es: *mut pg_sys::ExplainState,
    objtype: &CStr,
    labelname: Option<&CStr>,
    labeled: bool,
) {
    let ln = labelname.map_or(std::ptr::null(), CStr::as_ptr);
    pg_sys::ExplainOpenGroup(objtype.as_ptr(), ln, labeled, es);
}

unsafe fn close_group(
    es: *mut pg_sys::ExplainState,
    objtype: &CStr,
    labelname: Option<&CStr>,
    labeled: bool,
) {
    let ln = labelname.map_or(std::ptr::null(), CStr::as_ptr);
    pg_sys::ExplainCloseGroup(objtype.as_ptr(), ln, labeled, es);
}

unsafe fn prop_text(es: *mut pg_sys::ExplainState, label: &CStr, value: &str) {
    let v = CString::new(value).unwrap_or_default();
    pg_sys::ExplainPropertyText(label.as_ptr(), v.as_ptr(), es);
}

unsafe fn prop_int(es: *mut pg_sys::ExplainState, label: &CStr, value: i64) {
    pg_sys::ExplainPropertyInteger(label.as_ptr(), std::ptr::null(), value, es);
}

unsafe fn prop_float(es: *mut pg_sys::ExplainState, label: &CStr, value: f64) {
    pg_sys::ExplainPropertyFloat(label.as_ptr(), std::ptr::null(), value, 6, es);
}

/// Emit the graph as a real nested object so `EXPLAIN (SEMANTIC, FORMAT
/// JSON)` yields structured data (a sibling key of "Plan"), not a string
/// blob. The property API renders correctly for JSON, YAML and XML alike.
unsafe fn emit_graph_structured(es: *mut pg_sys::ExplainState, graph: &SemanticGraph) {
    let estimate = graph.is_estimate();
    open_group(
        es,
        c"Semantic Execution Graph",
        Some(c"Semantic Execution Graph"),
        true,
    );

    match &graph.mode {
        GraphMode::Estimate => prop_text(es, c"Mode", "ESTIMATE"),
        GraphMode::Analyze {
            result_rows,
            wall_ms,
            failed,
        } => {
            prop_text(es, c"Mode", "ANALYZE");
            if let Some(err) = failed {
                prop_text(es, c"Error", err);
            }
            if let Some(r) = result_rows {
                prop_int(es, c"Result Rows", *r);
            }
            if let Some(ms) = wall_ms {
                prop_int(es, c"Wall Ms", *ms as i64);
            }
        }
    }

    let summary = compute_summary(&graph.call_sites);

    open_group(es, c"Call Sites", Some(c"Call Sites"), false);
    for cs in &graph.call_sites {
        open_group(es, c"Call Site", None, true);
        prop_text(es, c"Operator", &cs.operator);
        if !cs.criterion.is_empty() {
            prop_text(es, c"Criterion", &cs.criterion);
        }
        prop_text(es, c"Shape", &cs.shape);
        prop_text(es, c"Return Type", &cs.return_type);
        prop_int(es, c"Invocations", cs.invocations as i64);
        prop_text(
            es,
            c"Invocations Kind",
            if estimate { "estimated" } else { "measured" },
        );
        if !estimate {
            prop_int(es, c"Cache L1", cs.l1 as i64);
            prop_int(es, c"Cache L2", cs.l2 as i64);
            prop_int(es, c"Fresh", cs.fresh as i64);
            if cs.errors > 0 {
                prop_int(es, c"Errors", cs.errors as i64);
            }
        }
        if let Some(a) = &cs.annotation {
            prop_text(es, c"Note", a);
        }

        open_group(es, c"Endpoints", Some(c"Endpoints"), false);
        for ep in &cs.endpoints {
            open_group(es, c"Endpoint", None, true);
            prop_text(es, c"Kind", ep.kind.label());
            prop_text(es, c"Name", &ep.name);
            prop_int(es, c"Calls", ep.calls as i64);
            prop_int(es, c"Tokens In", ep.tokens_in as i64);
            prop_int(es, c"Tokens Out", ep.tokens_out as i64);
            if !estimate {
                prop_int(es, c"Latency Ms", ep.latency_ms as i64);
            }
            match ep.cost {
                EndpointCost::Billable(v) => {
                    prop_text(es, c"Cost Status", "billable");
                    prop_float(es, c"Cost USD", v);
                }
                EndpointCost::Uncosted => prop_text(es, c"Cost Status", "uncosted"),
                EndpointCost::Local => {
                    prop_text(es, c"Cost Status", "local");
                    prop_float(es, c"Cost USD", 0.0);
                }
            }
            if ep.errors > 0 {
                prop_int(es, c"Errors", ep.errors as i64);
            }
            close_group(es, c"Endpoint", None, true);
        }
        close_group(es, c"Endpoints", Some(c"Endpoints"), false);

        close_group(es, c"Call Site", None, true);
    }
    close_group(es, c"Call Sites", Some(c"Call Sites"), false);

    open_group(
        es,
        c"External Call Summary",
        Some(c"External Call Summary"),
        true,
    );
    prop_int(es, c"LLM Calls", summary.llm_calls as i64);
    prop_int(es, c"Sidecar Calls", summary.sidecar_calls as i64);
    prop_int(es, c"Code Calls", summary.code_calls as i64);
    if !estimate {
        prop_int(es, c"Cache Served", summary.cache_served as i64);
    }
    prop_float(es, c"Total Cost USD", summary.total_cost);
    prop_text(
        es,
        c"Cost Basis",
        if estimate {
            "projected (cold-cache upper bound)"
        } else {
            "measured (LLM only — sidecar + code are local)"
        },
    );
    close_group(
        es,
        c"External Call Summary",
        Some(c"External Call Summary"),
        true,
    );

    close_group(
        es,
        c"Semantic Execution Graph",
        Some(c"Semantic Execution Graph"),
        true,
    );
}

/// Strip a leading `EXPLAIN (...)` so we are left with the inner query.
/// `EXPLAIN`'s `queryString` is the full command text; `SEMANTIC` always
/// arrives inside the parenthesised option list.
fn strip_explain_prefix(full: &str) -> String {
    let t = full.trim_start();
    if t.len() >= 7 && t[..7].eq_ignore_ascii_case("explain") {
        let rest = t[7..].trim_start();
        let rb = rest.as_bytes();
        if !rb.is_empty() && rb[0] == b'(' {
            if let Some(close) = find_matching_paren(rb, 0) {
                return rest[close + 1..]
                    .trim()
                    .trim_end_matches(';')
                    .trim()
                    .to_string();
            }
        }
        return rest.trim().trim_end_matches(';').trim().to_string();
    }
    full.trim().trim_end_matches(';').trim().to_string()
}

unsafe fn guc_get(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = pg_sys::GetConfigOption(cname.as_ptr(), true, false);
    if ptr.is_null() {
        None
    } else {
        Some(CStr::from_ptr(ptr).to_string_lossy().into_owned())
    }
}

unsafe fn guc_set(name: &str, value: &str) {
    let (Ok(cname), Ok(cval)) = (CString::new(name), CString::new(value)) else {
        return;
    };
    pg_sys::SetConfigOption(
        cname.as_ptr(),
        cval.as_ptr(),
        pg_sys::GucContext::PGC_USERSET,
        pg_sys::GucSource::PGC_S_SESSION,
    );
}

// ===========================================================================
// OpenRouter rate refresh.
// ===========================================================================

#[derive(serde::Deserialize)]
struct OrModelsResponse {
    #[serde(default)]
    data: Vec<OrModel>,
}

#[derive(serde::Deserialize)]
struct OrModel {
    id: String,
    #[serde(default)]
    pricing: OrPricing,
}

#[derive(serde::Deserialize, Default)]
struct OrPricing {
    #[serde(default)]
    prompt: String,
    #[serde(default)]
    completion: String,
}

fn fetch_openrouter_rates() -> Result<i64, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let mut req = client.get("https://openrouter.ai/api/v1/models");
    // The /models catalogue is public; send the key if we have one anyway.
    if let Ok(key) = std::env::var("OPENROUTER_API_KEY") {
        if !key.is_empty() {
            req = req.bearer_auth(key);
        }
    }
    let resp = req.send().map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!(
            "OpenRouter /models returned HTTP {}",
            resp.status().as_u16()
        ));
    }
    let body: OrModelsResponse = resp.json().map_err(|e| e.to_string())?;

    // OpenRouter prices are USD per *token* as strings; we store per-Mtok.
    let mut rows: Vec<(String, f64, f64)> = Vec::new();
    for m in body.data {
        let (Ok(p_in), Ok(p_out)) = (
            m.pricing.prompt.parse::<f64>(),
            m.pricing.completion.parse::<f64>(),
        ) else {
            continue;
        };
        if p_in < 0.0 || p_out < 0.0 {
            continue;
        }
        let in_mtok = p_in * 1_000_000.0;
        let out_mtok = p_out * 1_000_000.0;
        // model_rates is numeric(12,6) — skip anything that would overflow.
        if in_mtok >= 1_000_000.0 || out_mtok >= 1_000_000.0 {
            continue;
        }
        rows.push((m.id, in_mtok, out_mtok));
    }
    if rows.is_empty() {
        return Ok(0);
    }

    let mut values = String::new();
    for (i, (model, p_in, p_out)) in rows.iter().enumerate() {
        if i > 0 {
            values.push(',');
        }
        let m = model.replace('\'', "''");
        values.push_str(&format!("('{m}',{p_in:.6},{p_out:.6},'USD',now())"));
    }
    let sql = format!(
        "INSERT INTO rvbbit.model_rates \
            (model, input_per_mtok, output_per_mtok, currency, updated_at) \
         VALUES {values} \
         ON CONFLICT (model) DO UPDATE SET \
            input_per_mtok = EXCLUDED.input_per_mtok, \
            output_per_mtok = EXCLUDED.output_per_mtok, \
            currency = EXCLUDED.currency, \
            updated_at = now()"
    );
    Spi::run(&sql).map_err(|e| e.to_string())?;
    Ok(rows.len() as i64)
}

// ===========================================================================
// Cost helpers + catalog lookups.
// ===========================================================================

type HashMapRates = std::collections::HashMap<String, ModelRate>;

struct ModelRate {
    input_per_mtok: f64,
    output_per_mtok: f64,
}

fn load_model_rates() -> HashMapRates {
    let mut out = HashMapRates::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            "SELECT model, input_per_mtok::float8, output_per_mtok::float8 \
             FROM rvbbit.model_rates",
            None,
            &[],
        )?;
        for row in table {
            let model: Option<String> = row.get(1)?;
            let input: Option<f64> = row.get(2)?;
            let output: Option<f64> = row.get(3)?;
            if let (Some(m), Some(i), Some(o)) = (model, input, output) {
                out.insert(
                    m,
                    ModelRate {
                        input_per_mtok: i,
                        output_per_mtok: o,
                    },
                );
            }
        }
        Ok(())
    });
    out
}

fn endpoint_cost(
    kind: EndpointKind,
    name: &str,
    tokens_in: u64,
    tokens_out: u64,
    rates: &HashMapRates,
) -> EndpointCost {
    match kind {
        EndpointKind::Sidecar | EndpointKind::Code => EndpointCost::Local,
        EndpointKind::Llm => match rates.get(name) {
            Some(r) => EndpointCost::Billable(model_cost(r, tokens_in, tokens_out)),
            None => EndpointCost::Uncosted,
        },
    }
}

fn model_cost(rate: &ModelRate, tokens_in: u64, tokens_out: u64) -> f64 {
    (tokens_in as f64 / 1_000_000.0) * rate.input_per_mtok
        + (tokens_out as f64 / 1_000_000.0) * rate.output_per_mtok
}

fn uncosted_models(call_sites: &[CallSite]) -> Vec<String> {
    let mut set: BTreeSet<String> = BTreeSet::new();
    for cs in call_sites {
        for ep in &cs.endpoints {
            if matches!(ep.cost, EndpointCost::Uncosted) {
                set.insert(ep.name.clone());
            }
        }
    }
    set.into_iter().collect()
}

fn sort_endpoints(endpoints: &mut [EndpointLine]) {
    endpoints.sort_by(|a, b| {
        a.kind
            .priority()
            .cmp(&b.kind.priority())
            .then_with(|| a.name.cmp(&b.name))
    });
}

fn pct(num: u64, denom: u64) -> String {
    if denom == 0 {
        "—".to_string()
    } else {
        format!("{:.1}%", (num as f64 / denom as f64) * 100.0)
    }
}

fn fmt_usd(v: f64) -> String {
    if v <= 0.0 {
        "$0.00".to_string()
    } else if v < 0.01 {
        format!("${v:.6}")
    } else {
        format!("${v:.4}")
    }
}

struct OpMeta {
    shape: String,
    return_type: String,
    model: String,
}

fn lookup_operator_meta(name: &str) -> Option<OpMeta> {
    let name_esc = name.replace('\'', "''");
    let sql =
        format!("SELECT shape, return_type, model FROM rvbbit.operators WHERE name = '{name_esc}'");
    let mut out: Option<OpMeta> = None;
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let shape: Option<String> = row.get(1)?;
            let return_type: Option<String> = row.get(2)?;
            let model: Option<String> = row.get(3)?;
            if let (Some(s), Some(r), Some(m)) = (shape, return_type, model) {
                out = Some(OpMeta {
                    shape: s,
                    return_type: r,
                    model: m,
                });
            }
        }
        Ok(())
    });
    out
}

// ---------------------------------------------------------------------------
// Textual scan for rvbbit.<op>(...) calls — secondary operator detection
// (catches anything the EXPLAIN-plan walk misses).

#[derive(Debug)]
struct RvbbitCall {
    name: String,
    args: Vec<String>,
}

/// The call-site-defining criterion from a parsed call's args: every
/// argument after the first (the `text` subject), de-quoted, joined.
fn criterion_from_args(args: &[String]) -> String {
    args.iter()
        .skip(1)
        .map(|a| unquote(a.trim()))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Strip one layer of surrounding SQL quotes, if present.
fn unquote(s: &str) -> String {
    let b = s.as_bytes();
    if b.len() >= 2 && (b[0] == b'\'' || b[0] == b'"') && b[b.len() - 1] == b[0] {
        let inner = &s[1..s.len() - 1];
        if b[0] == b'\'' {
            inner.replace("''", "'")
        } else {
            inner.replace("\"\"", "\"")
        }
    } else {
        s.to_string()
    }
}

fn scan_rvbbit_calls(query: &str) -> Vec<RvbbitCall> {
    const SENTINEL: &str = "rvbbit.";
    let mut out = Vec::new();
    let bytes = query.as_bytes();
    let mut i = 0usize;
    while i + SENTINEL.len() < bytes.len() {
        let c = bytes[i] as char;
        if c == '\'' {
            i = skip_quoted(bytes, i, '\'');
            continue;
        }
        if c == '"' {
            i = skip_quoted(bytes, i, '"');
            continue;
        }
        if &query[i..i + SENTINEL.len()] == SENTINEL {
            if i > 0 {
                let prev = bytes[i - 1] as char;
                if prev.is_ascii_alphanumeric() || prev == '_' {
                    i += 1;
                    continue;
                }
            }
            let name_start = i + SENTINEL.len();
            let mut j = name_start;
            while j < bytes.len() && (bytes[j] as char).is_ascii_alphanumeric()
                || (j < bytes.len() && bytes[j] == b'_')
            {
                j += 1;
            }
            if j == name_start {
                i += 1;
                continue;
            }
            let name = query[name_start..j].to_string();
            let mut k = j;
            while k < bytes.len() && (bytes[k] as char).is_ascii_whitespace() {
                k += 1;
            }
            if k >= bytes.len() || bytes[k] != b'(' {
                i = j;
                continue;
            }
            match find_matching_paren(bytes, k) {
                Some(close) => {
                    let inner = &query[k + 1..close];
                    let args = split_top_level_commas(inner);
                    out.push(RvbbitCall { name, args });
                    i = close + 1;
                }
                None => break,
            }
        } else {
            i += 1;
        }
    }
    out
}

fn contains_outside_single_quotes(text: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return true;
    }
    let bytes = text.as_bytes();
    let needle = needle.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'\'' {
            i = skip_quoted(bytes, i, '\'');
            continue;
        }
        if bytes[i..].starts_with(needle) {
            return true;
        }
        i += 1;
    }
    false
}

fn skip_quoted(bytes: &[u8], start: usize, quote: char) -> usize {
    let mut i = start + 1;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if c == quote {
            if i + 1 < bytes.len() && bytes[i + 1] as char == quote {
                i += 2;
                continue;
            }
            return i + 1;
        }
        i += 1;
    }
    bytes.len()
}

fn find_matching_paren(bytes: &[u8], open: usize) -> Option<usize> {
    debug_assert_eq!(bytes[open], b'(');
    let mut depth = 0i32;
    let mut i = open;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if c == '\'' || c == '"' {
            i = skip_quoted(bytes, i, c);
            continue;
        }
        if c == '(' {
            depth += 1;
        } else if c == ')' {
            depth -= 1;
            if depth == 0 {
                return Some(i);
            }
        }
        i += 1;
    }
    None
}

fn split_top_level_commas(s: &str) -> Vec<String> {
    let bytes = s.as_bytes();
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut start = 0usize;
    let mut i = 0usize;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if c == '\'' || c == '"' {
            i = skip_quoted(bytes, i, c);
            continue;
        }
        if c == '(' {
            depth += 1;
        } else if c == ')' {
            depth -= 1;
        } else if c == ',' && depth == 0 {
            out.push(s[start..i].trim().to_string());
            start = i + 1;
        }
        i += 1;
    }
    let tail = s[start..].trim();
    if !tail.is_empty() || !out.is_empty() {
        out.push(tail.to_string());
    }
    out
}

// ===========================================================================
// Tests (pure Rust — run via cargo test).
// ===========================================================================

#[cfg(test)]
mod scan_tests {
    use super::*;

    #[test]
    fn detects_simple_call() {
        let calls = scan_rvbbit_calls("SELECT * FROM t WHERE rvbbit.means(body, 'angry customer')");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "means");
        assert_eq!(calls[0].args.len(), 2);
    }

    #[test]
    fn ignores_matches_inside_strings() {
        let calls = scan_rvbbit_calls("SELECT 'rvbbit.means(x)' AS lit");
        assert!(calls.is_empty(), "got {:?}", calls);
    }

    #[test]
    fn handles_nested_parens_in_args() {
        let calls =
            scan_rvbbit_calls("SELECT rvbbit.means(concat(body, ' suffix'), 'criterion (parens)')");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].args.len(), 2);
    }

    #[test]
    fn ignores_qualified_non_rvbbit_schema() {
        let calls = scan_rvbbit_calls("SELECT pgrvbbit.means(x) FROM t");
        assert!(calls.is_empty(), "got {:?}", calls);
    }
}

#[cfg(test)]
mod graph_tests {
    use super::*;

    fn rate(input: f64, output: f64) -> ModelRate {
        ModelRate {
            input_per_mtok: input,
            output_per_mtok: output,
        }
    }

    #[test]
    fn fmt_usd_brackets() {
        assert_eq!(fmt_usd(0.0), "$0.00");
        assert_eq!(fmt_usd(-1.0), "$0.00");
        assert_eq!(fmt_usd(0.0000073), "$0.000007");
        assert_eq!(fmt_usd(1.2345678), "$1.2346");
    }

    #[test]
    fn pct_handles_zero_denominator() {
        assert_eq!(pct(0, 0), "—");
        assert_eq!(pct(1, 4), "25.0%");
    }

    #[test]
    fn model_cost_arithmetic() {
        let r = rate(1.0, 5.0);
        assert!((model_cost(&r, 1_000_000, 1_000_000) - 6.0).abs() < 1e-9);
        assert_eq!(model_cost(&r, 0, 0), 0.0);
    }

    #[test]
    fn endpoint_cost_classification() {
        let mut rates = HashMapRates::new();
        rates.insert("haiku".to_string(), rate(1.0, 5.0));
        // LLM with a rate -> billable.
        assert!(matches!(
            endpoint_cost(EndpointKind::Llm, "haiku", 1_000_000, 0, &rates),
            EndpointCost::Billable(v) if (v - 1.0).abs() < 1e-9
        ));
        // LLM without a rate -> uncosted.
        assert!(matches!(
            endpoint_cost(EndpointKind::Llm, "mystery", 100, 10, &rates),
            EndpointCost::Uncosted
        ));
        // Sidecar / code -> always local.
        assert!(matches!(
            endpoint_cost(EndpointKind::Sidecar, "bge-m3", 0, 0, &rates),
            EndpointCost::Local
        ));
        assert!(matches!(
            endpoint_cost(EndpointKind::Code, "kmeans", 0, 0, &rates),
            EndpointCost::Local
        ));
    }

    fn measured_call_site() -> CallSite {
        CallSite {
            operator: "means".to_string(),
            criterion: "angry customer".to_string(),
            shape: "scalar".to_string(),
            return_type: "bool".to_string(),
            l1: 820,
            l2: 120,
            fresh: 60,
            errors: 0,
            invocations: 1000,
            endpoints: vec![EndpointLine {
                kind: EndpointKind::Llm,
                name: "openai/gpt-5.4-mini".to_string(),
                calls: 60,
                tokens_in: 4920,
                tokens_out: 180,
                latency_ms: 7400,
                errors: 0,
                cost: EndpointCost::Billable(0.0099),
            }],
            annotation: None,
        }
    }

    #[test]
    fn analyze_graph_renders_cascade_and_external_summary() {
        let g = SemanticGraph {
            query: "SELECT * FROM t WHERE rvbbit.means(body, 'x')".to_string(),
            mode: GraphMode::Analyze {
                result_rows: Some(940),
                wall_ms: Some(8123),
                failed: None,
            },
            call_sites: vec![measured_call_site()],
            notes: vec!["a note".to_string()],
        };
        let text = render_graph(&g).join("\n");
        assert!(text.contains("Semantic Execution Graph"), "{text}");
        assert!(text.contains("Mode:   ANALYZE"), "{text}");
        assert!(text.contains("rvbbit.means  [scalar -> bool]"), "{text}");
        assert!(text.contains("L1 820 (82.0%)"), "{text}");
        assert!(text.contains("-> LLM"), "{text}");
        assert!(text.contains("External call summary"), "{text}");
        // 820 + 120 = 940 cache-served.
        assert!(text.contains("940 served"), "{text}");
        assert!(text.contains("Total semantic cost:"), "{text}");
    }

    #[test]
    fn analyze_graph_renders_sidecar_as_local() {
        let mut cs = measured_call_site();
        cs.endpoints.push(EndpointLine {
            kind: EndpointKind::Sidecar,
            name: "bge-m3".to_string(),
            calls: 60,
            tokens_in: 0,
            tokens_out: 0,
            latency_ms: 900,
            errors: 0,
            cost: EndpointCost::Local,
        });
        sort_endpoints(&mut cs.endpoints);
        let g = SemanticGraph {
            query: "q".to_string(),
            mode: GraphMode::Analyze {
                result_rows: Some(1),
                wall_ms: Some(1),
                failed: None,
            },
            call_sites: vec![cs],
            notes: vec![],
        };
        let text = render_graph(&g).join("\n");
        assert!(text.contains("-> SIDECAR"), "{text}");
        assert!(text.contains("local ($0)"), "{text}");
        assert!(text.contains("SIDECAR        60 calls"), "{text}");
    }

    #[test]
    fn estimate_graph_marks_projection_and_uncosted() {
        let cs = CallSite {
            operator: "about".to_string(),
            criterion: "aggressive encounter".to_string(),
            shape: "scalar".to_string(),
            return_type: "float8".to_string(),
            l1: 0,
            l2: 0,
            fresh: 5200,
            errors: 0,
            invocations: 5200,
            endpoints: vec![EndpointLine {
                kind: EndpointKind::Llm,
                name: "mystery/model".to_string(),
                calls: 5200,
                tokens_in: 520_000,
                tokens_out: 52_000,
                latency_ms: 0,
                errors: 0,
                cost: EndpointCost::Uncosted,
            }],
            annotation: Some("planner estimate via EXPLAIN: Seq Scan on bfro_reports".to_string()),
        };
        let g = SemanticGraph {
            query: "SELECT 1".to_string(),
            mode: GraphMode::Estimate,
            call_sites: vec![cs],
            notes: vec![],
        };
        let text = render_graph(&g).join("\n");
        assert!(text.contains("Mode:   ESTIMATE"), "{text}");
        assert!(text.contains("~5200"), "{text}");
        assert!(text.contains("cold-cache upper bound"), "{text}");
        assert!(text.contains("uncosted"), "{text}");
        assert!(text.contains("Projected cost"), "{text}");
        // The criterion must be visible so the user can tell calls apart.
        assert!(text.contains("aggressive encounter"), "{text}");
    }

    #[test]
    fn analyze_graph_all_cached_reads_as_cache_served() {
        let cs = CallSite {
            operator: "sentiment".to_string(),
            criterion: String::new(),
            shape: "scalar".to_string(),
            return_type: "text".to_string(),
            l1: 0,
            l2: 500,
            fresh: 0,
            errors: 0,
            invocations: 500,
            endpoints: vec![], // every call was an L2 hit — nothing executed
            annotation: None,
        };
        let g = SemanticGraph {
            query: "SELECT rvbbit.sentiment(observed) FROM bf".to_string(),
            mode: GraphMode::Analyze {
                result_rows: Some(500),
                wall_ms: Some(130),
                failed: None,
            },
            call_sites: vec![cs],
            notes: vec![],
        };
        let text = render_graph(&g).join("\n");
        assert!(text.contains("served entirely from cache"), "{text}");
        assert!(!text.contains("no external endpoints recorded"), "{text}");
        assert!(text.contains("L2 500 (100.0%)"), "{text}");
    }

    #[test]
    fn walk_plan_splits_filter_and_output_positions() {
        // Limit(15) -> Seq Scan(604): a reserved-word op `extract` in the
        // projection, `means` in the scan filter. Leaf has no Relation Name
        // so reltuples_of is never called (keeps the test SPI-free).
        let plan = serde_json::json!({
            "Node Type": "Limit", "Plan Rows": 15,
            "Output": ["bfroid", "(rvbbit.\"extract\"(observed, 'p'::text))"],
            "Plans": [{
                "Node Type": "Seq Scan", "Plan Rows": 604,
                "Output": ["bfroid", "rvbbit.\"extract\"(observed, 'p'::text)"],
                "Filter": "(rvbbit.means(observed, 'x'::text) > 0)"
            }]
        });
        let ops = vec![
            OpSig {
                name: "extract".into(),
                fn_form: "rvbbit.extract(".into(),
                fn_form_quoted: "rvbbit.\"extract\"(".into(),
                infix: None,
            },
            OpSig {
                name: "means".into(),
                fn_form: "rvbbit.means(".into(),
                fn_form_quoted: "rvbbit.\"means\"(".into(),
                infix: None,
            },
        ];
        let mut acc: BTreeMap<String, OpAccum> = BTreeMap::new();
        walk_plan(&plan, &ops, &mut acc, &|_| None);
        // extract: output-position, min(Limit 15, Seq Scan 604) = 15.
        let ex = acc.get("extract").expect("extract found");
        assert_eq!(ex.filter_rows, None);
        assert_eq!(ex.output_rows, Some(15));
        // means: filter-position on the Seq Scan (leaf, no rel) -> 604.
        let m = acc.get("means").expect("means found");
        assert_eq!(m.filter_rows, Some(604));
    }

    #[test]
    fn analyze_graph_handles_failed_execution() {
        let g = SemanticGraph {
            query: "SELECT broken".to_string(),
            mode: GraphMode::Analyze {
                result_rows: Some(0),
                wall_ms: Some(1),
                failed: Some("column \"broken\" does not exist".to_string()),
            },
            call_sites: vec![],
            notes: vec![],
        };
        let text = render_graph(&g).join("\n");
        assert!(text.contains("FAILED"), "{text}");
        assert!(text.contains("does not exist"), "{text}");
    }
}
