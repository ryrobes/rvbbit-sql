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

// ---------------------------------------------------------------------------
// Query synth-sql: table-shaped text-to-SQL (Phase 1 — generation only).
//
// A query operator (shape='query', parser='sql') turns a natural-language intent
// into ONE read-only SELECT over the *live database* — the same model-as-compiler
// machinery as the rowset/scalar synths, with the scope widened from `_input` to
// real tables. The schema is grounded by catalog retrieval (rvbbit.data_search),
// so the model sees the relevant tables/columns (types, example values, FKs) and
// nothing else.
//
// `rvbbit.synth_sql(intent)` returns the generated SQL. It is validated by
// rvbbit._synth_validate: PREPARE (parse + analyze only — resolves tables/columns/
// types, no planning so no const-fold/execution and no EXPLAIN-of-WITH quirks),
// then a rolled-back EXPLAIN of the prepared plan that rejects any ModifyTable node
// so only read-only SELECTs pass. Validated SQL is cached in rvbbit.synth_cache, so
// it is inspectable/pinnable in the Cache app like any other synth snippet.
// Executing the SQL is Phase 2 (`rvbbit.synth` + a parse-tree read-only wrapper
// that also closes the residual non-transactional const-fold window).
// ---------------------------------------------------------------------------

const MAX_QUERY_ATTEMPTS: usize = 3;
const DEFAULT_RETRIEVE_K: i32 = 16;
const FALLBACK_MAX_TABLES: i32 = 60;

/// One read-only statement only. The generated SQL is handed to EXPLAIN, and
/// `EXPLAIN SELECT 1; DROP TABLE x` would parse as TWO statements and run the
/// second — so a semicolon anywhere is rejected (extract_sql already stripped a
/// single trailing one). The shared safety net before any DB contact.
pub(crate) fn is_single_statement(sql: &str) -> bool {
    !sql.contains(';')
}

/// Retrieve the most relevant table/column fingerprint docs for an intent from the
/// crawled catalog. Delegated to the plpgsql helper rvbbit._synth_retrieve, whose
/// EXCEPTION block gives a real subtransaction (a data_search failure rolls back
/// cleanly instead of aborting the surrounding transaction) and whose aggregation
/// order is deterministic (node_id tiebreaker), so the same intent produces the
/// same context — and thus the same synth_cache key. Returns "" when the catalog is
/// absent/empty (caller falls back to information_schema).
fn retrieve_catalog_context(intent: &str, k: i32) -> String {
    let sql = format!("SELECT rvbbit._synth_retrieve('{}', {})", esc(intent), k.max(1));
    Spi::get_one::<String>(&sql).ok().flatten().unwrap_or_default()
}

/// Fallback schema context when the catalog has not been crawled: one compact line
/// per user table — `schema.table(col type, col type, …)` — from information_schema.
/// Less grounded than the catalog (no example values / FK hints), but makes synth
/// work out of the box. Capped to `max_tables`.
pub(crate) fn information_schema_context(max_tables: i32) -> String {
    let sql = format!(
        "SELECT string_agg(line, E'\\n') FROM ( \
           SELECT format('%I.%I(%s)', table_schema, table_name, \
                         string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position)) AS line \
             FROM information_schema.columns \
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'rvbbit') \
              AND table_schema NOT LIKE 'pg\\_%' \
            GROUP BY table_schema, table_name \
            ORDER BY table_schema, table_name \
            LIMIT {max_tables} \
         ) s",
    );
    pgrx::PgTryBuilder::new(move || Spi::get_one::<String>(&sql).ok().flatten().unwrap_or_default())
        .catch_others(|_| String::new())
        .execute()
}

/// Assemble the schema context for a prompt. Prefers the crawled catalog
/// (rvbbit.data_search, intent-relevant); falls back to information_schema.
/// Returns (context, grounded) where grounded=true means it came from the catalog.
fn build_schema_context(intent: &str, k: i32) -> (String, bool) {
    let cat = retrieve_catalog_context(intent, k);
    if !cat.trim().is_empty() {
        return (cat, true);
    }
    (information_schema_context(FALLBACK_MAX_TABLES), false)
}

