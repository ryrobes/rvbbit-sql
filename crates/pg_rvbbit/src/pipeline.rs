//! Pipeline cascades — `rvbbit.flow('select … then op(…) then op2')`.
//!
//! Chained, full-resultset post-processing. The base query runs, then the whole
//! rowset is piped through a chain of stage operators, each producing a new
//! rowset (possibly a different shape). The `THEN`s live inside the dollar-quoted
//! string argument, so Postgres never parses them — rvbbit splits the string
//! itself (token-aware: respects strings / comments / CASE-depth / paren-depth),
//! exactly like the old larsql pipeline, but as a set-returning function.
//!
//! Each stage is either an inline deterministic builtin (`pass` / `limit` /
//! `count`) or a `shape='rowset'` semantic operator dispatched through the same
//! operator / receipts machinery as every other operator. Each step's resultset
//! is persisted to `rvbbit.flow_steps` for inspection.
//!
//! See docs/PIPELINE_CASCADES_PLAN.md.

use pgrx::prelude::*;
use pgrx::JsonB;
use serde_json::{json, Value};

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/pipeline.sql",
    name = "pipeline",
    requires = ["rvbbit_bootstrap"]
);

// ---------------------------------------------------------------------------
// The THEN splitter (pure; unit-tested below)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub(crate) enum StageArg {
    Str(String),
    Num(f64),
    Named(String, Box<StageArg>),
}

