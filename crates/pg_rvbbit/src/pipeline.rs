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
}

impl StageArg {
    fn to_value(&self) -> Value {
        match self {
            StageArg::Str(s) => Value::String(s.clone()),
            StageArg::Num(n) => json!(n),
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
            args.push(parse_single_arg(seg)?);
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
    let sql = format!("SELECT to_jsonb(t) FROM ({}) t", head.trim().trim_end_matches(';'));
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
}

fn take_rows(rows: &[Value], n: usize) -> Vec<Value> {
    rows.iter().take(n).cloned().collect()
}

/// Run one stage against the current rowset. Inline builtins are deterministic
/// (no model call); everything else dispatches to a `shape='rowset'` operator.
fn run_stage(stage: &Stage, rows: &[Value]) -> Result<Vec<Value>, String> {
    match stage.name.as_str() {
        "pass" => Ok(rows.to_vec()),
        "count" => Ok(vec![json!({ "n": rows.len() })]),
        "limit" | "head" => {
            let n = match stage.args.first() {
                Some(StageArg::Num(n)) => *n as usize,
                Some(StageArg::Str(s)) => s.trim().parse::<usize>().unwrap_or(10),
                None => 10,
            };
            Ok(take_rows(rows, n))
        }
        _ => {
            let pos_args: Vec<Value> = stage.args.iter().map(StageArg::to_value).collect();
            crate::operators::run_rowset_op(&stage.name, rows, &pos_args, &json!({}))
        }
    }
}

fn new_run_id() -> String {
    Spi::get_one::<String>("SELECT gen_random_uuid()::text")
        .ok()
        .flatten()
        .unwrap_or_default()
}

/// Best-effort persistence of one step's rowset to rvbbit.flow_steps.
fn persist_step(run_id: &str, step_idx: i32, stage: &str, spec: &str, rows: &[Value]) {
    if run_id.is_empty() {
        return;
    }
    let esc = |s: &str| s.replace('\'', "''");
    let rows_str = esc(&Value::Array(rows.to_vec()).to_string());
    let sql = format!(
        "INSERT INTO rvbbit.flow_steps (run_id, step_idx, stage, spec, rows, n_rows) \
         VALUES ('{run_id}'::uuid, {step_idx}, '{}', '{}', '{}'::jsonb, {}) \
         ON CONFLICT (run_id, step_idx) DO NOTHING",
        esc(stage),
        esc(spec),
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
        let args: Vec<String> = stage
            .args
            .iter()
            .map(|a| match a {
                StageArg::Str(s) => format!("'{}'", s.replace('\'', "''")),
                StageArg::Num(n) => n.to_string(),
            })
            .collect();
        format!("{}({})", stage.name, args.join(", "))
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
    persist_step(&run_id, 0, "base", &pipeline.head, &rows);

    for (idx, stage) in pipeline.stages.iter().enumerate() {
        match run_stage(stage, &rows) {
            Ok(next) => {
                rows = next;
                persist_step(&run_id, (idx + 1) as i32, &stage.name, &stage_spec(stage), &rows);
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
        assert_eq!(p.stages[0].args, vec![StageArg::Str("what stands out?".into())]);
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
        let p =
            parse_pipeline("select case when a then b end as x from t then count").unwrap();
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
        let p = parse_pipeline(
            "select (select case when x then 1 end) as y from t then limit(2)",
        )
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
        assert_eq!(p.stages[0].args, vec![StageArg::Str("what stands out".into())]);
    }

    #[test]
    fn multiple_args_and_escaped_quote() {
        let p =
            parse_pipeline("select * from t then pivot('by class', 'it''s grouped')").unwrap();
        assert_eq!(
            p.stages[0].args,
            vec![
                StageArg::Str("by class".into()),
                StageArg::Str("it's grouped".into())
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
}