/// Cheap read-only prefix gate. The authoritative read-only check is
/// validate_sql's ModifyTable plan inspection; this closes the extract_sql
/// JSON-branch gap (which does not enforce SELECT/WITH) before any DB contact.
fn looks_read_only(sql: &str) -> bool {
    let up = sql.trim_start().to_ascii_uppercase();
    up.starts_with("SELECT") || up.starts_with("WITH")
}

/// Validate a generated statement via rvbbit._synth_validate — PREPARE (parse +
/// analyze, so a bad column/table/type is caught without planning or executing)
/// followed by a rolled-back EXPLAIN of the prepared plan that rejects any
/// statement that writes (a ModifyTable node), so only read-only SELECTs pass.
/// Returns Ok(()) if valid, Err(reason) otherwise. Because the plpgsql helper
/// handles its own exceptions and returns normally, the surrounding transaction is
/// never left aborted (so the retry loop is safe). Callers MUST gate with
/// `is_single_statement` first. (Residual: non-transactional effects — e.g. nextval
/// — of a deliberately mislabeled IMMUTABLE function are not fully prevented until
/// Phase 2's parse-tree wrapper.)
pub(crate) fn validate_sql(sql: &str) -> Result<(), String> {
    let q = format!("SELECT rvbbit._synth_validate('{}')", esc(sql));
    match Spi::get_one::<String>(&q) {
        Ok(Some(err)) => Err(err),
        Ok(None) => Ok(()),
        Err(e) => Err(format!("{e:?}")),
    }
}

/// Generate a read-only SELECT for `intent`, grounded by retrieved schema, with the
/// same validate + error-feedback retry loop as the rowset synth (EXPLAIN instead
/// of execute). Validated SQL is cached under (operator, schema-fingerprint,
/// intent-hash). Does NOT execute the query.
pub(crate) fn run_synth_query_sql(op: &OpDef, intent: &str, opts: &Value) -> Result<String, String> {
    if intent.trim().is_empty() {
        return Err("synth: empty intent".into());
    }
    let k = (opts
        .get("k")
        .and_then(|v| v.as_i64())
        .unwrap_or(DEFAULT_RETRIEVE_K as i64) as i32)
        .max(1);
    let validate = opts.get("validate").and_then(|v| v.as_bool()).unwrap_or(true);

    let (schema_context, grounded) = build_schema_context(intent, k);
    if schema_context.trim().is_empty() {
        return Err(
            "synth: no schema available — run rvbbit.catalog_crawl(), or the database has no user tables"
                .into(),
        );
    }

    // Cache key: shape = the retrieved schema scope; prompt = the intent. A schema
    // change shifts the retrieved docs → new fingerprint → regenerate (auto-stale).
    let shape_fp = hash_hex(&schema_context);
    let p_hash = prompt_key(&op.name, intent);
    if let Some(sql) = synth_cache_get(&op.name, &shape_fp, &p_hash) {
        return Ok(sql);
    }

    let mut last_err = String::new();
    let mut last_sql = String::new();
    for attempt in 0..MAX_QUERY_ATTEMPTS {
        let mut inputs = serde_json::Map::new();
        inputs.insert("intent".into(), json!(intent));
        inputs.insert("_schema_context".into(), json!(schema_context));
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
        if !is_single_statement(&gen_sql) {
            last_err = "the SQL must be a single statement (no semicolons)".into();
            continue;
        }
        if !looks_read_only(&gen_sql) {
            last_err = "the SQL must be a read-only SELECT".into();
            continue;
        }
        last_sql = gen_sql.clone();
        if !validate {
            // Preview mode (opts.validate=false): no DB validation, and we do NOT
            // write an unvalidated statement into the cache's 'valid' slot — return
            // it ephemerally so it cannot be mistaken for a blessed snippet.
            return Ok(gen_sql);
        }
        if let Err(e) = validate_sql(&gen_sql) {
            last_err = e;
            continue;
        }
        let sample = json!({
            "kind": "query",
            "intent": intent,
            "grounded": grounded,
            "attempts": attempt + 1,
        });
        synth_cache_put(&op.name, &shape_fp, &p_hash, &gen_sql, &sample, false);
        return Ok(gen_sql);
    }

    // Validation never passed: hand back the best-effort generation (visibly marked,
    // NOT cached) so the user can see and fix it, rather than nothing.
    if !last_sql.is_empty() {
        Ok(format!("-- synth: unvalidated ({last_err})\n{last_sql}"))
    } else {
        Err(format!(
            "synth: no SQL after {MAX_QUERY_ATTEMPTS} attempts: {last_err}"
        ))
    }
}

