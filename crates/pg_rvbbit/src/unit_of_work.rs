//! Unit-of-work executor.
//!
//! One operator invocation = one UnitOfWork = one receipt row.
//! Internally the unit may execute N steps of different kinds (llm, code,
//! python, specialist). The receipt's `sub_calls` jsonb captures the per-step
//! audit (tokens, latency, errors). Roll-up totals are in the receipt's
//! top-level columns.
//!
//! When the operator's `steps` field is NULL, we run a single LLM call
//! using the operator's system_prompt + user_prompt (today's behavior —
//! backward compatible). When `steps` is set, we iterate the array and
//! dispatch by `kind`.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::providers::{self, ChatRequest, ProviderError};

/// Definition loaded from rvbbit.operators row. Mirrors the catalog row.
#[derive(Clone)]
pub struct OpDef {
    pub name: String,
    pub shape: String,
    pub return_type: String,
    pub model: String,
    pub system_prompt: String,
    pub user_prompt: String,
    pub parser: String,
    pub max_tokens: i32,
    pub temperature: Option<f32>,
    pub steps: Option<Value>, // jsonb array, parsed
    /// Operator-level retry plan (jsonb): {until, max_attempts, instructions}.
    /// None = run once. Applied by crate::validator::apply_retry.
    pub retry: Option<Value>,
    /// Pre/post validator gates (jsonb): {pre:[...], post:[...]}. Each ward
    /// is {validator, mode}. Applied by crate::validator wards functions.
    pub wards: Option<Value>,
    /// Multi-take plan (jsonb): {factor, models, reduce, filter, evaluator}.
    /// None = run once. Applied by crate::takes.
    pub takes: Option<Value>,
    /// Result-cache policy ('memoize' default | 'always' | 'never'). 'never' bypasses the
    /// L1/L2 result cache entirely — the operator always runs fresh (receipts are still logged
    /// for audit/cost). Required for stateful operators (e.g. agent loops, anything that reads
    /// mutable tables): without it, identical inputs return a frozen prior output.
    pub cache_policy: String,
}

/// What the executor returns to the calling UDF.
pub struct WorkResult {
    /// The final operator output as a raw string (the parser then turns
    /// it into bool/text/float8).
    pub output: String,
    pub sub_calls: Vec<SubCall>,
    pub total_tokens_in: i32,
    pub total_tokens_out: i32,
    pub total_latency_ms: i32,
    /// Set when execution failed at any step. The caller logs the error
    /// receipt and returns a safe default.
    pub error: Option<String>,
}

