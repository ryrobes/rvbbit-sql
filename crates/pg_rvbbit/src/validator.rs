//! Validators + retry — the deterministic check layer for semantic operators.
//!
//! A *validator* answers one question about an operator's output: is it
//! acceptable? rvbbit lives inside Postgres, so there is no polyglot sandbox
//! to build — SQL (and any PL the user has installed) IS the validator
//! language. A validator is one of:
//!
//!   {"sql": "<boolean expression>"}   -- inline; $output / $inputs bound
//!   {"function": "schema.fn"}          -- a fn(output text, inputs jsonb) -> bool
//!   "fn_name"                          -- shorthand for {"function": ...}
//!
//! A *retry plan* loops an operator: run -> validate -> if invalid, re-run
//! with feedback, up to max_attempts. Validation + the loop run on the
//! leader backend (SPI); the underlying model I/O still batches normally.

use serde_json::Value;

use crate::unit_of_work::{self, OpDef, WorkResult};

/// A reference to a validator, resolved at evaluation time.
pub enum ValidatorRef {
    /// Inline SQL boolean expression. `$output` (text) and `$inputs`
    /// (jsonb) are substituted with the operator output and inputs.
    Sql(String),
    /// A Postgres function `fn(output text, inputs jsonb) RETURNS bool`,
    /// in any PL the user has installed.
    Function(String),
}

impl ValidatorRef {
    /// Parse a validator from jsonb. Returns None when the shape is not a
    /// recognized validator (caller then treats it as "no validator").
    pub fn parse(v: &Value) -> Option<ValidatorRef> {
        match v {
            Value::String(s) if !s.trim().is_empty() => {
                Some(ValidatorRef::Function(s.trim().to_string()))
            }
            Value::Object(o) => {
                if let Some(s) = o.get("sql").and_then(|x| x.as_str()) {
                    Some(ValidatorRef::Sql(s.to_string()))
                } else if let Some(f) = o.get("function").and_then(|x| x.as_str()) {
                    Some(ValidatorRef::Function(f.trim().to_string()))
                } else {
                    None
                }
            }
            _ => None,
        }
    }
}

/// Evaluate a validator. LEADER / backend context only — uses SPI.
///
/// Ok(true) = output passes, Ok(false) = output fails the check,
/// Err = the validator itself is broken (bad SQL, missing function).
/// Callers treat Err as "pass" so a typo never silently eats every result.
pub fn evaluate(v: &ValidatorRef, output: &str, inputs: &Value) -> Result<bool, String> {
    let out_lit = sql_lit(output);
    let inputs_json = serde_json::to_string(inputs).unwrap_or_else(|_| "{}".to_string());
    let inputs_lit = format!("({}::jsonb)", sql_lit(&inputs_json));

    let sql = match v {
        ValidatorRef::Sql(expr) => {
            // $inputs first — otherwise the $output replace would also hit
            // the "output" inside a hypothetical "$inputs->>'output'".
            let bound = expr
                .replace("$inputs", &inputs_lit)
                .replace("$output", &out_lit);
            format!("SELECT ({bound})::boolean")
        }
        ValidatorRef::Function(f) => {
            format!(
                "SELECT {}({}, {})::boolean",
                qualify_func(f),
                out_lit,
                inputs_lit
            )
        }
    };

    match pgrx::Spi::get_one::<bool>(&sql) {
        Ok(Some(b)) => Ok(b),
        // NULL result — treat as pass; a validator that returns NULL has
        // not actually rejected the output.
        Ok(None) => Ok(true),
        Err(e) => Err(e.to_string()),
    }
}

/// A parsed retry plan from `rvbbit.operators.retry`.
pub struct RetryPlan {
    pub until: ValidatorRef,
    pub max_attempts: u32,
    /// Feedback template appended to the prompt on each retry. Has access
    /// to `{{ output }}` (the rejected output), `{{ attempt }}`, and the
    /// operator's `{{ inputs.* }}`.
    pub instructions: Option<String>,
}

/// Parse the operator-level `retry` jsonb. None when absent or malformed.
pub fn parse_retry(v: &Value) -> Option<RetryPlan> {
    let o = v.as_object()?;
    let until = ValidatorRef::parse(o.get("until")?)?;
    let max_attempts = o
        .get("max_attempts")
        .and_then(|x| x.as_u64())
        .unwrap_or(3)
        .clamp(1, 10) as u32;
    let instructions = o
        .get("instructions")
        .and_then(|x| x.as_str())
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.to_string());
    Some(RetryPlan {
        until,
        max_attempts,
        instructions,
    })
}

