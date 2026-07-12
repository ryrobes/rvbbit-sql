//! Semantic operator typed entry points.
//!
//! Three thin wrappers (`_exec_op_bool` / `_exec_op_text` / `_exec_op_float8`)
//! that:
//!   1. Load the operator definition from rvbbit.operators
//!   2. Look up content-addressed cache in rvbbit.receipts
//!   3. On miss, build a UnitOfWork and dispatch to the executor
//!   4. Log the receipt (with sub_calls + query_id)
//!   5. Parse the result string into the typed return value
//!
//! All prompt / step / model logic lives in catalog + unit_of_work — these
//! functions are pure plumbing.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use pgrx::prelude::*;
use pgrx::JsonB;

use crate::unit_of_work::{self, OpDef, SubCall, WorkResult};

// ---- Cache control (user-facing) ----------------------------------------

#[pg_extern(volatile, parallel_safe)]
fn flush_cache() {
    crate::cache::flush();
    crate::synth::clear_scalar_cache();
    flush_op_memo();
}

#[pg_extern(stable, parallel_safe)]
fn cache_size() -> i64 {
    crate::cache::stats().size as i64
}

#[pg_extern(stable, parallel_safe)]
fn cache_capacity() -> i64 {
    crate::cache::stats().capacity as i64
}

/// Per-operator cache observability (RYR-301). Reads rvbbit.receipts —
/// each successful operator call writes one row, so n_invocations is the
/// total work the cache has spared on repeat queries.
#[pg_extern(stable, parallel_safe)]
fn judgment_stats(
    op_name: &str,
) -> TableIterator<
    'static,
    (
        name!(op_name, String),
        name!(n_invocations, i64),
        name!(n_unique_inputs, i64),
        name!(total_tokens_in, i64),
        name!(total_tokens_out, i64),
        name!(total_cost_usd, pgrx::AnyNumeric),
        name!(total_latency_ms, i64),
        name!(first_at, Option<TimestampWithTimeZone>),
        name!(last_at, Option<TimestampWithTimeZone>),
    ),
> {
    let name_esc = op_name.replace('\'', "''");
    let sql = format!(
        "SELECT operator, \
                count(*)::bigint AS n_inv, \
                count(DISTINCT inputs_hash)::bigint AS n_unique, \
                coalesce(sum(n_tokens_in), 0)::bigint AS tin, \
                coalesce(sum(n_tokens_out), 0)::bigint AS tout, \
                coalesce(sum(cost_usd), 0::numeric) AS cost, \
                coalesce(sum(latency_ms), 0)::bigint AS lat, \
                min(invocation_at) AS first_at, \
                max(invocation_at) AS last_at \
         FROM rvbbit.receipts \
         WHERE operator = '{name_esc}' \
         GROUP BY operator"
    );
    let mut out: Vec<(
        String,
        i64,
        i64,
        i64,
        i64,
        pgrx::AnyNumeric,
        i64,
        Option<TimestampWithTimeZone>,
        Option<TimestampWithTimeZone>,
    )> = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let op: Option<String> = row.get(1)?;
            let n_inv: Option<i64> = row.get(2)?;
            let n_unique: Option<i64> = row.get(3)?;
            let tin: Option<i64> = row.get(4)?;
            let tout: Option<i64> = row.get(5)?;
            let cost: Option<pgrx::AnyNumeric> = row.get(6)?;
            let lat: Option<i64> = row.get(7)?;
            let first_at: Option<TimestampWithTimeZone> = row.get(8)?;
            let last_at: Option<TimestampWithTimeZone> = row.get(9)?;
            out.push((
                op.unwrap_or_default(),
                n_inv.unwrap_or(0),
                n_unique.unwrap_or(0),
                tin.unwrap_or(0),
                tout.unwrap_or(0),
                cost.unwrap_or_else(|| pgrx::AnyNumeric::try_from(0i64).unwrap()),
                lat.unwrap_or(0),
                first_at,
                last_at,
            ));
        }
        Ok(())
    });
    TableIterator::new(out.into_iter())
}

/// Manual purge of cached operator results. Useful when switching
/// providers without touching rvbbit.operators, or for testing.
#[pg_extern(volatile, parallel_safe)]
fn judgment_purge(op_name: &str) -> i64 {
    let name_esc = op_name.replace('\'', "''");
    let before: i64 = Spi::get_one(&format!(
        "SELECT count(*) FROM rvbbit.receipts WHERE operator = '{name_esc}'"
    ))
    .ok()
    .flatten()
    .unwrap_or(0);
    Spi::run(&format!(
        "DELETE FROM rvbbit.receipts WHERE operator = '{name_esc}'"
    ))
    .unwrap_or_else(|e| pgrx::error!("rvbbit.judgment_purge: {e}"));
    // Bust in-memory LRU too — otherwise next call returns stale.
    crate::cache::flush();
    before
}

