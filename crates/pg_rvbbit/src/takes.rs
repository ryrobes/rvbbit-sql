//! Takes — run a semantic operator N times and reduce to one answer.
//!
//! A `takes` plan turns a single operator call into an ensemble: `factor`
//! independent attempts (optionally each on a different model from a pool),
//! optionally filtered by a validator, then reduced to one result:
//!
//!   vote        — majority of the (trimmed) outputs; no extra model call.
//!   first_valid — the first attempt that passed the filter.
//!   evaluator   — an LLM judge picks the best, given an instructions prompt.
//!   consensus   — for JSON-ARRAY outputs (triples, entity lists): keep the
//!                 items that appear in >= min_agree independent takes,
//!                 matched on `consensus_keys` (case/space-insensitive).
//!                 Whole-output voting never converges on generative JSON;
//!                 per-item voting is what actually kills hallucinations —
//!                 a fabricated fact rarely repeats across decorrelated
//!                 samples, a real one nearly always does. Pair with a
//!                 nonzero `temperature` in the plan or the takes are
//!                 identical and the ensemble is theater.
//!
//! The N attempts run in parallel on the backend-local thread pool. Takes
//! orchestration is LEADER ONLY (the pool, filter validators, the evaluator
//! call) — pool worker threads must only ever run plain `unit_of_work`
//! attempts, never re-enter takes, or the pool could deadlock on itself.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::Value;

use crate::flow;
use crate::providers::{self, ChatRequest};
use crate::unit_of_work::{self, OpDef, SubCall, WorkResult};
use crate::validator::ValidatorRef;

/// Run one operator attempt — a takes ensemble if the operator carries a
/// `takes` plan, otherwise a single plain execution. LEADER ONLY.
pub fn execute_attempt(
    op: &OpDef,
    inputs: &Value,
    opts: &Value,
    feedback: Option<&str>,
) -> WorkResult {
    if op.takes.is_some() {
        execute_takes(op, inputs, opts, feedback)
    } else {
        unit_of_work::execute_with_feedback(op, inputs, opts, feedback)
    }
}

enum Reduce {
    Vote,
    FirstValid,
    Evaluator,
    Consensus,
}

struct TakesPlan {
    factor: usize,
    /// Model pool, round-robined across takes. Empty = the operator's model.
    models: Vec<String>,
    /// Heterogeneous takes: an explicit list of node specs, each the same
    /// shape as a `steps` entry (`{kind: llm|specialist|code, ...}`). When
    /// set, each node is one take and `factor` / `models` are ignored.
    nodes: Option<Vec<Value>>,
    reduce: Reduce,
    /// Pre-reduce filter: takes whose output fails it are dropped.
    filter: Option<ValidatorRef>,
    evaluator_model: Option<String>,
    evaluator_instructions: Option<String>,
    /// consensus: how many takes an item must appear in to survive.
    /// Default = a strict majority of the parseable takes.
    min_agree: Option<usize>,
    /// consensus: object fields forming an item's identity (e.g.
    /// ["subject","predicate","object"]). Empty = the whole item.
    consensus_keys: Vec<String>,
    /// Per-take temperature override (rides the opts override the same way
    /// a model-pool entry does). Consensus wants this nonzero.
    temperature: Option<f64>,
}

