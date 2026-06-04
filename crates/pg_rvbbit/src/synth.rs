//! synth.rs — shape-keyed SQL synthesis for rowset (pipeline) operators.
//!
//! The high-leverage idea from larsql, generalized: the model is a compiler from
//! (natural-language intent + the rowset's *structural shape*) to a SQL statement,
//! invoked once per distinct shape; the compiled SQL is cached and executed
//! natively. The cache is keyed on STRUCTURE, not content — so 50M rows of ~50
//! shapes cost ~50 model calls, then deterministic in-engine SQL.
//!
//! A synth operator (shape='rowset', parser='sql') sees only the schema + the
//! distinct values of low-cardinality text columns (never the data), and returns
//! `{"sql": "<one SELECT over _input>"}`. We register the rowset as `_input`
//! (jsonb_to_recordset) and run the SQL, isolated by a subtransaction so a bad
//! generation fails the stage without poisoning the surrounding query.
//!
//! See docs/PIPELINE_CASCADES_PLAN.md.

use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use crate::unit_of_work::OpDef;

const LOW_CARD_MAX: usize = 30;

fn esc(s: &str) -> String {
    s.replace('\'', "''")
}

fn quote_ident(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

fn hash_hex(s: &str) -> String {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    s.hash(&mut h);
    format!("{:016x}", h.finish())
}

/// Infer the (column, pg-type) schema of a jsonb rowset. Column order comes from
/// the first object row; the type is the widest seen (mixed → text).
fn infer_schema(rows: &[Value]) -> Vec<(String, String)> {
    let mut cols: Vec<String> = Vec::new();
    for r in rows {
        if let Some(o) = r.as_object() {
            cols = o.keys().cloned().collect();
            break;
        }
    }
    cols.into_iter()
        .map(|c| {
            let mut seen_num = false;
            let mut seen_bool = false;
            let mut seen_str = false;
            for r in rows {
                match r.get(&c) {
                    Some(Value::Number(_)) => seen_num = true,
                    Some(Value::Bool(_)) => seen_bool = true,
                    Some(Value::Null) | None => {}
                    Some(_) => seen_str = true,
                }
            }
            let ty = if seen_str {
                "text"
            } else if seen_bool && !seen_num {
                "boolean"
            } else if seen_num && !seen_bool {
                "numeric"
            } else {
                "text"
            };
            (c, ty.to_string())
        })
        .collect()
}

/// Distinct values of low-cardinality text columns (sorted), for the prompt and
/// the shape key. Columns with > LOW_CARD_MAX distinct values are omitted.
fn distinct_profile(
    rows: &[Value],
    schema: &[(String, String)],
) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    for (col, ty) in schema {
        if ty != "text" {
            continue;
        }
        let mut set = std::collections::BTreeSet::new();
        let mut overflow = false;
        for r in rows {
            if let Some(Value::String(s)) = r.get(col) {
                set.insert(s.clone());
                if set.len() > LOW_CARD_MAX {
                    overflow = true;
                    break;
                }
            }
        }
        if !overflow && !set.is_empty() {
            out.insert(col.clone(), json!(set.into_iter().collect::<Vec<_>>()));
        }
    }
    out
}

/// Deterministic structural fingerprint: schema (cols+types) + the sorted
/// distinct-value sets of low-cardinality text columns.
fn fingerprint(schema: &[(String, String)], distinct: &serde_json::Map<String, Value>) -> String {
    let canon = json!({
        "schema": schema.iter().map(|(c, t)| json!([c, t])).collect::<Vec<_>>(),
        "distinct": distinct,
    });
    hash_hex(&canon.to_string())
}

fn prompt_key(operator: &str, prompt: &str) -> String {
    hash_hex(&format!("{}\u{0}{}", operator, prompt.trim()))
}