// ---- Public entry points (one per return type) ---------------------------

#[pg_extern(parallel_safe, strict)]
fn _exec_op_bool(op_name: &str, inputs: JsonB, opts: JsonB) -> bool {
    let (op, prompt_seed) = match load_op_memo(op_name) {
        Some(t) => t,
        None => {
            pgrx::warning!("rvbbit: unknown operator '{}'", op_name);
            return false;
        }
    };
    if op.return_type != "bool" {
        pgrx::warning!(
            "rvbbit: operator '{}' is not bool (got '{}')",
            op_name,
            op.return_type
        );
        return false;
    }
    match invoke_with_cache_seeded(&op, &prompt_seed, &inputs.0, &opts.0) {
        Ok(s) => parse_bool(&s, &op.parser),
        Err(_) => false,
    }
}

#[pg_extern(parallel_safe, strict)]
fn _exec_op_text(op_name: &str, inputs: JsonB, opts: JsonB) -> String {
    let (op, prompt_seed) = match load_op_memo(op_name) {
        Some(t) => t,
        None => {
            pgrx::warning!("rvbbit: unknown operator '{}'", op_name);
            return String::new();
        }
    };
    if op.return_type != "text" {
        pgrx::warning!(
            "rvbbit: operator '{}' is not text (got '{}')",
            op_name,
            op.return_type
        );
        return String::new();
    }
    // Scalar synth-sql: the model authors one expression per value-shape, cached
    // and applied natively (Phase 5). The operator's args are value + intent.
    if op.parser == "sql" {
        return crate::synth::run_synth_sql_scalar(&op, &inputs.0, &opts.0);
    }
    match invoke_with_cache_seeded(&op, &prompt_seed, &inputs.0, &opts.0) {
        Ok(s) => parse_text(&s, &op.parser),
        Err(_) => String::new(),
    }
}

#[pg_extern(parallel_safe, strict)]
fn _exec_op_jsonb(op_name: &str, inputs: JsonB, opts: JsonB) -> Option<JsonB> {
    let (op, prompt_seed) = match load_op_memo(op_name) {
        Some(t) => t,
        None => {
            pgrx::warning!("rvbbit: unknown operator '{}'", op_name);
            return None;
        }
    };
    if op.return_type != "jsonb" {
        pgrx::warning!(
            "rvbbit: operator '{}' is not jsonb (got '{}')",
            op_name,
            op.return_type
        );
        return None;
    }
    let s = match invoke_with_cache_seeded(&op, &prompt_seed, &inputs.0, &opts.0) {
        Ok(s) => s,
        Err(_) => return None,
    };
    // parser == "json" expects the model/specialist output to BE valid JSON.
    // For non-json parsers we wrap the raw string as a JSON string value.
    match op.parser.as_str() {
        "json" => match serde_json::from_str::<serde_json::Value>(&s) {
            Ok(v) => Some(JsonB(v)),
            Err(_) => {
                pgrx::warning!(
                    "rvbbit: operator '{}' returned non-JSON output for jsonb return type",
                    op.name
                );
                None
            }
        },
        _ => Some(JsonB(serde_json::Value::String(s))),
    }
}

#[pg_extern(parallel_safe, strict)]
fn _exec_op_float8(op_name: &str, inputs: JsonB, opts: JsonB) -> f64 {
    let (op, prompt_seed) = match load_op_memo(op_name) {
        Some(t) => t,
        None => {
            pgrx::warning!("rvbbit: unknown operator '{}'", op_name);
            return 0.0;
        }
    };
    if op.return_type != "float8" {
        pgrx::warning!(
            "rvbbit: operator '{}' is not float8 (got '{}')",
            op_name,
            op.return_type
        );
        return 0.0;
    }
    match invoke_with_cache_seeded(&op, &prompt_seed, &inputs.0, &opts.0) {
        Ok(s) => parse_float8(&s, &op.parser),
        Err(_) => 0.0,
    }
}

// ---------------------------------------------------------------------------
// Dimension shape: SETOF return. Runs the operator pipeline once per call,
// then splits the result into rows. Recognized splits:
//   - JSON array → one row per element (string elements verbatim,
//     non-string elements rendered as JSON)
//   - newline-separated string → one row per non-empty line
//   - anything else → single row
// ---------------------------------------------------------------------------