/// One sub-call's audit entry — gets serialized into receipts.sub_calls.
#[derive(serde::Serialize, Debug, Default)]
pub struct SubCall {
    pub step: String,
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backend: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transport: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_request_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_generation_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub upstream_id: Option<String>,
    pub tokens_in: i32,
    pub tokens_out: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub native_tokens_in: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub native_tokens_out: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reasoning_tokens: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cached_tokens: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cost_usd: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cost_source: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_usage: Option<Value>,
    pub latency_ms: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Run one operator invocation. Pure function — caller handles cache
/// lookup, receipt logging, and parsing the return string into the typed
/// PG datum.
pub fn execute(op: &OpDef, inputs: &Value, opts: &Value) -> WorkResult {
    execute_with_feedback(op, inputs, opts, None)
}

/// Like `execute`, but appends `feedback` to the user prompt of a
/// single-step LLM operator. The retry driver uses this to tell the model
/// why its previous answer was rejected. Multi-step operators re-run
/// without feedback injection — there is no single obvious prompt to amend.
pub fn execute_with_feedback(
    op: &OpDef,
    inputs: &Value,
    opts: &Value,
    feedback: Option<&str>,
) -> WorkResult {
    // Template scope assembled once; each step renders against this and
    // step outputs accumulate into `scope.steps.<name>`.
    let mut scope = Scope::new(inputs.clone(), opts.clone());

    if let Some(steps) = op.steps.as_ref().and_then(|s| s.as_array()) {
        // Multi-step path.
        run_multi_step(op, steps, &mut scope)
    } else {
        // Single-step LLM path — today's behavior.
        run_single_llm(op, &mut scope, feedback)
    }
}

/// Run an explicit list of nodes as a pipeline — like `execute`, but the
/// steps come from the caller rather than `op.steps`. Used by heterogeneous
/// takes: each take is a one-node pipeline.
pub fn execute_steps(op: &OpDef, steps: &[Value], inputs: &Value, opts: &Value) -> WorkResult {
    let mut scope = Scope::new(inputs.clone(), opts.clone());
    run_multi_step(op, steps, &mut scope)
}

/// True if any node in `steps` has kind "sql". A sql node needs a Postgres
/// backend (SPI), so an operator containing one must run on the leader,
/// never a flow pool thread — callers use this to route execution.
pub fn contains_sql_node(steps: Option<&Value>) -> bool {
    contains_step_kind(steps, &["sql"])
}

/// True if any node must run on the leader backend instead of a flow-pool
/// worker. SQL nodes use SPI directly. MCP nodes may resolve the active
/// gateway URL from SQL and log per-call audit rows.
pub fn contains_leader_node(steps: Option<&Value>) -> bool {
    contains_sql_node(steps) || contains_step_kind(steps, &["mcp", "agent", "n8n"])
}

fn contains_step_kind(steps: Option<&Value>, kinds: &[&str]) -> bool {
    steps
        .and_then(|s| s.as_array())
        .map(|arr| {
            arr.iter().any(|n| {
                n.get("kind")
                    .and_then(|k| k.as_str())
                    .map(|kind| kinds.contains(&kind))
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false)
}

// ---------------------------------------------------------------------------
// Single-step path (backward compatible)
// ---------------------------------------------------------------------------

fn run_single_llm(op: &OpDef, scope: &mut Scope, feedback: Option<&str>) -> WorkResult {
    let model = scope
        .opts
        .get("model")
        .and_then(|v| v.as_str())
        .unwrap_or(&op.model)
        .to_string();
    let temperature = scope
        .opts
        .get("temperature")
        .and_then(|v| v.as_f64())
        .map(|f| f as f32)
        .or(op.temperature);

    let system = scope.render(&op.system_prompt);
    let mut user = scope.render(&op.user_prompt);
    // Retry feedback: the validator rejected the previous attempt; this
    // text tells the model what to fix.
    if let Some(fb) = feedback {
        if !fb.trim().is_empty() {
            user.push_str("\n\n");
            user.push_str(fb);
        }
    }

    match providers::chat(ChatRequest {
        model: model.clone(),
        system: Some(system),
        user,
        temperature,
        max_tokens: Some(op.max_tokens as u32),
        // Single-LLM operators use the default provider; pin a specific one
        // by expressing the operator as a one-node `steps` pipeline.
        provider: None,
    }) {
        Ok(resp) => WorkResult {
            output: resp.content,
            sub_calls: vec![SubCall {
                step: "main".into(),
                kind: "llm".into(),
                model: Some(model),
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
            }],
            total_tokens_in: resp.prompt_tokens,
            total_tokens_out: resp.completion_tokens,
            total_latency_ms: resp.latency_ms,
            error: None,
        },
        Err(e) => WorkResult {
            output: String::new(),
            sub_calls: vec![SubCall {
                step: "main".into(),
                kind: "llm".into(),
                model: Some(model),
                tokens_in: 0,
                tokens_out: 0,
                latency_ms: 0,
                error: Some(e.to_string()),
                ..Default::default()
            }],
            total_tokens_in: 0,
            total_tokens_out: 0,
            total_latency_ms: 0,
            error: Some(e.to_string()),
        },
    }
}

// ---------------------------------------------------------------------------
// Multi-step path
// ---------------------------------------------------------------------------

fn run_multi_step(op: &OpDef, steps: &[Value], scope: &mut Scope) -> WorkResult {
    run_multi_step_inner(op, steps, scope, None)
}

/// Run a multi-step pipeline with one step pre-computed and skipped. The named
/// step's output must already be in `scope.steps`, its execution is skipped, and
/// `seed_sub` is recorded in its place. prewarm uses this to batch a specialist
/// step across rows and then run only the remaining (local) steps per row.
pub fn run_multistep_seeded(
    op: &OpDef,
    steps: &[Value],
    inputs: &Value,
    opts: &Value,
    seed_step: &str,
    seed_output: Value,
    seed_sub: SubCall,
) -> WorkResult {
    let mut scope = Scope::new(inputs.clone(), opts.clone());
    scope.steps.insert(seed_step.to_string(), seed_output);
    run_multi_step_inner(
        op,
        steps,
        &mut scope,
        Some((seed_step.to_string(), seed_sub)),
    )
}

fn run_multi_step_inner(
    op: &OpDef,
    steps: &[Value],
    scope: &mut Scope,
    mut seeded: Option<(String, SubCall)>,
) -> WorkResult {
    let mut sub_calls: Vec<SubCall> = Vec::with_capacity(steps.len());
    let mut total_tokens_in: i32 = 0;
    let mut total_tokens_out: i32 = 0;
    let total_t0 = Instant::now();
    let mut last_output_text: String = String::new();

    for (i, step) in steps.iter().enumerate() {
        let step_name = step
            .get("name")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("step_{i}"));

        // A pre-seeded step (batched elsewhere): its output is already in
        // scope; record the supplied sub_call in order and skip execution.
        if seeded.as_ref().is_some_and(|(name, _)| name == &step_name) {
            let (_, sub) = seeded.take().unwrap();
            total_tokens_in += sub.tokens_in;
            total_tokens_out += sub.tokens_out;
            last_output_text = scope
                .steps
                .get(&step_name)
                .and_then(|s| s.get("output"))
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default();
            sub_calls.push(sub);
            continue;
        }

        let kind = step.get("kind").and_then(|v| v.as_str()).unwrap_or("");

        let (sub, step_output, output_text) = match kind {
            "llm" => run_step_llm(op, step, &step_name, scope),
            "code" => run_step_code(step, &step_name, scope),
            "python" => run_step_python(step, &step_name, scope),
            "specialist" => run_step_specialist(step, &step_name, scope),
            "sql" => run_step_sql(step, &step_name, scope),
            "mcp" => run_step_mcp(step, &step_name, scope),
            "n8n" => run_step_n8n(step, &step_name, scope),
            "agent" => run_step_agent(op, step, &step_name, scope),
            other => (
                SubCall {
                    step: step_name.clone(),
                    kind: kind.into(),
                    model: None,
                    tokens_in: 0,
                    tokens_out: 0,
                    latency_ms: 0,
                    error: Some(format!("unknown step kind '{other}'")),
                    ..Default::default()
                },
                Value::Null,
                String::new(),
            ),
        };

        total_tokens_in += sub.tokens_in;
        total_tokens_out += sub.tokens_out;
        let had_error = sub.error.clone();
        sub_calls.push(sub);

        if let Some(err) = had_error {
            return WorkResult {
                output: String::new(),
                sub_calls,
                total_tokens_in,
                total_tokens_out,
                total_latency_ms: total_t0.elapsed().as_millis().min(i32::MAX as u128) as i32,
                error: Some(format!("step '{step_name}': {err}")),
            };
        }

        scope.steps.insert(step_name, step_output);
        last_output_text = output_text;
    }

    WorkResult {
        output: last_output_text,
        sub_calls,
        total_tokens_in,
        total_tokens_out,
        total_latency_ms: total_t0.elapsed().as_millis().min(i32::MAX as u128) as i32,
        error: None,
    }
}

/// Run an LLM step. The step config looks like:
///   {"name":"name","kind":"llm","model":"haiku","system":"...","user":"..."}
/// Templates inside system/user have full access to `scope`.
fn run_step_llm(
    op: &OpDef,
    step: &Value,
    step_name: &str,
    scope: &Scope,
) -> (SubCall, Value, String) {
    let model = step
        .get("model")
        .and_then(|v| v.as_str())
        .unwrap_or(&op.model)
        .to_string();
    let system_tmpl = step.get("system").and_then(|v| v.as_str()).unwrap_or("");
    let user_tmpl = step.get("user").and_then(|v| v.as_str()).unwrap_or("");
    let max_tokens = step
        .get("max_tokens")
        .and_then(|v| v.as_i64())
        .map(|n| n as u32)
        .unwrap_or(op.max_tokens as u32);
    let temperature = step
        .get("temperature")
        .and_then(|v| v.as_f64())
        .map(|f| f as f32)
        .or(op.temperature);
    // Optional LLM provider backend; absent -> the default provider.
    let provider = step
        .get("provider")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());

    let system_rendered = scope.render(system_tmpl);
    let user_rendered = scope.render(user_tmpl);

    match providers::chat(ChatRequest {
        model: model.clone(),
        system: if system_rendered.is_empty() {
            None
        } else {
            Some(system_rendered)
        },
        user: user_rendered,
        temperature,
        max_tokens: Some(max_tokens),
        provider,
    }) {
        Ok(resp) => (
            SubCall {
                step: step_name.into(),
                kind: "llm".into(),
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
            },
            // Step output exposed under steps.<name> for later templates:
            // we store both the raw `output` and the (possibly parsed) result.
            serde_json::json!({"output": resp.content}),
            resp.content.clone(),
        ),
        Err(e) => (
            SubCall {
                step: step_name.into(),
                kind: "llm".into(),
                model: Some(model),
                tokens_in: 0,
                tokens_out: 0,
                latency_ms: 0,
                error: Some(e.to_string()),
                ..Default::default()
            },
            Value::Null,
            String::new(),
        ),
    }
}

// ---------------------------------------------------------------------------
// Agent step (v0): a bounded tool-calling loop.
//
// Step config:
//   {"name":"report","kind":"agent","model":"...","system":"...","task":"...",
//    "tools":[{"builtin":"query"},{"server":"linear","tool":"list_issues"}],
//    "max_iters":8,"budget":{"tokens":N,"cost_usd":F,"wall_ms":N},
//    "tool_result_max_chars":8000}
//
// The model gets the system prompt + task + the tool specs, then drives itself:
// it calls tools (the built-in read-only `query`, or any allow-listed MCP tool),
// each result is fed back, and the loop ends when the model answers with no tool
// call — or a cap trips (max_iters / token / cost / wall budget). The final
// answer is the step output. Every turn is appended to rvbbit.agent_messages,
// keyed by a generated run_id that is also returned in the step output, for
// token/cost debugging.
//
// v0 scope: one agent, no sub-agents, no validators-back-into-loop. Two seams are
// left open for v1 without a rewrite: (A) operator-as-tool — a {server:"rvbbit-op"}
// entry would add an `AgentTool::Operator` arm here; (B) structured output — a
// "schema" on the step would force a final tool call feeding a reduce step. Audit
// rows are written in-transaction (visible on commit); out-of-band durability on
// abort is a v0.1 refinement. Agent operators MUST set cache_policy='never'
// (a memoized agent would replay a frozen transcript).
enum AgentTool {
    Query,
    Mcp {
        server: String,
        tool: String,
    },
    Memory {
        action: AgentMemoryAction,
        config: AgentMemoryConfig,
    },
}

const AGENT_QUERY_DESC: &str = "Run a single read-only SQL query against this Postgres database and get the rows back as JSON (capped at 200 rows). Use it to inspect tables, pg_stat_* views, and rvbbit telemetry. SELECT/WITH only — writes and DDL are rejected by the engine.";
const AGENT_MEMORY_RECALL_DESC: &str = "Search this agent node's scoped long-term memory for facts relevant to a query. The bank is fixed by Rvbbit from operator + node + context; do not include or request a bank id.";
const AGENT_MEMORY_REFLECT_DESC: &str = "Ask this agent node's scoped long-term memory to synthesize relevant context for a query. The bank is fixed by Rvbbit from operator + node + context; do not include or request a bank id.";
const AGENT_MEMORY_RETAIN_DESC: &str = "Store durable memory for this agent node's scoped context. Retain only stable facts, decisions, preferences, or useful observations; do not store transient chain-of-thought.";

#[derive(Clone, Copy)]
enum AgentMemoryAction {
    Recall,
    Reflect,
    Retain,
}

#[derive(Clone)]
struct AgentMemoryConfig {
    service_name: String,
    context: String,
    bank_id: String,
    required: bool,
    allow_tools: bool,
    recall_before_run: bool,
    retain_final: bool,
    async_retain: bool,
    limit: i64,
    max_chars: usize,
    recall_options: Value,
    retain_options: Value,
}

fn run_step_agent(
    op: &OpDef,
    step: &Value,
    step_name: &str,
    scope: &Scope,
) -> (SubCall, Value, String) {
    let model = step
        .get("model")
        .and_then(|v| v.as_str())
        .unwrap_or(&op.model)
        .to_string();
    let provider = step
        .get("provider")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    let system = scope.render(step.get("system").and_then(|v| v.as_str()).unwrap_or(""));
    let task = scope.render(step.get("task").and_then(|v| v.as_str()).unwrap_or(""));
    let max_iters = step
        .get("max_iters")
        .and_then(|v| v.as_u64())
        .unwrap_or(8)
        .clamp(1, 50) as usize;
    let budget = step.get("budget");
    let budget_tokens = budget
        .and_then(|b| b.get("tokens"))
        .and_then(|v| v.as_i64());
    let budget_cost = budget
        .and_then(|b| b.get("cost_usd"))
        .and_then(|v| v.as_f64());
    let budget_wall = budget
        .and_then(|b| b.get("wall_ms"))
        .and_then(|v| v.as_u64());
    let tool_result_max = step
        .get("tool_result_max_chars")
        .and_then(|v| v.as_u64())
        .unwrap_or(8000)
        .max(256) as usize;
    let memory_requested = agent_memory_config(step, op, step_name, scope, tool_result_max);
    let run_id = new_agent_run_id();
    let mut turn_idx: i32 = 0;
    let total_t0 = Instant::now();

    let mut messages: Vec<providers::ChatMessage> = Vec::new();
    if !system.is_empty() {
        messages.push(providers::ChatMessage::system(system.clone()));
        audit_agent_turn(
            &run_id,
            &op.name,
            &model,
            &mut turn_idx,
            "system",
            Some(&system),
            None,
            None,
            None,
            0,
            0,
            None,
            0,
            None,
        );
    }

    let memory = match memory_requested {
        Some(cfg) => match agent_memory_service_available(&cfg) {
            Ok(()) => Some(cfg),
            Err(err) => {
                let msg = format!("agent memory disabled: {err}");
                audit_agent_turn(
                    &run_id,
                    &op.name,
                    &model,
                    &mut turn_idx,
                    if cfg.required { "error" } else { "tool" },
                    Some(&msg),
                    Some("memory_status"),
                    None,
                    None,
                    0,
                    0,
                    None,
                    0,
                    Some(&err),
                );
                if cfg.required {
                    return (
                        agent_subcall(step_name, &model, None, 0, 0, 0.0, &total_t0, Some(msg)),
                        Value::Null,
                        String::new(),
                    );
                }
                None
            }
        },
        None => None,
    };

    if let Some(cfg) = memory.as_ref() {
        if cfg.allow_tools {
            let memory_note = format!(
                "Scoped long-term memory is available for this agent context. Use memory_recall or memory_reflect before relying on prior facts, and use memory_retain only for stable facts or useful decisions worth remembering. The memory bank is fixed by Rvbbit as {}; never ask the user for a bank id.",
                cfg.bank_id
            );
            messages.push(providers::ChatMessage::system(memory_note.clone()));
            audit_agent_turn(
                &run_id,
                &op.name,
                &model,
                &mut turn_idx,
                "system",
                Some(&memory_note),
                None,
                None,
                None,
                0,
                0,
                None,
                0,
                None,
            );
        }
        if cfg.recall_before_run {
            let (result_text, err) = agent_run_memory_action(
                AgentMemoryAction::Recall,
                cfg,
                &serde_json::json!({ "query": task.as_str(), "options": cfg.recall_options.clone() }),
                cfg.max_chars,
            );
            audit_agent_turn(
                &run_id,
                &op.name,
                &model,
                &mut turn_idx,
                "tool",
                Some(&result_text),
                Some("memory_recall"),
                None,
                None,
                0,
                0,
                None,
                0,
                err.as_deref(),
            );
            if let Some(e) = err {
                if cfg.required {
                    return (
                        agent_subcall(step_name, &model, None, 0, 0, 0.0, &total_t0, Some(e)),
                        Value::Null,
                        String::new(),
                    );
                }
            } else if !result_text.trim().is_empty() && result_text.trim() != "null" {
                let memory_context = format!(
                    "Long-term memory for this agent context (bank: {}):\n{}",
                    cfg.bank_id, result_text
                );
                messages.push(providers::ChatMessage::system(memory_context.clone()));
                audit_agent_turn(
                    &run_id,
                    &op.name,
                    &model,
                    &mut turn_idx,
                    "system",
                    Some(&memory_context),
                    None,
                    None,
                    None,
                    0,
                    0,
                    None,
                    0,
                    None,
                );
            }
        }
    }
    messages.push(providers::ChatMessage::user(task.clone()));
    audit_agent_turn(
        &run_id,
        &op.name,
        &model,
        &mut turn_idx,
        "user",
        Some(&task),
        None,
        None,
        None,
        0,
        0,
        None,
        0,
        None,
    );

    // Build the tool specs advertised to the model + the name->handler allowlist.
    let mut tool_specs: Vec<providers::ToolSpec> = Vec::new();
    let mut handlers: HashMap<String, AgentTool> = HashMap::new();
    if let Some(arr) = step.get("tools").and_then(|v| v.as_array()) {
        for t in arr {
            if let Some(b) = t.get("builtin").and_then(|v| v.as_str()) {
                if b == "query" && !handlers.contains_key("query") {
                    tool_specs.push(providers::ToolSpec {
                        name: "query".into(),
                        description: AGENT_QUERY_DESC.into(),
                        parameters: serde_json::json!({
                            "type": "object",
                            "properties": {
                                "sql": {
                                    "type": "string",
                                    "description": "A single read-only SQL SELECT/WITH statement. Capped at 200 rows."
                                }
                            },
                            "required": ["sql"]
                        }),
                    });
                    handlers.insert("query".into(), AgentTool::Query);
                }
            } else if let (Some(srv), Some(tool)) = (
                t.get("server").and_then(|v| v.as_str()),
                t.get("tool").and_then(|v| v.as_str()),
            ) {
                if handlers.contains_key(tool) {
                    continue;
                }
                if let Some((desc, schema)) = load_mcp_tool_spec(srv, tool) {
                    tool_specs.push(providers::ToolSpec {
                        name: tool.into(),
                        description: desc,
                        parameters: schema,
                    });
                    handlers.insert(
                        tool.into(),
                        AgentTool::Mcp {
                            server: srv.into(),
                            tool: tool.into(),
                        },
                    );
                }
            }
        }
    }
    if let Some(cfg) = memory.as_ref().filter(|m| m.allow_tools) {
        if !handlers.contains_key("memory_recall") {
            tool_specs.push(providers::ToolSpec {
                name: "memory_recall".into(),
                description: AGENT_MEMORY_RECALL_DESC.into(),
                parameters: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "What to search this agent context's memory for." },
                        "options": { "type": "object", "description": "Optional Hindsight recall options such as top_k." }
                    },
                    "required": ["query"]
                }),
            });
            handlers.insert(
                "memory_recall".into(),
                AgentTool::Memory {
                    action: AgentMemoryAction::Recall,
                    config: cfg.clone(),
                },
            );
        }
        if !handlers.contains_key("memory_reflect") {
            tool_specs.push(providers::ToolSpec {
                name: "memory_reflect".into(),
                description: AGENT_MEMORY_REFLECT_DESC.into(),
                parameters: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "What to synthesize from this agent context's memory." },
                        "options": { "type": "object", "description": "Optional Hindsight reflect options." }
                    },
                    "required": ["query"]
                }),
            });
            handlers.insert(
                "memory_reflect".into(),
                AgentTool::Memory {
                    action: AgentMemoryAction::Reflect,
                    config: cfg.clone(),
                },
            );
        }
        if !handlers.contains_key("memory_retain") {
            tool_specs.push(providers::ToolSpec {
                name: "memory_retain".into(),
                description: AGENT_MEMORY_RETAIN_DESC.into(),
                parameters: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "content": { "type": "string", "description": "Stable fact, decision, preference, or observation to remember." },
                        "options": { "type": "object", "description": "Optional Hindsight retain metadata/tags." }
                    },
                    "required": ["content"]
                }),
            });
            handlers.insert(
                "memory_retain".into(),
                AgentTool::Memory {
                    action: AgentMemoryAction::Retain,
                    config: cfg.clone(),
                },
            );
        }
    }
    let mut agg_in = 0i32;
    let mut agg_out = 0i32;
    let mut agg_cost = 0f64;
    let mut gen_id: Option<String> = None;
    let mut last_content = String::new();
    let mut status = "max_iters";

    for _iter in 0..max_iters {
        if let Some(bt) = budget_tokens {
            if (agg_in + agg_out) as i64 >= bt {
                status = "budget_tokens";
                break;
            }
        }
        if let Some(bc) = budget_cost {
            if agg_cost >= bc {
                status = "budget_cost";
                break;
            }
        }
        if let Some(bw) = budget_wall {
            if total_t0.elapsed().as_millis() as u64 >= bw {
                status = "budget_wall";
                break;
            }
        }

        let resp =
            match providers::chat_with_tools(&model, provider.as_deref(), &messages, &tool_specs) {
                Ok(r) => r,
                Err(e) => {
                    let err = e.to_string();
                    audit_agent_turn(
                        &run_id,
                        &op.name,
                        &model,
                        &mut turn_idx,
                        "error",
                        Some(&err),
                        None,
                        None,
                        None,
                        0,
                        0,
                        None,
                        0,
                        Some(&err),
                    );
                    return (
                        agent_subcall(
                            step_name,
                            &model,
                            gen_id,
                            agg_in,
                            agg_out,
                            agg_cost,
                            &total_t0,
                            Some(err),
                        ),
                        Value::Null,
                        String::new(),
                    );
                }
            };

        agg_in += resp.prompt_tokens;
        agg_out += resp.completion_tokens;
        if let Some(c) = resp.cost_usd {
            agg_cost += c;
        }
        if resp.provider_generation_id.is_some() {
            gen_id = resp.provider_generation_id.clone();
        }

        audit_agent_turn(
            &run_id,
            &op.name,
            &model,
            &mut turn_idx,
            "assistant",
            resp.content.as_deref(),
            None,
            resp.raw_tool_calls.clone(),
            resp.finish_reason.as_deref(),
            resp.prompt_tokens,
            resp.completion_tokens,
            resp.cost_usd,
            resp.latency_ms,
            None,
        );

        if !resp.tool_calls.is_empty() {
            // Echo the assistant turn (content + tool_calls) so the tool-result
            // turns that follow have the calls they answer.
            messages.push(providers::ChatMessage {
                role: "assistant".into(),
                content: resp.content.clone(),
                tool_calls: resp.raw_tool_calls.clone(),
                tool_call_id: None,
            });
            for tc in &resp.tool_calls {
                let (result_text, err) = match handlers.get(&tc.name) {
                    Some(AgentTool::Query) => {
                        agent_run_readonly_query(&tc.arguments, tool_result_max)
                    }
                    Some(AgentTool::Mcp { server, tool }) => {
                        agent_run_mcp_tool(server, tool, &tc.arguments, tool_result_max)
                    }
                    Some(AgentTool::Memory { action, config }) => {
                        agent_run_memory_action(*action, config, &tc.arguments, config.max_chars)
                    }
                    None => (
                        format!("ERROR: tool '{}' is not permitted for this agent", tc.name),
                        Some("tool not permitted".to_string()),
                    ),
                };
                audit_agent_turn(
                    &run_id,
                    &op.name,
                    &model,
                    &mut turn_idx,
                    "tool",
                    Some(&result_text),
                    Some(&tc.name),
                    Some(tc.arguments.clone()),
                    None,
                    0,
                    0,
                    None,
                    0,
                    err.as_deref(),
                );
                messages.push(providers::ChatMessage {
                    role: "tool".into(),
                    content: Some(result_text),
                    tool_calls: None,
                    tool_call_id: Some(tc.id.clone()),
                });
            }
            continue;
        }

        // No tool call -> the model is done.
        last_content = resp.content.unwrap_or_default();
        status = "done";
        break;
    }

    if let Some(cfg) = memory.as_ref().filter(|m| m.retain_final) {
        if !last_content.trim().is_empty() {
            let mut options = object_or_empty(&cfg.retain_options);
            let mut metadata = object_or_empty(options.get("metadata").unwrap_or(&Value::Null));
            metadata.insert("source".into(), Value::String("rvbbit_agent".into()));
            metadata.insert("operator".into(), Value::String(op.name.clone()));
            metadata.insert("step".into(), Value::String(step_name.to_string()));
            metadata.insert("run_id".into(), Value::String(run_id.clone()));
            metadata.insert("context".into(), Value::String(cfg.context.clone()));
            metadata.insert("kind".into(), Value::String("final_answer".into()));
            options.insert("metadata".into(), Value::Object(metadata));
            options
                .entry("source")
                .or_insert_with(|| Value::String("rvbbit_agent".into()));
            let args = serde_json::json!({
                "content": last_content.as_str(),
                "options": Value::Object(options),
            });
            let (result_text, err) =
                agent_run_memory_action(AgentMemoryAction::Retain, cfg, &args, cfg.max_chars);
            audit_agent_turn(
                &run_id,
                &op.name,
                &model,
                &mut turn_idx,
                "tool",
                Some(&result_text),
                Some("memory_retain"),
                None,
                None,
                0,
                0,
                None,
                0,
                err.as_deref(),
            );
            if let Some(e) = err {
                if cfg.required {
                    return (
                        agent_subcall(
                            step_name,
                            &model,
                            gen_id,
                            agg_in,
                            agg_out,
                            agg_cost,
                            &total_t0,
                            Some(e),
                        ),
                        Value::Null,
                        String::new(),
                    );
                }
            }
        }
    }

    let out = serde_json::json!({
        "output": last_content,
        "agent_run_id": run_id,
        "status": status,
        "turns": turn_idx,
        "tokens_in": agg_in,
        "tokens_out": agg_out,
        "memory": memory.as_ref().map(|cfg| serde_json::json!({
            "provider": "hindsight",
            "service": cfg.service_name,
            "bank_id": cfg.bank_id,
            "context": cfg.context,
            "tools": cfg.allow_tools,
            "retain_final": cfg.retain_final,
        })),
    });
    (
        agent_subcall(
            step_name, &model, gen_id, agg_in, agg_out, agg_cost, &total_t0, None,
        ),
        out,
        last_content,
    )
}

