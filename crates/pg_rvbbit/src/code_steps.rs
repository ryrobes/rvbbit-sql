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

use serde_json::{json, Value};

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
        m.insert("ui_metric_card".into(), ui_metric_card_fn);
        m.insert("ui_bar_chart".into(), ui_bar_chart_fn);
        m.insert("ui_table_view".into(), ui_table_view_fn);
        m.insert("ui_vega_lite".into(), ui_vega_lite_fn);
        m.insert("ui_filter_control".into(), ui_filter_control_fn);
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

fn opt_str_input(inputs: &Value, key: &str) -> String {
    inputs
        .get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string()
}

fn rows_input(inputs: &Value) -> Vec<Value> {
    inputs
        .get("rows")
        .or_else(|| inputs.get("_table"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default()
}

fn first_row_value(rows: &[Value], field: &str) -> Value {
    rows.first()
        .and_then(|r| r.as_object())
        .and_then(|o| o.get(field))
        .cloned()
        .unwrap_or(Value::Null)
}

fn first_row_string(rows: &[Value], field: &str) -> String {
    match first_row_value(rows, field) {
        Value::String(s) => s,
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn field_vega_type(rows: &[Value], field: &str) -> &'static str {
    for row in rows {
        let Some(value) = row.as_object().and_then(|o| o.get(field)) else {
            continue;
        };
        match value {
            Value::Number(_) => return "quantitative",
            Value::Bool(_) => return "nominal",
            Value::String(s) => {
                if s.len() >= 10 {
                    let dateish =
                        s.as_bytes().get(4) == Some(&b'-') && s.as_bytes().get(7) == Some(&b'-');
                    if dateish {
                        return "temporal";
                    }
                }
                return "nominal";
            }
            _ => {}
        }
    }
    "nominal"
}

fn artifact_id(renderer: &str, title: &str) -> String {
    let mut slug = title
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect::<String>();
    while slug.contains("__") {
        slug = slug.replace("__", "_");
    }
    slug = slug.trim_matches('_').to_string();
    if slug.is_empty() {
        renderer.to_string()
    } else {
        format!("{renderer}_{slug}")
    }
}

fn ui_artifact(renderer: &str, title: &str, spec: Value, data: Vec<Value>) -> Value {
    ui_artifact_kind(
        renderer,
        title,
        "visual",
        spec,
        data,
        Value::Object(Default::default()),
    )
}

fn ui_artifact_kind(
    renderer: &str,
    title: &str,
    artifact_kind: &str,
    spec: Value,
    data: Vec<Value>,
    bindings: Value,
) -> Value {
    Value::Array(vec![json!({
        "rvbbit_artifact": "ui",
        "artifact_id": artifact_id(renderer, title),
        "artifact_kind": artifact_kind,
        "renderer": renderer,
        "title": title,
        "spec": spec,
        "data": data,
        "layout": {},
        "bindings": bindings,
        "diagnostics": {}
    })])
}

fn normalize_control_kind(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "" | "select" | "single" | "single_select" | "single-select" | "dropdown" => {
            "dropdown".to_string()
        }
        "multi" | "multi_select" | "multi-select" | "multiselect" => "multiselect".to_string(),
        "date" | "date_picker" | "date-picker" | "datepicker" => "datepicker".to_string(),
        "number" | "range" | "slider" => "slider".to_string(),
        other => other.to_string(),
    }
}

fn normalize_control_operator(raw: &str, kind: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "eq" | "in" | "gte" | "lte" => raw.trim().to_ascii_lowercase(),
        _ if kind == "datepicker" || kind == "slider" => "gte".to_string(),
        _ => "in".to_string(),
    }
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

fn ui_metric_card_fn(inputs: &Value) -> Result<Value, String> {
    let rows = rows_input(inputs);
    let label_field = opt_str_input(inputs, "label");
    let value_field = opt_str_input(inputs, "value");
    if value_field.is_empty() {
        return Err("ui_metric_card: missing value field".into());
    }
    let mut title = opt_str_input(inputs, "title");
    let label = if label_field.is_empty() {
        title.clone()
    } else {
        first_row_string(&rows, &label_field)
    };
    if title.is_empty() {
        title = if !label.is_empty() {
            label.clone()
        } else {
            value_field.clone()
        };
    }
    let value = first_row_value(&rows, &value_field);
    let spec = json!({
        "label_field": label_field,
        "value_field": value_field,
        "label": label,
        "value": value,
        "row_count": rows.len()
    });
    Ok(ui_artifact("metric_card", &title, spec, rows))
}

fn ui_bar_chart_fn(inputs: &Value) -> Result<Value, String> {
    let rows = rows_input(inputs);
    let x = opt_str_input(inputs, "x");
    let y = opt_str_input(inputs, "y");
    if x.is_empty() || y.is_empty() {
        return Err("ui_bar_chart: missing x or y field".into());
    }
    let title = {
        let t = opt_str_input(inputs, "title");
        if t.is_empty() {
            format!("{y} by {x}")
        } else {
            t
        }
    };
    let spec = json!({
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": { "values": rows.clone() },
        "mark": { "type": "bar", "tooltip": true },
        "encoding": {
            "x": { "field": x, "type": field_vega_type(&rows, &x), "sort": "-y" },
            "y": { "field": y, "type": field_vega_type(&rows, &y) },
            "tooltip": [
                { "field": x, "type": field_vega_type(&rows, &x) },
                { "field": y, "type": field_vega_type(&rows, &y) }
            ]
        },
        "width": "container",
        "height": "container",
        "autosize": { "type": "fit", "contains": "padding", "resize": true }
    });
    Ok(ui_artifact("vega_lite", &title, spec, rows))
}

fn ui_table_view_fn(inputs: &Value) -> Result<Value, String> {
    let rows = rows_input(inputs);
    let title = {
        let t = opt_str_input(inputs, "title");
        if t.is_empty() {
            "Table".to_string()
        } else {
            t
        }
    };
    let columns = rows
        .iter()
        .find_map(|r| r.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()))
        .unwrap_or_default();
    let spec = json!({
        "columns": columns,
        "row_count": rows.len()
    });
    Ok(ui_artifact("table_view", &title, spec, rows))
}

fn ui_vega_lite_fn(inputs: &Value) -> Result<Value, String> {
    let rows = rows_input(inputs);
    let raw = inputs.get("spec").cloned().unwrap_or(Value::Null);
    let mut spec = match raw {
        Value::String(s) => serde_json::from_str::<Value>(&s)
            .map_err(|e| format!("ui_vega_lite: invalid spec JSON: {e}"))?,
        Value::Object(_) => raw,
        Value::Null => return Err("ui_vega_lite: missing spec".into()),
        other => {
            return Err(format!(
                "ui_vega_lite: spec must be object or JSON string, got {other}"
            ))
        }
    };
    if spec.get("data").is_none() {
        if let Some(obj) = spec.as_object_mut() {
            obj.insert("data".into(), json!({ "values": rows.clone() }));
        }
    }
    let title = {
        let t = opt_str_input(inputs, "title");
        if t.is_empty() {
            "Vega-Lite".to_string()
        } else {
            t
        }
    };
    Ok(ui_artifact("vega_lite", &title, spec, rows))
}

fn ui_filter_control_fn(inputs: &Value) -> Result<Value, String> {
    let rows = rows_input(inputs);
    let field = opt_str_input(inputs, "field");
    if field.is_empty() {
        return Err("ui_filter_control: missing field".into());
    }
    let kind = normalize_control_kind(&opt_str_input(inputs, "kind"));
    let operator = normalize_control_operator(&opt_str_input(inputs, "operator"), &kind);
    let title = {
        let t = opt_str_input(inputs, "title");
        if t.is_empty() {
            field.clone()
        } else {
            t
        }
    };
    let spec = json!({
        "field": field.clone(),
        "kind": kind,
        "operator": operator.clone(),
        "label": title,
        "row_count": rows.len()
    });
    Ok(ui_artifact_kind(
        "filter_control",
        &title,
        "control",
        spec,
        rows,
        json!({
            "param": {
                "field": field,
                "operator": operator
            }
        }),
    ))
}