fn split_output_for_dim(s: &str) -> Vec<String> {
    let trimmed = s.trim();
    if trimmed.starts_with('[') {
        if let Ok(serde_json::Value::Array(arr)) =
            serde_json::from_str::<serde_json::Value>(trimmed)
        {
            return arr
                .into_iter()
                .map(|v| match v {
                    serde_json::Value::String(s) => s,
                    other => other.to_string(),
                })
                .collect();
        }
    }
    let lines: Vec<String> = trimmed
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect();
    if lines.len() > 1 {
        return lines;
    }
    vec![trimmed.to_string()]
}

#[pg_extern(parallel_safe, strict)]
fn _dim_exec_text(
    op_name: &str,
    inputs: JsonB,
    opts: JsonB,
) -> TableIterator<'static, (name!(value, String),)> {
    let op = match load_op(op_name) {
        Some(o) => o,
        None => {
            pgrx::warning!("rvbbit: unknown operator '{}'", op_name);
            return TableIterator::new(Vec::<(String,)>::new().into_iter());
        }
    };
    if op.shape != "dimension" {
        pgrx::warning!(
            "rvbbit: operator '{}' is not dimension shape (got '{}')",
            op_name,
            op.shape
        );
        return TableIterator::new(Vec::<(String,)>::new().into_iter());
    }
    let raw = match invoke_with_cache(&op, &inputs.0, &opts.0) {
        Ok(s) => s,
        Err(_) => return TableIterator::new(Vec::<(String,)>::new().into_iter()),
    };
    let rows: Vec<(String,)> = split_output_for_dim(&raw)
        .into_iter()
        .map(|s| (s,))
        .collect();
    TableIterator::new(rows.into_iter())
}

// ---------------------------------------------------------------------------
// Rowset shape: a whole resultset in, a whole resultset out. Used by pipeline
// cascades (rvbbit.flow). The table travels in inputs as `_table` (a JSON array
// of row objects) alongside `_table_columns` / `_table_row_count`; positional
// stage args bind to the operator's arg_names. The model output is parsed
// permissively back into a rowset.
// ---------------------------------------------------------------------------

fn load_arg_names(op_name: &str) -> Vec<String> {
    let escaped = op_name.replace('\'', "''");
    let sql = format!("SELECT arg_names FROM rvbbit.operators WHERE name = '{escaped}'");
    let mut names = Vec::new();
    let _: Result<(), pgrx::spi::Error> = Spi::connect(|client| {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            if let Some(arr) = row.get::<Vec<String>>(1)? {
                names = arr;
            }
        }
        Ok(())
    });
    names
}

fn infer_columns(rows: &[serde_json::Value]) -> Vec<String> {
    for r in rows {
        if let Some(obj) = r.as_object() {
            return obj.keys().cloned().collect();
        }
    }
    Vec::new()
}

/// Parse a model's rowset output permissively into a JSON array of row objects.
/// Accepts {data|rows|table|_table|records|results:[...]}, a bare array, or a
/// single object (one row). Non-object array elements become {"value": elem}.
pub(crate) fn parse_rowset_output(raw: &str) -> Vec<serde_json::Value> {
    let trimmed = raw.trim();
    let v: serde_json::Value = match serde_json::from_str(trimmed) {
        Ok(v) => v,
        Err(_) => return vec![serde_json::json!({ "value": trimmed })],
    };
    let arr = match v {
        serde_json::Value::Array(a) => a,
        serde_json::Value::Object(ref o) => {
            let mut found = None;
            for key in ["data", "rows", "table", "_table", "records", "results"] {
                if let Some(serde_json::Value::Array(a)) = o.get(key) {
                    found = Some(a.clone());
                    break;
                }
            }
            found.unwrap_or_else(|| vec![v])
        }
        other => vec![other],
    };
    arr.into_iter()
        .map(|e| match e {
            serde_json::Value::Object(_) => e,
            other => serde_json::json!({ "value": other }),
        })
        .collect()
}

/// Dispatch a `shape='rowset'` operator: serialize the rowset + bind positional
/// args, invoke through the cached operator path (logging a receipt), and parse
/// the output back into a rowset.
pub(crate) fn run_rowset_op(
    op_name: &str,
    rows: &[serde_json::Value],
    pos_args: &[serde_json::Value],
    opts: &serde_json::Value,
) -> Result<(Vec<serde_json::Value>, Option<String>), String> {
    run_rowset_op_with_named(op_name, rows, pos_args, &serde_json::Map::new(), opts)
}