/// The aggregate sub-call for an agent step: kind "llm" (so its summed tokens/cost
/// land in cost_events like any model call), with per-turn detail in agent_messages.
#[allow(clippy::too_many_arguments)]
fn agent_subcall(
    step_name: &str,
    model: &str,
    gen_id: Option<String>,
    tokens_in: i32,
    tokens_out: i32,
    cost: f64,
    t0: &Instant,
    error: Option<String>,
) -> SubCall {
    SubCall {
        step: step_name.into(),
        kind: "llm".into(),
        model: Some(model.to_string()),
        provider_generation_id: gen_id,
        tokens_in,
        tokens_out,
        // Pre-summed across turns; tell the reconciler not to overwrite from a
        // single generation id (per-turn ids live in agent_messages).
        cost_usd: if cost > 0.0 { Some(cost) } else { None },
        cost_source: Some("agent".into()),
        latency_ms: t0.elapsed().as_millis().min(i32::MAX as u128) as i32,
        error,
        ..Default::default()
    }
}

/// Fresh transcript id. The agent step always runs on the leader
/// (contains_leader_node), so SPI is available.
fn new_agent_run_id() -> String {
    pgrx::Spi::get_one::<String>("SELECT gen_random_uuid()::text")
        .ok()
        .flatten()
        .unwrap_or_else(|| "agent-run".to_string())
}

