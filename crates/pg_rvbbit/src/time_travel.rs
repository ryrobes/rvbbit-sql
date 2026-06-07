//! Statement-scoped time-travel helpers.
//!
//! PostgreSQL will not parse `FROM t AS OF ...` without grammar changes, so
//! rvbbit exposes a normal-SQL comment directive:
//!
//!   /* rvbbit: as_of = '2026-05-28 02:25:00+00' */
//!   SELECT ...
//!
//! The planner hook seeds the backend-local directive from the original
//! statement text. Executors resolve that timestamp to the table's generation
//! at scan begin. The low-level `rvbbit.as_of_generation` GUC remains supported
//! for debugging and exact generation reads.

use std::cell::{Cell, RefCell};
use std::ffi::{CStr, CString};

use pgrx::pg_guard;
use pgrx::{pg_sys, Spi};

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
pub(crate) enum AsOf {
    Generation(i64),
    Timestamp(String),
}

thread_local! {
    static STATEMENT_AS_OF_TIMESTAMP: RefCell<Option<String>> = const { RefCell::new(None) };
    static PLANNER_DEPTH: Cell<usize> = const { Cell::new(0) };
    static EXECUTOR_DEPTH: Cell<usize> = const { Cell::new(0) };
}

static mut PREV_EXECUTOR_START_HOOK: pg_sys::ExecutorStart_hook_type = None;
static mut PREV_EXECUTOR_END_HOOK: pg_sys::ExecutorEnd_hook_type = None;

pub(crate) unsafe fn register_hooks() {
    PREV_EXECUTOR_START_HOOK = pg_sys::ExecutorStart_hook;
    PREV_EXECUTOR_END_HOOK = pg_sys::ExecutorEnd_hook;
    pg_sys::ExecutorStart_hook = Some(rvbbit_asof_executor_start_hook);
    pg_sys::ExecutorEnd_hook = Some(rvbbit_asof_executor_end_hook);
}

pub(crate) struct PlannerScope;

impl Drop for PlannerScope {
    fn drop(&mut self) {
        PLANNER_DEPTH.with(|depth| depth.set(depth.get().saturating_sub(1)));
    }
}

pub(crate) fn planner_scope(query_string: *const std::ffi::c_char) -> PlannerScope {
    let outer_planner = PLANNER_DEPTH.with(|depth| {
        let current = depth.get();
        depth.set(current.saturating_add(1));
        current == 0
    });
    let inside_executor = EXECUTOR_DEPTH.with(|depth| depth.get() > 0);
    let directive = query_string_asof_timestamp(query_string);
    if outer_planner && !inside_executor {
        set_statement_timestamp(directive);
    } else if let Some(ts) = directive {
        set_statement_timestamp(Some(ts));
    }
    PlannerScope
}

pub(crate) fn has_as_of_timestamp_directive(sql: &str) -> bool {
    extract_as_of_timestamp_directive(sql).is_some()
}

pub(crate) fn active_as_of() -> Option<AsOf> {
    if let Some(ts) = STATEMENT_AS_OF_TIMESTAMP.with(|slot| slot.borrow().clone()) {
        return Some(AsOf::Timestamp(ts));
    }
    read_generation_guc().map(AsOf::Generation)
}

pub(crate) fn active_as_of_enabled() -> bool {
    active_as_of().is_some()
}

pub(crate) fn generation_for_table(
    table_oid: u32,
    asof: Option<&AsOf>,
) -> Result<Option<i64>, String> {
    match asof {
        Some(AsOf::Generation(g)) => Ok(Some(*g)),
        Some(AsOf::Timestamp(ts)) => resolve_timestamp_generation(table_oid, ts).map(Some),
        None => Ok(None),
    }
}

/// SQL fragment that is true exactly for SNAPSHOT-mode tables. A table is in
/// snapshot mode iff `min_visible_generation > 0` — only `rvbbit.snapshot_load`
/// sets that, and it means each generation is a COMPLETE table snapshot (full
/// trunc+load) rather than an append/delta. Snapshot mode flips time-travel
/// from cumulative `<= G` to exact `= G` (you want the snapshot in effect at G,
/// not the union of every prior snapshot).
fn is_snapshot_expr(table_oid_expr: &str) -> String {
    format!(
        "coalesce((SELECT t.min_visible_generation FROM rvbbit.tables t \
                     WHERE t.table_oid = {table_oid_expr}), 0) > 0"
    )
}