pub(crate) fn run_rowset_op_with_named(
    op_name: &str,
    rows: &[serde_json::Value],
    pos_args: &[serde_json::Value],
    named_args: &serde_json::Map<String, serde_json::Value>,
    opts: &serde_json::Value,
) -> Result<(Vec<serde_json::Value>, Option<String>), String> {
    let op = load_op(op_name).ok_or_else(|| format!("unknown operator '{op_name}'"))?;
    if op.shape != "rowset" {
        return Err(format!(
            "operator '{op_name}' is not rowset shape (got '{}')",
            op.shape
        ));
    }
    // parser='sql' selects the synth-sql strategy: the model authors SQL keyed by
    // the rowset's structural shape, cached and executed natively.
    if op.parser == "sql" {
        let prompt = named_args
            .get("prompt")
            .and_then(|v| v.as_str())
            .or_else(|| pos_args.first().and_then(|v| v.as_str()))
            .unwrap_or("");
        return crate::synth::run_synth_sql_op(&op, prompt, rows, opts);
    }
    let arg_names = load_arg_names(op_name);
    let mut inputs = serde_json::Map::new();
    for (i, name) in arg_names.iter().enumerate() {
        inputs.insert(
            name.clone(),
            pos_args.get(i).cloned().unwrap_or(serde_json::Value::Null),
        );
    }
    for (name, value) in named_args {
        inputs.insert(name.clone(), value.clone());
    }
    inputs.insert(
        "_table_columns".to_string(),
        serde_json::json!(infer_columns(rows)),
    );
    inputs.insert(
        "_table_row_count".to_string(),
        serde_json::json!(rows.len()),
    );
    inputs.insert(
        "_table".to_string(),
        serde_json::Value::Array(rows.to_vec()),
    );
    let inputs_v = serde_json::Value::Object(inputs);
    let raw = invoke_with_cache(&op, &inputs_v, opts)
        .map_err(|_| format!("operator '{op_name}' invocation failed"))?;
    Ok((parse_rowset_output(&raw), None))
}

#[pg_extern(parallel_safe)]
fn _exec_op_rowset(
    op_name: &str,
    rows: JsonB,
    args: JsonB,
    opts: JsonB,
) -> TableIterator<'static, (name!(value, JsonB),)> {
    let rows_vec = rows.0.as_array().cloned().unwrap_or_default();
    let pos_args = args.0.as_array().cloned().unwrap_or_default();
    match run_rowset_op(op_name, &rows_vec, &pos_args, &opts.0) {
        Ok((out, _generated_sql)) => {
            let mapped: Vec<(JsonB,)> = out.into_iter().map(|v| (JsonB(v),)).collect();
            TableIterator::new(mapped.into_iter())
        }
        Err(e) => {
            pgrx::warning!("rvbbit._exec_op_rowset: {e}");
            TableIterator::new(Vec::<(JsonB,)>::new().into_iter())
        }
    }
}

// ---------------------------------------------------------------------------
// Aggregate shape: CREATE AGGREGATE driven by these generic helpers.
//   _agg_append_state(state, row_inputs) — SFUNC, runs per row
//   _agg_run_op_<type>(op_name, state) — FFUNC, runs once per group
// The per-operator SFUNC + FFUNC SQL wrappers (generated by
// rvbbit.create_operator) bind the op_name string into the call.
// State shape: {"collection": [<row1_inputs>, <row2_inputs>, ...]}.
// ---------------------------------------------------------------------------

#[pg_extern(parallel_safe)]
fn _agg_append_state(state: Option<JsonB>, row_inputs: JsonB) -> JsonB {
    let mut s = state.map(|j| j.0).unwrap_or_else(|| serde_json::json!({}));
    if !s.is_object() {
        s = serde_json::json!({});
    }
    let obj = s.as_object_mut().expect("just set to object");
    let arr = obj
        .entry("collection".to_string())
        .or_insert_with(|| serde_json::json!([]));
    if let Some(a) = arr.as_array_mut() {
        a.push(row_inputs.0);
    }
    JsonB(s)
}

/// Run the aggregate operator's pipeline once, with the accumulated
/// collection bound as `inputs.collection`. Returns the result rendered
/// as a plain string (parser applied), suitable for cast to text.
#[pg_extern(parallel_safe)]
fn _agg_run_op_text(op_name: &str, state: Option<JsonB>) -> Option<String> {
    let raw = agg_run_inner(op_name, state, "text")?;
    Some(parse_text(&raw, "raw_text"))
}

#[pg_extern(parallel_safe)]
fn _agg_run_op_bool(op_name: &str, state: Option<JsonB>) -> Option<bool> {
    let raw = agg_run_inner(op_name, state, "bool")?;
    Some(parse_bool(&raw, "yes_no"))
}

#[pg_extern(parallel_safe)]
fn _agg_run_op_float8(op_name: &str, state: Option<JsonB>) -> Option<f64> {
    let raw = agg_run_inner(op_name, state, "float8")?;
    Some(parse_float8(&raw, "score_0_1"))
}

#[pg_extern(parallel_safe)]
fn _agg_run_op_jsonb(op_name: &str, state: Option<JsonB>) -> Option<JsonB> {
    let raw = agg_run_inner(op_name, state, "jsonb")?;
    serde_json::from_str::<serde_json::Value>(&raw)
        .ok()
        .map(JsonB)
}

