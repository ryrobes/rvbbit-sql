//! Unit-of-work executor.
//!
//! One operator invocation = one UnitOfWork = one receipt row.
//! Internally the unit may execute N steps of different kinds (llm, code,
//! specialist). The receipt's `sub_calls` jsonb captures the per-step
//! audit (tokens, latency, errors). Roll-up totals are in the receipt's
//! top-level columns.
//!
//! When the operator's `steps` field is NULL, we run a single LLM call
//! using the operator's system_prompt + user_prompt (today's behavior —
//! backward compatible). When `steps` is set, we iterate the array and
//! dispatch by `kind`.

use std::collections::HashMap;
use std::time::Instant;

use serde_json::Value;

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
    steps
        .and_then(|s| s.as_array())
        .map(|arr| {
            arr.iter()
                .any(|n| n.get("kind").and_then(|k| k.as_str()) == Some("sql"))
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
        let kind = step.get("kind").and_then(|v| v.as_str()).unwrap_or("");

        let (sub, step_output, output_text) = match kind {
            "llm" => run_step_llm(op, step, &step_name, scope),
            "code" => run_step_code(step, &step_name, scope),
            "specialist" => run_step_specialist(step, &step_name, scope),
            "sql" => run_step_sql(step, &step_name, scope),
            "mcp" => run_step_mcp(step, &step_name, scope),
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