/// AS-OF row-group predicate for a resolved generation expression `g_expr`.
/// Append tables: `generation <= g` (cumulative). Snapshot tables: `= g`
/// (exact — each generation is a full snapshot). Used by both read engines.
pub(crate) fn asof_gen_predicate(
    g_expr: &str,
    table_oid_expr: &str,
    generation_expr: &str,
) -> String {
    let is_snap = is_snapshot_expr(table_oid_expr);
    format!(
        "AND {generation_expr} <= {g_expr} \
         AND (NOT ({is_snap}) OR {generation_expr} = {g_expr})"
    )
}

pub(crate) fn row_group_predicate(
    asof: &AsOf,
    table_oid_expr: &str,
    generation_expr: &str,
) -> String {
    match asof {
        AsOf::Generation(g) => asof_gen_predicate(&g.to_string(), table_oid_expr, generation_expr),
        AsOf::Timestamp(ts) => {
            let lit = sql_text_literal(ts);
            let g_expr = format!(
                "coalesce((SELECT max(g.generation)::bigint \
                    FROM rvbbit.generations g \
                   WHERE g.table_oid = {table_oid_expr} \
                     AND g.committed_at <= {lit}::timestamptz), 0)"
            );
            asof_gen_predicate(&g_expr, table_oid_expr, generation_expr)
        }
    }
}

/// Latest-view (no AS OF) predicate. Append tables: all generations (no
/// restriction). Snapshot tables: ONLY the current snapshot generation
/// (`generation = min_visible_generation`), so the latest view is the newest
/// snapshot, not the union of every retained one. Both read engines call this
/// when `active_as_of()` is None so they can't diverge. The floor pointer is
/// set even for an empty (0-row) snapshot, so an emptied source correctly shows
/// nothing at latest.
pub(crate) fn latest_predicate(table_oid_expr: &str, generation_expr: &str) -> String {
    let floor = format!(
        "coalesce((SELECT t.min_visible_generation FROM rvbbit.tables t \
                     WHERE t.table_oid = {table_oid_expr}), 0)"
    );
    format!("AND ({floor} = 0 OR {generation_expr} = {floor})")
}

pub(crate) fn tombstone_predicate(
    asof: &AsOf,
    table_oid_expr: &str,
    deleted_generation_expr: &str,
) -> String {
    match asof {
        AsOf::Generation(g) => format!("AND {deleted_generation_expr} <= {g}"),
        AsOf::Timestamp(ts) => {
            let lit = sql_text_literal(ts);
            format!(
                "AND {deleted_generation_expr} <= coalesce(\
                 (SELECT max(g.generation)::bigint \
                    FROM rvbbit.generations g \
                   WHERE g.table_oid = {table_oid_expr} \
                     AND g.committed_at <= {lit}::timestamptz), 0)"
            )
        }
    }
}

pub(crate) fn label(asof: &AsOf) -> String {
    match asof {
        AsOf::Generation(g) => format!("generation {g}"),
        AsOf::Timestamp(ts) => format!("timestamp {ts}"),
    }
}

fn read_generation_guc() -> Option<i64> {
    let cname = CString::new("rvbbit.as_of_generation").ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        return None;
    }
    let raw = unsafe { CStr::from_ptr(ptr).to_string_lossy() };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    trimmed.parse::<i64>().ok().filter(|&g| g > 0)
}

fn query_string_asof_timestamp(query_string: *const std::ffi::c_char) -> Option<String> {
    if query_string.is_null() {
        return None;
    }
    let sql = unsafe { CStr::from_ptr(query_string).to_string_lossy().into_owned() };
    extract_as_of_timestamp_directive(&sql)
}