impl StageArg {
    fn to_value(&self) -> Value {
        match self {
            StageArg::Str(s) => Value::String(s.clone()),
            StageArg::Num(n) => json!(n),
            StageArg::Named(_, v) => v.to_value(),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Stage {
    pub name: String,
    pub args: Vec<StageArg>,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Pipeline {
    pub head: String,
    pub stages: Vec<Stage>,
}

fn is_word_start(c: u8) -> bool {
    c.is_ascii_alphabetic() || c == b'_'
}
fn is_word_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_'
}

/// If `bytes[i]` opens a dollar-quote tag (`$tag$`, tag = `[A-Za-z0-9_]*`),
/// return the byte index just past the opening tag's closing `$`. Otherwise None
/// (so `$1` params and lone `$` are treated as ordinary characters).
fn dollar_tag_end(bytes: &[u8], i: usize) -> Option<usize> {
    if bytes.get(i) != Some(&b'$') {
        return None;
    }
    let mut j = i + 1;
    while j < bytes.len() && is_word_char(bytes[j]) {
        j += 1;
    }
    if bytes.get(j) == Some(&b'$') {
        Some(j + 1)
    } else {
        None
    }
}

/// Byte spans `(start, end)` of each statement-level `THEN` keyword — outside of
/// strings, comments, parentheses, and CASE…END.
fn top_level_then_spans(spec: &str) -> Vec<(usize, usize)> {
    let bytes = spec.as_bytes();
    let n = bytes.len();
    let mut i = 0usize;
    let mut spans = Vec::new();
    let mut paren_depth: i32 = 0;
    let mut case_depth: i32 = 0;

    while i < n {
        let c = bytes[i];
        // line comment --
        if c == b'-' && bytes.get(i + 1) == Some(&b'-') {
            i += 2;
            while i < n && bytes[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        // block comment /* */
        if c == b'/' && bytes.get(i + 1) == Some(&b'*') {
            i += 2;
            while i + 1 < n && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                i += 1;
            }
            i = (i + 2).min(n);
            continue;
        }
        // single-quoted string ('' escapes)
        if c == b'\'' {
            i += 1;
            while i < n {
                if bytes[i] == b'\'' {
                    if bytes.get(i + 1) == Some(&b'\'') {
                        i += 2;
                        continue;
                    }
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        // double-quoted identifier ("" escapes)
        if c == b'"' {
            i += 1;
            while i < n {
                if bytes[i] == b'"' {
                    if bytes.get(i + 1) == Some(&b'"') {
                        i += 2;
                        continue;
                    }
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        // dollar-quoted string $tag$ … $tag$
        if c == b'$' {
            if let Some(tag_end) = dollar_tag_end(bytes, i) {
                let tag = &bytes[i..tag_end];
                let mut j = tag_end;
                let mut closed = false;
                while j + tag.len() <= n {
                    if &bytes[j..j + tag.len()] == tag {
                        j += tag.len();
                        closed = true;
                        break;
                    }
                    j += 1;
                }
                i = if closed { j } else { n };
                continue;
            }
        }
        // word
        if is_word_start(c) {
            let start = i;
            i += 1;
            while i < n && is_word_char(bytes[i]) {
                i += 1;
            }
            let word = &spec[start..i];
            if word.eq_ignore_ascii_case("case") {
                case_depth += 1;
            } else if word.eq_ignore_ascii_case("end") {
                if case_depth > 0 {
                    case_depth -= 1;
                }
            } else if word.eq_ignore_ascii_case("then") && paren_depth == 0 && case_depth == 0 {
                spans.push((start, i));
            }
            continue;
        }
        if c == b'(' {
            paren_depth += 1;
        } else if c == b')' {
            if paren_depth > 0 {
                paren_depth -= 1;
            }
        }
        i += 1;
    }
    spans
}

fn parse_single_arg(seg: &str) -> Result<StageArg, String> {
    let s = seg.trim();
    if s.is_empty() {
        return Err("empty stage argument".into());
    }
    if s.len() >= 2 && s.starts_with('\'') && s.ends_with('\'') {
        let inner = &s[1..s.len() - 1];
        return Ok(StageArg::Str(inner.replace("''", "'")));
    }
    if let Ok(num) = s.parse::<f64>() {
        return Ok(StageArg::Num(num));
    }
    // bare word / unquoted token → string
    Ok(StageArg::Str(s.to_string()))
}

fn top_level_arrow(seg: &str) -> Option<usize> {
    let bytes = seg.as_bytes();
    let mut i = 0usize;
    let mut depth = 0i32;
    while i + 1 < bytes.len() {
        let c = bytes[i];
        if c == b'\'' {
            i += 1;
            while i < bytes.len() {
                if bytes[i] == b'\'' {
                    if bytes.get(i + 1) == Some(&b'\'') {
                        i += 2;
                        continue;
                    }
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        if c == b'(' {
            depth += 1;
        } else if c == b')' {
            if depth > 0 {
                depth -= 1;
            }
        } else if c == b'=' && bytes.get(i + 1) == Some(&b'>') && depth == 0 {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn is_arg_name(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn parse_stage_arg(seg: &str) -> Result<StageArg, String> {
    let s = seg.trim();
    if let Some(i) = top_level_arrow(s) {
        let name = s[..i].trim();
        let value = s[i + 2..].trim();
        if !is_arg_name(name) {
            return Err(format!("invalid named stage argument '{name}'"));
        }
        if value.is_empty() {
            return Err(format!("named stage argument '{name}' has no value"));
        }
        return Ok(StageArg::Named(
            name.to_ascii_lowercase(),
            Box::new(parse_single_arg(value)?),
        ));
    }
    parse_single_arg(s)
}

/// Parse the argument list of `(...)`, splitting on top-level commas while
/// respecting single-quoted strings and nested parens.
fn parse_paren_args(s: &str) -> Result<Vec<StageArg>, String> {
    let bytes = s.as_bytes();
    debug_assert_eq!(bytes.first(), Some(&b'('));
    let n = bytes.len();
    let mut i = 1usize;
    let mut depth = 1i32;
    let mut seg_start = 1usize;
    let mut segs: Vec<&str> = Vec::new();
    while i < n && depth > 0 {
        let c = bytes[i];
        if c == b'\'' {
            i += 1;
            while i < n {
                if bytes[i] == b'\'' {
                    if bytes.get(i + 1) == Some(&b'\'') {
                        i += 2;
                        continue;
                    }
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        if c == b'(' {
            depth += 1;
            i += 1;
            continue;
        }
        if c == b')' {
            depth -= 1;
            if depth == 0 {
                segs.push(&s[seg_start..i]);
                break;
            }
            i += 1;
            continue;
        }
        if c == b',' && depth == 1 {
            segs.push(&s[seg_start..i]);
            i += 1;
            seg_start = i;
            continue;
        }
        i += 1;
    }
    let mut args = Vec::new();
    for seg in segs {
        if !seg.trim().is_empty() {
            args.push(parse_stage_arg(seg)?);
        }
    }
    Ok(args)
}

fn parse_stage(text: &str) -> Result<Stage, String> {
    let t = text.trim().trim_end_matches(';').trim();
    if t.is_empty() {
        return Err("empty pipeline stage".into());
    }
    let bytes = t.as_bytes();
    if !is_word_start(bytes[0]) {
        return Err(format!("stage must start with an operator name: '{t}'"));
    }
    let mut i = 0usize;
    while i < bytes.len() && is_word_char(bytes[i]) {
        i += 1;
    }
    let name = t[..i].to_lowercase();
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    let rest = t[i..].trim();
    let args = if rest.starts_with('(') {
        parse_paren_args(rest)?
    } else if rest.is_empty() {
        Vec::new()
    } else {
        vec![parse_single_arg(rest)?]
    };
    Ok(Stage { name, args })
}

/// Split a pipeline spec into its head SQL and ordered stages.
pub(crate) fn parse_pipeline(spec: &str) -> Result<Pipeline, String> {
    let spans = top_level_then_spans(spec);
    if spans.is_empty() {
        return Ok(Pipeline {
            head: spec.trim().trim_end_matches(';').trim().to_string(),
            stages: Vec::new(),
        });
    }
    let head = spec[..spans[0].0].trim().to_string();
    if head.is_empty() {
        return Err("pipeline has no base query before the first THEN".into());
    }
    let mut stages = Vec::with_capacity(spans.len());
    for (k, span) in spans.iter().enumerate() {
        let seg_start = span.1;
        let seg_end = spans.get(k + 1).map(|s| s.0).unwrap_or(spec.len());
        stages.push(parse_stage(&spec[seg_start..seg_end])?);
    }
    Ok(Pipeline { head, stages })
}

// ---------------------------------------------------------------------------
// Execution
// ---------------------------------------------------------------------------

fn run_query_to_rows(head: &str) -> Result<Vec<Value>, String> {
    let sql = format!(
        "SELECT to_jsonb(t) FROM ({}) t",
        head.trim().trim_end_matches(';')
    );
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
    Ok(unwrap_single_jsonb_column(out))
}

/// If a head produced exactly one column whose every value is a jsonb OBJECT — e.g.
/// `select * from rvbbit.synth(...)` (one jsonb column) or any `… returns jsonb …` —
/// unwrap each row to that inner object so downstream stages see the real fields
/// instead of a single wrapper column. Only fires when ALL rows match (single key,
/// object value), so ordinary multi-column or scalar-valued rows are untouched. This
/// is what makes `synth(…) then group(…)` pipe real columns to the stages.
fn unwrap_single_jsonb_column(rows: Vec<Value>) -> Vec<Value> {
    if rows.is_empty() {
        return rows;
    }
    let all_single_obj = rows.iter().all(|r| {
        r.as_object()
            .map(|o| o.len() == 1 && o.values().next().map(|v| v.is_object()).unwrap_or(false))
            .unwrap_or(false)
    });
    if !all_single_obj {
        return rows;
    }
    rows.into_iter()
        .map(|r| {
            r.as_object()
                .and_then(|o| o.values().next().cloned())
                .unwrap_or(r)
        })
        .collect()
}

fn take_rows(rows: &[Value], n: usize) -> Vec<Value> {
    rows.iter().take(n).cloned().collect()
}

/// First numeric arg of a stage (limit/sample), with a default.
fn stage_n(stage: &Stage, default: usize) -> usize {
    match stage.args.first() {
        Some(StageArg::Num(n)) => *n as usize,
        Some(StageArg::Str(s)) => s.trim().parse::<usize>().unwrap_or(default),
        Some(StageArg::Named(_, value)) => match value.as_ref() {
            StageArg::Num(n) => *n as usize,
            StageArg::Str(s) => s.trim().parse::<usize>().unwrap_or(default),
            StageArg::Named(_, _) => default,
        },
        None => default,
    }
}

/// Deterministic systematic sample: n rows spread evenly across the rowset (a
/// representative subset, e.g. to bound the cost of a downstream model stage).
fn stride_sample(rows: &[Value], n: usize) -> Vec<Value> {
    let len = rows.len();
    if n == 0 {
        return Vec::new();
    }
    if n >= len {
        return rows.to_vec();
    }
    (0..n).map(|i| rows[(i * len) / n].clone()).collect()
}

/// Run one stage against the current rowset. Returns the new rowset plus, for
/// synth-sql stages, the SQL the model authored (recorded for the inspector).
/// Inline builtins are deterministic (no model call).
fn run_stage(stage: &Stage, rows: &[Value]) -> Result<(Vec<Value>, Option<String>), String> {
    match stage.name.as_str() {
        "pass" => Ok((rows.to_vec(), None)),
        "count" => Ok((vec![json!({ "n": rows.len() })], None)),
        "limit" | "head" => {
            let n = stage_n(stage, 10);
            Ok((take_rows(rows, n), None))
        }
        "sample" => {
            let n = stage_n(stage, 10);
            Ok((stride_sample(rows, n), None))
        }
        _ => {
            let mut pos_args: Vec<Value> = Vec::new();
            let mut named_args = serde_json::Map::new();
            for arg in &stage.args {
                match arg {
                    StageArg::Named(name, value) => {
                        named_args.insert(name.clone(), value.to_value());
                    }
                    other => pos_args.push(other.to_value()),
                }
            }
            crate::operators::run_rowset_op_with_named(
                &stage.name,
                rows,
                &pos_args,
                &named_args,
                &json!({}),
            )
        }
    }
}

fn new_run_id() -> String {
    Spi::get_one::<String>("SELECT gen_random_uuid()::text")
        .ok()
        .flatten()
        .unwrap_or_default()
}

/// Cap on the rowset sample stored per step (n_rows keeps the true count).
const MAX_STORED_STEP_ROWS: usize = 500;

/// Best-effort persistence of one step's rowset to rvbbit.flow_steps. Stores at
/// most MAX_STORED_STEP_ROWS rows (a sample); n_rows is the true count.
fn persist_step(
    run_id: &str,
    step_idx: i32,
    stage: &str,
    spec: &str,
    generated_sql: Option<&str>,
    rows: &[Value],
) {
    if run_id.is_empty() {
        return;
    }
    let esc = |s: &str| s.replace('\'', "''");
    let stored: Vec<Value> = rows.iter().take(MAX_STORED_STEP_ROWS).cloned().collect();
    let rows_str = esc(&Value::Array(stored).to_string());
    let gsql = match generated_sql {
        Some(s) => format!("'{}'", esc(s)),
        None => "NULL".to_string(),
    };
    let sql = format!(
        "INSERT INTO rvbbit.flow_steps (run_id, step_idx, stage, spec, generated_sql, rows, n_rows) \
         VALUES ('{run_id}'::uuid, {step_idx}, '{}', '{}', {}, '{}'::jsonb, {}) \
         ON CONFLICT (run_id, step_idx) DO NOTHING",
        esc(stage),
        esc(spec),
        gsql,
        rows_str,
        rows.len()
    );
    let _ = Spi::run(&sql);
}

fn rows_to_iter(rows: Vec<Value>) -> TableIterator<'static, (name!(value, JsonB),)> {
    let mapped: Vec<(JsonB,)> = rows.into_iter().map(|v| (JsonB(v),)).collect();
    TableIterator::new(mapped.into_iter())
}

fn stage_spec(stage: &Stage) -> String {
    if stage.args.is_empty() {
        stage.name.clone()
    } else {
        let args: Vec<String> = stage.args.iter().map(stage_arg_spec).collect();
        format!("{}({})", stage.name, args.join(", "))
    }
}

fn stage_arg_spec(arg: &StageArg) -> String {
    match arg {
        StageArg::Str(s) => format!("'{}'", s.replace('\'', "''")),
        StageArg::Num(n) => n.to_string(),
        StageArg::Named(name, value) => format!("{name} => {}", stage_arg_spec(value)),
    }
}

/// `SELECT * FROM rvbbit.flow($$ select … then op(…) then op2 $$)` — run the base
/// query, then pipe the rowset through each stage. Returns one jsonb object per
/// final row. Each step is recorded in rvbbit.flow_steps.
#[pg_extern(volatile)]
fn flow(spec: &str) -> TableIterator<'static, (name!(value, JsonB),)> {
    let pipeline = match parse_pipeline(spec) {
        Ok(p) => p,
        Err(e) => {
            pgrx::warning!("rvbbit.flow: {e}");
            return rows_to_iter(Vec::new());
        }
    };

    let mut rows = match run_query_to_rows(&pipeline.head) {
        Ok(r) => r,
        Err(e) => {
            pgrx::warning!("rvbbit.flow: base query failed: {e}");
            return rows_to_iter(Vec::new());
        }
    };

    let run_id = new_run_id();
    persist_step(&run_id, 0, "base", &pipeline.head, None, &rows);

    for (idx, stage) in pipeline.stages.iter().enumerate() {
        match run_stage(stage, &rows) {
            Ok((next, generated_sql)) => {
                rows = next;
                persist_step(
                    &run_id,
                    (idx + 1) as i32,
                    &stage.name,
                    &stage_spec(stage),
                    generated_sql.as_deref(),
                    &rows,
                );
            }
            Err(e) => {
                pgrx::warning!("rvbbit.flow: stage '{}' failed: {e}", stage.name);
                break;
            }
        }
    }

    rows_to_iter(rows)
}

// ---------------------------------------------------------------------------
// Unit tests for the splitter (pure; `cargo test --lib`)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod split_tests {
    use super::*;

    fn names(p: &Pipeline) -> Vec<String> {
        p.stages.iter().map(|s| s.name.clone()).collect()
    }

    #[test]
    fn no_then_is_passthrough() {
        let p = parse_pipeline("select * from t").unwrap();
        assert_eq!(p.head, "select * from t");
        assert!(p.stages.is_empty());
    }

    #[test]
    fn trailing_semicolon_stripped() {
        let p = parse_pipeline("select * from t;").unwrap();
        assert_eq!(p.head, "select * from t");
    }

    #[test]
    fn single_stage_function_args() {
        let p = parse_pipeline("select * from t then analyze('what stands out?')").unwrap();
        assert_eq!(p.head, "select * from t");
        assert_eq!(names(&p), vec!["analyze"]);
        assert_eq!(
            p.stages[0].args,
            vec![StageArg::Str("what stands out?".into())]
        );
    }

    #[test]
    fn chained_stages() {
        let p = parse_pipeline("select * from t then limit(3) then count").unwrap();
        assert_eq!(names(&p), vec!["limit", "count"]);
        assert_eq!(p.stages[0].args, vec![StageArg::Num(3.0)]);
        assert!(p.stages[1].args.is_empty());
    }

    #[test]
    fn case_then_is_not_a_split() {
        let p = parse_pipeline("select case when a then b else c end from t").unwrap();
        assert!(p.stages.is_empty(), "THEN inside CASE must not split");
        assert_eq!(p.head, "select case when a then b else c end from t");
    }

    #[test]
    fn case_then_then_pipeline() {
        let p = parse_pipeline("select case when a then b end as x from t then count").unwrap();
        assert_eq!(names(&p), vec!["count"]);
        assert!(p.head.contains("case when a then b end"));
    }

    #[test]
    fn then_inside_string_is_not_a_split() {
        let p = parse_pipeline("select 'a then b' as x from t").unwrap();
        assert!(p.stages.is_empty());
    }

    #[test]
    fn then_inside_line_comment_is_ignored() {
        let p = parse_pipeline("select 1 -- then nope\n from t then count").unwrap();
        assert_eq!(names(&p), vec!["count"]);
    }

    #[test]
    fn then_inside_subquery_case_in_parens_ignored() {
        let p = parse_pipeline("select (select case when x then 1 end) as y from t then limit(2)")
            .unwrap();
        assert_eq!(names(&p), vec!["limit"]);
        assert_eq!(p.stages[0].args, vec![StageArg::Num(2.0)]);
    }

    #[test]
    fn case_insensitive_keywords_and_names() {
        let p = parse_pipeline("SELECT * FROM t THEN Analyze('x')").unwrap();
        assert_eq!(names(&p), vec!["analyze"]);
    }

    #[test]
    fn infix_string_arg() {
        let p = parse_pipeline("select * from t then analyze 'what stands out'").unwrap();
        assert_eq!(names(&p), vec!["analyze"]);
        assert_eq!(
            p.stages[0].args,
            vec![StageArg::Str("what stands out".into())]
        );
    }

    #[test]
    fn multiple_args_and_escaped_quote() {
        let p = parse_pipeline("select * from t then pivot('by class', 'it''s grouped')").unwrap();
        assert_eq!(
            p.stages[0].args,
            vec![
                StageArg::Str("by class".into()),
                StageArg::Str("it's grouped".into())
            ]
        );
    }

    #[test]
    fn named_stage_args() {
        let p = parse_pipeline(
            "select * from t then bar_chart(x => 'status', y => 'n', title => 'By status')",
        )
        .unwrap();
        assert_eq!(names(&p), vec!["bar_chart"]);
        assert_eq!(
            p.stages[0].args,
            vec![
                StageArg::Named("x".into(), Box::new(StageArg::Str("status".into()))),
                StageArg::Named("y".into(), Box::new(StageArg::Str("n".into()))),
                StageArg::Named("title".into(), Box::new(StageArg::Str("By status".into()))),
            ]
        );
    }

    #[test]
    fn then_inside_dollar_quote_ignored() {
        let p = parse_pipeline("select $q$ a then b $q$ as x from t then count").unwrap();
        assert_eq!(names(&p), vec!["count"]);
    }
}

// ---------------------------------------------------------------------------
// Live tests against a real Postgres (`cargo pgrx test`): the full flow() path
// — split, run the base query, deterministic builtin stages, step persistence,
// and the seeded rowset operator. No model calls.
// ---------------------------------------------------------------------------

#[cfg(any(test, feature = "pg_test"))]
#[pg_schema]
mod tests {
    use pgrx::prelude::*;

    #[pg_test]
    fn flow_passthrough_returns_all_rows() {
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.flow($q$ select g from generate_series(1,5) g $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 5);
    }

    #[pg_test]
    fn flow_pass_is_identity() {
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.flow($q$ select g from generate_series(1,7) g then pass $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 7);
    }

    #[pg_test]
    fn flow_limit_then_count() {
        let v: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ select g from generate_series(1,10) g then limit(3) then count $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(v.0.get("n").and_then(|x| x.as_i64()), Some(3));
    }

    #[pg_test]
    fn flow_persists_steps() {
        Spi::run(
            "SELECT count(*) FROM rvbbit.flow($q$ select g from generate_series(1,4) g then limit(2) then count $q$)",
        )
        .unwrap();
        // base(0), limit(1), count(2) for the run we just executed.
        let steps: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.flow_steps \
             WHERE run_id = (SELECT run_id FROM rvbbit.flow_steps ORDER BY created_at DESC LIMIT 1)",
        )
        .unwrap()
        .unwrap_or(0);
        assert_eq!(steps, 3, "expected 3 persisted steps");
    }

    #[pg_test]
    fn analyze_operator_is_registered_rowset() {
        let shape: String =
            Spi::get_one("SELECT shape FROM rvbbit.operators WHERE name = 'analyze'")
                .unwrap()
                .unwrap();
        assert_eq!(shape, "rowset");
    }

    #[pg_test]
    fn rowset_operators_are_seeded() {
        let synth: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.operators \
             WHERE name IN ('pivot','group','top','filter','normalize') AND shape='rowset' AND parser='sql'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(synth, 5, "synth-sql rowset operators");
        let value: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.operators \
             WHERE name IN ('analyze','enrich') AND shape='rowset' AND parser='json'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(value, 2, "value-mode rowset operators");
    }

    #[pg_test]
    fn visual_rowset_operators_are_seeded() {
        let value: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.operators \
             WHERE name IN ('metric_card','bar_chart','table_view','vega_lite','filter_control') \
               AND shape='rowset' AND parser='json'",
        )
        .unwrap()
        .unwrap();
        assert_eq!(value, 5, "visual/control artifact rowset operators");
    }

    #[pg_test]
    fn metric_card_emits_ui_artifact_rows() {
        let v: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ \
             select 'Revenue'::text as label, 123::int as value \
             then metric_card(label => 'label', value => 'value', title => 'Revenue') \
             $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            v.0.get("rvbbit_artifact").and_then(|x| x.as_str()),
            Some("ui")
        );
        assert_eq!(
            v.0.get("renderer").and_then(|x| x.as_str()),
            Some("metric_card")
        );
        assert_eq!(
            v.0.pointer("/spec/value").and_then(|x| x.as_i64()),
            Some(123)
        );
    }

    #[pg_test]
    fn bar_chart_named_args_emit_vega_lite_artifact() {
        let v: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ \
             select * from (values ('Todo', 2), ('Done', 5)) v(status, n) \
             then bar_chart(x => 'status', y => 'n', title => 'Issues') \
             $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            v.0.get("rvbbit_artifact").and_then(|x| x.as_str()),
            Some("ui")
        );
        assert_eq!(
            v.0.get("renderer").and_then(|x| x.as_str()),
            Some("vega_lite")
        );
        assert_eq!(
            v.0.pointer("/spec/encoding/x/field")
                .and_then(|x| x.as_str()),
            Some("status")
        );
        assert_eq!(
            v.0.pointer("/spec/encoding/y/field")
                .and_then(|x| x.as_str()),
            Some("n")
        );
    }

    #[pg_test]
    fn filter_control_named_args_emit_control_artifact() {
        let v: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ \
             select * from (values ('Todo'), ('Done')) v(status) \
             then filter_control(field => 'status', kind => 'dropdown', title => 'Status') \
             $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            v.0.get("rvbbit_artifact").and_then(|x| x.as_str()),
            Some("ui")
        );
        assert_eq!(
            v.0.get("artifact_kind").and_then(|x| x.as_str()),
            Some("control")
        );
        assert_eq!(
            v.0.get("renderer").and_then(|x| x.as_str()),
            Some("filter_control")
        );
        assert_eq!(
            v.0.pointer("/spec/field").and_then(|x| x.as_str()),
            Some("status")
        );
        assert_eq!(
            v.0.pointer("/bindings/param/operator")
                .and_then(|x| x.as_str()),
            Some("in")
        );
    }

    #[pg_test]
    fn flow_sample_spreads_rows() {
        // sample(3) of 9 rows -> 3 evenly-spread rows (indices 0,3,6 -> g 1,4,7)
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.flow($q$ select g from generate_series(1,9) g then sample(3) $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 3);
        let first: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ select g from generate_series(1,9) g then sample(3) $q$) ORDER BY (value->>'g')::int LIMIT 1",
        )
        .unwrap()
        .unwrap();
        assert_eq!(first.0.get("g").and_then(|x| x.as_i64()), Some(1));
    }

    #[pg_test]
    fn shape_fingerprint_is_order_independent() {
        let a: String = Spi::get_one(
            "SELECT rvbbit.flow_shape('[{\"class\":\"A\"},{\"class\":\"B\"}]'::jsonb)",
        )
        .unwrap()
        .unwrap();
        let b: String = Spi::get_one(
            "SELECT rvbbit.flow_shape('[{\"class\":\"B\"},{\"class\":\"A\"}]'::jsonb)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(a, b, "fingerprint must be independent of row/value order");
    }

    #[pg_test]
    fn shape_fingerprint_differs_by_schema() {
        let a: String = Spi::get_one("SELECT rvbbit.flow_shape('[{\"class\":\"A\"}]'::jsonb)")
            .unwrap()
            .unwrap();
        let b: String = Spi::get_one(
            "SELECT rvbbit.flow_shape('[{\"class\":\"A\",\"season\":\"Spring\"}]'::jsonb)",
        )
        .unwrap()
        .unwrap();
        assert_ne!(a, b, "different schema must give a different fingerprint");
    }

    #[pg_test]
    fn synth_put_then_flow_uses_cached_sql_no_model() {
        // Pin a generated snippet for the shape {class text; distinct A,B} + prompt.
        Spi::run(
            "SELECT rvbbit.synth_put('pivot', 'rowcounts by class', \
             '[{\"class\":\"A\"},{\"class\":\"B\"}]'::jsonb, \
             'SELECT class, count(*) AS n FROM _input GROUP BY class ORDER BY class')",
        )
        .unwrap();
        // A flow whose rowset has the SAME shape hits the cache and runs the SQL
        // deterministically (no model call).
        let first: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ select class from (values ('A'),('A'),('B')) v(class) then pivot('rowcounts by class') $q$) \
             ORDER BY value->>'class' LIMIT 1",
        )
        .unwrap()
        .unwrap();
        assert_eq!(first.0.get("class").and_then(|x| x.as_str()), Some("A"));
        assert_eq!(first.0.get("n").and_then(|x| x.as_i64()), Some(2));
    }

    #[pg_test]
    fn synth_generated_sql_is_recorded_per_step() {
        Spi::run(
            "SELECT rvbbit.synth_put('pivot', 'rc2', '[{\"class\":\"A\"},{\"class\":\"B\"}]'::jsonb, \
             'SELECT class, count(*) AS n FROM _input GROUP BY class')",
        )
        .unwrap();
        Spi::run(
            "SELECT count(*) FROM rvbbit.flow($q$ select class from (values ('A'),('A'),('B')) v(class) then pivot('rc2') $q$)",
        )
        .unwrap();
        let gsql: String = Spi::get_one(
            "SELECT generated_sql FROM rvbbit.flow_steps \
             WHERE stage = 'pivot' AND run_id = (SELECT run_id FROM rvbbit.flow_steps ORDER BY created_at DESC LIMIT 1)",
        )
        .unwrap()
        .unwrap_or_default();
        assert!(
            gsql.contains("GROUP BY class"),
            "generated_sql not recorded: {gsql}"
        );
    }

    #[pg_test]
    fn synth_bad_cached_sql_fails_stage_gracefully() {
        // A cached SQL that references a missing column must fail the stage (via
        // the PgTry subtransaction) without poisoning the surrounding query: flow
        // returns the prior (base) rowset.
        Spi::run(
            "SELECT rvbbit.synth_put('pivot', 'oops', '[{\"class\":\"A\"},{\"class\":\"B\"}]'::jsonb, \
             'SELECT nonexistent_col FROM _input')",
        )
        .unwrap();
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.flow($q$ select class from (values ('A'),('B')) v(class) then pivot('oops') $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            n, 2,
            "flow should return the base rowset when a stage fails"
        );
    }

    #[pg_test]
    fn value_shape_groups_by_format() {
        let a: String = Spi::get_one("SELECT rvbbit.value_shape('(303) 555-1234')")
            .unwrap()
            .unwrap();
        let b: String = Spi::get_one("SELECT rvbbit.value_shape('(720) 867-5309')")
            .unwrap()
            .unwrap();
        assert_eq!(a, b, "same format -> same shape");
        assert_eq!(a, "(ddd) ddd-dddd");
        let c: String = Spi::get_one("SELECT rvbbit.value_shape('303-555-1234')")
            .unwrap()
            .unwrap();
        assert_ne!(a, c, "different format -> different shape");
    }

    #[pg_test]
    fn reshape_uses_cached_expression_per_shape_no_model() {
        // Pin an expression for the (ddd) ddd-dddd phone shape.
        Spi::run(
            "SELECT rvbbit.synth_put_scalar('reshape', 'digits only', '(303) 555-1234', \
             'regexp_replace(x, ''[^0-9]'', '''', ''g'')')",
        )
        .unwrap();
        // A different value of the SAME shape -> cache hit -> deterministic apply.
        let out: String = Spi::get_one("SELECT rvbbit.reshape('(720) 867-5309', 'digits only')")
            .unwrap()
            .unwrap();
        assert_eq!(out, "7208675309");
    }

    #[pg_test]
    fn parse_and_normalize_value_use_cached_expression_per_shape_no_model() {
        Spi::run(
            "SELECT rvbbit.synth_put_scalar('parse', 'extract phone digits', '(303) 555-1234', \
             'regexp_replace(x, ''[^0-9]'', '''', ''g'')')",
        )
        .unwrap();
        let parsed: String =
            Spi::get_one("SELECT rvbbit.parse('(720) 867-5309', 'extract phone digits')")
                .unwrap()
                .unwrap();
        assert_eq!(parsed, "7208675309");

        Spi::run(
            "SELECT rvbbit.synth_put_scalar('normalize_value', 'lowercase and trim', '  ACME  ', \
             'lower(btrim(x))')",
        )
        .unwrap();
        let normalized: String =
            Spi::get_one("SELECT rvbbit.normalize_value('  BETA  ', 'lowercase and trim')")
                .unwrap()
                .unwrap();
        assert_eq!(normalized, "beta");
    }

    #[pg_test]
    fn normalize_rowset_stage_uses_cached_sql_no_model() {
        Spi::run(
            "SELECT rvbbit.synth_put('normalize', 'phone digits', \
             '[{\"id\":1,\"phone\":\"(303) 555-1234\"},{\"id\":2,\"phone\":\"(720) 867-5309\"}]'::jsonb, \
             'SELECT id, regexp_replace(phone, ''[^0-9]'', '''', ''g'') AS phone_digits FROM _input ORDER BY id')",
        )
        .unwrap();
        let first: pgrx::JsonB = Spi::get_one(
            "SELECT value FROM rvbbit.flow($q$ select * from (values (1, '(303) 555-1234'), (2, '(720) 867-5309')) v(id, phone) then normalize('phone digits') $q$) \
             ORDER BY (value->>'id')::int LIMIT 1",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            first.0.get("phone_digits").and_then(|x| x.as_str()),
            Some("3035551234")
        );
        let gsql: String = Spi::get_one(
            "SELECT generated_sql FROM rvbbit.flow_steps \
             WHERE stage = 'normalize' AND run_id = (SELECT run_id FROM rvbbit.flow_steps ORDER BY created_at DESC LIMIT 1)",
        )
        .unwrap()
        .unwrap_or_default();
        assert!(
            gsql.contains("regexp_replace"),
            "generated_sql not recorded: {gsql}"
        );
    }

    // ---- Query synth-sql (table-shaped text-to-SQL, Phase 1) ----

    #[pg_test]
    fn synth_validate_accepts_select_rejects_bad_and_writes() {
        Spi::run("CREATE TABLE synth_demo_t (id int, label text)").unwrap();
        Spi::run("INSERT INTO synth_demo_t VALUES (1, 'a'), (2, 'b')").unwrap();
        // A valid read-only SELECT passes; an unknown column is rejected.
        assert!(crate::synth::validate_sql("SELECT id, label FROM synth_demo_t").is_ok());
        assert!(crate::synth::validate_sql("SELECT nope FROM synth_demo_t").is_err());
        // Writes are rejected (read-only only) — including data-modifying CTEs —
        // and validation rolls back, so the rows are untouched.
        assert!(crate::synth::validate_sql("DELETE FROM synth_demo_t").is_err());
        assert!(crate::synth::validate_sql(
            "WITH t AS (DELETE FROM synth_demo_t RETURNING *) SELECT * FROM t"
        )
        .is_err());
        let n: i64 = Spi::get_one("SELECT count(*)::bigint FROM synth_demo_t")
            .unwrap()
            .unwrap();
        assert_eq!(n, 2, "validation must not execute writes");
    }

    #[pg_test]
    fn information_schema_context_lists_user_tables() {
        Spi::run("CREATE TABLE synth_ctx_t (a int, b text)").unwrap();
        let ctx = crate::synth::information_schema_context(200);
        assert!(
            ctx.contains("synth_ctx_t"),
            "fallback context should list the user table; got: {ctx}"
        );
        assert!(
            ctx.contains("a integer"),
            "fallback context should include column types; got: {ctx}"
        );
    }

    #[pg_test]
    fn synth_execute_runs_select_and_caps_rows() {
        Spi::run("CREATE TABLE synth_exec_t (a int)").unwrap();
        Spi::run("INSERT INTO synth_exec_t SELECT g FROM generate_series(1, 5) g").unwrap();
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit._synth_execute('SELECT a FROM synth_exec_t', 3, 5000)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 3, "row cap should apply");
        let v: pgrx::JsonB = Spi::get_one(
            "SELECT v FROM rvbbit._synth_execute('SELECT a FROM synth_exec_t ORDER BY a', 1, 5000) AS v",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            v.0.get("a").and_then(|x| x.as_i64()),
            Some(1),
            "rows come back as jsonb"
        );
    }

    #[pg_test]
    fn synth_execute_blocks_writes_read_only() {
        Spi::run("CREATE TABLE synth_exec_w (a int)").unwrap();
        Spi::run("INSERT INTO synth_exec_w VALUES (1), (2), (3)").unwrap();
        // A write that reaches execution is blocked by the read-only guard → no rows,
        // and the table is untouched.
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit._synth_execute('WITH d AS (DELETE FROM synth_exec_w RETURNING *) SELECT * FROM d', 100, 5000)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            n, 0,
            "a write must produce no rows under the read-only guard"
        );
        let left: i64 = Spi::get_one("SELECT count(*)::bigint FROM synth_exec_w")
            .unwrap()
            .unwrap();
        assert_eq!(left, 3, "the read-only guard must prevent the delete");
    }

    #[pg_test]
    fn synth_disabled_returns_no_rows() {
        // rvbbit.synth_enabled defaults off → synth() is a no-op (and makes no model call).
        let n: i64 = Spi::get_one("SELECT count(*)::bigint FROM rvbbit.synth('anything at all')")
            .unwrap()
            .unwrap();
        assert_eq!(
            n, 0,
            "synth must be a no-op while rvbbit.synth_enabled is off"
        );
    }

    #[pg_test]
    fn synth_schema_captures_authoritative_types() {
        Spi::run("CREATE TABLE ss_t (id int, label text)").unwrap();
        Spi::run("INSERT INTO ss_t VALUES (1, 'a')").unwrap();
        // Parse-only capture: names + Postgres types, no execution.
        let schema: pgrx::JsonB = Spi::get_one(
            "SELECT rvbbit._synth_schema('SELECT id, label, count(*) AS n FROM ss_t GROUP BY id, label')",
        )
        .unwrap()
        .unwrap();
        let arr = schema.0.as_array().cloned().unwrap_or_default();
        let names: Vec<String> = arr
            .iter()
            .filter_map(|c| c.get("name").and_then(|v| v.as_str()).map(String::from))
            .collect();
        assert_eq!(names, vec!["id", "label", "n"]);
        let n_type = arr
            .iter()
            .find(|c| c.get("name").and_then(|v| v.as_str()) == Some("n"))
            .and_then(|c| c.get("type"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        assert_eq!(
            n_type, "bigint",
            "count(*) result type is authoritative bigint"
        );
        // Bad SQL → empty array, gracefully.
        let bad: pgrx::JsonB =
            Spi::get_one("SELECT rvbbit._synth_schema('SELECT nope FROM nowhere')")
                .unwrap()
                .unwrap();
        assert_eq!(bad.0.as_array().map(|a| a.len()), Some(0));
    }

    // ---- Phase C: typed synth relations ----

    #[pg_test]
    fn synth_coldef_formats_schema() {
        // quote_ident leaves simple lowercase identifiers unquoted; both forms are
        // valid in an AS column list. A name needing quoting (e.g. a space) is quoted.
        let cd: String = Spi::get_one(
            r#"SELECT rvbbit._synth_coldef_from_schema('[{"name":"season","type":"text"},{"name":"n","type":"bigint"}]'::jsonb)"#,
        )
        .unwrap()
        .unwrap();
        assert_eq!(cd, "season text, n bigint");
        let cd2: String = Spi::get_one(
            r#"SELECT rvbbit._synth_coldef_from_schema('[{"name":"my col","type":"numeric"}]'::jsonb)"#,
        )
        .unwrap()
        .unwrap();
        assert_eq!(cd2, "\"my col\" numeric");
    }

    #[pg_test]
    fn synth_exec_record_returns_typed_columns() {
        // The read-only record executor yields real typed columns (caller supplies AS).
        let a: i32 = Spi::get_one(
            "SELECT a FROM rvbbit._synth_exec_record('SELECT 7 AS a, ''hi''::text AS b') AS r(a int, b text)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(a, 7);
        let b: String = Spi::get_one(
            "SELECT b FROM rvbbit._synth_exec_record('SELECT 7 AS a, ''hi''::text AS b') AS r(a int, b text)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(b, "hi");
    }

    // ---- Phase D: flow single-jsonb-head unwrap ----

    #[pg_test]
    fn flow_unwraps_single_jsonb_head() {
        // A head yielding ONE jsonb-object column (like synth) is unwrapped so stages
        // see the inner fields. Here the head is `select to_jsonb(v) AS value …`, one
        // jsonb column of {"class": …} objects — flow must surface 'class', not 'value'.
        let n: i64 = Spi::get_one(
            "SELECT count(*)::bigint FROM rvbbit.flow($q$ select to_jsonb(v) AS value from (values ('A'),('B')) v(class) $q$)",
        )
        .unwrap()
        .unwrap();
        assert_eq!(n, 2);
        let saw_class: bool = Spi::get_one(
            "SELECT bool_and((value->>'class') IS NOT NULL) FROM rvbbit.flow($q$ select to_jsonb(v) AS value from (values ('A'),('B')) v(class) $q$)",
        )
        .unwrap()
        .unwrap();
        assert!(
            saw_class,
            "the single-jsonb head must be unwrapped to its inner fields"
        );
    }

    #[pg_test]
    fn flow_leaves_multicolumn_head_untouched() {
        // A normal multi-column head is NOT unwrapped — rows keep their columns.
        let saw_g: bool = Spi::get_one(
            "SELECT bool_and((value->>'g') IS NOT NULL) FROM rvbbit.flow($q$ select g, g*2 AS d from generate_series(1,3) g $q$)",
        )
        .unwrap()
        .unwrap();
        assert!(saw_g, "ordinary multi-column heads must be left as-is");
    }
}