fn agg_run_inner(op_name: &str, state: Option<JsonB>, expected_return: &str) -> Option<String> {
    let op = load_op(op_name)?;
    if op.shape != "aggregate" {
        pgrx::warning!(
            "rvbbit agg: op '{}' shape mismatch (got '{}', want 'aggregate')",
            op_name,
            op.shape
        );
        return None;
    }
    if op.return_type != expected_return {
        pgrx::warning!(
            "rvbbit agg: op '{}' return_type mismatch (op declares '{}', call expects '{}')",
            op_name,
            op.return_type,
            expected_return
        );
        return None;
    }
    let collection = state
        .as_ref()
        .and_then(|j| j.0.get("collection").cloned())
        .unwrap_or_else(|| serde_json::json!([]));
    let inputs = serde_json::json!({ "collection": collection });
    let opts = serde_json::json!({});
    let result = unit_of_work::execute(&op, &inputs, &opts);
    crate::probe::record_fresh(&op.name, &inputs, &result);
    // Cache key + receipts for aggregates use the collection hash —
    // same machinery as scalar invoke_with_cache, but the "inputs" is
    // the full collection (so re-running the same group hits cache).
    use blake3::Hasher;
    let model_override = "";
    let runtime_seed = crate::python_runtime::dependency_seed(op.steps.as_ref(), op.takes.as_ref());
    let prompt_seed = format!(
        "{}\0{}\0{}\0{}",
        op.system_prompt,
        op.user_prompt,
        serde_json::to_string(&op.steps).unwrap_or_default(),
        runtime_seed
    );
    let mut h = Hasher::new();
    h.update(op.name.as_bytes());
    h.update(b"\0");
    // Match operators::input_hash + prewarm::build_hash key shape so the
    // aggregate path uses the same cache entries as scalar.
    h.update(op.model.as_bytes());
    h.update(b"\0");
    h.update(model_override.as_bytes());
    h.update(b"\0");
    h.update(
        serde_json::to_string(&inputs)
            .unwrap_or_default()
            .as_bytes(),
    );
    h.update(b"\0");
    h.update(prompt_seed.as_bytes());
    let hash = h.finalize().as_bytes().to_vec();
    if result.error.is_none() {
        crate::cache::put(&hash, result.output.clone());
    }
    log_receipt(&op, &hash, &result, &inputs);
    if let Some(err) = &result.error {
        pgrx::warning!("rvbbit agg: operator '{}' failed: {}", op.name, err);
        return None;
    }
    Some(result.output)
}

// ---- Loaded operator definition -----------------------------------------

pub(crate) fn load_op(name: &str) -> Option<OpDef> {
    let escaped = name.replace('\'', "''");
    let sql = format!(
        "SELECT shape, return_type, model, system_prompt, user_prompt, parser, \
                max_tokens, temperature, steps, retry, wards, takes, cache_policy \
         FROM rvbbit.operators WHERE name = '{escaped}'"
    );
    let mut result: Option<OpDef> = None;
    let _: Result<(), pgrx::spi::Error> = Spi::connect(|client| {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let shape: Option<String> = row.get(1)?;
            let return_type: Option<String> = row.get(2)?;
            let model: Option<String> = row.get(3)?;
            let system_prompt: Option<String> = row.get(4)?;
            let user_prompt: Option<String> = row.get(5)?;
            let parser: Option<String> = row.get(6)?;
            let max_tokens: Option<i32> = row.get(7)?;
            let temperature: Option<f32> = row.get(8)?;
            let steps_jsonb: Option<pgrx::JsonB> = row.get(9)?;
            let retry_jsonb: Option<pgrx::JsonB> = row.get(10)?;
            let wards_jsonb: Option<pgrx::JsonB> = row.get(11)?;
            let takes_jsonb: Option<pgrx::JsonB> = row.get(12)?;
            let cache_policy: Option<String> = row.get(13)?;
            if let (Some(sh), Some(rt), Some(m), Some(sp), Some(up), Some(p), Some(mt)) = (
                shape,
                return_type,
                model,
                system_prompt,
                user_prompt,
                parser,
                max_tokens,
            ) {
                result = Some(OpDef {
                    name: name.to_string(),
                    shape: sh,
                    return_type: rt,
                    model: m,
                    system_prompt: sp,
                    user_prompt: up,
                    parser: p,
                    max_tokens: mt,
                    temperature,
                    steps: steps_jsonb.map(|j| j.0),
                    retry: retry_jsonb.map(|j| j.0),
                    wards: wards_jsonb.map(|j| j.0),
                    takes: takes_jsonb.map(|j| j.0),
                    cache_policy: cache_policy.unwrap_or_else(|| "memoize".to_string()),
                });
            }
        }
        Ok(())
    });
    result
}