fn set_statement_timestamp(value: Option<String>) {
    STATEMENT_AS_OF_TIMESTAMP.with(|slot| {
        *slot.borrow_mut() = value;
    });
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_asof_executor_start_hook(
    query_desc: *mut pg_sys::QueryDesc,
    eflags: std::ffi::c_int,
) {
    let outer_executor = EXECUTOR_DEPTH.with(|depth| {
        let current = depth.get();
        depth.set(current.saturating_add(1));
        current == 0
    });
    if outer_executor {
        let directive = if query_desc.is_null() {
            None
        } else {
            query_string_asof_timestamp((*query_desc).sourceText)
        };
        set_statement_timestamp(directive);
    }
    if let Some(prev) = PREV_EXECUTOR_START_HOOK {
        prev(query_desc, eflags);
    } else {
        pg_sys::standard_ExecutorStart(query_desc, eflags);
    }
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_asof_executor_end_hook(query_desc: *mut pg_sys::QueryDesc) {
    if let Some(prev) = PREV_EXECUTOR_END_HOOK {
        prev(query_desc);
    } else {
        pg_sys::standard_ExecutorEnd(query_desc);
    }
    let finished_outer = EXECUTOR_DEPTH.with(|depth| {
        let next = depth.get().saturating_sub(1);
        depth.set(next);
        next == 0
    });
    if finished_outer {
        set_statement_timestamp(None);
    }
}

fn resolve_timestamp_generation(table_oid: u32, timestamp: &str) -> Result<i64, String> {
    let lit = sql_text_literal(timestamp);
    Spi::get_one::<i64>(&format!(
        "SELECT coalesce(max(generation), 0)::bigint \
         FROM rvbbit.generations \
         WHERE table_oid = {table_oid}::oid \
           AND committed_at <= {lit}::timestamptz"
    ))
    .map_err(|e| format!("resolve AS OF timestamp {timestamp:?}: {e}"))
    .map(|value| value.unwrap_or(0))
}

fn sql_text_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn extract_as_of_timestamp_directive(sql: &str) -> Option<String> {
    let mut rest = sql.trim_start();
    loop {
        if let Some(after) = rest.strip_prefix("/*") {
            let end = after.find("*/")?;
            let body = &after[..end];
            if let Some(value) = parse_comment_directive(body) {
                return Some(value);
            }
            rest = after[end + 2..].trim_start();
            continue;
        }
        if let Some(after) = rest.strip_prefix("--") {
            let (body, tail) = match after.find('\n') {
                Some(idx) => (&after[..idx], &after[idx + 1..]),
                None => (after, ""),
            };
            if let Some(value) = parse_comment_directive(body) {
                return Some(value);
            }
            rest = tail.trim_start();
            continue;
        }
        return None;
    }
}

fn parse_comment_directive(comment: &str) -> Option<String> {
    let lower = comment.to_ascii_lowercase();
    let rvbbit_pos = lower.find("rvbbit")?;
    let tail = &comment[rvbbit_pos + "rvbbit".len()..];
    let lower_tail = tail.to_ascii_lowercase();
    let key_pos = lower_tail
        .find("as_of_timestamp")
        .or_else(|| lower_tail.find("as_of"))?;
    let mut value = &tail[key_pos..];
    if value.to_ascii_lowercase().starts_with("as_of_timestamp") {
        value = &value["as_of_timestamp".len()..];
    } else {
        value = &value["as_of".len()..];
    }
    value = value.trim_start();
    if value.starts_with('=') || value.starts_with(':') {
        value = value[1..].trim_start();
    }
    parse_directive_value(value)
}

fn parse_directive_value(value: &str) -> Option<String> {
    let value = value.trim_start();
    if value.is_empty() {
        return None;
    }
    let first = value.chars().next()?;
    if first == '\'' || first == '"' {
        let mut escaped = false;
        let mut out = String::new();
        for ch in value[first.len_utf8()..].chars() {
            if escaped {
                out.push(ch);
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == first {
                return non_empty(out);
            } else {
                out.push(ch);
            }
        }
        return None;
    }

    let end = value
        .find(|ch: char| ch == ',' || ch == ';' || ch == '\n' || ch == '\r')
        .unwrap_or(value.len());
    non_empty(value[..end].trim().to_string())
}

fn non_empty(value: String) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}