/// Generate (but do not run) a read-only SELECT for a natural-language intent,
/// grounded by the crawled catalog (rvbbit.data_search; falls back to
/// information_schema). Returns the SQL text and caches validated SQL in
/// rvbbit.synth_cache (inspectable/pinnable in the Cache app). `operator` points at
/// the built-in 'synth' or any custom shape='query', parser='sql' operator.
#[pg_extern(volatile)]
fn synth_sql(
    intent: &str,
    operator: default!(&str, "'synth'"),
    opts: default!(JsonB, "'{}'::jsonb"),
) -> Option<String> {
    let op = match crate::operators::load_op(operator) {
        Some(o) => o,
        None => {
            pgrx::warning!("rvbbit.synth_sql: unknown operator '{}'", operator);
            return None;
        }
    };
    if op.parser != "sql" {
        pgrx::warning!(
            "rvbbit.synth_sql: operator '{}' is not a synth operator (parser must be 'sql')",
            operator
        );
        return None;
    }
    match run_synth_query_sql(&op, intent, &opts.0) {
        Ok(sql) => Some(sql),
        Err(e) => {
            pgrx::warning!("rvbbit.synth_sql: {}", e);
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Executing form (Phase 2): rvbbit.synth(intent) RETURNS SETOF jsonb.
//
// Generates SQL exactly like synth_sql, then runs it behind a read-only guard
// (rvbbit._synth_execute: statement timeout + READ ONLY transaction + row cap), and
// returns each result row as jsonb — the same SETOF jsonb shape as rvbbit.flow().
// Executing model-authored SQL is opt-in: gated behind `rvbbit.synth_enabled`
// (default off). synth_sql (which never executes) is always available.
// ---------------------------------------------------------------------------

const SYNTH_EXEC_MAX_ROWS: i64 = 1000;
const SYNTH_EXEC_TIMEOUT_MS: i64 = 10_000;

/// Whether executing generated SQL is enabled. Placeholder GUC read via
/// GetConfigOption; default OFF (opt in with `SET rvbbit.synth_enabled = on`).
fn synth_enabled() -> bool {
    let name = match std::ffi::CString::new("rvbbit.synth_enabled") {
        Ok(c) => c,
        Err(_) => return false,
    };
    let ptr = unsafe { pgrx::pg_sys::GetConfigOption(name.as_ptr(), true, false) };
    if ptr.is_null() {
        return false;
    }
    let v = unsafe { std::ffi::CStr::from_ptr(ptr).to_string_lossy() };
    !matches!(
        v.trim().to_ascii_lowercase().as_str(),
        "" | "0" | "false" | "no" | "off" | "disabled"
    )
}

fn rows_to_iter(rows: Vec<Value>) -> TableIterator<'static, (name!(value, JsonB),)> {
    let mapped: Vec<(JsonB,)> = rows.into_iter().map(|v| (JsonB(v),)).collect();
    TableIterator::new(mapped.into_iter())
}

/// Run a validated SELECT through the guard-railed plpgsql executor and collect the
/// jsonb result rows. The executor handles its own errors (returns empty + WARNING),
/// so the surrounding statement is not aborted.
fn synth_execute(sql: &str, max_rows: i64, timeout_ms: i64) -> Vec<Value> {
    let q = format!(
        "SELECT v FROM rvbbit._synth_execute('{}', {}, {}) AS v",
        esc(sql),
        max_rows,
        timeout_ms
    );
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&q, None, &[])?;
        for row in table {
            if let Some(j) = row.get::<JsonB>(1)? {
                out.push(j.0);
            }
        }
        Ok(())
    });
    out
}

