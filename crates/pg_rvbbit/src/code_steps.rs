//! Registry of Rust functions callable from operator step definitions.
//!
//! When an operator's `steps` array contains `{"kind": "code", "fn": "name", ...}`,
//! the executor looks up the function here and calls it with the rendered inputs.
//!
//! This is the deliberate escape hatch: when an operator author needs
//! deterministic pure-Rust logic between LLM calls (validation, JSON
//! shaping, math, regex extraction), they reference a registered fn name
//! instead of writing a new SQL/plpgsql shim. Future work: load user-defined
//! code functions via dynamic library OR a sandboxed scripting language.

use std::collections::HashMap;
use std::sync::OnceLock;

use serde_json::Value;

pub type CodeFn = fn(inputs: &Value) -> Result<Value, String>;

static REGISTRY: OnceLock<HashMap<String, CodeFn>> = OnceLock::new();

pub fn registry() -> &'static HashMap<String, CodeFn> {
    REGISTRY.get_or_init(|| {
        let mut m: HashMap<String, CodeFn> = HashMap::new();
        m.insert("trim".into(), trim_fn);
        m.insert("lowercase".into(), lowercase_fn);
        m.insert("uppercase".into(), uppercase_fn);
        m.insert("first_non_empty_line".into(), first_non_empty_line_fn);
        m.insert("extract_int".into(), extract_int_fn);
        m.insert("validate_one_of".into(), validate_one_of_fn);
        m.insert("char_count".into(), char_count_fn);
        m.insert("json_parse".into(), json_parse_fn);
        m.insert("json_get".into(), json_get_fn);
        m.insert("json_length".into(), json_length_fn);
        m.insert("json_length_gte".into(), json_length_gte_fn);
        m.insert("number_gte".into(), number_gte_fn);
        m.insert("string_eq".into(), string_eq_fn);
        m.insert("cosine_similarity".into(), cosine_similarity_fn);
        m
    })
}

pub fn invoke(name: &str, inputs: &Value) -> Result<Value, String> {
    let r = registry();
    match r.get(name) {
        Some(f) => f(inputs),
        None => Err(format!("rvbbit: unknown code fn '{name}'")),
    }
}

// ---- Built-ins -----------------------------------------------------------
// All take {key: value} JSON and return either a primitive Value or another
// object. Step output is whatever the fn returns.

fn str_input(inputs: &Value, key: &str) -> Result<String, String> {
    inputs
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| format!("missing or non-string input '{key}'"))
}

fn trim_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    Ok(Value::String(s.trim().to_string()))
}

fn lowercase_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    Ok(Value::String(s.to_lowercase()))
}

fn uppercase_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    Ok(Value::String(s.to_uppercase()))
}

fn first_non_empty_line_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    for line in s.lines() {
        let t = line.trim();
        if !t.is_empty() {
            return Ok(Value::String(t.to_string()));
        }
    }
    Ok(Value::String(String::new()))
}

/// Extracts the first integer found in the text. Useful for postprocessing
/// LLM responses that ramble ("The answer is 42 because…") into a number.
fn extract_int_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    let mut current = String::new();
    let mut found = String::new();
    let mut started = false;
    for c in s.chars() {
        if c == '-' && current.is_empty() && !started {
            current.push(c);
        } else if c.is_ascii_digit() {
            current.push(c);
            started = true;
        } else if started {
            found = current.clone();
            break;
        } else {
            current.clear();
        }
    }
    if found.is_empty() && started {
        found = current;
    }
    found
        .parse::<i64>()
        .map(|n| Value::Number(n.into()))
        .map_err(|e| format!("no integer found in '{s}': {e}"))
}

/// Validates that `value` is one of the allowed values; returns it on
/// match, otherwise returns the `default`. Critical primitive for making
/// LLM output safe to trust downstream.
///
///   inputs: {"value": "X",
///            "allowed": ["A","B","C"] | "A,B,C",
///            "default": "unknown"}
///
/// `allowed` accepts either a JSON array or a comma-separated string
/// (since SQL operator args are typically text). Matching is
/// case-insensitive and ignores surrounding whitespace on each candidate.
fn validate_one_of_fn(inputs: &Value) -> Result<Value, String> {
    let value = inputs
        .get("value")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let allowed: Vec<String> = match inputs.get("allowed") {
        Some(Value::Array(arr)) => arr
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.trim().to_string()))
            .collect(),
        Some(Value::String(s)) => s.split(',').map(|p| p.trim().to_string()).collect(),
        _ => Vec::new(),
    };
    let default = inputs
        .get("default")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();
    if allowed.iter().any(|a| a.eq_ignore_ascii_case(&value)) {
        // Return the canonical form from the allowed list (preserves
        // user-specified casing), not the LLM's literal output.
        let canon = allowed
            .iter()
            .find(|a| a.eq_ignore_ascii_case(&value))
            .cloned()
            .unwrap_or(value);
        Ok(Value::String(canon))
    } else {
        Ok(Value::String(default))
    }
}