fn coldefs(schema: &[(String, String)]) -> String {
    schema
        .iter()
        .map(|(c, t)| format!("{} {}", quote_ident(c), t))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Run a generated SELECT over the rowset registered as `_input`, isolated so a
/// bad generation returns Err instead of aborting the surrounding statement.
fn execute_over_input(
    rows: &[Value],
    schema: &[(String, String)],
    generated_sql: &str,
) -> Result<Vec<Value>, String> {
    let rows_json = Value::Array(rows.to_vec()).to_string();
    let sql = format!(
        "WITH _input AS (SELECT * FROM jsonb_to_recordset('{}'::jsonb) AS _t({})) \
         SELECT to_jsonb(q) FROM ({}) q",
        esc(&rows_json),
        coldefs(schema),
        generated_sql
    );
    pgrx::PgTryBuilder::new(move || -> Result<Vec<Value>, String> {
        let mut out = Vec::new();
        let r: Result<(), pgrx::spi::Error> = Spi::connect(|client| {
            let table = client.select(&sql, None, &[])?;
            for row in table {
                if let Some(j) = row.get::<JsonB>(1)? {
                    out.push(j.0);
                }
            }
            Ok(())
        });
        r.map_err(|e| format!("{e:?}"))?;
        Ok(out)
    })
    .catch_others(|caught| Err(format!("{caught:?}")))
    .execute()
}

fn synth_cache_get(operator: &str, shape_fp: &str, prompt_hash: &str) -> Option<String> {
    let sql = format!(
        "SELECT generated_sql FROM rvbbit.synth_cache \
         WHERE operator = '{}' AND shape_fingerprint = '{}' AND prompt_hash = '{}' \
           AND status = 'valid'",
        esc(operator),
        esc(shape_fp),
        esc(prompt_hash)
    );
    Spi::get_one::<String>(&sql).ok().flatten()
}

fn synth_cache_put(
    operator: &str,
    shape_fp: &str,
    prompt_hash: &str,
    generated_sql: &str,
    sample: &Value,
    pinned: bool,
) {
    let sql = format!(
        "INSERT INTO rvbbit.synth_cache \
           (operator, shape_fingerprint, prompt_hash, generated_sql, status, sample, pinned, updated_at) \
         VALUES ('{}', '{}', '{}', '{}', 'valid', '{}'::jsonb, {}, clock_timestamp()) \
         ON CONFLICT (operator, shape_fingerprint, prompt_hash) DO UPDATE SET \
           generated_sql = CASE WHEN rvbbit.synth_cache.pinned THEN rvbbit.synth_cache.generated_sql \
                                ELSE EXCLUDED.generated_sql END, \
           status = 'valid', sample = EXCLUDED.sample, \
           pinned = rvbbit.synth_cache.pinned OR EXCLUDED.pinned, \
           updated_at = clock_timestamp()",
        esc(operator),
        esc(shape_fp),
        esc(prompt_hash),
        esc(generated_sql),
        esc(&sample.to_string()),
        pinned
    );
    let _ = Spi::run(&sql);
}

/// Extract a SQL statement from the model output: `{"sql": "..."}`, or a bare
/// statement (markdown fences stripped).
fn extract_sql(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if let Ok(Value::Object(o)) = serde_json::from_str::<Value>(trimmed) {
        if let Some(Value::String(s)) = o.get("sql") {
            return Some(s.trim().trim_end_matches(';').to_string());
        }
    }
    let mut s = trimmed;
    if s.starts_with("```") {
        s = s.trim_start_matches("```sql").trim_start_matches("```").trim();
        if let Some(idx) = s.rfind("```") {
            s = s[..idx].trim();
        }
    }
    let up = s.trim_start().to_ascii_uppercase();
    if up.starts_with("SELECT") || up.starts_with("WITH") {
        Some(s.trim().trim_end_matches(';').to_string())
    } else {
        None
    }
}

const MAX_SYNTH_ATTEMPTS: usize = 3;
const SYNTH_SAMPLE_ROWS: usize = 200;

/// The synth-sql strategy for a rowset operator: shape-fingerprint the rowset,
/// reuse cached SQL if present, otherwise synthesize via the model — validating
/// each attempt on a sample and feeding the Postgres error back for up to
/// MAX_SYNTH_ATTEMPTS — then run on the full rowset and cache. Returns the new
/// rowset plus the SQL that produced it (for the step inspector).
pub(crate) fn run_synth_sql_op(
    op: &OpDef,
    prompt: &str,
    rows: &[Value],
    opts: &Value,
) -> Result<(Vec<Value>, Option<String>), String> {
    let schema = infer_schema(rows);
    if schema.is_empty() {
        return Err("synth: rowset has no columns".into());
    }
    let distinct = distinct_profile(rows, &schema);
    let shape_fp = fingerprint(&schema, &distinct);
    let p_hash = prompt_key(&op.name, prompt);

    // Cache hit: execute the stored SQL directly (no model call).
    if let Some(sql) = synth_cache_get(&op.name, &shape_fp, &p_hash) {
        let out = execute_over_input(rows, &schema, &sql)?;
        return Ok((out, Some(sql)));
    }

    // Cache miss: synthesize. The model sees only schema + distinct values.
    let profile_cols: Vec<Value> = schema
        .iter()
        .map(|(c, t)| json!({ "column": c, "type": t }))
        .collect();
    let sample: Vec<Value> = rows.iter().take(SYNTH_SAMPLE_ROWS).cloned().collect();
    let mut last_err = String::new();

    for attempt in 0..MAX_SYNTH_ATTEMPTS {
        let mut inputs = serde_json::Map::new();
        inputs.insert("prompt".into(), json!(prompt));
        inputs.insert("_table_schema".into(), json!(profile_cols));
        inputs.insert("_table_distinct".into(), Value::Object(distinct.clone()));
        inputs.insert("_table_row_count".into(), json!(rows.len()));
        inputs.insert(
            "_last_sql_error".into(),
            json!(if attempt == 0 { String::new() } else { last_err.clone() }),
        );
        let inputs_v = Value::Object(inputs);

        let raw = crate::operators::invoke_with_cache(op, &inputs_v, opts)
            .map_err(|_| format!("synth: operator '{}' invocation failed", op.name))?;
        let gen_sql = match extract_sql(&raw) {
            Some(s) => s,
            None => {
                last_err = format!("the model did not return SQL (got: {raw})");
                continue;
            }
        };
        // Validate on a sample (cheap); on success, run on the full rowset.
        match execute_over_input(&sample, &schema, &gen_sql) {
            Ok(_) => {
                let out = execute_over_input(rows, &schema, &gen_sql)?;
                let meta = json!({
                    "schema": schema.iter().map(|(c, t)| json!([c, t])).collect::<Vec<_>>(),
                    "attempts": attempt + 1,
                });
                synth_cache_put(&op.name, &shape_fp, &p_hash, &gen_sql, &meta, false);
                return Ok((out, Some(gen_sql)));
            }
            Err(e) => {
                last_err = e;
                continue;
            }
        }
    }
    Err(format!(
        "synth: no valid SQL after {MAX_SYNTH_ATTEMPTS} attempts; last error: {last_err}"
    ))
}

// ---------------------------------------------------------------------------
// SQL-facing helpers (inspection + manual snippet authoring/pinning)
// ---------------------------------------------------------------------------

/// The structural shape fingerprint of a rowset — the synth_cache key component.
#[pg_extern(stable, parallel_safe)]
fn flow_shape(rows: JsonB) -> String {
    let r = rows.0.as_array().cloned().unwrap_or_default();
    let schema = infer_schema(&r);
    let distinct = distinct_profile(&r, &schema);
    fingerprint(&schema, &distinct)
}

/// Pin a SQL snippet for the shape of `sample_rows` + `prompt` under `operator`.
/// Lets you author / freeze the generated SQL by hand (and is how a reviewer
/// locks the ~K snippets). Returns the shape fingerprint it was stored under.
#[pg_extern(volatile)]
fn synth_put(operator: &str, prompt: &str, sample_rows: JsonB, generated_sql: &str) -> String {
    let r = sample_rows.0.as_array().cloned().unwrap_or_default();
    let schema = infer_schema(&r);
    let distinct = distinct_profile(&r, &schema);
    let shape_fp = fingerprint(&schema, &distinct);
    let p_hash = prompt_key(operator, prompt);
    synth_cache_put(operator, &shape_fp, &p_hash, generated_sql, &json!({}), true);
    shape_fp
}

// ---------------------------------------------------------------------------
// Scalar synth-sql: shape-keyed value reshaping (Phase 5).
//
// A scalar operator (shape='scalar', parser='sql') maps a text value to its
// STRUCTURAL shape (each digit -> 'd', letter -> 'a', other chars kept), and the
// model authors ONE PostgreSQL expression over `x` per distinct shape — cached
// and reused. So 50M values of ~50 formats cost ~50 model calls, then native SQL.
// ---------------------------------------------------------------------------

const SCALAR_SHAPE_MAX: usize = 80;
const MAX_SCALAR_ATTEMPTS: usize = 3;

/// Structural shape of a scalar value: ASCII digit -> 'd', ASCII letter -> 'a',
/// other characters kept verbatim. Length-preserving (capped), so values with the
/// same format share a shape (e.g. every '(ddd) ddd-dddd' phone number).
fn scalar_shape(value: &str) -> String {
    let mut s = String::with_capacity(value.len().min(SCALAR_SHAPE_MAX) + 4);
    for (i, c) in value.chars().enumerate() {
        if i >= SCALAR_SHAPE_MAX {
            s.push('~');
            break;
        }
        if c.is_ascii_digit() {
            s.push('d');
        } else if c.is_ascii_alphabetic() {
            s.push('a');
        } else {
            s.push(c);
        }
    }
    s
}

/// A canonical example value for a shape ('d' -> '0', 'a' -> 'x', others kept), so
/// the model's input — and thus the cache key — is identical for every value of a
/// shape.
fn canonical_example(shape: &str) -> String {
    shape
        .chars()
        .map(|c| match c {
            'd' => '0',
            'a' => 'x',
            other => other,
        })
        .collect()
}

/// In-memory L1: (operator, shape, prompt) -> validated expression. Skips the
/// per-row synth_cache SPI lookup; cleared by rvbbit.flush_cache().
fn scalar_cache() -> &'static Mutex<HashMap<String, String>> {
    static C: OnceLock<Mutex<HashMap<String, String>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

pub(crate) fn clear_scalar_cache() {
    if let Ok(mut m) = scalar_cache().lock() {
        m.clear();
    }
}

fn l1_key(op: &str, shape: &str, p_hash: &str) -> String {
    format!("{op}\u{0}{shape}\u{0}{p_hash}")
}

/// Extract a scalar expression (over `x`) from model output: {"sql"/"expr": "..."}
/// or a bare single-line expression (markdown fences stripped).
fn extract_scalar_expr(raw: &str) -> Option<String> {
    let t = raw.trim();
    if let Ok(Value::Object(o)) = serde_json::from_str::<Value>(t) {
        for k in ["sql", "expr", "expression"] {
            if let Some(Value::String(s)) = o.get(k) {
                return Some(s.trim().trim_end_matches(';').trim().to_string());
            }
        }
    }
    let mut s = t;
    if s.starts_with("```") {
        s = s.trim_start_matches("```sql").trim_start_matches("```").trim();
        if let Some(i) = s.rfind("```") {
            s = s[..i].trim();
        }
    }
    if s.is_empty() || s.contains('\n') || s.len() > 400 {
        return None;
    }
    Some(s.trim_end_matches(';').trim().to_string())
}

/// Apply a generated expression (over text input `x`) to one value, isolated by a
/// subtransaction so a bad expression can't poison the surrounding query.
fn apply_scalar(expr: &str, value: &str) -> Result<String, String> {
    let sql = format!(
        "SELECT ({})::text FROM (SELECT '{}'::text AS x) _v",
        expr,
        value.replace('\'', "''")
    );
    pgrx::PgTryBuilder::new(move || -> Result<String, String> {
        Spi::get_one::<String>(&sql)
            .map(|o| o.unwrap_or_default())
            .map_err(|e| format!("{e:?}"))
    })
    .catch_others(|caught| Err(format!("{caught:?}")))
    .execute()
}

/// Scalar synth-sql dispatch (from `_exec_op_text` when parser='sql', shape scalar).
/// `inputs` is the operator's arg map: `value` (the text to transform) + `intent`
/// (the request). On any failure the original value passes through.
pub(crate) fn run_synth_sql_scalar(op: &OpDef, inputs: &Value, opts: &Value) -> String {
    let value = inputs.get("value").and_then(|v| v.as_str()).unwrap_or("");
    let intent = inputs.get("intent").and_then(|v| v.as_str()).unwrap_or("");
    if value.is_empty() {
        return String::new();
    }
    let shape = scalar_shape(value);
    let p_hash = prompt_key(&op.name, intent);
    let key = l1_key(&op.name, &shape, &p_hash);
    let fallback = || value.to_string();

    // L1 (in-memory) then L2 (synth_cache).
    if let Some(expr) = scalar_cache().lock().ok().and_then(|m| m.get(&key).cloned()) {
        return apply_scalar(&expr, value).unwrap_or_else(|_| fallback());
    }
    if let Some(expr) = synth_cache_get(&op.name, &shape, &p_hash) {
        if let Ok(mut m) = scalar_cache().lock() {
            m.insert(key, expr.clone());
        }
        return apply_scalar(&expr, value).unwrap_or_else(|_| fallback());
    }

    // Miss: author an expression for this shape (validated, with error-feedback retry).
    let example = canonical_example(&shape);
    let mut last_err = String::new();
    for attempt in 0..MAX_SCALAR_ATTEMPTS {
        let mut llm = serde_json::Map::new();
        llm.insert("intent".into(), json!(intent));
        llm.insert("shape".into(), json!(shape));
        llm.insert("example".into(), json!(example));
        llm.insert(
            "_last_sql_error".into(),
            json!(if attempt == 0 { String::new() } else { last_err.clone() }),
        );
        let raw = match crate::operators::invoke_with_cache(op, &Value::Object(llm), opts) {
            Ok(r) => r,
            Err(_) => return fallback(),
        };
        let expr = match extract_scalar_expr(&raw) {
            Some(e) => e,
            None => {
                last_err = "the model did not return an expression".to_string();
                continue;
            }
        };
        match (apply_scalar(&expr, &example), apply_scalar(&expr, value)) {
            (Ok(_), Ok(out)) => {
                synth_cache_put(&op.name, &shape, &p_hash, &expr, &json!({ "kind": "scalar", "attempts": attempt + 1 }), false);
                if let Ok(mut m) = scalar_cache().lock() {
                    m.insert(l1_key(&op.name, &shape, &p_hash), expr);
                }
                return out;
            }
            (Err(e), _) | (_, Err(e)) => last_err = e,
        }
    }
    pgrx::warning!(
        "rvbbit.{}: no valid expression for shape '{}' ({}); value passed through",
        op.name,
        shape,
        last_err
    );
    fallback()
}

/// The structural shape of a scalar value (each digit -> d, letter -> a) — the
/// key the scalar synth cache groups by.
#[pg_extern(stable, parallel_safe)]
fn value_shape(value: &str) -> String {
    scalar_shape(value)
}

/// Pin a scalar expression (over `x`) for the shape of `example_value` + `intent`
/// under `operator`. Author/freeze a snippet by hand; returns the shape it was
/// stored under.
#[pg_extern(volatile)]
fn synth_put_scalar(operator: &str, intent: &str, example_value: &str, expr: &str) -> String {
    let shape = scalar_shape(example_value);
    let p_hash = prompt_key(operator, intent);
    synth_cache_put(operator, &shape, &p_hash, expr, &json!({ "kind": "scalar" }), true);
    clear_scalar_cache();
    shape
}