// ---- Operator memo (per-backend) -----------------------------------------
//
// `load_op` runs a full SPI `SELECT ... FROM rvbbit.operators` every call, and
// the cached-invoke path rebuilds the prompt seed (serializing `steps`, resolving
// the runtime dependency seed) every call. On a large scan that's evaluated per
// row — e.g. `SELECT rvbbit.classify(col, '...') FROM big_table` — those two costs
// dominate the wall-clock even when every row is a cache hit (no model call at all).
//
// Both the OpDef and the prompt seed are invariant for a given operator, so we
// memoize them per backend behind a short TTL: stable within a query (where it
// matters), refreshed across queries so a `register_operator` edit is picked up
// within a few seconds, and cleared eagerly by `flush_cache()`.

struct OpMemoEntry {
    at: Instant,
    op: Arc<OpDef>,
    prompt_seed: Arc<str>,
}

thread_local! {
    static OP_MEMO: RefCell<HashMap<String, OpMemoEntry>> = RefCell::new(HashMap::new());
}

fn op_memo_ttl() -> Duration {
    Duration::from_millis(
        std::env::var("RVBBIT_OP_MEMO_TTL_MS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(3000),
    )
}

/// Memoized operator load: returns the `OpDef` plus its precomputed `prompt_seed`,
/// avoiding a per-row catalog SPI and prompt-seed rebuild. The seed is exactly what
/// `compute_prompt_seed` returns, so the cache key (and every existing receipt) is
/// byte-identical.
pub(crate) fn load_op_memo(name: &str) -> Option<(Arc<OpDef>, Arc<str>)> {
    let ttl = op_memo_ttl();
    let hit = OP_MEMO.with(|m| {
        m.borrow()
            .get(name)
            .filter(|e| e.at.elapsed() < ttl)
            .map(|e| (e.op.clone(), e.prompt_seed.clone()))
    });
    if let Some(found) = hit {
        return Some(found);
    }
    let op = Arc::new(load_op(name)?);
    let prompt_seed: Arc<str> = Arc::from(compute_prompt_seed(&op));
    OP_MEMO.with(|m| {
        m.borrow_mut().insert(
            name.to_string(),
            OpMemoEntry {
                at: Instant::now(),
                op: op.clone(),
                prompt_seed: prompt_seed.clone(),
            },
        );
    });
    Some((op, prompt_seed))
}

/// Drop the per-backend operator memo. Called by `flush_cache()` so an explicit
/// flush picks up catalog edits immediately rather than waiting out the TTL.
pub(crate) fn flush_op_memo() {
    OP_MEMO.with(|m| m.borrow_mut().clear());
}

// ---- The cached invoke ---------------------------------------------------

pub(crate) fn invoke_with_cache(
    op: &OpDef,
    inputs: &serde_json::Value,
    opts: &serde_json::Value,
) -> Result<String, ()> {
    // Cold-path entry: compute the prompt seed inline. The hot scalar externs
    // skip straight to invoke_with_cache_seeded with a memoized seed.
    let prompt_seed = compute_prompt_seed(op);
    invoke_with_cache_seeded(op, &prompt_seed, inputs, opts)
}

/// The prompt-seed component of the cache key — invariant for a given operator
/// (independent of the per-row inputs and of opts.model). Folded into the hash
/// so editing the operator's prompt/steps via `rvbbit.operators` auto-invalidates
/// its cache entries. Recomputing this per row (serializing `steps`, resolving
/// the runtime dependency seed) is pure overhead on the hot path — memoized by
/// `load_op_memo`.
pub(crate) fn compute_prompt_seed(op: &OpDef) -> String {
    let runtime_seed = crate::python_runtime::dependency_seed(op.steps.as_ref(), op.takes.as_ref());
    format!(
        "{}\0{}\0{}\0{}",
        op.system_prompt,
        op.user_prompt,
        serde_json::to_string(&op.steps).unwrap_or_default(),
        runtime_seed
    )
}

/// Cached invoke with a precomputed `prompt_seed`. Hash bytes are identical to
/// the inline path — same `input_hash` arguments, same seed content — so existing
/// receipts stay valid.
pub(crate) fn invoke_with_cache_seeded(
    op: &OpDef,
    prompt_seed: &str,
    inputs: &serde_json::Value,
    opts: &serde_json::Value,
) -> Result<String, ()> {
    // For now we hash on a canonical (operator name + inputs + opts.model)
    // because the rendered prompt is internal and the operator name +
    // inputs are the contract surface. The prompt_seed component invalidates
    // the cache cheaply when the operator definition changes.
    let model_override = opts.get("model").and_then(|v| v.as_str()).unwrap_or("");
    let hash = input_hash(&op.name, &op.model, model_override, inputs, prompt_seed);

    // Honor cache_policy. 'never' bypasses the result cache (READ + WRITE) so a stateful
    // operator — one that reads mutable tables or runs an agent loop — always runs fresh
    // instead of returning a frozen prior output for identical inputs. Receipts are still
    // logged below for audit/cost. (Gating only the read is insufficient: the receipt we'd
    // write becomes the L2 entry; gating the read means a 'never' op never consults it.)
    //
    // The `rvbbit.cache_bypass` GUC forces the same bypass transiently WITHOUT touching an
    // operator's production cache_policy — Semantic Tests sets it (txn-local) so every
    // battery run re-exercises the model instead of re-serving cached verdicts, which is
    // what makes drift detection meaningful.
    let cacheable = op.cache_policy != "never" && !cache_bypass_active();

    if cacheable {
        // L1: in-memory LRU cache. ~5μs lookup. Skips SPI entirely.
        if let Some(cached) = crate::cache::get(&hash) {
            crate::probe::record_l1_hit(&op.name, inputs);
            return Ok(cached);
        }

        // L2: cross-backend persistent cache (rvbbit.receipts). ~1-3ms SPI.
        if let Some(cached) = lookup_cached(&hash) {
            crate::probe::record_l2_hit(&op.name, inputs);
            // Backfill L1 so future calls in this backend skip the SPI cost.
            crate::cache::put(&hash, cached.clone());
            return Ok(cached);
        }
    }

    // Pre-wards gate the inputs before the operator runs at all.
    if let Err(reason) = crate::validator::check_pre_wards(op, inputs) {
        let result = crate::validator::errored(reason);
        crate::probe::record_fresh(&op.name, inputs, &result);
        log_receipt(op, &hash, &result, inputs);
        pgrx::warning!(
            "rvbbit: operator '{}' blocked: {}",
            op.name,
            result.error.as_deref().unwrap_or("")
        );
        return Err(());
    }

    // Pre-load any specialist backends this operator references, so pool
    // threads running specialist nodes (takes) find the spec cached — a
    // worker thread cannot do the SPI spec load itself.
    crate::specialists::warm_operator_specs(op.steps.as_ref(), op.takes.as_ref());
    // Same for Python handler/env specs; workers can call the sidecar, but
    // they cannot look up handler code or package lists through SPI.
    crate::python_runtime::warm_operator_specs(op.steps.as_ref(), op.takes.as_ref());

    // Leader-side live progress: a genuine per-row call (cache missed both
    // tiers above, so prewarmed rows — which return at the L1/L2 hits — are not
    // double-counted). Covers the per-row / over-cap path that skips prewarm.
    crate::live_counters::tick(&op.name, 1);
    let result: WorkResult = crate::takes::execute_attempt(op, inputs, opts, None);
    // Validators + retry: if the operator carries a retry plan and the
    // output fails its validator, re-run with feedback. No-op otherwise.
    let result = crate::validator::apply_retry(op, inputs, opts, result);
    // Post-wards gate the final output.
    let result = crate::validator::apply_post_wards(op, inputs, result);
    crate::probe::record_fresh(&op.name, inputs, &result);

    log_receipt(op, &hash, &result, inputs);

    if let Some(err) = &result.error {
        pgrx::warning!("rvbbit: operator '{}' failed: {}", op.name, err);
        return Err(());
    }
    // Populate L1 so subsequent calls in this backend are sub-millisecond (skip for 'never').
    if cacheable {
        crate::cache::put(&hash, result.output.clone());
    }
    Ok(result.output)
}

// ---- Parsers (unchanged from previous session) ---------------------------

/// True when `rvbbit.cache_bypass` is set on — the operator result cache is
/// bypassed (READ + WRITE) for this call. Read via GetConfigOption (no SPI),
/// so it is safe on the leader operator-exec path. Set txn-locally by the
/// Semantic Tests runner so batteries re-exercise the model each run.
fn cache_bypass_active() -> bool {
    crate::duck_backend::guc_setting("rvbbit.cache_bypass")
        .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "on" | "true" | "1" | "yes"))
        .unwrap_or(false)
}