fn parse_takes(v: &Value) -> Option<TakesPlan> {
    let o = v.as_object()?;
    let factor = o
        .get("factor")
        .and_then(|x| x.as_u64())
        .unwrap_or(1)
        .clamp(1, 12) as usize;
    let models = o
        .get("models")
        .and_then(|x| x.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|m| m.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let nodes = o
        .get("nodes")
        .and_then(|x| x.as_array())
        .filter(|a| !a.is_empty())
        .cloned();
    let reduce = match o.get("reduce").and_then(|x| x.as_str()).unwrap_or("vote") {
        "evaluator" => Reduce::Evaluator,
        "first_valid" => Reduce::FirstValid,
        "consensus" => Reduce::Consensus,
        _ => Reduce::Vote,
    };
    let filter = o.get("filter").and_then(ValidatorRef::parse);
    let (evaluator_model, evaluator_instructions) =
        match o.get("evaluator").and_then(|e| e.as_object()) {
            Some(e) => (
                e.get("model")
                    .and_then(|x| x.as_str())
                    .map(|s| s.to_string()),
                e.get("instructions")
                    .and_then(|x| x.as_str())
                    .map(|s| s.to_string()),
            ),
            None => (None, None),
        };
    let min_agree = o
        .get("min_agree")
        .and_then(|x| x.as_u64())
        .map(|n| (n as usize).max(1));
    let consensus_keys = o
        .get("consensus_keys")
        .and_then(|x| x.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|k| k.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let temperature = o.get("temperature").and_then(|x| x.as_f64());
    Some(TakesPlan {
        factor,
        models,
        nodes,
        reduce,
        filter,
        evaluator_model,
        evaluator_instructions,
        min_agree,
        consensus_keys,
        temperature,
    })
}

fn execute_takes(op: &OpDef, inputs: &Value, opts: &Value, feedback: Option<&str>) -> WorkResult {
    let plan = match op.takes.as_ref().and_then(parse_takes) {
        Some(p) => p,
        None => return unit_of_work::execute_with_feedback(op, inputs, opts, feedback),
    };

    // Build the take jobs. Two modes:
    //   heterogeneous — an explicit `nodes` list: each take is a distinct
    //     node (an llm / specialist / python / code engine), run once.
    //   homogeneous   — `factor` runs of the operator body, optionally
    //     round-robined across a `models` pool.
    let jobs: Vec<TakeKind> = match &plan.nodes {
        Some(nodes) => nodes.iter().cloned().map(TakeKind::Node).collect(),
        None => {
            // factor 1 with no model pool is just a plain attempt.
            if plan.factor <= 1 && plan.models.len() <= 1 {
                let opts1 = with_take_overrides(opts, plan.models.first(), plan.temperature);
                return unit_of_work::execute_with_feedback(op, inputs, &opts1, feedback);
            }
            (0..plan.factor)
                .map(|i| TakeKind::Body {
                    model: take_model(&plan, i),
                    temperature: plan.temperature,
                })
                .collect()
        }
    };

    // Run the jobs. On the leader they fan out across the pool; inside a
    // pool worker (the batched warm path) sub-submitting would deadlock the
    // pool, so they run inline — the rows are already parallel.
    let takes: Vec<WorkResult> = run_take_jobs(op, inputs, opts, feedback, jobs);

    // Surviving indices — drop transport-level failures first.
    let mut alive: Vec<usize> = (0..takes.len())
        .filter(|&i| takes[i].error.is_none())
        .collect();
    if alive.is_empty() {
        // Every take errored; return the first so the error surfaces.
        return takes
            .into_iter()
            .next()
            .unwrap_or_else(|| crate::validator::errored("all takes failed".to_string()));
    }

    // Pre-reduce filter. If it would drop everything, keep all (a filter
    // is advisory — better a flagged answer than no answer). The filter
    // validator needs SPI, which is leader-only, so it is skipped inside a
    // pool worker (the batched warm path) — the reducer still runs.
    if !flow::in_pool_worker() {
        if let Some(filter) = &plan.filter {
            let passing: Vec<usize> = alive
                .iter()
                .copied()
                .filter(|&i| {
                    crate::validator::evaluate(filter, &takes[i].output, inputs).unwrap_or(true)
                })
                .collect();
            if !passing.is_empty() {
                alive = passing;
            }
        }
    }

    // Reduce N -> 1. Consensus is the odd one out: it synthesizes a NEW
    // output (the per-item merge) rather than choosing a take, so it
    // overrides the assembled output afterwards.
    let consensus_output = match plan.reduce {
        Reduce::Consensus => consensus_merge(&takes, &alive, &plan),
        _ => None,
    };
    let (chosen, eval_sub) = match plan.reduce {
        Reduce::FirstValid => (alive[0], None),
        Reduce::Vote => (vote(&takes, &alive), None),
        // No parseable JSON-array take → fall back to plain voting.
        Reduce::Consensus => (
            if consensus_output.is_some() {
                alive[0]
            } else {
                vote(&takes, &alive)
            },
            None,
        ),
        Reduce::Evaluator => evaluator_pick(op, inputs, &takes, &alive, &plan),
    };

    let mut result = assemble(takes, chosen);
    if let Some(merged) = consensus_output {
        result.output = merged;
    }
    if let Some(sc) = eval_sub {
        result.total_tokens_in += sc.tokens_in;
        result.total_tokens_out += sc.tokens_out;
        result.total_latency_ms += sc.latency_ms;
        result.sub_calls.push(sc);
    }
    result
}

/// One take's work: re-run the operator body with model/temperature
/// overrides (homogeneous), or run a single explicit node (heterogeneous).
enum TakeKind {
    Body {
        model: Option<String>,
        temperature: Option<f64>,
    },
    Node(Value),
}

/// Run every take job — fanned out across the pool on the leader, inline
/// when already on a pool worker (avoids the pool deadlocking on itself;
/// see flow::in_pool_worker).
fn run_take_jobs(
    op: &OpDef,
    inputs: &Value,
    opts: &Value,
    feedback: Option<&str>,
    jobs: Vec<TakeKind>,
) -> Vec<WorkResult> {
    // SQL and MCP nodes need the leader. Run all takes inline rather than
    // pooling when the operator body has one (homogeneous takes) or a take
    // node is one (heterogeneous). Inside a pool worker we run inline too
    // — sub-submitting would deadlock the pool.
    let needs_leader = unit_of_work::contains_leader_node(op.steps.as_ref())
        || jobs.iter().any(|j| {
            matches!(j, TakeKind::Node(n)
                if matches!(n.get("kind").and_then(|k| k.as_str()), Some("sql" | "mcp")))
        });
    if flow::in_pool_worker() || needs_leader {
        return jobs
            .iter()
            .map(|j| run_one_take(op, inputs, opts, feedback, j))
            .collect();
    }
    // The pool closures need owned 'static data — clone the OpDef behind
    // an Arc.
    let op_arc = Arc::new(op.clone());
    let pool = flow::pool();
    let fb = feedback.map(|s| s.to_string());
    let mut receivers = Vec::with_capacity(jobs.len());
    for job in jobs {
        let op_c = op_arc.clone();
        let inputs_c = inputs.clone();
        let opts_c = opts.clone();
        let fb_c = fb.clone();
        receivers.push(
            pool.submit(move || run_one_take(&op_c, &inputs_c, &opts_c, fb_c.as_deref(), &job)),
        );
    }
    receivers
        .into_iter()
        .map(|rx| {
            rx.recv().unwrap_or_else(|_| {
                pgrx::error!("rvbbit: pool worker panicked while running a semantic take")
            })
        })
        .collect()
}

fn run_one_take(
    op: &OpDef,
    inputs: &Value,
    opts: &Value,
    feedback: Option<&str>,
    job: &TakeKind,
) -> WorkResult {
    match job {
        TakeKind::Body { model, temperature } => {
            let o = with_take_overrides(opts, model.as_ref(), *temperature);
            unit_of_work::execute_with_feedback(op, inputs, &o, feedback)
        }
        TakeKind::Node(node) => {
            unit_of_work::execute_steps(op, std::slice::from_ref(node), inputs, opts)
        }
    }
}

/// The model for take `i` — round-robin over the pool, or None to use the
/// operator's own default model.
fn take_model(plan: &TakesPlan, i: usize) -> Option<String> {
    if plan.models.is_empty() {
        None
    } else {
        Some(plan.models[i % plan.models.len()].clone())
    }
}

/// Clone `opts` and set/override the per-take `model` / `temperature` —
/// both ride the same opts-beats-operator override the executor honors.
fn with_take_overrides(opts: &Value, model: Option<&String>, temperature: Option<f64>) -> Value {
    let mut o = if opts.is_object() {
        opts.clone()
    } else {
        serde_json::json!({})
    };
    if let Value::Object(map) = &mut o {
        if let Some(m) = model {
            map.insert("model".to_string(), Value::String(m.clone()));
        }
        if let Some(t) = temperature {
            if let Some(num) = serde_json::Number::from_f64(t) {
                map.insert("temperature".to_string(), Value::Number(num));
            }
        }
    }
    o
}

/// Per-item consensus over JSON-array takes. An item's identity is the
/// case/whitespace-insensitive join of `consensus_keys` (or the whole item
/// when none are given); an item survives when it appears in >= min_agree
/// DISTINCT takes (default: strict majority of the parseable takes).
/// Survivors keep their first-seen full object, in first-seen order, with
/// numeric `confidence` averaged across the agreeing takes. Returns None
/// when no take parses as a JSON array (caller falls back to voting).
fn consensus_merge(takes: &[WorkResult], alive: &[usize], plan: &TakesPlan) -> Option<String> {
    fn item_key(item: &Value, keys: &[String]) -> String {
        fn norm(v: &Value) -> String {
            let s = match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            s.split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .to_lowercase()
        }
        if keys.is_empty() || !item.is_object() {
            return norm(item);
        }
        keys.iter()
            .map(|k| item.get(k).map(norm).unwrap_or_default())
            .collect::<Vec<_>>()
            .join("\u{1f}")
    }

    let parsed: Vec<Vec<Value>> = alive
        .iter()
        .filter_map(|&i| {
            serde_json::from_str::<Value>(takes[i].output.trim())
                .ok()
                .and_then(|v| v.as_array().cloned())
        })
        .collect();
    if parsed.is_empty() {
        return None;
    }
    let min_agree = plan
        .min_agree
        .unwrap_or(parsed.len() / 2 + 1)
        .clamp(1, parsed.len());

    struct Tally {
        count: usize,
        first: Value,
        first_order: usize,
        confidences: Vec<f64>,
    }
    let mut tallies: HashMap<String, Tally> = HashMap::new();
    let mut order = 0usize;
    for take_items in &parsed {
        let mut seen_this_take: std::collections::HashSet<String> = Default::default();
        for item in take_items {
            let key = item_key(item, &plan.consensus_keys);
            if key.is_empty() {
                continue;
            }
            let conf = item.get("confidence").and_then(|c| c.as_f64());
            let entry = tallies.entry(key.clone()).or_insert_with(|| {
                order += 1;
                Tally { count: 0, first: item.clone(), first_order: order, confidences: Vec::new() }
            });
            // A duplicate within ONE take is not extra agreement.
            if seen_this_take.insert(key) {
                entry.count += 1;
                if let Some(c) = conf {
                    entry.confidences.push(c);
                }
            }
        }
    }

    let mut survivors: Vec<&Tally> = tallies.values().filter(|t| t.count >= min_agree).collect();
    survivors.sort_by_key(|t| t.first_order);
    let merged: Vec<Value> = survivors
        .into_iter()
        .map(|t| {
            let mut item = t.first.clone();
            if !t.confidences.is_empty() {
                if let (Value::Object(map), Some(num)) = (
                    &mut item,
                    serde_json::Number::from_f64(
                        t.confidences.iter().sum::<f64>() / t.confidences.len() as f64,
                    ),
                ) {
                    map.insert("confidence".to_string(), Value::Number(num));
                }
            }
            item
        })
        .collect();
    Some(Value::Array(merged).to_string())
}

/// Majority vote over the trimmed output strings. Ties break toward the
/// earliest take (stable).
fn vote(takes: &[WorkResult], alive: &[usize]) -> usize {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for &i in alive {
        *counts.entry(takes[i].output.trim()).or_insert(0) += 1;
    }
    let mut best = alive[0];
    let mut best_count = 0usize;
    for &i in alive {
        let c = counts[takes[i].output.trim()];
        if c > best_count {
            best_count = c;
            best = i;
        }
    }
    best
}

/// LLM judge — picks the best take given the operator inputs and the
/// candidate outputs. Falls back to the first candidate on any failure.
fn evaluator_pick(
    op: &OpDef,
    inputs: &Value,
    takes: &[WorkResult],
    alive: &[usize],
    plan: &TakesPlan,
) -> (usize, Option<SubCall>) {
    let model = plan
        .evaluator_model
        .clone()
        .unwrap_or_else(|| op.model.clone());
    let instructions = plan
        .evaluator_instructions
        .clone()
        .unwrap_or_else(|| "You are selecting the single best answer to the task.".to_string());

    let mut user = String::from("TASK INPUTS:\n");
    user.push_str(&serde_json::to_string_pretty(inputs).unwrap_or_default());
    user.push_str("\n\nCANDIDATE ANSWERS:\n");
    for (n, &i) in alive.iter().enumerate() {
        user.push_str(&format!("[{}] {}\n", n + 1, takes[i].output.trim()));
    }
    user.push_str(&format!(
        "\nReply with ONLY the number (1-{}) of the best candidate.",
        alive.len()
    ));

    match providers::chat(ChatRequest {
        model: model.clone(),
        system: Some(instructions),
        user,
        temperature: Some(0.0),
        max_tokens: Some(16),
        provider: None,
    }) {
        Ok(resp) => {
            let digits: String = resp
                .content
                .chars()
                .filter(|c| c.is_ascii_digit())
                .collect();
            let pick = digits
                .parse::<usize>()
                .ok()
                .filter(|&n| n >= 1)
                .map(|n| n - 1)
                .unwrap_or(0)
                .min(alive.len() - 1);
            let sub = SubCall {
                step: "evaluator".to_string(),
                kind: "llm".to_string(),
                model: Some(resp.model.clone()),
                backend: Some(resp.provider.clone()),
                transport: Some(resp.transport.clone()),
                provider_request_id: resp.provider_request_id.clone(),
                provider_generation_id: resp.provider_generation_id.clone(),
                upstream_id: resp.upstream_id.clone(),
                tokens_in: resp.prompt_tokens,
                tokens_out: resp.completion_tokens,
                native_tokens_in: resp.native_tokens_in,
                native_tokens_out: resp.native_tokens_out,
                reasoning_tokens: resp.reasoning_tokens,
                cached_tokens: resp.cached_tokens,
                cost_usd: resp.cost_usd,
                cost_source: resp.cost_source.clone(),
                raw_usage: resp.raw_usage.clone(),
                latency_ms: resp.latency_ms,
                error: None,
            };
            (alive[pick], Some(sub))
        }
        Err(_) => (alive[0], None),
    }
}

/// Collapse the takes into one result: the chosen output, with every
/// take's audit (sub_calls + token/latency totals) merged so the receipt
/// reflects the full ensemble cost.
fn assemble(takes: Vec<WorkResult>, chosen: usize) -> WorkResult {
    let mut sub_calls: Vec<SubCall> = Vec::new();
    let mut total_tokens_in = 0;
    let mut total_tokens_out = 0;
    let mut total_latency_ms = 0;
    let mut output = String::new();
    let mut error = None;
    for (i, t) in takes.into_iter().enumerate() {
        total_tokens_in += t.total_tokens_in;
        total_tokens_out += t.total_tokens_out;
        total_latency_ms += t.total_latency_ms;
        if i == chosen {
            output = t.output;
            error = t.error;
        }
        sub_calls.extend(t.sub_calls);
    }
    WorkResult {
        output,
        sub_calls,
        total_tokens_in,
        total_tokens_out,
        total_latency_ms,
        error,
    }
}