fn char_count_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    Ok(Value::Number((s.chars().count() as i64).into()))
}

fn json_parse_fn(inputs: &Value) -> Result<Value, String> {
    let s = str_input(inputs, "text")?;
    serde_json::from_str(&s).map_err(|e| format!("invalid JSON: {e}"))
}

fn json_get_fn(inputs: &Value) -> Result<Value, String> {
    let mut cur = inputs.get("value").cloned().unwrap_or(Value::Null);
    let path = inputs
        .get("path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    if !path.is_empty() {
        for part in path.split('.').filter(|p| !p.is_empty()) {
            cur = if let Ok(idx) = part.parse::<usize>() {
                cur.get(idx).cloned().unwrap_or(Value::Null)
            } else {
                cur.get(part).cloned().unwrap_or(Value::Null)
            };
        }
    }
    if cur.is_null() {
        Ok(inputs.get("default").cloned().unwrap_or(Value::Null))
    } else {
        Ok(cur)
    }
}

fn json_length_value(value: &Value) -> usize {
    match value {
        Value::Array(values) => values.len(),
        Value::Object(values) => values.len(),
        Value::String(value) => value.chars().count(),
        Value::Null => 0,
        _ => 1,
    }
}

fn json_length_fn(inputs: &Value) -> Result<Value, String> {
    let len = json_length_value(inputs.get("value").unwrap_or(&Value::Null));
    Ok(Value::Number((len as i64).into()))
}

fn value_as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

fn json_length_gte_fn(inputs: &Value) -> Result<Value, String> {
    let len = json_length_value(inputs.get("value").unwrap_or(&Value::Null)) as f64;
    let threshold = inputs
        .get("threshold")
        .or_else(|| inputs.get("min"))
        .and_then(value_as_f64)
        .unwrap_or(1.0);
    Ok(Value::Bool(len >= threshold))
}

fn number_gte_fn(inputs: &Value) -> Result<Value, String> {
    let value = value_as_f64(inputs.get("value").unwrap_or(&Value::Null)).unwrap_or(0.0);
    let threshold = value_as_f64(inputs.get("threshold").unwrap_or(&Value::Null)).unwrap_or(0.5);
    Ok(Value::Bool(value >= threshold))
}

fn string_eq_fn(inputs: &Value) -> Result<Value, String> {
    let value = inputs
        .get("value")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let expected = inputs
        .get("expected")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let case_sensitive = inputs
        .get("case_sensitive")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let matched = if case_sensitive {
        value == expected
    } else {
        value.eq_ignore_ascii_case(expected)
    };
    Ok(Value::Bool(matched))
}

fn number_array(v: &Value, key: &str) -> Result<Vec<f64>, String> {
    let Some(arr) = v.get(key).and_then(|value| value.as_array()) else {
        return Err(format!("missing or non-array input '{key}'"));
    };
    arr.iter()
        .map(|value| value_as_f64(value).ok_or_else(|| format!("non-numeric value in '{key}'")))
        .collect()
}

fn cosine_similarity_fn(inputs: &Value) -> Result<Value, String> {
    let left = number_array(inputs, "left")?;
    let right = number_array(inputs, "right")?;
    if left.len() != right.len() {
        return Err(format!(
            "vector length mismatch: left={} right={}",
            left.len(),
            right.len()
        ));
    }
    if left.is_empty() {
        return Ok(Value::Number(serde_json::Number::from(0)));
    }
    let mut dot = 0.0;
    let mut left_norm = 0.0;
    let mut right_norm = 0.0;
    for (a, b) in left.iter().zip(right.iter()) {
        dot += a * b;
        left_norm += a * a;
        right_norm += b * b;
    }
    let denom = left_norm.sqrt() * right_norm.sqrt();
    let score = if denom > 0.0 {
        (dot / denom).clamp(-1.0, 1.0)
    } else {
        0.0
    };
    serde_json::Number::from_f64(score)
        .map(Value::Number)
        .ok_or_else(|| "cosine similarity produced a non-finite score".to_string())
}