/// Generate a read-only SELECT for `intent` (grounded by the catalog) and RUN it,
/// returning rows as SETOF jsonb. Gated behind `rvbbit.synth_enabled` (default off).
/// The generated SQL is cached/inspectable exactly like rvbbit.synth_sql.
#[pg_extern(volatile)]
fn synth(
    intent: &str,
    operator: default!(&str, "'synth'"),
    opts: default!(JsonB, "'{}'::jsonb"),
) -> TableIterator<'static, (name!(value, JsonB),)> {
    if !synth_enabled() {
        pgrx::warning!(
            "rvbbit.synth is disabled; run `SET rvbbit.synth_enabled = on` to execute generated SQL \
             (rvbbit.synth_sql returns the SQL without running it)"
        );
        return rows_to_iter(Vec::new());
    }
    let op = match crate::operators::load_op(operator) {
        Some(o) => o,
        None => {
            pgrx::warning!("rvbbit.synth: unknown operator '{}'", operator);
            return rows_to_iter(Vec::new());
        }
    };
    if op.parser != "sql" {
        pgrx::warning!(
            "rvbbit.synth: operator '{}' is not a synth operator (parser must be 'sql')",
            operator
        );
        return rows_to_iter(Vec::new());
    }
    let sql = match run_synth_query_sql(&op, intent, &opts.0) {
        Ok(s) => s,
        Err(e) => {
            pgrx::warning!("rvbbit.synth: {}", e);
            return rows_to_iter(Vec::new());
        }
    };
    // Only execute a validated single read-only statement. run_synth_query_sql
    // guarantees this for cached/fresh SQL; reject the best-effort "-- synth: …"
    // unvalidated comment that it returns when validation never passed.
    if sql.trim_start().starts_with("--") || !is_single_statement(&sql) || !looks_read_only(&sql) {
        pgrx::warning!(
            "rvbbit.synth: generated SQL did not validate as a single read-only SELECT; not executing"
        );
        return rows_to_iter(Vec::new());
    }
    let max_rows = opts
        .0
        .get("max_rows")
        .and_then(|v| v.as_i64())
        .unwrap_or(SYNTH_EXEC_MAX_ROWS)
        .max(0);
    let timeout_ms = opts
        .0
        .get("timeout_ms")
        .and_then(|v| v.as_i64())
        .unwrap_or(SYNTH_EXEC_TIMEOUT_MS)
        .max(100);
    rows_to_iter(synth_execute(&sql, max_rows, timeout_ms))
}

#[cfg(test)]
mod synth_query_unit {
    use super::*;

    #[test]
    fn single_statement_guard_rejects_semicolons() {
        assert!(is_single_statement("SELECT a FROM t WHERE b > 1"));
        assert!(!is_single_statement("SELECT 1; DROP TABLE t"));
        assert!(!is_single_statement("SELECT a FROM t; SELECT b FROM u"));
    }

    #[test]
    fn extract_sql_pulls_select_from_json_and_fences() {
        assert_eq!(
            extract_sql(r#"{"sql": "SELECT 1"}"#).as_deref(),
            Some("SELECT 1")
        );
        assert_eq!(
            extract_sql("```sql\nSELECT a FROM t\n```").as_deref(),
            Some("SELECT a FROM t")
        );
        // Not a SELECT/WITH → rejected.
        assert!(extract_sql("DELETE FROM t").is_none());
    }
}