fn agent_memory_config(
    step: &Value,
    op: &OpDef,
    step_name: &str,
    scope: &Scope,
    tool_result_max: usize,
) -> Option<AgentMemoryConfig> {
    let memory = step.get("memory")?;
    let obj = match memory {
        Value::Bool(false) | Value::Null => return None,
        Value::Bool(true) => Map::new(),
        Value::Object(map) => {
            if map.get("enabled").and_then(Value::as_bool) == Some(false) {
                return None;
            }
            let provider = map
                .get("provider")
                .and_then(Value::as_str)
                .unwrap_or("hindsight")
                .trim()
                .to_ascii_lowercase();
            if provider != "hindsight" {
                return None;
            }
            map.clone()
        }
        _ => return None,
    };

    let rendered_string = |keys: &[&str], fallback: &str| {
        keys.iter()
            .find_map(|key| obj.get(*key).and_then(Value::as_str))
            .map(|s| scope.render(s).trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| fallback.to_string())
    };
    let service_name = rendered_string(&["service", "service_name"], "");
    let context = rendered_string(&["context", "context_id", "context_name"], "default");
    let limit = obj
        .get("limit")
        .or_else(|| obj.get("top_k"))
        .and_then(Value::as_i64)
        .unwrap_or(6)
        .clamp(1, 50);
    let max_chars = obj
        .get("max_chars")
        .or_else(|| obj.get("memory_max_chars"))
        .and_then(Value::as_u64)
        .map(|n| n as usize)
        .unwrap_or_else(|| tool_result_max.min(4000))
        .clamp(256, 64000);
    let mut recall_options = obj
        .get("recall_options")
        .or_else(|| obj.get("options"))
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    ensure_memory_limit(&mut recall_options, limit);

    Some(AgentMemoryConfig {
        service_name,
        context: context.clone(),
        bank_id: derive_agent_memory_bank(&op.name, step_name, &context),
        required: obj.get("required").and_then(Value::as_bool).unwrap_or(true),
        allow_tools: obj
            .get("allow_tools")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        recall_before_run: obj
            .get("recall_before_run")
            .or_else(|| obj.get("auto_recall"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        retain_final: obj
            .get("retain_final")
            .or_else(|| obj.get("auto_retain"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        async_retain: obj
            .get("async_retain")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        limit,
        max_chars,
        recall_options,
        retain_options: obj
            .get("retain_options")
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new())),
    })
}

fn derive_agent_memory_bank(operator: &str, step_name: &str, context: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(operator.as_bytes());
    hasher.update(b"\0");
    hasher.update(step_name.as_bytes());
    hasher.update(b"\0");
    hasher.update(context.as_bytes());
    let digest = hasher.finalize();
    let short = digest
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!(
        "rvbbit_agent_{}_{}_{}",
        memory_bank_segment(operator),
        memory_bank_segment(step_name),
        short
    )
    .chars()
    .take(120)
    .collect()
}

fn memory_bank_segment(raw: &str) -> String {
    let mut out = raw
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect::<String>();
    while out.contains("__") {
        out = out.replace("__", "_");
    }
    let trimmed = out.trim_matches('_').to_string();
    if trimmed.is_empty() {
        "agent".to_string()
    } else {
        trimmed.chars().take(32).collect()
    }
}

fn agent_memory_service_available(cfg: &AgentMemoryConfig) -> Result<(), String> {
    let arg = if cfg.service_name.trim().is_empty() {
        "NULL".to_string()
    } else {
        sql_lit(cfg.service_name.trim())
    };
    let sql = format!("SELECT rvbbit.hindsight_service({arg}) IS NOT NULL");
    let result: Result<Option<bool>, String> = pgrx::PgTryBuilder::new(move || {
        pgrx::Spi::get_one::<bool>(&sql).map_err(|e| e.to_string())
    })
    .catch_others(|caught| Err(crate::router::caught_error_message(caught)))
    .execute();
    match result {
        Ok(Some(true)) => Ok(()),
        Ok(_) => Err("no ready Hindsight memory service is registered".into()),
        Err(e) => Err(e),
    }
}

fn agent_run_memory_action(
    action: AgentMemoryAction,
    cfg: &AgentMemoryConfig,
    args: &Value,
    max_chars: usize,
) -> (String, Option<String>) {
    let query = args
        .get("query")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let content = args
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let mut options = match action {
        AgentMemoryAction::Recall | AgentMemoryAction::Reflect => {
            object_or_empty(&cfg.recall_options)
        }
        AgentMemoryAction::Retain => object_or_empty(&cfg.retain_options),
    };
    merge_object(
        &mut options,
        &object_or_empty(args.get("options").unwrap_or(&Value::Null)),
    );

    let sql = match action {
        AgentMemoryAction::Recall => {
            if query.is_empty() {
                return (
                    "ERROR: memory_recall requires a non-empty 'query' string".into(),
                    Some("no query".into()),
                );
            }
            ensure_memory_limit_value(&mut options, cfg.limit);
            format!(
                "SELECT rvbbit.hindsight_recall({}, {}, {}::jsonb, {})::text",
                sql_lit(&cfg.bank_id),
                sql_lit(query),
                sql_lit(&Value::Object(options).to_string()),
                sql_lit(&cfg.service_name),
            )
        }
        AgentMemoryAction::Reflect => {
            if query.is_empty() {
                return (
                    "ERROR: memory_reflect requires a non-empty 'query' string".into(),
                    Some("no query".into()),
                );
            }
            format!(
                "SELECT rvbbit.hindsight_reflect({}, {}, {}::jsonb, {})::text",
                sql_lit(&cfg.bank_id),
                sql_lit(query),
                sql_lit(&Value::Object(options).to_string()),
                sql_lit(&cfg.service_name),
            )
        }
        AgentMemoryAction::Retain => {
            if content.is_empty() {
                return (
                    "ERROR: memory_retain requires a non-empty 'content' string".into(),
                    Some("no content".into()),
                );
            }
            options
                .entry("source")
                .or_insert_with(|| Value::String("rvbbit_agent".into()));
            format!(
                "SELECT rvbbit.hindsight_retain({}, {}, {}::jsonb, {}, {})::text",
                sql_lit(&cfg.bank_id),
                sql_lit(content),
                sql_lit(&Value::Object(options).to_string()),
                sql_lit(&cfg.service_name),
                if cfg.async_retain { "true" } else { "false" },
            )
        }
    };

    let result: Result<Option<String>, String> = pgrx::PgTryBuilder::new(move || {
        pgrx::Spi::get_one::<String>(&sql).map_err(|e| e.to_string())
    })
    .catch_others(|caught| Err(crate::router::caught_error_message(caught)))
    .execute();
    match result {
        Ok(Some(json)) => (truncate_tool_result(&json, max_chars), None),
        Ok(None) => ("null".into(), None),
        Err(e) => {
            let msg = format!("ERROR: {e}");
            (truncate_tool_result(&msg, max_chars), Some(e))
        }
    }
}

fn object_or_empty(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

fn merge_object(base: &mut Map<String, Value>, overlay: &Map<String, Value>) {
    for (key, value) in overlay {
        base.insert(key.clone(), value.clone());
    }
}

fn ensure_memory_limit(value: &mut Value, limit: i64) {
    if let Value::Object(map) = value {
        ensure_memory_limit_value(map, limit);
    }
}

fn ensure_memory_limit_value(map: &mut Map<String, Value>, limit: i64) {
    if !map.contains_key("top_k") && !map.contains_key("limit") {
        map.insert("top_k".into(), Value::from(limit));
    }
}

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// Load an MCP tool's description + input schema so the model can be told how to
/// call it. Returns None (tool silently skipped) if the server/tool is unknown.
fn load_mcp_tool_spec(server: &str, tool: &str) -> Option<(String, Value)> {
    let wrapped = format!(
        "SELECT to_jsonb(t) FROM (SELECT coalesce(description,'') AS d, \
         coalesce(input_schema, '{{\"type\":\"object\"}}'::jsonb) AS s \
         FROM rvbbit.mcp_tools WHERE server = '{}' AND name = '{}' LIMIT 1) t",
        server.replace('\'', "''"),
        tool.replace('\'', "''"),
    );
    let row = pgrx::Spi::get_one::<pgrx::JsonB>(&wrapped)
        .ok()
        .flatten()?
        .0;
    let desc = row
        .get("d")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let schema = row
        .get("s")
        .cloned()
        .unwrap_or_else(|| serde_json::json!({"type": "object"}));
    Some((desc, schema))
}

/// Built-in read-only `query` tool. The model's SQL runs read-only at the SPI
/// level (writes/DML-in-CTE are rejected by the engine — not by a keyword
/// blacklist, so time functions like now()/generate_series stay available),
/// inside a subtransaction (a bad query can't abort the agent's operator txn),
/// capped at 200 rows + a wall timeout, result truncated to `max_chars`.
fn agent_run_readonly_query(args: &Value, max_chars: usize) -> (String, Option<String>) {
    let raw = args
        .get("sql")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let sql = raw.trim_end_matches(';').trim();
    if sql.is_empty() {
        return (
            "ERROR: the `query` tool requires a non-empty 'sql' string".into(),
            Some("no sql".into()),
        );
    }
    let head = sql.to_lowercase();
    if !(head.starts_with("select") || head.starts_with("with")) {
        return (
            "ERROR: the `query` tool only runs read-only SELECT/WITH statements (no writes or DDL)."
                .into(),
            Some("not a select".into()),
        );
    }
    // jsonb_agg wrapper -> one text payload; LIMIT caps rows. SELECT shape forces
    // a read; SPI read-only is the real write guard.
    let wrapped = format!(
        "SELECT coalesce(jsonb_agg(t), '[]'::jsonb)::text \
         FROM (SELECT * FROM ({}) _agent_q LIMIT 200) t",
        sql
    );
    let result: Result<Option<String>, String> = pgrx::PgTryBuilder::new(move || {
        let _ = pgrx::Spi::run("SET LOCAL statement_timeout = '15s'");
        pgrx::Spi::get_one::<String>(&wrapped).map_err(|e| e.to_string())
    })
    // Clean PG message (`column "x" does not exist`) instead of the raw struct
    // debug — the model recovers faster and the transcript log reads cleanly.
    .catch_others(|caught| Err(crate::router::caught_error_message(caught)))
    .execute();
    match result {
        Ok(Some(json)) => (truncate_tool_result(&json, max_chars), None),
        Ok(None) => ("[]".into(), None),
        Err(e) => {
            let msg = format!("ERROR: {}", e);
            (truncate_tool_result(&msg, max_chars), Some(e))
        }
    }
}

/// Allow-listed MCP tool — calls the gateway, returns the tool's text body.
fn agent_run_mcp_tool(
    server: &str,
    tool: &str,
    args: &Value,
    max_chars: usize,
) -> (String, Option<String>) {
    match crate::mcp::call(server, tool, args) {
        Ok(envelope) => {
            let text = crate::mcp::first_text(&envelope).unwrap_or_else(|| envelope.to_string());
            (truncate_tool_result(&text, max_chars), None)
        }
        Err(e) => {
            let msg = format!("ERROR calling {server}/{tool}: {e}");
            (truncate_tool_result(&msg, max_chars), Some(e.to_string()))
        }
    }
}

/// Clamp a tool result so one big payload can't blow the context window.
fn truncate_tool_result(s: &str, max_chars: usize) -> String {
    let total = s.chars().count();
    if total <= max_chars {
        return s.to_string();
    }
    let kept: String = s.chars().take(max_chars).collect();
    format!(
        "{kept}\n…[truncated {} of {total} chars — narrow the query/filter to see more]",
        total - max_chars
    )
}

/// Append one transcript turn to rvbbit.agent_messages. Best-effort + in-txn for
/// v0 (visible on commit); an insert failure (e.g. table absent) is swallowed so
/// it can never abort the operator. Out-of-band durability on abort is a v0.1 step.
#[allow(clippy::too_many_arguments)]
fn audit_agent_turn(
    run_id: &str,
    operator: &str,
    model: &str,
    idx: &mut i32,
    role: &str,
    content: Option<&str>,
    tool_name: Option<&str>,
    tool_calls: Option<Value>,
    finish: Option<&str>,
    tokens_in: i32,
    tokens_out: i32,
    cost: Option<f64>,
    latency_ms: i32,
    error: Option<&str>,
) {
    let esc = |s: &str| s.replace('\'', "''");
    let txt = |o: Option<&str>| {
        o.map(|s| format!("'{}'", esc(s)))
            .unwrap_or_else(|| "NULL".into())
    };
    let jsn = |o: Option<Value>| {
        o.map(|v| format!("'{}'::jsonb", esc(&v.to_string())))
            .unwrap_or_else(|| "NULL".into())
    };
    let num = |o: Option<f64>| o.map(|c| c.to_string()).unwrap_or_else(|| "NULL".into());
    let sql = format!(
        "INSERT INTO rvbbit.agent_messages \
         (run_id, operator, model, turn_idx, role, content, tool_name, tool_calls, finish_reason, \
          tokens_in, tokens_out, cost_usd, latency_ms, error) \
         VALUES ('{}', '{}', '{}', {}, '{}', {}, {}, {}, {}, {}, {}, {}, {}, {})",
        esc(run_id),
        esc(operator),
        esc(model),
        idx,
        esc(role),
        txt(content),
        txt(tool_name),
        jsn(tool_calls),
        txt(finish),
        tokens_in,
        tokens_out,
        num(cost),
        latency_ms,
        txt(error),
    );
    let _ = pgrx::PgTryBuilder::new(move || {
        let _ = pgrx::Spi::run(&sql);
    })
    .catch_others(|_| ())
    .execute();
    *idx += 1;
}

/// Run a code step. The step config looks like:
///   {"name":"clean","kind":"code","fn":"trim","inputs":{"text":"{{ steps.x.output }}"}}
fn run_step_code(step: &Value, step_name: &str, scope: &Scope) -> (SubCall, Value, String) {
    let fn_name = step
        .get("fn")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let inputs_raw = step
        .get("inputs")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let rendered = render_value_templates(&inputs_raw, scope);

    let t0 = Instant::now();
    let res = crate::code_steps::invoke(&fn_name, &rendered);
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

    match res {
        Ok(value) => {
            let text = match &value {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            (
                SubCall {
                    step: step_name.into(),
                    kind: "code".into(),
                    model: Some(fn_name),
                    tokens_in: 0,
                    tokens_out: 0,
                    latency_ms,
                    error: None,
                    ..Default::default()
                },
                serde_json::json!({"output": value}),
                text,
            )
        }
        Err(e) => (
            SubCall {
                step: step_name.into(),
                kind: "code".into(),
                model: Some(fn_name),
                tokens_in: 0,
                tokens_out: 0,
                latency_ms,
                error: Some(e),
                ..Default::default()
            },
            Value::Null,
            String::new(),
        ),
    }
}

/// Run a Python sidecar step. The step config looks like:
///   {"name":"score","kind":"python","handler":"sla_score","env":"analytics",
///    "inputs":{"text":"{{ inputs.body }}"}, "timeout_ms":1000}
fn run_step_python(step: &Value, step_name: &str, scope: &Scope) -> (SubCall, Value, String) {
    let handler_name = step
        .get("handler")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if handler_name.is_empty() {
        return python_error(step_name, "?", "step missing 'handler' field");
    }
    let expected_env = step
        .get("env")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty());
    let inputs_raw = step
        .get("inputs")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let rendered = render_value_templates(&inputs_raw, scope);
    let timeout_override = step
        .get("timeout_ms")
        .and_then(|v| v.as_u64())
        .filter(|n| *n > 0);

    let spec = match crate::python_runtime::load_spec(&handler_name, expected_env) {
        Ok(s) => s,
        Err(e) => return python_error(step_name, &handler_name, &e),
    };
    let label = crate::python_runtime::label(&spec);
    match crate::python_runtime::run(&spec, &rendered, timeout_override) {
        Ok(run) => {
            let text = match &run.output {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                other => other.to_string(),
            };
            (
                SubCall {
                    step: step_name.into(),
                    kind: "python".into(),
                    model: Some(label),
                    backend: Some(spec.env_name.clone()),
                    transport: Some("python_sidecar".into()),
                    tokens_in: 0,
                    tokens_out: 0,
                    latency_ms: run.duration_ms,
                    error: None,
                    ..Default::default()
                },
                serde_json::json!({"output": run.output}),
                text,
            )
        }
        Err(e) => python_error(step_name, &handler_name, &e),
    }
}

fn python_error(step_name: &str, handler_name: &str, err: &str) -> (SubCall, Value, String) {
    (
        SubCall {
            step: step_name.into(),
            kind: "python".into(),
            model: Some(handler_name.into()),
            tokens_in: 0,
            tokens_out: 0,
            latency_ms: 0,
            error: Some(err.into()),
            ..Default::default()
        },
        Value::Null,
        String::new(),
    )
}

/// Run a specialist step. The step config looks like:
///   {"name":"s","kind":"specialist","specialist":"sentiment_v1",
///    "inputs":{"text":"{{ inputs.text }}"}}
///
/// Spec resolution: tries the per-backend cache first (safe from any thread),
/// then falls back to an SPI load (LEADER ONLY — workers must rely on cache).
/// When called from a prewarm pool thread, the leader is responsible for
/// having warmed the cache before dispatch (specialists::reload_all() or
/// load_spec()).
fn run_step_specialist(step: &Value, step_name: &str, scope: &Scope) -> (SubCall, Value, String) {
    let spec_name = step
        .get("specialist")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if spec_name.is_empty() {
        return specialist_error(step_name, "?", "step missing 'specialist' field");
    }

    // Render templates inside `inputs`, then send the rendered object as
    // a single specialist input. (Batching across rows happens at prewarm.)
    let inputs_raw = step
        .get("inputs")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let rendered = render_value_templates(&inputs_raw, scope);

    let spec = match crate::specialists::get_cached_spec(&spec_name) {
        Some(s) => s,
        None => match crate::specialists::load_spec(&spec_name) {
            Ok(s) => s,
            Err(e) => return specialist_error(step_name, &spec_name, &e.to_string()),
        },
    };

    let t0 = Instant::now();
    let res = crate::specialists::predict_one(&spec, &rendered);
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

    match res {
        Ok(value) => {
            let text = match &value {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            (
                SubCall {
                    step: step_name.into(),
                    kind: "specialist".into(),
                    model: Some(spec_name),
                    backend: Some(spec.name.clone()),
                    transport: Some(spec.transport.clone()),
                    tokens_in: 0,
                    tokens_out: 0,
                    latency_ms,
                    error: None,
                    ..Default::default()
                },
                serde_json::json!({"output": value}),
                text,
            )
        }
        Err(e) => specialist_error(step_name, &spec_name, &e.to_string()),
    }
}

fn specialist_error(step_name: &str, spec_name: &str, err: &str) -> (SubCall, Value, String) {
    (
        SubCall {
            step: step_name.into(),
            kind: "specialist".into(),
            model: Some(spec_name.into()),
            tokens_in: 0,
            tokens_out: 0,
            latency_ms: 0,
            error: Some(err.into()),
            ..Default::default()
        },
        Value::Null,
        String::new(),
    )
}

/// Run a SQL step — a parameterized SELECT against the database. `$1..$N`
/// in the `sql` text are filled from the rendered `params` templates as
/// quoted literals (parameterized — an LLM-derived param cannot inject).
/// The first row is returned as a {column: value} jsonb object, so a later
/// node reads `{{ steps.<name>.output.<column> }}`. Zero rows → null.
///
/// LEADER / backend context only — SPI cannot run on a flow pool thread,
/// so callers route sql-bearing operators to the leader.
fn run_step_sql(step: &Value, step_name: &str, scope: &Scope) -> (SubCall, Value, String) {
    let sql_tmpl = step.get("sql").and_then(|v| v.as_str()).unwrap_or("");
    if sql_tmpl.trim().is_empty() {
        return sql_error(step_name, "step missing 'sql' field");
    }

    let params: Vec<Value> = step
        .get("params")
        .and_then(|p| p.as_array())
        .map(|arr| {
            arr.iter()
                .map(|p| render_value_templates(p, scope))
                .collect()
        })
        .unwrap_or_default();
    let mut sql = sql_tmpl.to_string();
    // Highest index first so $10 is substituted before $1.
    for (i, p) in params.iter().enumerate().rev() {
        sql = sql.replace(&format!("${}", i + 1), &sql_param_literal(p));
    }

    // Wrap so the first row comes back as one {column: value} object.
    let wrapped = format!("SELECT to_jsonb(t) FROM ({sql}) t LIMIT 1");
    let t0 = Instant::now();
    let res = pgrx::Spi::get_one::<pgrx::JsonB>(&wrapped);
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

    match res {
        Ok(found) => {
            let value = found.map(|j| j.0).unwrap_or(Value::Null);
            let text = match &value {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                other => other.to_string(),
            };
            (
                SubCall {
                    step: step_name.into(),
                    kind: "sql".into(),
                    model: None,
                    tokens_in: 0,
                    tokens_out: 0,
                    latency_ms,
                    error: None,
                    ..Default::default()
                },
                serde_json::json!({ "output": value }),
                text,
            )
        }
        Err(e) => sql_error(step_name, &e.to_string()),
    }
}

fn sql_error(step_name: &str, err: &str) -> (SubCall, Value, String) {
    (
        SubCall {
            step: step_name.into(),
            kind: "sql".into(),
            model: None,
            tokens_in: 0,
            tokens_out: 0,
            latency_ms: 0,
            error: Some(err.into()),
            ..Default::default()
        },
        Value::Null,
        String::new(),
    )
}

/// Render a JSON value as a SQL literal for `$N` substitution. Strings are
/// quote-escaped; numbers and bools pass through bare; objects/arrays
/// become a quoted JSON text (cast with `::jsonb` in the query if needed).
fn sql_param_literal(v: &Value) -> String {
    match v {
        Value::Null => "NULL".to_string(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => format!("'{}'", s.replace('\'', "''")),
        other => format!("'{}'", other.to_string().replace('\'', "''")),
    }
}

/// Run an MCP step — call a tool on a registered MCP server. The step
/// looks like `{kind:"mcp", server:"x", tool:"y", inputs:{...}}`. Inputs
/// are templated (like a specialist node's inputs) then sent to the
/// gateway as the tool's arguments.
///
/// Output: the text body of the tool result, parsed as JSON if possible.
/// So if the tool returns `{"items":[...]}` as text, downstream nodes can
/// read `{{ steps.<name>.output.items }}`; if it returns plain text,
/// `{{ steps.<name>.output }}` is that string. A tool that returned
/// `isError=true` surfaces as a step error (sub_call.error is set).
fn run_step_mcp(step: &Value, step_name: &str, scope: &Scope) -> (SubCall, Value, String) {
    let server = step
        .get("server")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let tool = step
        .get("tool")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if server.is_empty() || tool.is_empty() {
        return mcp_error(step_name, &server, &tool, "step missing 'server' or 'tool'");
    }

    let inputs_raw = step
        .get("inputs")
        .cloned()
        .unwrap_or(Value::Object(Default::default()));
    let rendered = render_value_templates(&inputs_raw, scope);

    let t0 = Instant::now();
    let res = crate::mcp::call(&server, &tool, &rendered);
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

    match res {
        Ok(envelope) => {
            let is_error = envelope
                .get("isError")
                .and_then(|b| b.as_bool())
                .unwrap_or(false);
            let text = crate::mcp::first_text(&envelope).unwrap_or_default();
            // Best-effort log to rvbbit.mcp_invocations (skipped on pool
            // threads — see crate::mcp::log_invocation).
            let error_text = if is_error {
                Some(if text.is_empty() {
                    "tool returned isError=true".to_string()
                } else {
                    text.clone()
                })
            } else {
                None
            };
            crate::mcp::log_invocation(
                &server,
                &tool,
                &rendered,
                &envelope,
                error_text.as_deref(),
                latency_ms,
                false,
            );

            // Parse text as JSON if possible; otherwise return the string.
            let payload = serde_json::from_str::<Value>(&text)
                .unwrap_or_else(|_| Value::String(text.clone()));
            let output_text = if payload.is_string() {
                text.clone()
            } else {
                payload.to_string()
            };

            (
                SubCall {
                    step: step_name.into(),
                    kind: "mcp".into(),
                    model: Some(format!("{server}.{tool}")),
                    backend: Some("mcp".into()),
                    transport: Some("mcp".into()),
                    tokens_in: 0,
                    tokens_out: 0,
                    latency_ms,
                    error: error_text,
                    ..Default::default()
                },
                serde_json::json!({ "output": payload }),
                output_text,
            )
        }
        Err(e) => mcp_error(step_name, &server, &tool, &e.to_string()),
    }
}

fn mcp_error(step_name: &str, server: &str, tool: &str, err: &str) -> (SubCall, Value, String) {
    (
        SubCall {
            step: step_name.into(),
            kind: "mcp".into(),
            model: Some(if server.is_empty() || tool.is_empty() {
                "?.?".into()
            } else {
                format!("{server}.{tool}")
            }),
            tokens_in: 0,
            tokens_out: 0,
            latency_ms: 0,
            error: Some(err.into()),
            ..Default::default()
        },
        Value::Null,
        String::new(),
    )
}

#[derive(Clone, Debug)]
struct N8nRuntime {
    name: String,
    base_url: String,
    webhook_path_prefix: String,
    auth_header_name: Option<String>,
    auth_header_env: Option<String>,
}

/// Run an n8n step by posting rendered inputs to a configured production
/// webhook. The n8n database is intentionally not part of execution; DB
/// introspection only helps Lens populate workflow/path pickers.
fn run_step_n8n(step: &Value, step_name: &str, scope: &Scope) -> (SubCall, Value, String) {
    let runtime_name = step
        .get("runtime")
        .or_else(|| step.get("runtime_name"))
        .and_then(|v| v.as_str())
        .unwrap_or("default")
        .trim()
        .to_string();
    let webhook_path = step
        .get("webhook")
        .or_else(|| step.get("webhook_path"))
        .or_else(|| step.get("path"))
        .and_then(|v| v.as_str())
        .map(|s| scope.render(s).trim().to_string())
        .unwrap_or_default();
    if webhook_path.is_empty() {
        return n8n_error(step_name, &runtime_name, "", "step missing 'webhook' path");
    }

    let runtime = match load_n8n_runtime(&runtime_name) {
        Ok(runtime) => runtime,
        Err(e) => return n8n_error(step_name, &runtime_name, &webhook_path, &e),
    };
    let url = n8n_webhook_url(&runtime, &webhook_path);
    let method_raw = step
        .get("method")
        .and_then(|v| v.as_str())
        .unwrap_or("POST")
        .trim()
        .to_ascii_uppercase();
    let method = match reqwest::Method::from_bytes(method_raw.as_bytes()) {
        Ok(method) => method,
        Err(e) => return n8n_error(step_name, &runtime.name, &webhook_path, &e.to_string()),
    };
    let timeout = step
        .get("timeout_ms")
        .and_then(|v| v.as_u64())
        .filter(|n| *n > 0)
        .unwrap_or(60_000)
        .min(15 * 60_000);

    let payload_raw = step
        .get("body")
        .or_else(|| step.get("inputs"))
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()));
    let payload = render_value_templates(&payload_raw, scope);
    let headers_raw = step
        .get("headers")
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()));
    let headers = render_value_templates(&headers_raw, scope);

    let t0 = Instant::now();
    let mut req = crate::specialists::http_client()
        .request(method.clone(), &url)
        .timeout(Duration::from_millis(timeout));

    match n8n_auth_header(&runtime) {
        Ok(Some((header_name, header_value))) => {
            req = match add_header(req, &header_name, &header_value) {
                Ok(req) => req,
                Err(e) => return n8n_error(step_name, &runtime.name, &webhook_path, &e),
            };
        }
        Ok(None) => {}
        Err(e) => return n8n_error(step_name, &runtime.name, &webhook_path, &e),
    }
    if let Value::Object(map) = &headers {
        for (key, value) in map {
            let value = value_to_string(value);
            if value.trim().is_empty() {
                continue;
            }
            req = match add_header(req, key, &value) {
                Ok(req) => req,
                Err(e) => return n8n_error(step_name, &runtime.name, &webhook_path, &e),
            };
        }
    }

    req = if method == reqwest::Method::GET || method == reqwest::Method::HEAD {
        let query = flat_query_params(&payload);
        req.query(&query)
    } else {
        req.json(&payload)
    };

    let response = match req.send() {
        Ok(resp) => resp,
        Err(e) => return n8n_error(step_name, &runtime.name, &webhook_path, &e.to_string()),
    };
    let status = response.status();
    let text = match response.text() {
        Ok(text) => text,
        Err(e) => return n8n_error(step_name, &runtime.name, &webhook_path, &e.to_string()),
    };
    let latency_ms = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;

    if !status.is_success() {
        return (
            SubCall {
                step: step_name.into(),
                kind: "n8n".into(),
                model: Some(n8n_model_label(&runtime.name, &webhook_path)),
                backend: Some(runtime.name.clone()),
                transport: Some("http_webhook".into()),
                upstream_id: step
                    .get("workflow_id")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                tokens_in: 0,
                tokens_out: 0,
                latency_ms,
                error: Some(format!(
                    "n8n webhook returned HTTP {}: {}",
                    status.as_u16(),
                    truncate_tool_result(&text, 2000)
                )),
                ..Default::default()
            },
            Value::Null,
            String::new(),
        );
    }

    let payload =
        serde_json::from_str::<Value>(&text).unwrap_or_else(|_| Value::String(text.clone()));
    let output_text = if payload.is_string() {
        text
    } else {
        payload.to_string()
    };
    (
        SubCall {
            step: step_name.into(),
            kind: "n8n".into(),
            model: Some(n8n_model_label(&runtime.name, &webhook_path)),
            backend: Some(runtime.name.clone()),
            transport: Some("http_webhook".into()),
            upstream_id: step
                .get("workflow_id")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
            tokens_in: 0,
            tokens_out: 0,
            latency_ms,
            error: None,
            ..Default::default()
        },
        serde_json::json!({
            "output": payload,
            "status": status.as_u16(),
            "runtime": runtime.name,
            "webhook": webhook_path,
        }),
        output_text,
    )
}

fn load_n8n_runtime(name: &str) -> Result<N8nRuntime, String> {
    let wanted = if name.trim().is_empty() {
        "default"
    } else {
        name.trim()
    };
    let sql = format!(
        "WITH picked AS ( \
             SELECT name, base_url, webhook_path_prefix, auth_header_name, auth_header_env \
               FROM rvbbit.n8n_runtimes WHERE name = {wanted} \
             UNION ALL \
             SELECT name, base_url, webhook_path_prefix, auth_header_name, auth_header_env \
               FROM rvbbit.n8n_runtimes \
              WHERE {wanted} = 'default' AND (SELECT count(*) FROM rvbbit.n8n_runtimes) = 1 \
         ) \
         SELECT to_jsonb(picked) FROM picked LIMIT 1",
        wanted = sql_lit(wanted),
    );
    let row: Result<Option<Value>, String> = pgrx::PgTryBuilder::new(move || {
        Ok(pgrx::Spi::get_one::<pgrx::JsonB>(&sql)
            .map_err(|e| e.to_string())?
            .map(|j| j.0))
    })
    .catch_others(|caught| Err(crate::router::caught_error_message(caught)))
    .execute();
    let row = row?;
    let row = row.ok_or_else(|| format!("no rvbbit.n8n_runtimes row named '{wanted}'"))?;
    Ok(N8nRuntime {
        name: row
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or(wanted)
            .to_string(),
        base_url: row
            .get("base_url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim_end_matches('/')
            .to_string(),
        webhook_path_prefix: row
            .get("webhook_path_prefix")
            .and_then(Value::as_str)
            .unwrap_or("/webhook")
            .trim_end_matches('/')
            .to_string(),
        auth_header_name: row
            .get("auth_header_name")
            .and_then(Value::as_str)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
        auth_header_env: row
            .get("auth_header_env")
            .and_then(Value::as_str)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
    })
}

fn n8n_webhook_url(runtime: &N8nRuntime, path: &str) -> String {
    let p = path.trim();
    if p.starts_with("http://") || p.starts_with("https://") {
        return p.to_string();
    }
    if p.starts_with('/') {
        return format!("{}{}", runtime.base_url, p);
    }
    let prefix = if runtime.webhook_path_prefix.is_empty() {
        "/webhook"
    } else {
        runtime.webhook_path_prefix.as_str()
    };
    format!(
        "{}/{}/{}",
        runtime.base_url,
        prefix.trim_matches('/'),
        p.trim_matches('/')
    )
}

fn n8n_auth_header(runtime: &N8nRuntime) -> Result<Option<(String, String)>, String> {
    let Some(name) = runtime.auth_header_name.as_ref() else {
        return Ok(None);
    };
    let Some(env) = runtime.auth_header_env.as_ref() else {
        return Ok(None);
    };
    let value = std::env::var(env)
        .map_err(|_| format!("configured n8n auth env '{env}' is not visible to Postgres"))?
        .trim()
        .to_string();
    if value.is_empty() {
        Err(format!("configured n8n auth env '{env}' is empty"))
    } else {
        Ok(Some((name.clone(), value)))
    }
}

fn add_header(
    req: reqwest::blocking::RequestBuilder,
    name: &str,
    value: &str,
) -> Result<reqwest::blocking::RequestBuilder, String> {
    let header_name = reqwest::header::HeaderName::from_bytes(name.trim().as_bytes())
        .map_err(|e| format!("invalid HTTP header name '{name}': {e}"))?;
    let header_value = reqwest::header::HeaderValue::from_str(value)
        .map_err(|e| format!("invalid HTTP header value for '{name}': {e}"))?;
    Ok(req.header(header_name, header_value))
}

fn flat_query_params(value: &Value) -> Vec<(String, String)> {
    match value {
        Value::Object(map) => map
            .iter()
            .map(|(k, v)| (k.clone(), value_to_string(v)))
            .collect(),
        other => vec![("payload".into(), value_to_string(other))],
    }
}

fn n8n_model_label(runtime: &str, webhook: &str) -> String {
    format!("{runtime}:{}", webhook.trim_matches('/'))
}

fn n8n_error(step_name: &str, runtime: &str, webhook: &str, err: &str) -> (SubCall, Value, String) {
    (
        SubCall {
            step: step_name.into(),
            kind: "n8n".into(),
            model: Some(n8n_model_label(runtime, webhook)),
            backend: if runtime.is_empty() {
                None
            } else {
                Some(runtime.into())
            },
            transport: Some("http_webhook".into()),
            tokens_in: 0,
            tokens_out: 0,
            latency_ms: 0,
            error: Some(err.into()),
            ..Default::default()
        },
        Value::Null,
        String::new(),
    )
}

// ---------------------------------------------------------------------------
// Templating scope
// ---------------------------------------------------------------------------

/// Variables available to {{ … }} placeholders in step prompts:
///   inputs.<name>       — operator arg
///   opts.<name>         — per-call option
///   steps.<name>.<field> — output of an earlier step
///
/// Backward compat: in single-step operators, `{{ text }}` also resolves
/// to `{{ inputs.text }}` (the existing behavior we promised never to
/// break for built-ins).
pub struct Scope {
    pub inputs: Value,
    pub opts: Value,
    pub steps: HashMap<String, Value>,
}

impl Scope {
    pub fn new(inputs: Value, opts: Value) -> Self {
        Self {
            inputs,
            opts,
            steps: HashMap::new(),
        }
    }

    /// Render a template by substituting `{{ key }}` and `{{ a.b.c }}`
    /// references.
    pub fn render(&self, template: &str) -> String {
        let mut out = String::with_capacity(template.len());
        let bytes = template.as_bytes();
        let mut i = 0;
        while i < bytes.len() {
            if i + 1 < bytes.len() && bytes[i] == b'{' && bytes[i + 1] == b'{' {
                let start = i + 2;
                let mut end = start;
                while end + 1 < bytes.len() && !(bytes[end] == b'}' && bytes[end + 1] == b'}') {
                    end += 1;
                }
                if end + 1 < bytes.len() {
                    let raw = std::str::from_utf8(&bytes[start..end]).unwrap_or("").trim();
                    let v = self.lookup(raw);
                    out.push_str(&value_to_string(&v));
                    i = end + 2;
                    continue;
                }
            }
            out.push(bytes[i] as char);
            i += 1;
        }
        out
    }

    fn lookup(&self, path: &str) -> Value {
        // Empty / weird → empty string
        if path.is_empty() {
            return Value::String(String::new());
        }
        let parts: Vec<&str> = path.split('.').collect();
        let root = parts[0];
        let descend = &parts[1..];

        let base = match root {
            "inputs" => self.inputs.clone(),
            "opts" => self.opts.clone(),
            "steps" => {
                if let Some(name) = descend.first() {
                    let s = self.steps.get(*name).cloned().unwrap_or(Value::Null);
                    return descend_value(&s, &descend[1..]);
                }
                return Value::Object(Default::default());
            }
            // Backward-compat single-step: `{{ foo }}` -> inputs.foo
            other => {
                if let Some(v) = self.inputs.get(other) {
                    return v.clone();
                }
                Value::Null
            }
        };
        descend_value(&base, descend)
    }
}

fn descend_value(base: &Value, path: &[&str]) -> Value {
    let mut cur = base.clone();
    for p in path {
        // Numeric segments index into arrays; otherwise object lookup.
        cur = if let Ok(idx) = p.parse::<usize>() {
            cur.get(idx).cloned().unwrap_or(Value::Null)
        } else {
            cur.get(*p).cloned().unwrap_or(Value::Null)
        };
    }
    cur
}

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Recursively walk a Value rendering any string templates against the
/// scope. Used for code-step inputs which look like
///   {"text": "{{ steps.x.output }}", "n": "{{ inputs.k }}"}
pub fn render_value_templates(v: &Value, scope: &Scope) -> Value {
    match v {
        Value::String(s) => {
            // If the entire string is a single {{...}} reference, return
            // the raw underlying Value (preserving type). Otherwise treat
            // as a template and return a string.
            let trimmed = s.trim();
            if trimmed.starts_with("{{") && trimmed.ends_with("}}") {
                let inner = trimmed[2..trimmed.len() - 2].trim();
                if !inner.contains("{{") {
                    return scope.lookup(inner);
                }
            }
            Value::String(scope.render(s))
        }
        Value::Array(arr) => Value::Array(
            arr.iter()
                .map(|x| render_value_templates(x, scope))
                .collect(),
        ),
        Value::Object(obj) => Value::Object(
            obj.iter()
                .map(|(k, v)| (k.clone(), render_value_templates(v, scope)))
                .collect(),
        ),
        other => other.clone(),
    }
}

// Suppress unused-import warning when providers::ProviderError isn't
// directly named (we propagate via to_string).
#[allow(dead_code)]
fn _silence_unused_err(_: ProviderError) {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn test_op() -> OpDef {
        OpDef {
            name: "support_triage".into(),
            shape: "scalar".into(),
            return_type: "text".into(),
            model: "openai/gpt-5.4-mini".into(),
            system_prompt: String::new(),
            user_prompt: String::new(),
            parser: "text".into(),
            max_tokens: 256,
            temperature: None,
            steps: None,
            retry: None,
            wards: None,
            takes: None,
            cache_policy: "never".into(),
        }
    }

    #[test]
    fn agent_memory_true_uses_scoped_defaults() {
        let scope = Scope::new(json!({"tenant": "acme"}), Value::Null);
        let step = json!({"name": "analyst", "kind": "agent", "memory": true});
        let cfg = agent_memory_config(&step, &test_op(), "analyst", &scope, 8000).unwrap();
        assert_eq!(cfg.context, "default");
        assert_eq!(cfg.service_name, "");
        assert!(cfg.allow_tools);
        assert!(cfg.recall_before_run);
        assert!(cfg.retain_final);
        assert!(cfg.required);
        assert_eq!(cfg.max_chars, 4000);
        assert!(cfg
            .bank_id
            .starts_with("rvbbit_agent_support_triage_analyst_"));
        assert_eq!(cfg.recall_options["top_k"], 6);
    }

    #[test]
    fn agent_memory_object_renders_context_and_options() {
        let scope = Scope::new(json!({"tenant": "acme"}), Value::Null);
        let step = json!({
            "name": "analyst",
            "kind": "agent",
            "memory": {
                "enabled": true,
                "context": "{{ inputs.tenant }}",
                "service": "hindsight_default",
                "required": true,
                "allow_tools": false,
                "recall_before_run": false,
                "retain_final": false,
                "limit": 9,
                "max_chars": 1234,
                "recall_options": { "top_k": 3 },
                "retain_options": { "source": "unit" }
            }
        });
        let cfg = agent_memory_config(&step, &test_op(), "analyst", &scope, 8000).unwrap();
        assert_eq!(cfg.context, "acme");
        assert_eq!(cfg.service_name, "hindsight_default");
        assert!(cfg.required);
        assert!(!cfg.allow_tools);
        assert!(!cfg.recall_before_run);
        assert!(!cfg.retain_final);
        assert_eq!(cfg.limit, 9);
        assert_eq!(cfg.max_chars, 1234);
        assert_eq!(cfg.recall_options["top_k"], 3);
        assert_eq!(cfg.retain_options["source"], "unit");
    }

    #[test]
    fn agent_memory_bank_changes_with_context() {
        let a = derive_agent_memory_bank("support_triage", "analyst", "acme");
        let b = derive_agent_memory_bank("support_triage", "analyst", "globex");
        assert_ne!(a, b);
        assert!(a.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'));
    }
}