/// Validate an operator result and, if it fails and the operator carries a
/// retry plan, re-run with feedback up to max_attempts. LEADER ONLY.
///
/// `first` is the already-computed first attempt — batched (warm path) or
/// single-row. The first valid result is returned; if every attempt fails
/// the last one is returned (the parser still turns it into a typed value).
/// The receipt audit (`sub_calls`, token + latency totals) accumulates
/// across every attempt.
pub fn apply_retry(op: &OpDef, inputs: &Value, opts: &Value, first: WorkResult) -> WorkResult {
    let plan = match op.retry.as_ref().and_then(parse_retry) {
        Some(p) => p,
        None => return first,
    };

    let mut result = first;
    let mut attempt: u32 = 1;
    loop {
        // A transport-level failure is not a validation failure — surface
        // it rather than burning retries on a dead provider.
        if result.error.is_some() {
            return result;
        }
        let valid = match evaluate(&plan.until, &result.output, inputs) {
            Ok(v) => v,
            Err(e) => {
                pgrx::warning!(
                    "rvbbit: retry validator for operator '{}' is broken ({}); \
                     treating output as valid",
                    op.name,
                    e
                );
                true
            }
        };
        if valid || attempt >= plan.max_attempts {
            return result;
        }

        attempt += 1;
        let feedback = plan
            .instructions
            .as_ref()
            .map(|tmpl| render_feedback(tmpl, inputs, &result.output, attempt));
        let prior = result;
        // execute_attempt re-runs the takes ensemble when the operator has
        // one, so retry composes with takes.
        let mut next = crate::takes::execute_attempt(op, inputs, opts, feedback.as_deref());
        // Carry the audit trail of every attempt into the final receipt.
        let mut calls = prior.sub_calls;
        calls.append(&mut next.sub_calls);
        next.sub_calls = calls;
        next.total_tokens_in += prior.total_tokens_in;
        next.total_tokens_out += prior.total_tokens_out;
        next.total_latency_ms += prior.total_latency_ms;
        result = next;
    }
}

/// Render the retry feedback template. `{{ output }}` and `{{ attempt }}`
/// are injected alongside the operator's inputs.
fn render_feedback(tmpl: &str, inputs: &Value, last_output: &str, attempt: u32) -> String {
    let mut aug = inputs.clone();
    if let Value::Object(m) = &mut aug {
        m.insert("output".to_string(), Value::String(last_output.to_string()));
        m.insert("attempt".to_string(), Value::Number(attempt.into()));
    }
    let scope = unit_of_work::Scope::new(aug, Value::Object(Default::default()));
    scope.render(tmpl)
}

// ---- Wards — pre/post validator gates ------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum WardMode {
    /// A failed ward fails the operator call.
    Blocking,
    /// A failed ward logs a warning; the call proceeds.
    Advisory,
}

struct Ward {
    validator: ValidatorRef,
    mode: WardMode,
}

fn parse_ward(v: &Value) -> Option<Ward> {
    let o = v.as_object()?;
    let validator = ValidatorRef::parse(o.get("validator")?)?;
    let mode = match o.get("mode").and_then(|m| m.as_str()).unwrap_or("blocking") {
        "advisory" => WardMode::Advisory,
        _ => WardMode::Blocking,
    };
    Some(Ward { validator, mode })
}

fn ward_list(wards: &Value, slot: &str) -> Vec<Ward> {
    wards
        .get(slot)
        .and_then(|x| x.as_array())
        .map(|arr| arr.iter().filter_map(parse_ward).collect())
        .unwrap_or_default()
}

/// Run the operator's pre-wards against its inputs. Err(reason) when a
/// blocking pre-ward rejects the input; advisory failures warn and pass.
/// LEADER ONLY (SPI). A pre-ward validator sees $inputs — $output is empty.
pub fn check_pre_wards(op: &OpDef, inputs: &Value) -> Result<(), String> {
    let wards = match op.wards.as_ref() {
        Some(w) => w,
        None => return Ok(()),
    };
    for ward in ward_list(wards, "pre") {
        let ok = match evaluate(&ward.validator, "", inputs) {
            Ok(v) => v,
            Err(e) => {
                pgrx::warning!(
                    "rvbbit: pre-ward validator for '{}' is broken ({e}); passing",
                    op.name
                );
                true
            }
        };
        if !ok {
            match ward.mode {
                WardMode::Blocking => {
                    return Err("input rejected by a blocking pre-ward".to_string());
                }
                WardMode::Advisory => {
                    pgrx::warning!(
                        "rvbbit: pre-ward advisory for '{}': input failed the check",
                        op.name
                    );
                }
            }
        }
    }
    Ok(())
}

/// Run the operator's post-wards against its final output. A blocking
/// failure sets result.error (rejecting the output); advisory warns and
/// keeps the output. LEADER ONLY (SPI).
pub fn apply_post_wards(op: &OpDef, inputs: &Value, mut result: WorkResult) -> WorkResult {
    let wards = match op.wards.as_ref() {
        Some(w) => w,
        None => return result,
    };
    // Don't pile a ward error onto an already-failed result.
    if result.error.is_some() {
        return result;
    }
    for ward in ward_list(wards, "post") {
        let ok = match evaluate(&ward.validator, &result.output, inputs) {
            Ok(v) => v,
            Err(e) => {
                pgrx::warning!(
                    "rvbbit: post-ward validator for '{}' is broken ({e}); passing",
                    op.name
                );
                true
            }
        };
        if !ok {
            match ward.mode {
                WardMode::Blocking => {
                    result.error = Some("output rejected by a blocking post-ward".to_string());
                    return result;
                }
                WardMode::Advisory => {
                    pgrx::warning!(
                        "rvbbit: post-ward advisory for '{}': output failed the check",
                        op.name
                    );
                }
            }
        }
    }
    result
}

/// Build a failed WorkResult carrying `msg` — used when a pre-ward blocks
/// the call before the operator ever runs.
pub fn errored(msg: String) -> WorkResult {
    WorkResult {
        output: String::new(),
        sub_calls: Vec::new(),
        total_tokens_in: 0,
        total_tokens_out: 0,
        total_latency_ms: 0,
        error: Some(msg),
    }
}

fn sql_lit(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// Quote a possibly schema-qualified function name component-by-component.
fn qualify_func(name: &str) -> String {
    name.split('.')
        .map(|part| format!("\"{}\"", part.replace('"', "\"\"")))
        .collect::<Vec<_>>()
        .join(".")
}