fn parse_bool(s: &str, parser: &str) -> bool {
    match parser {
        "yes_no" => {
            let t = s.trim().to_ascii_uppercase();
            t.starts_with("YES") || t == "TRUE" || t == "1"
        }
        _ => s.trim().eq_ignore_ascii_case("true"),
    }
}

fn parse_text(s: &str, parser: &str) -> String {
    match parser {
        "strip" => s.trim().to_string(),
        _ => s.to_string(),
    }
}

fn parse_float8(s: &str, parser: &str) -> f64 {
    match parser {
        "score_0_1" => {
            for token in s.split(|c: char| !c.is_ascii_digit() && c != '.' && c != '-') {
                if !token.is_empty() {
                    if let Ok(f) = token.parse::<f64>() {
                        return f.clamp(0.0, 1.0);
                    }
                }
            }
            0.0
        }
        _ => s.trim().parse().unwrap_or(0.0),
    }
}

// ---- Cache + receipts ----------------------------------------------------

fn input_hash(
    op_name: &str,
    op_model: &str,
    model_override: &str,
    inputs: &serde_json::Value,
    prompt_seed: &str,
) -> Vec<u8> {
    let mut h = blake3::Hasher::new();
    h.update(op_name.as_bytes());
    h.update(b"\0");
    // op_model is the catalog-default model; folding it in means editing
    // `rvbbit.operators SET model = 'new'` auto-invalidates cache entries
    // for that operator (RYR-301). model_override stays separate so an
    // explicit per-call opts.model still differentiates.
    h.update(op_model.as_bytes());
    h.update(b"\0");
    h.update(model_override.as_bytes());
    h.update(b"\0");
    h.update(serde_json::to_string(inputs).unwrap_or_default().as_bytes());
    h.update(b"\0");
    h.update(prompt_seed.as_bytes());
    h.finalize().as_bytes().to_vec()
}

fn lookup_cached(hash: &[u8]) -> Option<String> {
    let hex = bytes_to_hex(hash);
    let sql = format!(
        "SELECT output FROM rvbbit.receipts \
         WHERE inputs_hash = '\\x{hex}'::bytea AND error IS NULL \
         ORDER BY invocation_at DESC LIMIT 1"
    );
    Spi::get_one::<String>(&sql).ok().flatten()
}

// Receipts are INSERTs, which PG forbids during parallel queries.
// `IsInParallelMode()` is true for BOTH the leader and workers when a
// parallel scan is active. PG18 keeps it inline-static so pgrx can't
// link to it. Workaround uses two detectors:
//
//   1. `ParallelWorkerNumber` (extern int, exported): identifies actual
//      parallel workers. Skips INSERT in workers.
//   2. SKIP_RECEIPTS thread-local + set_skip_receipts() SQL function:
//      the leader sets it before launching a parallel query so the
//      leader's own UDF calls during the parallel scan also skip.
//
// The L1 cache still works per-backend; receipts get logged for any
// non-parallel calls. Worker→leader audit queue is a real follow-up
// when receipts at scale matter more than maximum parallelism.
#[allow(non_upper_case_globals)]
extern "C" {
    static ParallelWorkerNumber: i32;
}

thread_local! {
    static SKIP_RECEIPTS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

#[pg_extern(volatile, parallel_safe)]
fn set_skip_receipts(skip: bool) -> bool {
    SKIP_RECEIPTS.with(|c| c.set(skip));
    skip
}

#[pg_extern(stable, parallel_safe)]
fn get_skip_receipts() -> bool {
    SKIP_RECEIPTS.with(|c| c.get())
}

fn log_receipt(op: &OpDef, hash: &[u8], res: &WorkResult, inputs: &serde_json::Value) {
    let record = crate::costs::record_from_work(op, hash, res, inputs);
    let pwn = unsafe { ParallelWorkerNumber };
    if pwn >= 0 || SKIP_RECEIPTS.with(|c| c.get()) {
        if let Err(e) = crate::costs::enqueue_receipt(
            &record,
            if pwn >= 0 {
                "parallel_worker"
            } else {
                "skip_receipts"
            },
        ) {
            pgrx::warning!("rvbbit: failed to queue delayed receipt: {}", e);
        }
        return;
    }
    crate::costs::flush_receipt_queue_best_effort(64);
    if let Err(e) = crate::costs::write_receipt_now(&record, crate::costs::MissingQueryId::Generate)
    {
        pgrx::warning!("rvbbit: failed to log receipt: {}", e);
    }
}

fn bytes_to_hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for byte in b {
        s.push_str(&format!("{:02x}", byte));
    }
    s
}

// Silence unused warnings for HashMap import — kept for future opt merging.
#[allow(dead_code)]
fn _unused() -> HashMap<String, String> {
    HashMap::new()
}
#[allow(dead_code)]
fn _once_lock() -> OnceLock<()> {
    OnceLock::new()
}
#[allow(dead_code)]
fn _sub() -> SubCall {
    SubCall {
        step: String::new(),
        kind: String::new(),
        model: None,
        tokens_in: 0,
        tokens_out: 0,
        latency_ms: 0,
        error: None,
        ..Default::default()
    }
}
