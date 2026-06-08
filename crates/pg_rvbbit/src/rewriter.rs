//! Phase 5: Query rewriter.
//!
//! Architectural note (designed to support the full Lars-style operator
//! system later — see session discussion):
//!
//!     A single walker, run from post_parse_analyze_hook, matches
//!     expression sub-trees against rule families in priority order:
//!
//!     1. const-from-metadata  (count(*) -> Const, etc.)
//!     2. shred substitution    (response->>'foo' -> Var(x_response_foo))
//!     3. semantic operators    (description MEANS 'x' -> _op_means(...))
//!
//! All three are the same primitive: "match expression pattern, replace
//! with another expression". They share one walker, one catalog
//! (rvbbit.shreds / rvbbit.operators), one inline-directive parser
//! (`-- @ model: ...`).
//!
//! R2b polish (this revision): support nested jsonb paths AND wrapping
//! casts. The user's expression `(response->'usage'->>'input_tokens')::int`
//! becomes `Var(x_response_input_tokens, int4)` — the whole cast subtree
//! is replaced.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::ffi::{c_void, CStr, CString};
use std::str::FromStr;

use pgrx::pg_extern;
use pgrx::pg_guard;
use pgrx::pg_sys;
use pgrx::IntoDatum;
use pgrx::PgMemoryContexts;
use rvbbit_storage::metadata::ColumnStats;
use serde_json::Value;

use crate::scan::{group_count_map, scan_numeric_sum_count, NumericScan};
use crate::{duck_backend, route_log, router};

static mut PREV_POST_PARSE_ANALYZE_HOOK: pg_sys::post_parse_analyze_hook_type = None;

thread_local! {
    static IN_REWRITER: Cell<bool> = const { Cell::new(false) };
    static DUCK_REWRITE_DISABLED: Cell<bool> = const { Cell::new(false) };
    static DUCK_ROUTE_CACHE: RefCell<HashMap<String, Value>> = RefCell::new(HashMap::new());
    static NATIVE_REWRITE_CACHE: RefCell<HashMap<String, NativeRewriteCacheEntry>> =
        RefCell::new(HashMap::new());
    static NUMERIC_STATS_CACHE: RefCell<HashMap<NumericStatsCacheKey, HashMap<String, NumericScan>>> =
        RefCell::new(HashMap::new());
    /// Lazy per-table cache of shred mappings, loaded from rvbbit.shreds
    /// on first use. R3a: refreshed only on backend restart or
    /// `rvbbit_reset_shred_cache()` (TODO). When compact() adds new shred
    /// rows, currently-open backends won't see them until reset.
    static SHRED_CACHE: RefCell<HashMap<u32, Vec<ShredEntry>>> = RefCell::new(HashMap::new());
    /// Current Query being rewritten — needed by try_shred_rule to
    /// translate Var.varno → table_oid via rtable.
    static CURRENT_QUERY: Cell<*mut pg_sys::Query> =
        const { Cell::new(std::ptr::null_mut()) };
    /// Source SQL for the statement currently passing through the
    /// post-parse hook. Implicit semantic prewarm uses this only for narrow
    /// single-table SELECTs so the prewarm query can preserve WHERE /
    /// ORDER BY / LIMIT text without hand-deparsing every expression shape.
    static CURRENT_SOURCE_SQL: RefCell<Option<String>> = const { RefCell::new(None) };
}

#[derive(Clone, Hash, PartialEq, Eq)]
struct NumericStatsCacheKey {
    rel_oid: u32,
    row_group_count: i64,
    max_rg_id: i64,
    total_rows: i64,
}

// jsonb operator OIDs from pg_operator.
const JSONB_OBJECT_FIELD_OP: u32 = 3211; // jsonb -> text -> jsonb
const JSONB_OBJECT_FIELD_TEXT_OP: u32 = 3477; // jsonb ->> text -> text

const TEXT_OID: u32 = 25;
const DUCK_ROUTE_CACHE_MAX: usize = 512;
const NATIVE_REWRITE_CACHE_MAX: usize = 512;

#[pg_extern]
fn route_cache_reset() -> i64 {
    let duck = DUCK_ROUTE_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        let count = cache.len() as i64;
        cache.clear();
        count
    });
    let native = NATIVE_REWRITE_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        let count = cache.len() as i64;
        cache.clear();
        count
    });
    duck + native
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct NativeRewriteTableSignature {
    rel_oid: u32,
    row_group_count: i64,
    max_rg_id: i64,
    max_generation: i64,
    total_rows: i64,
    total_bytes: i64,
}

#[derive(Clone)]
struct NativeRewriteCacheEntry {
    table_sig: NativeRewriteTableSignature,
    donor_query: *mut pg_sys::Query,
}

#[derive(Debug, Clone)]
struct ShredEntry {
    src_attnum: i16,
    path: Vec<String>,
    dst_attnum: i16,
    dst_typoid: u32,
}

pub unsafe fn register_hooks() {
    PREV_POST_PARSE_ANALYZE_HOOK = pg_sys::post_parse_analyze_hook;
    pg_sys::post_parse_analyze_hook = Some(rvbbit_post_parse_analyze_hook);
}

pub(crate) fn with_duck_rewrite_disabled<R>(f: impl FnOnce() -> R) -> R {
    let old = DUCK_REWRITE_DISABLED.with(|flag| {
        let old = flag.get();
        flag.set(true);
        old
    });
    let out = f();
    DUCK_REWRITE_DISABLED.with(|flag| flag.set(old));
    out
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_post_parse_analyze_hook(
    pstate: *mut pg_sys::ParseState,
    query: *mut pg_sys::Query,
    jstate: *mut pg_sys::JumbleState,
) {
    if let Some(prev) = PREV_POST_PARSE_ANALYZE_HOOK {
        prev(pstate, query, jstate);
    }
    if query.is_null() {
        return;
    }
    // Never rewrite queries issued *during* CREATE/ALTER EXTENSION: the rvbbit
    // catalog (e.g. rvbbit.accel_policy, which the rewrite rules look up) isn't
    // fully built yet at that point, so the lookup would fail and abort the
    // install with "relation rvbbit.accel_policy does not exist".
    if core::ptr::addr_of!(pg_sys::creating_extension).read() {
        return;
    }
    router::set_pg_rowstore_route_selected(false);
    router::set_native_vortex_route_selected(false);
    if (*query).commandType != pg_sys::CmdType::CMD_SELECT {
        return;
    }
    if force_heap_scan_enabled() {
        return;
    }
    if IN_REWRITER.with(|f| f.get()) {
        return;
    }
    // Dynamic/prepared queries carry external ($n) params (EXECUTE ... USING,
    // PREPARE, SPI with argtypes). The rewriter re-parses source SQL with fixed
    // ZERO params (parse_to_query), which ereports "there is no parameter $1" and
    // would abort the user's statement. Such queries are never rewrite targets —
    // skip them so dynamic parameter binding keeps working.
    if query_has_extern_params(query) {
        return;
    }
    let previous_source = CURRENT_SOURCE_SQL.with(|cell| {
        let mut slot = cell.borrow_mut();
        let previous = slot.take();
        *slot = source_sql_from_parse_state(pstate);
        previous
    });
    IN_REWRITER.with(|f| f.set(true));
    let duck_rewritten = try_duck_backend_rewrite(pstate, query);
    let pg_rowstore_selected = router::pg_rowstore_route_selected();
    // When the router picks native+vortex, skip ALL the native SQL rewrites (the
    // projected-aggregate rules + native cache) so the query falls through to the
    // native CustomScan, which reads the vortex layout via the route flag. Mirrors
    // pg_rowstore_selected — otherwise a projected-aggregate rewrite would hijack the
    // query to the parquet path and the vortex flag would never take effect.
    let native_vortex_selected = router::native_vortex_route_selected();
    let native_cache_rewritten =
        if !duck_rewritten && !pg_rowstore_selected && !native_vortex_selected {
            try_apply_native_rewrite_cache(query)
        } else {
            false
        };
    if !duck_rewritten
        && !pg_rowstore_selected
        && !native_vortex_selected
        && !native_cache_rewritten
        && !try_source_correlated_scalar_agg_rule(pstate, query)
        && !try_source_exclusive_member_semijoin_rule(pstate, query)
        && !try_source_simple_exists_semijoin_rule(pstate, query)
        && !try_source_dimension_key_filter_rule(pstate, query)
    {
        rewrite_query(query);
    }
    IN_REWRITER.with(|f| f.set(false));
    CURRENT_SOURCE_SQL.with(|cell| {
        *cell.borrow_mut() = previous_source;
    });
}

unsafe fn source_sql_from_parse_state(pstate: *mut pg_sys::ParseState) -> Option<String> {
    if pstate.is_null() || (*pstate).p_sourcetext.is_null() {
        return None;
    }
    Some(
        CStr::from_ptr((*pstate).p_sourcetext)
            .to_string_lossy()
            .into_owned(),
    )
}

/// Tree walker that flags any external ($n) Param. Stops on the first hit.
unsafe extern "C-unwind" fn rvbbit_extern_param_walker(
    node: *mut pg_sys::Node,
    context: *mut core::ffi::c_void,
) -> bool {
    if node.is_null() {
        return false;
    }
    if (*node).type_ == pg_sys::NodeTag::T_Param {
        let p = node as *mut pg_sys::Param;
        if (*p).paramkind == pg_sys::ParamKind::PARAM_EXTERN {
            *(context as *mut bool) = true;
            return true; // found one — stop walking
        }
    }
    if (*node).type_ == pg_sys::NodeTag::T_Query {
        return pg_sys::query_tree_walker_impl(
            node as *mut pg_sys::Query,
            Some(rvbbit_extern_param_walker),
            context,
            0,
        );
    }
    pg_sys::expression_tree_walker_impl(node, Some(rvbbit_extern_param_walker), context)
}

/// True iff the analyzed query references any external ($n) parameters anywhere
/// (including sublinks / subquery RTEs). Used to keep the rewriter's hands off
/// dynamic/prepared statements, whose source re-parse would fail on the params.
unsafe fn query_has_extern_params(query: *mut pg_sys::Query) -> bool {
    if query.is_null() {
        return false;
    }
    let mut found: bool = false;
    pg_sys::query_tree_walker_impl(
        query,
        Some(rvbbit_extern_param_walker),
        (&mut found as *mut bool).cast::<core::ffi::c_void>(),
        0,
    );
    found
}

/// Diagnostic — parse a SQL string and return a description of its
/// Query node. Lets us inspect groupClause / groupingSets / rtable
/// from outside any hook context.
#[pg_extern]
fn _debug_parse(sql: &str) -> String {
    unsafe {
        let cstr = match std::ffi::CString::new(sql) {
            Ok(c) => c,
            Err(e) => return format!("CString error: {e}"),
        };
        let parsetree_list =
            pg_sys::raw_parser(cstr.as_ptr(), pg_sys::RawParseMode::RAW_PARSE_DEFAULT);
        if parsetree_list.is_null() || (*parsetree_list).length == 0 {
            return "parse error".into();
        }
        let raw_stmt = (*(*parsetree_list).elements).ptr_value as *mut pg_sys::RawStmt;
        let queries = pg_sys::pg_analyze_and_rewrite_fixedparams(
            raw_stmt,
            cstr.as_ptr(),
            std::ptr::null(),
            0,
            std::ptr::null_mut(),
        );
        if queries.is_null() || (*queries).length == 0 {
            return "no queries".into();
        }
        let q = (*(*queries).elements).ptr_value as *mut pg_sys::Query;
        let rt_len = if (*q).rtable.is_null() {
            0
        } else {
            (*(*q).rtable).length
        };
        let mut kinds = Vec::new();
        for i in 0..rt_len {
            let rte =
                (*(*(*q).rtable).elements.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
            kinds.push(format!("{:?}", (*rte).rtekind));
        }
        let gc_len = if (*q).groupClause.is_null() {
            -1i32
        } else {
            (*(*q).groupClause).length
        };
        let gs_len = if (*q).groupingSets.is_null() {
            -1i32
        } else {
            (*(*q).groupingSets).length
        };
        let tl_len = if (*q).targetList.is_null() {
            -1i32
        } else {
            (*(*q).targetList).length
        };
        format!(
            "hasAggs={} groupClause_len={} groupingSets_len={} targetList_len={} rtable=[{}]",
            (*q).hasAggs,
            gc_len,
            gs_len,
            tl_len,
            kinds.join(", "),
        )
    }
}

/// Rule family S1: source-level decorrelation for scalar aggregate subqueries.
///
/// PostgreSQL leaves shapes like
/// `outer_col > (SELECT c * sum(inner_col) FROM inner WHERE inner_k = outer_k
/// AND local_filters...)` as a per-row SubPlan. For columnar scans that is the
/// wrong execution shape: the inner relation should be scanned once, grouped
/// by the correlation keys, then joined/semi-joined. This pass rewrites only a
/// narrow, semantics-preserving subset into:
///
/// `WITH rvbbit_corr_agg_n AS MATERIALIZED (...) ... EXISTS (...)`
///
/// It is deliberately source-backed instead of benchmark-name-backed: the
/// recognizer requires a single inner relation, SUM/AVG, equality
/// correlations, and conjunctive local filters. If the generated SQL fails
/// parse/analyze, the original query is left untouched.
unsafe fn try_source_correlated_scalar_agg_rule(
    pstate: *mut pg_sys::ParseState,
    query: *mut pg_sys::Query,
) -> bool {
    if query.is_null()
        || !(*query).hasSubLinks
        || pstate.is_null()
        || (*pstate).p_sourcetext.is_null()
    {
        return false;
    }
    let source = std::ffi::CStr::from_ptr((*pstate).p_sourcetext)
        .to_string_lossy()
        .into_owned();
    let query_source = source_slice_for_query(&source, query);
    let Some(rewritten) = rewrite_correlated_scalar_agg_sql(query_source) else {
        return false;
    };
    let Some(donor) = parse_to_query(&rewritten) else {
        pgrx::warning!(
            "rvbbit: correlated aggregate decorrelation parse failed for: {}",
            rewritten
        );
        return false;
    };
    std::ptr::copy_nonoverlapping(donor, query, 1);
    true
}

unsafe fn source_slice_for_query<'a>(source: &'a str, query: *mut pg_sys::Query) -> &'a str {
    let loc = (*query).stmt_location;
    if loc < 0 {
        return source_select_tail(source).unwrap_or(source);
    }
    let start = loc as usize;
    if start >= source.len() || !source.is_char_boundary(start) {
        return source_select_tail(source).unwrap_or(source);
    }
    let len = (*query).stmt_len;
    if len <= 0 {
        let slice = &source[start..];
        return if starts_with_keyword(slice.trim_start(), "select") {
            slice
        } else {
            source_select_tail(slice)
                .or_else(|| source_select_tail(source))
                .unwrap_or(slice)
        };
    }
    let end = start.saturating_add(len as usize);
    if end > source.len() || !source.is_char_boundary(end) {
        return source_select_tail(source).unwrap_or(source);
    }
    let slice = &source[start..end];
    if starts_with_keyword(slice.trim_start(), "select") {
        slice
    } else {
        source_select_tail(slice)
            .or_else(|| source_select_tail(source))
            .unwrap_or(slice)
    }
}

unsafe fn try_apply_native_rewrite_cache(query: *mut pg_sys::Query) -> bool {
    let Some(cache_key) = native_rewrite_cache_source(query) else {
        return false;
    };
    let Some(entry) = NATIVE_REWRITE_CACHE.with(|cache| cache.borrow().get(&cache_key).cloned())
    else {
        return false;
    };
    let Some(rel_oid) = primary_relation_oid_for_cache(query) else {
        NATIVE_REWRITE_CACHE.with(|cache| {
            cache.borrow_mut().remove(&cache_key);
        });
        return false;
    };
    if rel_oid != entry.table_sig.rel_oid {
        NATIVE_REWRITE_CACHE.with(|cache| {
            cache.borrow_mut().remove(&cache_key);
        });
        return false;
    }
    let Some(current_sig) = native_rewrite_table_signature(rel_oid) else {
        NATIVE_REWRITE_CACHE.with(|cache| {
            cache.borrow_mut().remove(&cache_key);
        });
        return false;
    };
    if current_sig != entry.table_sig || entry.donor_query.is_null() {
        NATIVE_REWRITE_CACHE.with(|cache| {
            cache.borrow_mut().remove(&cache_key);
        });
        return false;
    }

    let donor = pg_sys::copyObjectImpl(entry.donor_query as *const c_void) as *mut pg_sys::Query;
    if donor.is_null() {
        NATIVE_REWRITE_CACHE.with(|cache| {
            cache.borrow_mut().remove(&cache_key);
        });
        return false;
    }
    apply_native_rewrite_donor(query, donor);
    true
}

unsafe fn apply_native_rewrite_and_cache(
    query: *mut pg_sys::Query,
    table_oid: u32,
    sql: &str,
    warning_label: &str,
) -> bool {
    let donor = match parse_to_query(sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: {} rewrite parse failed for: {}",
                warning_label,
                sql
            );
            return false;
        }
    };
    store_native_rewrite_cache(query, table_oid, donor);
    apply_native_rewrite_donor(query, donor);
    true
}

unsafe fn apply_native_rewrite_donor(query: *mut pg_sys::Query, donor: *mut pg_sys::Query) {
    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).groupingSets = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    (*query).hasWindowFuncs = false;
    (*query).hasSubLinks = false;
}

unsafe fn store_native_rewrite_cache(
    query: *mut pg_sys::Query,
    table_oid: u32,
    donor: *mut pg_sys::Query,
) {
    let Some(cache_key) = native_rewrite_cache_source(query) else {
        return;
    };
    if cache_key.to_ascii_lowercase().contains("rvbbit.") {
        return;
    }
    if primary_relation_oid_for_cache(query) != Some(table_oid) {
        return;
    }
    let Some(table_sig) = native_rewrite_table_signature(table_oid) else {
        return;
    };
    let mut cache_context = PgMemoryContexts::CacheMemoryContext;
    let donor_query = cache_context
        .switch_to(|_| pg_sys::copyObjectImpl(donor as *const c_void) as *mut pg_sys::Query);
    if donor_query.is_null() {
        return;
    }
    NATIVE_REWRITE_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        if !cache.contains_key(&cache_key) && cache.len() >= NATIVE_REWRITE_CACHE_MAX {
            cache.clear();
        }
        cache.insert(
            cache_key,
            NativeRewriteCacheEntry {
                table_sig,
                donor_query,
            },
        );
    });
}

unsafe fn native_rewrite_cache_source(query: *mut pg_sys::Query) -> Option<String> {
    CURRENT_SOURCE_SQL.with(|cell| {
        let source = cell.borrow();
        let source = source.as_ref()?;
        let query_source = source_slice_for_query(source, query).trim();
        if query_source.is_empty() || query_source.to_ascii_lowercase().contains("rvbbit.") {
            None
        } else {
            Some(query_source.to_string())
        }
    })
}

unsafe fn primary_relation_oid_for_cache(query: *mut pg_sys::Query) -> Option<u32> {
    if query.is_null()
        || (*query).commandType != pg_sys::CmdType::CMD_SELECT
        || !(*query).cteList.is_null()
        || !(*query).setOperations.is_null()
        || (*query).hasSubLinks
    {
        return None;
    }
    let rtable = (*query).rtable;
    if rtable.is_null() {
        return None;
    }
    let mut rel_oid = None;
    for i in 0..(*rtable).length {
        let rte = (*(*rtable).elements.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
        if rte.is_null() {
            continue;
        }
        match (*rte).rtekind {
            pg_sys::RTEKind::RTE_RELATION => {
                let oid = (*rte).relid.to_u32();
                if rel_oid.replace(oid).is_some() {
                    return None;
                }
            }
            pg_sys::RTEKind::RTE_GROUP => {}
            _ => return None,
        }
    }
    rel_oid
}

fn native_rewrite_table_signature(table_oid: u32) -> Option<NativeRewriteTableSignature> {
    if current_source_has_asof_timestamp_directive() {
        return None;
    }
    if let Some(val) = guc_setting("rvbbit.as_of_generation") {
        if val.trim().parse::<i64>().ok().is_some_and(|g| g > 0) {
            return None;
        }
    }
    if guc_setting("rvbbit.as_of_timestamp").is_some_and(|v| !v.trim().is_empty()) {
        return None;
    }
    let sql = format!(
        "SELECT rg.row_group_count, rg.max_rg_id, rg.max_generation, \
                rg.total_rows, rg.total_bytes, \
                pg_relation_size(t.table_oid)::bigint, \
                coalesce(t.shadow_heap_retained, false), \
                coalesce(t.shadow_heap_dirty, false), \
                EXISTS(SELECT 1 FROM rvbbit.delete_log), \
                EXISTS(SELECT 1 FROM rvbbit.row_groups WHERE cold_url IS NOT NULL) \
         FROM rvbbit.tables t \
         LEFT JOIN LATERAL ( \
             SELECT count(*)::bigint AS row_group_count, \
                    coalesce(max(rg_id), -1)::bigint AS max_rg_id, \
                    coalesce(max(generation), 0)::bigint AS max_generation, \
                    coalesce(sum(n_rows), 0)::bigint AS total_rows, \
                    coalesce(sum(n_bytes), 0)::bigint AS total_bytes \
             FROM rvbbit.row_groups \
             WHERE table_oid = t.table_oid \
         ) rg ON true \
         WHERE t.table_oid = {table_oid}::oid"
    );
    let mut out = None;
    pgrx::Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let row_group_count = row.get::<i64>(1)?.unwrap_or(0);
            let max_rg_id = row.get::<i64>(2)?.unwrap_or(-1);
            let max_generation = row.get::<i64>(3)?.unwrap_or(0);
            let total_rows = row.get::<i64>(4)?.unwrap_or(0);
            let total_bytes = row.get::<i64>(5)?.unwrap_or(0);
            let heap_bytes = row.get::<i64>(6)?.unwrap_or(0);
            let shadow_heap_retained = row.get::<bool>(7)?.unwrap_or(false);
            let shadow_heap_dirty = row.get::<bool>(8)?.unwrap_or(false);
            let has_tombstones = row.get::<bool>(9)?.unwrap_or(false);
            let has_cold = row.get::<bool>(10)?.unwrap_or(false);
            if has_tombstones || has_cold {
                continue;
            }
            if row_group_count <= 0 || total_rows <= 0 {
                continue;
            }
            if heap_bytes > 0 && !(shadow_heap_retained && !shadow_heap_dirty) {
                if unsafe { heap_visible_row_count(table_oid) }.unwrap_or(1) != 0 {
                    continue;
                }
            }
            out = Some(NativeRewriteTableSignature {
                rel_oid: table_oid,
                row_group_count,
                max_rg_id,
                max_generation,
                total_rows,
                total_bytes,
            });
        }
        Ok(())
    })
    .ok()?;
    out
}

#[derive(Clone, Debug)]
struct DuckOutputColumn {
    name: String,
    type_sql: String,
}

unsafe fn try_duck_backend_rewrite(
    pstate: *mut pg_sys::ParseState,
    query: *mut pg_sys::Query,
) -> bool {
    if DUCK_REWRITE_DISABLED.with(|flag| flag.get())
        || !duck_backend::backend_enabled()
        || query.is_null()
        || pstate.is_null()
        || (*pstate).p_sourcetext.is_null()
    {
        return false;
    }
    if (*query).commandType != pg_sys::CmdType::CMD_SELECT {
        return false;
    }

    let source = std::ffi::CStr::from_ptr((*pstate).p_sourcetext)
        .to_string_lossy()
        .into_owned();
    let query_source = source_slice_for_query(&source, query).trim();
    if query_source.is_empty() || query_source.to_ascii_lowercase().contains("rvbbit.") {
        return false;
    }
    if crate::time_travel::has_as_of_timestamp_directive(query_source) {
        return false;
    }

    let Some(columns) = duck_output_columns(query) else {
        return false;
    };
    if columns.is_empty() {
        return false;
    }

    let route_probe = duck_route_doc_for_probe(query_source);
    let route_doc = &route_probe.doc;
    if route_doc.get("safe_select").and_then(Value::as_bool) != Some(true) {
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    }
    let Some(chosen_candidate) = route_doc.get("chosen_candidate").and_then(Value::as_str) else {
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    };
    if chosen_candidate == "pg_rowstore" {
        router::set_pg_rowstore_route_selected(true);
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    }
    if chosen_candidate == "rvbbit_native_vortex" {
        // Falls through to the native CustomScan (no SQL rewrite); the flag is read
        // by the planner (add_rvbbit_path) and stashed in the scan node, so it
        // survives the execution-time route re-computation to fetch_best_row_group_paths.
        router::set_native_vortex_route_selected(true);
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    }
    if !matches!(
        chosen_candidate,
        "duck_vector"
            | "datafusion_vector"
            | "duck_hive"
            | "duck_vortex"
            | "datafusion_hive"
            | "datafusion_vortex"
            | "datafusion_mem"
    ) {
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    }

    let features = route_doc.get("features").unwrap_or(&Value::Null);
    let select_star = features
        .get("select_star")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let has_limit = features
        .get("limit_bucket")
        .and_then(Value::as_str)
        .is_some_and(|bucket| bucket != "unknown");
    let has_agg = features
        .get("aggregate_count")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        > 0;
    if select_star && !has_limit && !has_agg {
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    }

    let wrapper = build_duck_backend_sql(
        query_source,
        &columns,
        duck_backend::max_rows(),
        chosen_candidate,
    );
    let Some(donor) = parse_to_query(&wrapper) else {
        pgrx::warning!("rvbbit: Duck backend wrapper parse failed for: {}", wrapper);
        log_route_probe(query_source, route_doc, route_probe.cache_hit, false);
        return false;
    };
    std::ptr::copy_nonoverlapping(donor, query, 1);
    log_route_probe(query_source, route_doc, route_probe.cache_hit, true);
    true
}

fn log_route_probe(query_sql: &str, route_doc: &Value, cache_hit: bool, rewritten: bool) {
    route_log::enqueue_decision(query_sql, route_doc, cache_hit, rewritten);
    route_log::record_pending_execution(query_sql, route_doc, cache_hit, rewritten);
}

struct DuckRouteProbe {
    doc: Value,
    cache_hit: bool,
}

fn duck_route_doc_for_probe(query_sql: &str) -> DuckRouteProbe {
    let cache_key = format!("{}\n{}", router::route_runtime_stamp(), query_sql.trim());
    if let Some(cached) = DUCK_ROUTE_CACHE.with(|cache| cache.borrow().get(&cache_key).cloned()) {
        return DuckRouteProbe {
            doc: cached,
            cache_hit: true,
        };
    }
    let old = IN_REWRITER.with(|flag| {
        let old = flag.get();
        flag.set(false);
        old
    });
    let out = router::route_rewrite_value(query_sql);
    IN_REWRITER.with(|flag| flag.set(old));
    DUCK_ROUTE_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        if cache.len() >= DUCK_ROUTE_CACHE_MAX {
            cache.clear();
        }
        cache.insert(cache_key, out.clone());
    });
    DuckRouteProbe {
        doc: out,
        cache_hit: false,
    }
}

unsafe fn duck_output_columns(query: *mut pg_sys::Query) -> Option<Vec<DuckOutputColumn>> {
    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut out = Vec::new();
    let mut names = HashSet::new();
    for idx in 0..(*tlist).length as usize {
        let tle = (*(*tlist).elements.add(idx)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        let name = target_alias(tle)?;
        if name.is_empty() || !names.insert(name.clone()) {
            return None;
        }
        let typoid = pg_sys::exprType((*tle).expr as *mut pg_sys::Node).to_u32();
        let typmod = pg_sys::exprTypmod((*tle).expr as *mut pg_sys::Node);
        let type_sql = duck_supported_output_type_sql(typoid, typmod)?;
        out.push(DuckOutputColumn { name, type_sql });
    }
    Some(out)
}

fn duck_supported_output_type_sql(typoid: u32, typmod: i32) -> Option<String> {
    match typoid {
        16 | 20 | 21 | 23 | 25 | 700 | 701 | 1042 | 1043 | 1082 | 1083 | 1114 | 1184 | 1700 => {
            format_type_sql(typoid, typmod)
        }
        _ => None,
    }
}

fn format_type_sql(typoid: u32, typmod: i32) -> Option<String> {
    pgrx::Spi::get_one::<String>(&format!(
        "SELECT pg_catalog.format_type({typoid}::oid, {typmod})"
    ))
    .ok()
    .flatten()
}

fn build_duck_backend_sql(
    query_source: &str,
    columns: &[DuckOutputColumn],
    max_rows: i32,
    chosen_candidate: &str,
) -> String {
    let column_names = columns
        .iter()
        .map(|column| column.name.clone())
        .collect::<Vec<_>>();
    let column_json = serde_json::to_string(&column_names).unwrap_or_else(|_| "[]".into());
    let defs = columns
        .iter()
        .map(|column| format!("{} {}", quote_ident(&column.name), column.type_sql))
        .collect::<Vec<_>>()
        .join(", ");
    let engine_fn = match chosen_candidate {
        "datafusion_mem" => "rvbbit.datafusion_mem_query_json",
        "datafusion_vector" => "rvbbit.datafusion_query_json",
        "duck_hive" => "rvbbit.duck_hive_query_json",
        "duck_vortex" => "rvbbit.duck_vortex_query_json",
        "datafusion_hive" => "rvbbit.datafusion_hive_query_json",
        "datafusion_vortex" => "rvbbit.datafusion_vortex_query_json",
        _ => "rvbbit.duck_query_json",
    };
    format!(
        "SELECT * FROM jsonb_to_recordset({engine_fn}({}, {}::jsonb, {})) AS rvbbit_duck_result({})",
        sql_text_literal(query_source),
        sql_text_literal(&column_json),
        max_rows,
        defs
    )
}

fn source_select_tail(source: &str) -> Option<&str> {
    let trimmed = source.trim_start();
    if starts_with_keyword(trimmed, "select") {
        return Some(trimmed);
    }
    if !starts_with_keyword(trimmed, "explain") {
        return None;
    }
    let skipped = source.len() - trimmed.len();
    let select_pos = find_keyword_ci(source, "select", skipped)?;
    Some(&source[select_pos..])
}

#[derive(Clone)]
struct SourceCorrAggRewrite {
    replace_start: usize,
    replace_end: usize,
    cte_name: String,
    cte_sql: String,
    replacement_sql: String,
}

struct SourceScalarAgg {
    from_sql: String,
    agg_sql: String,
    correlations: Vec<SourceCorrelation>,
    local_filters: Vec<String>,
}

struct SourceCorrelation {
    inner_col: String,
    outer_expr: String,
}

fn rewrite_correlated_scalar_agg_sql(sql: &str) -> Option<String> {
    let trimmed = sql.trim_start();
    if !starts_with_keyword(trimmed, "select")
        || starts_with_keyword(trimmed, "with")
        || ascii_contains_ci(sql, "rvbbit_corr_agg_")
    {
        return None;
    }
    let rewrite = find_source_corr_agg_rewrite(sql)?;
    let mut out =
        String::with_capacity(sql.len() + rewrite.cte_sql.len() + rewrite.replacement_sql.len());
    out.push_str("WITH ");
    out.push_str(&rewrite.cte_name);
    out.push_str(" AS MATERIALIZED (");
    out.push_str(&rewrite.cte_sql);
    out.push_str(") ");
    out.push_str(&sql[..rewrite.replace_start]);
    out.push_str(&rewrite.replacement_sql);
    out.push_str(&sql[rewrite.replace_end..]);
    Some(out)
}

fn find_source_corr_agg_rewrite(sql: &str) -> Option<SourceCorrAggRewrite> {
    let mut search = 0usize;
    while let Some(select_pos) = find_keyword_ci(sql, "select", search) {
        search = select_pos + "select".len();
        let Some(open_paren) = previous_nonspace(sql, select_pos)
            .filter(|&idx| sql.as_bytes().get(idx).copied() == Some(b'('))
        else {
            continue;
        };
        let Some(close_paren) = find_matching_paren(sql, open_paren) else {
            continue;
        };
        let subquery = &sql[select_pos..close_paren];
        let Some(agg) = parse_source_scalar_agg_subquery(subquery) else {
            continue;
        };
        let Some((op_start, _op_end, op_sql)) = comparison_before_open_paren(sql, open_paren)
        else {
            continue;
        };
        let clause_start = find_clause_start(sql, op_start)?;
        let lhs_sql = sql[clause_start..op_start].trim();
        if lhs_sql.is_empty() {
            continue;
        }
        let join_conds = agg
            .correlations
            .iter()
            .map(|corr| {
                format!(
                    "a.{} = {}",
                    quote_ident(&corr.inner_col),
                    corr.outer_expr.trim()
                )
            })
            .collect::<Vec<_>>();
        let cte_name = "rvbbit_corr_agg_1".to_string();
        let mut exists_parts = join_conds;
        exists_parts.push(format!("{lhs_sql} {op_sql} a.agg_value"));
        let replacement_sql = format!(
            "EXISTS (SELECT 1 FROM {cte_name} a WHERE {})",
            exists_parts.join(" AND ")
        );
        let key_list = agg
            .correlations
            .iter()
            .map(|corr| quote_ident(&corr.inner_col))
            .collect::<Vec<_>>();
        let where_sql = if agg.local_filters.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", agg.local_filters.join(" AND "))
        };
        let cte_sql = format!(
            "SELECT {keys}, {agg_sql} AS agg_value FROM {from_sql}{where_sql} GROUP BY {keys}",
            keys = key_list.join(", "),
            agg_sql = agg.agg_sql,
            from_sql = agg.from_sql,
            where_sql = where_sql
        );
        return Some(SourceCorrAggRewrite {
            replace_start: clause_start,
            replace_end: close_paren + 1,
            cte_name,
            cte_sql,
            replacement_sql,
        });
    }
    None
}

fn parse_source_scalar_agg_subquery(sql: &str) -> Option<SourceScalarAgg> {
    if !starts_with_keyword(sql.trim_start(), "select") {
        return None;
    }
    let select_start = find_keyword_ci(sql, "select", 0)? + "select".len();
    let from_pos = find_top_level_keyword(sql, "from", select_start)?;
    let where_pos = find_top_level_keyword(sql, "where", from_pos + "from".len())?;
    let select_expr = sql[select_start..from_pos].trim();
    let from_sql = sql[from_pos + "from".len()..where_pos].trim();
    if from_sql.is_empty()
        || from_sql.contains(',')
        || ascii_contains_ci(from_sql, " join ")
        || ascii_contains_ci(from_sql, "\njoin ")
    {
        return None;
    }
    let (rel_name, rel_alias) = parse_single_from_relation(from_sql)?;
    let rel_oid = resolve_relation_oid(&rel_name)?;
    if !is_rvbbit_table_cached(rel_oid) {
        return None;
    }
    let inner_cols = fetch_attnames_inline(rel_oid)?;
    let agg_sql = parse_source_agg_expr(select_expr)?;
    let where_sql = sql[where_pos + "where".len()..].trim();
    let clauses = split_top_level_and(where_sql)?;
    let mut correlations = Vec::<SourceCorrelation>::new();
    let mut local_filters = Vec::<String>::new();
    for clause in clauses {
        let trimmed = clause.trim();
        if trimmed.is_empty() {
            return None;
        }
        if let Some((left, right)) = split_top_level_equality(trimmed) {
            let left_inner = source_inner_column(left, &rel_alias, &rel_name, &inner_cols);
            let right_inner = source_inner_column(right, &rel_alias, &rel_name, &inner_cols);
            match (left_inner, right_inner) {
                (Some(inner_col), None) => {
                    correlations.push(SourceCorrelation {
                        inner_col,
                        outer_expr: right.trim().to_string(),
                    });
                    continue;
                }
                (None, Some(inner_col)) => {
                    correlations.push(SourceCorrelation {
                        inner_col,
                        outer_expr: left.trim().to_string(),
                    });
                    continue;
                }
                _ => {}
            }
        }
        local_filters.push(trimmed.to_string());
    }
    if correlations.is_empty() || correlations.len() > 4 {
        return None;
    }
    Some(SourceScalarAgg {
        from_sql: from_sql.to_string(),
        agg_sql,
        correlations,
        local_filters,
    })
}

unsafe fn try_source_exclusive_member_semijoin_rule(
    pstate: *mut pg_sys::ParseState,
    query: *mut pg_sys::Query,
) -> bool {
    if query.is_null()
        || !(*query).hasSubLinks
        || pstate.is_null()
        || (*pstate).p_sourcetext.is_null()
    {
        return false;
    }
    let source = std::ffi::CStr::from_ptr((*pstate).p_sourcetext)
        .to_string_lossy()
        .into_owned();
    let query_source = source_slice_for_query(&source, query);
    let Some(rewritten) = rewrite_exclusive_member_semijoin_sql(query_source) else {
        return false;
    };
    let Some(donor) = parse_to_query(&rewritten) else {
        pgrx::warning!(
            "rvbbit: exclusive member semijoin rewrite parse failed for: {}",
            rewritten
        );
        return false;
    };
    std::ptr::copy_nonoverlapping(donor, query, 1);
    true
}

struct SourceExclusiveMemberPlan {
    rel_name: String,
    outer_alias: String,
    group_col: String,
    member_col: String,
    filter_left_col: String,
    filter_right_col: String,
    filter_kind: &'static str,
    filter_op: &'static str,
    remove_clause_idxs: Vec<usize>,
}

struct SourceMemberSubquery {
    rel_name: String,
    outer_alias: String,
    group_col: String,
    member_col: String,
    filter: Option<SourceColumnCompare>,
}

#[derive(Clone)]
struct SourceColumnCompare {
    left_col: String,
    right_col: String,
    op_token: &'static str,
}

fn rewrite_exclusive_member_semijoin_sql(sql: &str) -> Option<String> {
    let trimmed = sql.trim_start();
    if !starts_with_keyword(trimmed, "select")
        || starts_with_keyword(trimmed, "with")
        || ascii_contains_ci(sql, "vector_group_member_filtered_rows")
    {
        return None;
    }
    let from_pos = find_top_level_keyword(sql, "from", 0)?;
    let where_pos = find_top_level_keyword(sql, "where", from_pos + "from".len())?;
    let where_end = find_next_top_level_clause(sql, where_pos + "where".len()).unwrap_or(sql.len());
    let from_sql = &sql[from_pos + "from".len()..where_pos];
    let where_sql = &sql[where_pos + "where".len()..where_end];
    let from_items = split_top_level_comma(from_sql)?;
    let clauses = split_top_level_and(where_sql)?;
    let plan = find_exclusive_member_plan(&from_items, &clauses)?;

    let mut new_from_items = Vec::with_capacity(from_items.len());
    let mut replaced = false;
    for item in from_items {
        let trimmed_item = item.trim();
        if let Some((rel, alias)) = parse_single_from_relation(trimmed_item) {
            if alias.eq_ignore_ascii_case(&plan.outer_alias)
                && rel.eq_ignore_ascii_case(&plan.rel_name)
            {
                new_from_items.push(format!(
                    "rvbbit.vector_group_member_filtered_rows({rel}::regclass, {group_col}, {member_col}, {filter_left}, {filter_right}, {filter_kind}, {filter_op}) AS {alias}({group_alias}, {member_alias})",
                    rel = sql_text_literal(&plan.rel_name),
                    group_col = sql_text_literal(&plan.group_col),
                    member_col = sql_text_literal(&plan.member_col),
                    filter_left = sql_text_literal(&plan.filter_left_col),
                    filter_right = sql_text_literal(&plan.filter_right_col),
                    filter_kind = sql_text_literal(plan.filter_kind),
                    filter_op = sql_text_literal(plan.filter_op),
                    alias = quote_ident(&plan.outer_alias),
                    group_alias = quote_ident(&plan.group_col),
                    member_alias = quote_ident(&plan.member_col),
                ));
                replaced = true;
                continue;
            }
        }
        new_from_items.push(trimmed_item.to_string());
    }
    if !replaced {
        return None;
    }

    let remove = plan.remove_clause_idxs.into_iter().collect::<HashSet<_>>();
    let kept_clauses = clauses
        .iter()
        .enumerate()
        .filter_map(|(idx, clause)| (!remove.contains(&idx)).then_some(clause.trim()))
        .filter(|clause| !clause.is_empty())
        .collect::<Vec<_>>();
    if kept_clauses.is_empty() {
        return None;
    }

    let mut out = String::with_capacity(sql.len() + 256);
    out.push_str(&sql[..from_pos + "from".len()]);
    out.push('\n');
    out.push_str("    ");
    out.push_str(&new_from_items.join(",\n    "));
    out.push('\n');
    out.push_str("WHERE\n    ");
    out.push_str(&kept_clauses.join("\n    AND "));
    out.push_str(&sql[where_end..]);
    Some(out)
}

fn find_exclusive_member_plan(
    from_items: &[&str],
    clauses: &[&str],
) -> Option<SourceExclusiveMemberPlan> {
    let mut exists: Option<(usize, SourceMemberSubquery)> = None;
    let mut not_exists: Option<(usize, SourceMemberSubquery)> = None;
    for (idx, clause) in clauses.iter().enumerate() {
        if let Some(subquery) = parse_source_member_exists_clause(clause, false) {
            exists = Some((idx, subquery));
        } else if let Some(subquery) = parse_source_member_exists_clause(clause, true) {
            not_exists = Some((idx, subquery));
        }
    }
    let (exists_idx, exists) = exists?;
    let (not_exists_idx, not_exists) = not_exists?;
    let filter = not_exists.filter.clone()?;
    if exists.filter.is_some()
        || !exists.rel_name.eq_ignore_ascii_case(&not_exists.rel_name)
        || !exists
            .outer_alias
            .eq_ignore_ascii_case(&not_exists.outer_alias)
        || !exists.group_col.eq_ignore_ascii_case(&not_exists.group_col)
        || !exists
            .member_col
            .eq_ignore_ascii_case(&not_exists.member_col)
    {
        return None;
    }

    let outer_filter_idx = clauses.iter().enumerate().find_map(|(idx, clause)| {
        let cmp = parse_source_outer_column_compare(clause, &exists.outer_alias)?;
        (cmp.left_col.eq_ignore_ascii_case(&filter.left_col)
            && cmp.right_col.eq_ignore_ascii_case(&filter.right_col)
            && cmp.op_token == filter.op_token)
            .then_some(idx)
    })?;

    if !from_items.iter().any(|item| {
        parse_single_from_relation(item.trim()).is_some_and(|(rel, alias)| {
            rel.eq_ignore_ascii_case(&exists.rel_name)
                && alias.eq_ignore_ascii_case(&exists.outer_alias)
        })
    }) {
        return None;
    }

    let rel_oid = resolve_relation_oid(&exists.rel_name)?;
    if !is_rvbbit_table_cached(rel_oid) {
        return None;
    }
    let filter_typoid = fetch_att_typoid(rel_oid, &filter.left_col)?;
    let filter_kind = source_vector_filter_kind(filter_typoid)?;

    Some(SourceExclusiveMemberPlan {
        rel_name: exists.rel_name,
        outer_alias: exists.outer_alias,
        group_col: exists.group_col,
        member_col: exists.member_col,
        filter_left_col: filter.left_col,
        filter_right_col: filter.right_col,
        filter_kind,
        filter_op: filter.op_token,
        remove_clause_idxs: vec![exists_idx, not_exists_idx, outer_filter_idx],
    })
}

fn parse_source_member_exists_clause(clause: &str, negated: bool) -> Option<SourceMemberSubquery> {
    let clause = clause.trim();
    let after_not = if negated {
        if !starts_with_keyword(clause, "not") {
            return None;
        }
        skip_ascii_ws(clause, "not".len())
    } else {
        0
    };
    if !starts_with_keyword(&clause[after_not..], "exists") {
        return None;
    }
    let open = skip_ascii_ws(clause, after_not + "exists".len());
    if clause.as_bytes().get(open).copied() != Some(b'(') {
        return None;
    }
    let close = find_matching_paren(clause, open)?;
    if !clause[close + 1..].trim().is_empty() {
        return None;
    }
    parse_source_member_subquery(&clause[open + 1..close])
}

fn parse_source_member_subquery(sql: &str) -> Option<SourceMemberSubquery> {
    if !starts_with_keyword(sql.trim_start(), "select") {
        return None;
    }
    let from_pos = find_top_level_keyword(sql, "from", 0)?;
    let where_pos = find_top_level_keyword(sql, "where", from_pos + "from".len())?;
    let from_sql = sql[from_pos + "from".len()..where_pos].trim();
    let (rel_name, inner_alias) = parse_single_from_relation(from_sql)?;
    let rel_oid = resolve_relation_oid(&rel_name)?;
    if !is_rvbbit_table_cached(rel_oid) {
        return None;
    }
    let inner_cols = fetch_attnames_inline(rel_oid)?;
    let clauses = split_top_level_and(&sql[where_pos + "where".len()..])?;
    let mut outer_alias = None::<String>;
    let mut group_col = None::<String>;
    let mut member_col = None::<String>;
    let mut filter = None::<SourceColumnCompare>;
    for clause in clauses {
        let clause = clause.trim();
        if let Some((left, right)) = split_top_level_equality(clause) {
            let left_ref = source_column_ref(left)?;
            let right_ref = source_column_ref(right)?;
            if let Some((inner_col, outer_qual, outer_col)) =
                classify_inner_outer_refs(&left_ref, &right_ref, &inner_alias, &inner_cols)
            {
                if !inner_col.eq_ignore_ascii_case(&outer_col) {
                    return None;
                }
                group_col = Some(inner_col);
                outer_alias = Some(outer_qual);
                continue;
            }
        }
        if let Some((left, right, op)) = split_top_level_compare(clause) {
            let left_ref = source_column_ref(left)?;
            let right_ref = source_column_ref(right)?;
            if op == "<>" {
                if let Some((inner_col, outer_qual, outer_col)) =
                    classify_inner_outer_refs(&left_ref, &right_ref, &inner_alias, &inner_cols)
                {
                    if !inner_col.eq_ignore_ascii_case(&outer_col) {
                        return None;
                    }
                    member_col = Some(inner_col);
                    outer_alias.get_or_insert(outer_qual);
                    continue;
                }
            } else if left_ref.0.eq_ignore_ascii_case(&inner_alias)
                && right_ref.0.eq_ignore_ascii_case(&inner_alias)
                && filter.is_none()
            {
                let op_token = source_compare_op_token(op)?;
                filter = Some(SourceColumnCompare {
                    left_col: canonical_source_col(&left_ref.1, &inner_cols)?,
                    right_col: canonical_source_col(&right_ref.1, &inner_cols)?,
                    op_token,
                });
                continue;
            }
        }
        return None;
    }
    Some(SourceMemberSubquery {
        rel_name,
        outer_alias: outer_alias?,
        group_col: group_col?,
        member_col: member_col?,
        filter,
    })
}

fn classify_inner_outer_refs(
    left: &(String, String),
    right: &(String, String),
    inner_alias: &str,
    inner_cols: &[String],
) -> Option<(String, String, String)> {
    if left.0.eq_ignore_ascii_case(inner_alias) && !right.0.eq_ignore_ascii_case(inner_alias) {
        return Some((
            canonical_source_col(&left.1, inner_cols)?,
            right.0.clone(),
            right.1.clone(),
        ));
    }
    if right.0.eq_ignore_ascii_case(inner_alias) && !left.0.eq_ignore_ascii_case(inner_alias) {
        return Some((
            canonical_source_col(&right.1, inner_cols)?,
            left.0.clone(),
            left.1.clone(),
        ));
    }
    None
}

fn parse_source_outer_column_compare(
    clause: &str,
    outer_alias: &str,
) -> Option<SourceColumnCompare> {
    let (left, right, op) = split_top_level_compare(clause.trim())?;
    let left_ref = source_column_ref(left)?;
    let right_ref = source_column_ref(right)?;
    if !left_ref.0.eq_ignore_ascii_case(outer_alias)
        || !right_ref.0.eq_ignore_ascii_case(outer_alias)
    {
        return None;
    }
    Some(SourceColumnCompare {
        left_col: left_ref.1,
        right_col: right_ref.1,
        op_token: source_compare_op_token(op)?,
    })
}

unsafe fn try_source_simple_exists_semijoin_rule(
    pstate: *mut pg_sys::ParseState,
    query: *mut pg_sys::Query,
) -> bool {
    if query.is_null()
        || !(*query).hasSubLinks
        || pstate.is_null()
        || (*pstate).p_sourcetext.is_null()
    {
        return false;
    }
    let source = std::ffi::CStr::from_ptr((*pstate).p_sourcetext)
        .to_string_lossy()
        .into_owned();
    let query_source = source_slice_for_query(&source, query);
    let Some(rewritten) = rewrite_simple_exists_semijoin_sql(query_source) else {
        return false;
    };
    let Some(donor) = parse_to_query(&rewritten) else {
        pgrx::warning!(
            "rvbbit: simple EXISTS semijoin rewrite parse failed for: {}",
            rewritten
        );
        return false;
    };
    std::ptr::copy_nonoverlapping(donor, query, 1);
    true
}

struct SourceSimpleExistsSemiJoinPlan {
    exists_idx: usize,
    outer_expr: String,
    inner_rel_name: String,
    inner_key_col: String,
    filter_left_col: String,
    filter_right_col: String,
    filter_kind: &'static str,
    filter_op: &'static str,
}

struct SourceSimpleExistsSemiJoin {
    outer_expr: String,
    outer_col: String,
    inner_rel_name: String,
    inner_key_col: String,
    filter_left_col: String,
    filter_right_col: String,
    filter_op: &'static str,
}

fn rewrite_simple_exists_semijoin_sql(sql: &str) -> Option<String> {
    let trimmed = sql.trim_start();
    if !starts_with_keyword(trimmed, "select")
        || starts_with_keyword(trimmed, "with")
        || ascii_contains_ci(sql, "vector_filtered_distinct_int_keys")
    {
        return None;
    }
    let from_pos = find_top_level_keyword(sql, "from", 0)?;
    let where_pos = find_top_level_keyword(sql, "where", from_pos + "from".len())?;
    let where_end = find_next_top_level_clause(sql, where_pos + "where".len()).unwrap_or(sql.len());
    let from_items = split_top_level_comma(&sql[from_pos + "from".len()..where_pos])?;
    if from_items.len() != 1 {
        return None;
    }
    let (outer_rel_name, outer_alias) = parse_single_from_relation(from_items[0].trim())?;
    let outer_oid = resolve_relation_oid(&outer_rel_name)?;
    if !is_rvbbit_table_cached(outer_oid) {
        return None;
    }
    let outer_cols = fetch_attnames_inline(outer_oid)?;
    let clauses = split_top_level_and(&sql[where_pos + "where".len()..where_end])?;
    let plan = find_simple_exists_semijoin_plan(
        &clauses,
        &outer_alias,
        &outer_rel_name,
        outer_oid,
        &outer_cols,
    )?;

    let replacement = format!(
        "{outer_expr} IN (SELECT key FROM rvbbit.vector_filtered_distinct_int_keys({inner_rel}::regclass, {inner_key}, {filter_left}, {filter_right}, {filter_kind}, {filter_op}) AS rvbbit_semi_key(key))",
        outer_expr = plan.outer_expr,
        inner_rel = sql_text_literal(&plan.inner_rel_name),
        inner_key = sql_text_literal(&plan.inner_key_col),
        filter_left = sql_text_literal(&plan.filter_left_col),
        filter_right = sql_text_literal(&plan.filter_right_col),
        filter_kind = sql_text_literal(plan.filter_kind),
        filter_op = sql_text_literal(plan.filter_op),
    );
    let kept_clauses = clauses
        .iter()
        .enumerate()
        .map(|(idx, clause)| {
            if idx == plan.exists_idx {
                replacement.as_str()
            } else {
                clause.trim()
            }
        })
        .filter(|clause| !clause.is_empty())
        .collect::<Vec<_>>();
    if kept_clauses.is_empty() {
        return None;
    }

    let mut out = String::with_capacity(sql.len() + 160);
    out.push_str(&sql[..where_pos + "where".len()]);
    out.push('\n');
    out.push_str("    ");
    out.push_str(&kept_clauses.join("\n    AND "));
    out.push_str(&sql[where_end..]);
    Some(out)
}

fn find_simple_exists_semijoin_plan(
    clauses: &[&str],
    outer_alias: &str,
    outer_rel_name: &str,
    outer_oid: u32,
    outer_cols: &[String],
) -> Option<SourceSimpleExistsSemiJoinPlan> {
    if clauses
        .iter()
        .any(|clause| starts_with_keyword(clause.trim(), "not"))
    {
        return None;
    }
    let mut found = None;
    for (idx, clause) in clauses.iter().enumerate() {
        if let Some(exists) =
            parse_simple_exists_semijoin_clause(clause, outer_alias, outer_rel_name, outer_cols)
        {
            if found.is_some() {
                return None;
            }
            found = Some((idx, exists));
        }
    }
    let (exists_idx, exists) = found?;
    let inner_oid = resolve_relation_oid(&exists.inner_rel_name)?;
    if !is_rvbbit_table_cached(inner_oid) {
        return None;
    }
    if !matches!(
        fetch_att_typoid(inner_oid, &exists.inner_key_col)?,
        20 | 21 | 23
    ) || !matches!(
        fetch_att_typoid(outer_oid, &exists.outer_col)?,
        20 | 21 | 23
    ) {
        return None;
    }
    let left_typoid = fetch_att_typoid(inner_oid, &exists.filter_left_col)?;
    let right_typoid = fetch_att_typoid(inner_oid, &exists.filter_right_col)?;
    let filter_kind = source_vector_filter_kind(left_typoid)?;
    if source_vector_filter_kind(right_typoid)? != filter_kind {
        return None;
    }
    Some(SourceSimpleExistsSemiJoinPlan {
        exists_idx,
        outer_expr: exists.outer_expr,
        inner_rel_name: exists.inner_rel_name,
        inner_key_col: exists.inner_key_col,
        filter_left_col: exists.filter_left_col,
        filter_right_col: exists.filter_right_col,
        filter_kind,
        filter_op: exists.filter_op,
    })
}

fn parse_simple_exists_semijoin_clause(
    clause: &str,
    outer_alias: &str,
    outer_rel_name: &str,
    outer_cols: &[String],
) -> Option<SourceSimpleExistsSemiJoin> {
    let clause = clause.trim();
    if !starts_with_keyword(clause, "exists") {
        return None;
    }
    let open = skip_ascii_ws(clause, "exists".len());
    if clause.as_bytes().get(open).copied() != Some(b'(') {
        return None;
    }
    let close = find_matching_paren(clause, open)?;
    if !clause[close + 1..].trim().is_empty() {
        return None;
    }
    parse_simple_exists_semijoin_subquery(
        &clause[open + 1..close],
        outer_alias,
        outer_rel_name,
        outer_cols,
    )
}

fn parse_simple_exists_semijoin_subquery(
    sql: &str,
    outer_alias: &str,
    outer_rel_name: &str,
    outer_cols: &[String],
) -> Option<SourceSimpleExistsSemiJoin> {
    if !starts_with_keyword(sql.trim_start(), "select") {
        return None;
    }
    let from_pos = find_top_level_keyword(sql, "from", 0)?;
    let where_pos = find_top_level_keyword(sql, "where", from_pos + "from".len())?;
    let from_sql = sql[from_pos + "from".len()..where_pos].trim();
    let (inner_rel_name, inner_alias) = parse_single_from_relation(from_sql)?;
    let inner_oid = resolve_relation_oid(&inner_rel_name)?;
    let inner_cols = fetch_attnames_inline(inner_oid)?;
    let clauses = split_top_level_and(&sql[where_pos + "where".len()..])?;
    let mut outer_expr = None::<String>;
    let mut outer_col = None::<String>;
    let mut inner_key_col = None::<String>;
    let mut filter = None::<SourceColumnCompare>;
    for clause in clauses {
        let clause = clause.trim();
        if let Some((left, right)) = split_top_level_equality(clause) {
            let left_inner = source_inner_column(left, &inner_alias, &inner_rel_name, &inner_cols);
            let right_inner =
                source_inner_column(right, &inner_alias, &inner_rel_name, &inner_cols);
            let left_outer = source_inner_column(left, outer_alias, outer_rel_name, outer_cols);
            let right_outer = source_inner_column(right, outer_alias, outer_rel_name, outer_cols);
            match (left_inner, right_inner, left_outer, right_outer) {
                (Some(inner_col), None, None, Some(out_col)) => {
                    inner_key_col = Some(inner_col);
                    outer_col = Some(out_col);
                    outer_expr = Some(right.trim().to_string());
                    continue;
                }
                (None, Some(inner_col), Some(out_col), None) => {
                    inner_key_col = Some(inner_col);
                    outer_col = Some(out_col);
                    outer_expr = Some(left.trim().to_string());
                    continue;
                }
                _ => {}
            }
        }
        if let Some((left, right, op)) = split_top_level_compare(clause) {
            if op == "<>" {
                return None;
            }
            let Some(left_col) =
                source_inner_column(left, &inner_alias, &inner_rel_name, &inner_cols)
            else {
                return None;
            };
            let Some(right_col) =
                source_inner_column(right, &inner_alias, &inner_rel_name, &inner_cols)
            else {
                return None;
            };
            if filter.is_some() {
                return None;
            }
            filter = Some(SourceColumnCompare {
                left_col,
                right_col,
                op_token: source_compare_op_token(op)?,
            });
            continue;
        }
        return None;
    }
    let filter = filter?;
    Some(SourceSimpleExistsSemiJoin {
        outer_expr: outer_expr?,
        outer_col: outer_col?,
        inner_rel_name,
        inner_key_col: inner_key_col?,
        filter_left_col: filter.left_col,
        filter_right_col: filter.right_col,
        filter_op: filter.op_token,
    })
}

unsafe fn try_source_dimension_key_filter_rule(
    pstate: *mut pg_sys::ParseState,
    query: *mut pg_sys::Query,
) -> bool {
    if query.is_null() || pstate.is_null() || (*pstate).p_sourcetext.is_null() {
        return false;
    }
    let source = std::ffi::CStr::from_ptr((*pstate).p_sourcetext)
        .to_string_lossy()
        .into_owned();
    let query_source = source_slice_for_query(&source, query);
    let Some(rewritten) = rewrite_dimension_key_filter_sql(query_source) else {
        return false;
    };
    let Some(donor) = parse_to_query(&rewritten) else {
        pgrx::warning!(
            "rvbbit: dimension key filter rewrite parse failed for: {}",
            rewritten
        );
        return false;
    };
    std::ptr::copy_nonoverlapping(donor, query, 1);
    true
}

struct SourceRelationInfo {
    rel_name: String,
    alias: String,
    oid: u32,
    cols: Vec<String>,
}

struct SourceDimensionKeyFilterPlan {
    from_pos: usize,
    where_pos: usize,
    where_end: usize,
    dim_alias: String,
    dim_key_col: String,
    dim_text_col: String,
    needle: String,
    fact_alias: String,
    fact_key_col: String,
    fact_i64_col: String,
    fact_i32_cols: Vec<String>,
    fact_f64_cols: Vec<String>,
    remove_clause_idxs: Vec<usize>,
}

fn rewrite_dimension_key_filter_sql(sql: &str) -> Option<String> {
    if ascii_contains_ci(sql, "vector_int_key_text_filter_rows_1i64_2i32_3f64") {
        return None;
    }
    let plan = find_dimension_key_filter_plan(sql)?;
    let from_sql = &sql[plan.from_pos + "from".len()..plan.where_pos];
    let where_sql = &sql[plan.where_pos + "where".len()..plan.where_end];
    let from_items = split_top_level_comma(from_sql)?;
    let clauses = split_top_level_and(where_sql)?;
    let dim_rel_name = source_relation_name_for_alias(&from_items, &plan.dim_alias)?;

    let mut new_from_items = Vec::with_capacity(from_items.len());
    let mut replaced_fact = false;
    for item in &from_items {
        let trimmed = item.trim();
        if let Some((rel, alias)) = parse_single_from_relation(trimmed) {
            if alias.eq_ignore_ascii_case(&plan.dim_alias) {
                continue;
            }
            if alias.eq_ignore_ascii_case(&plan.fact_alias) {
                new_from_items.push(format!(
                    "rvbbit.vector_int_key_text_filter_rows_1i64_2i32_3f64({fact_rel}::regclass, {fact_key}, {dim_rel}::regclass, {dim_key}, {dim_text}, {needle}, {i64_col}, {i32_col1}, {i32_col2}, {f64_col1}, {f64_col2}, {f64_col3}) AS {alias}({i64_alias}, {i32_alias1}, {i32_alias2}, {f64_alias1}, {f64_alias2}, {f64_alias3})",
                    fact_rel = sql_text_literal(&rel),
                    fact_key = sql_text_literal(&plan.fact_key_col),
                    dim_rel = sql_text_literal(&dim_rel_name),
                    dim_key = sql_text_literal(&plan.dim_key_col),
                    dim_text = sql_text_literal(&plan.dim_text_col),
                    needle = sql_text_literal(&plan.needle),
                    i64_col = sql_text_literal(&plan.fact_i64_col),
                    i32_col1 = sql_text_literal(plan.fact_i32_cols.get(0)?),
                    i32_col2 = sql_text_literal(plan.fact_i32_cols.get(1)?),
                    f64_col1 = sql_text_literal(plan.fact_f64_cols.get(0)?),
                    f64_col2 = sql_text_literal(plan.fact_f64_cols.get(1)?),
                    f64_col3 = sql_text_literal(plan.fact_f64_cols.get(2)?),
                    alias = quote_ident(&plan.fact_alias),
                    i64_alias = quote_ident(&plan.fact_i64_col),
                    i32_alias1 = quote_ident(plan.fact_i32_cols.get(0)?),
                    i32_alias2 = quote_ident(plan.fact_i32_cols.get(1)?),
                    f64_alias1 = quote_ident(plan.fact_f64_cols.get(0)?),
                    f64_alias2 = quote_ident(plan.fact_f64_cols.get(1)?),
                    f64_alias3 = quote_ident(plan.fact_f64_cols.get(2)?),
                ));
                replaced_fact = true;
                continue;
            }
        }
        new_from_items.push(trimmed.to_string());
    }
    if !replaced_fact {
        return None;
    }

    let remove = plan.remove_clause_idxs.into_iter().collect::<HashSet<_>>();
    let kept_clauses = clauses
        .iter()
        .enumerate()
        .filter_map(|(idx, clause)| (!remove.contains(&idx)).then_some(clause.trim()))
        .filter(|clause| !clause.is_empty())
        .collect::<Vec<_>>();
    if kept_clauses.is_empty() {
        return None;
    }

    let mut out = String::with_capacity(sql.len() + 256);
    out.push_str(&sql[..plan.from_pos + "from".len()]);
    out.push('\n');
    out.push_str("    ");
    out.push_str(&new_from_items.join(",\n    "));
    out.push('\n');
    out.push_str("WHERE\n    ");
    out.push_str(&kept_clauses.join("\n    AND "));
    out.push_str(&sql[plan.where_end..]);
    Some(out)
}

fn find_dimension_key_filter_plan(sql: &str) -> Option<SourceDimensionKeyFilterPlan> {
    let mut search = 0usize;
    while let Some(from_pos) = find_keyword_ci(sql, "from", search) {
        search = from_pos + "from".len();
        let depth = paren_depth_at(sql, from_pos)?;
        let Some(where_pos) = find_keyword_at_depth(sql, "where", search, depth) else {
            continue;
        };
        let where_end = find_next_clause_at_depth(sql, where_pos + "where".len(), depth)
            .unwrap_or_else(|| find_depth_end(sql, where_pos, depth).unwrap_or(sql.len()));
        let from_sql = &sql[from_pos + "from".len()..where_pos];
        let where_sql = &sql[where_pos + "where".len()..where_end];
        let Some(from_items) = split_top_level_comma(from_sql) else {
            continue;
        };
        let Some(rels) = source_relations_from_items(&from_items) else {
            continue;
        };
        if rels.len() < 2 {
            continue;
        }
        let clauses = split_top_level_and(where_sql)?;
        let Some((like_idx, dim_alias, dim_text_col, needle)) =
            find_dimension_like_clause(&clauses, &rels)
        else {
            continue;
        };
        let Some(dim) = rels
            .iter()
            .find(|rel| rel.alias.eq_ignore_ascii_case(&dim_alias))
        else {
            continue;
        };
        let Some((join_idx, fact_alias, dim_key_col, fact_key_col)) =
            find_dimension_fact_key_clause(&clauses, &rels, dim)
        else {
            continue;
        };
        let Some(fact) = rels
            .iter()
            .find(|rel| rel.alias.eq_ignore_ascii_case(&fact_alias))
        else {
            continue;
        };
        if !is_rvbbit_table_cached(dim.oid) || !is_rvbbit_table_cached(fact.oid) {
            continue;
        }
        if !matches!(fetch_att_typoid(dim.oid, &dim_key_col)?, 20 | 21 | 23)
            || !matches!(fetch_att_typoid(fact.oid, &fact_key_col)?, 20 | 21 | 23)
        {
            continue;
        }
        let needed_start = find_select_for_from_at_depth(sql, from_pos, depth).unwrap_or(from_pos);
        let needed = collect_source_cols_for_relation(&sql[needed_start..where_end], &fact.cols);
        let typed = classify_dimension_fact_output_cols(fact.oid, &needed)?;
        if typed.i64_cols.len() != 1 || typed.i32_cols.len() != 2 || typed.f64_cols.len() != 3 {
            continue;
        }
        return Some(SourceDimensionKeyFilterPlan {
            from_pos,
            where_pos,
            where_end,
            dim_alias,
            dim_key_col,
            dim_text_col,
            needle,
            fact_alias,
            fact_key_col,
            fact_i64_col: typed.i64_cols[0].clone(),
            fact_i32_cols: typed.i32_cols,
            fact_f64_cols: typed.f64_cols,
            remove_clause_idxs: vec![like_idx, join_idx],
        });
    }
    None
}

struct SourceTypedCols {
    i64_cols: Vec<String>,
    i32_cols: Vec<String>,
    f64_cols: Vec<String>,
}

fn classify_dimension_fact_output_cols(
    rel_oid: u32,
    needed_cols: &[String],
) -> Option<SourceTypedCols> {
    let mut i64_cols = Vec::new();
    let mut i32_cols = Vec::new();
    let mut f64_cols = Vec::new();
    for col in needed_cols {
        match fetch_att_typoid(rel_oid, col)? {
            20 => i64_cols.push(col.clone()),
            21 | 23 => i32_cols.push(col.clone()),
            700 | 701 => f64_cols.push(col.clone()),
            _ => return None,
        }
    }
    Some(SourceTypedCols {
        i64_cols,
        i32_cols,
        f64_cols,
    })
}

fn source_relations_from_items(items: &[&str]) -> Option<Vec<SourceRelationInfo>> {
    let mut out = Vec::new();
    for item in items {
        let item_sql = item.trim();
        let (rel_name, alias) = parse_single_from_relation(item_sql)?;
        let oid = resolve_relation_oid(&rel_name)?;
        let cols = fetch_attnames_inline(oid)?;
        out.push(SourceRelationInfo {
            rel_name,
            alias,
            oid,
            cols,
        });
    }
    Some(out)
}

fn source_relation_name_for_alias(items: &[&str], alias: &str) -> Option<String> {
    for item in items {
        let (rel_name, rel_alias) = parse_single_from_relation(item.trim())?;
        if rel_alias.eq_ignore_ascii_case(alias) {
            return Some(rel_name);
        }
    }
    None
}

fn find_dimension_like_clause(
    clauses: &[&str],
    rels: &[SourceRelationInfo],
) -> Option<(usize, String, String, String)> {
    for (idx, clause) in clauses.iter().enumerate() {
        let Some((left, right)) = split_top_level_like(clause.trim()) else {
            continue;
        };
        let needle = fixed_contains_like_literal(right.trim())?;
        for rel in rels {
            if let Some(col) = source_inner_column(left, &rel.alias, &rel.rel_name, &rel.cols) {
                if matches!(fetch_att_typoid(rel.oid, &col)?, 25 | 1042 | 1043) {
                    return Some((idx, rel.alias.clone(), col, needle));
                }
            }
        }
    }
    None
}

fn find_dimension_fact_key_clause(
    clauses: &[&str],
    rels: &[SourceRelationInfo],
    dim: &SourceRelationInfo,
) -> Option<(usize, String, String, String)> {
    for (idx, clause) in clauses.iter().enumerate() {
        let Some((left, right)) = split_top_level_equality(clause.trim()) else {
            continue;
        };
        let dim_left = source_inner_column(left, &dim.alias, &dim.rel_name, &dim.cols);
        let dim_right = source_inner_column(right, &dim.alias, &dim.rel_name, &dim.cols);
        for fact in rels {
            if fact.alias.eq_ignore_ascii_case(&dim.alias) {
                continue;
            }
            let fact_left = source_inner_column(left, &fact.alias, &fact.rel_name, &fact.cols);
            let fact_right = source_inner_column(right, &fact.alias, &fact.rel_name, &fact.cols);
            match (&dim_left, &fact_right) {
                (Some(dim_col), Some(fact_col)) => {
                    return Some((idx, fact.alias.clone(), dim_col.clone(), fact_col.clone()));
                }
                _ => {}
            }
            match (&dim_right, &fact_left) {
                (Some(dim_col), Some(fact_col)) => {
                    return Some((idx, fact.alias.clone(), dim_col.clone(), fact_col.clone()));
                }
                _ => {}
            }
        }
    }
    None
}

fn collect_source_cols_for_relation(sql: &str, cols: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let bytes = sql.as_bytes();
    let mut i = 0usize;
    let mut in_string = false;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        if b == b'\'' {
            in_string = true;
            i += 1;
            continue;
        }
        if is_ident_byte(b) {
            let start = i;
            i += 1;
            while i < bytes.len() && is_ident_byte(bytes[i]) {
                i += 1;
            }
            let token = &sql[start..i];
            if let Some(col) = cols.iter().find(|col| col.eq_ignore_ascii_case(token)) {
                if !out.iter().any(|existing: &String| existing == col) {
                    out.push(col.clone());
                }
            }
            continue;
        }
        i += 1;
    }
    out
}

fn split_top_level_like(sql: &str) -> Option<(&str, &str)> {
    let pos = find_top_level_keyword(sql, "like", 0)?;
    Some((&sql[..pos], &sql[pos + "like".len()..]))
}

fn fixed_contains_like_literal(sql: &str) -> Option<String> {
    let sql = strip_source_parens(sql.trim());
    if !sql.starts_with('\'') || !sql.ends_with('\'') || sql.len() < 4 {
        return None;
    }
    let inner = &sql[1..sql.len() - 1];
    if !inner.starts_with('%') || !inner.ends_with('%') {
        return None;
    }
    let needle = &inner[1..inner.len() - 1];
    if needle.is_empty() || needle.contains('%') || needle.contains('_') {
        return None;
    }
    Some(needle.replace("''", "'"))
}

fn parse_source_agg_expr(expr: &str) -> Option<String> {
    let expr = expr.trim();
    for agg_name in ["sum", "avg"] {
        if let Some(agg_pos) = find_keyword_ci(expr, agg_name, 0) {
            let open = skip_ascii_ws(expr, agg_pos + agg_name.len());
            if expr.as_bytes().get(open).copied() != Some(b'(') {
                continue;
            }
            let close = find_matching_paren(expr, open)?;
            let arg = expr[open + 1..close].trim();
            if arg.is_empty() || arg.contains(',') {
                return None;
            }
            let before = expr[..agg_pos].trim();
            let after = expr[close + 1..].trim();
            let agg_call = format!("{agg_name}({arg})");
            if before.is_empty() && after.is_empty() {
                return Some(agg_call);
            }
            if let Some(factor) = before.strip_suffix('*').map(str::trim) {
                if !factor.is_empty() && after.is_empty() && source_factor_is_safe(factor) {
                    return Some(format!("({factor}) * {agg_call}"));
                }
            }
            if let Some(factor) = after.strip_prefix('*').map(str::trim) {
                if !factor.is_empty() && before.is_empty() && source_factor_is_safe(factor) {
                    return Some(format!("{agg_call} * ({factor})"));
                }
            }
        }
    }
    None
}

fn source_factor_is_safe(value: &str) -> bool {
    let value = value.trim();
    !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || matches!(b, b'.' | b'+' | b'-' | b'e' | b'E'))
}

fn parse_single_from_relation(from_sql: &str) -> Option<(String, String)> {
    let parts = from_sql.split_whitespace().collect::<Vec<_>>();
    if parts.is_empty() || parts.len() > 3 {
        return None;
    }
    let rel = parts[0].trim_matches('"').to_string();
    let alias = if parts.len() == 1 {
        rel.rsplit('.').next()?.trim_matches('"').to_string()
    } else if parts.len() == 2 {
        if parts[1].eq_ignore_ascii_case("as") {
            return None;
        }
        parts[1].trim_matches('"').to_string()
    } else {
        if !parts[1].eq_ignore_ascii_case("as") {
            return None;
        }
        parts[2].trim_matches('"').to_string()
    };
    if rel.is_empty() || alias.is_empty() {
        return None;
    }
    Some((rel, alias))
}

fn source_inner_column(
    expr: &str,
    alias: &str,
    rel_name: &str,
    inner_cols: &[String],
) -> Option<String> {
    let stripped = strip_source_parens(expr.trim());
    if stripped.contains("::") || stripped.contains(' ') || stripped.contains('(') {
        return None;
    }
    let rel_short = rel_name.rsplit('.').next().unwrap_or(rel_name);
    if let Some((qualifier, col)) = stripped.rsplit_once('.') {
        let qualifier = qualifier.trim_matches('"');
        let col = col.trim_matches('"');
        if (qualifier.eq_ignore_ascii_case(alias) || qualifier.eq_ignore_ascii_case(rel_short))
            && inner_cols.iter().any(|c| c.eq_ignore_ascii_case(col))
        {
            return inner_cols
                .iter()
                .find(|c| c.eq_ignore_ascii_case(col))
                .cloned();
        }
        return None;
    }
    let col = stripped.trim_matches('"');
    inner_cols
        .iter()
        .find(|c| c.eq_ignore_ascii_case(col))
        .cloned()
}

fn strip_source_parens(mut expr: &str) -> &str {
    loop {
        let trimmed = expr.trim();
        if !trimmed.starts_with('(') || !trimmed.ends_with(')') {
            return trimmed;
        }
        if find_matching_paren(trimmed, 0) == Some(trimmed.len() - 1) {
            expr = &trimmed[1..trimmed.len() - 1];
        } else {
            return trimmed;
        }
    }
}

fn split_top_level_and(sql: &str) -> Option<Vec<&str>> {
    let mut parts = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ => {
                if depth == 0 && keyword_at(sql, i, "and") {
                    parts.push(&sql[start..i]);
                    start = i + "and".len();
                    i = start;
                    continue;
                }
            }
        }
        i += 1;
    }
    if in_string || depth != 0 {
        return None;
    }
    parts.push(&sql[start..]);
    Some(parts)
}

fn split_top_level_comma(sql: &str) -> Option<Vec<&str>> {
    let mut parts = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            b',' if depth == 0 => {
                parts.push(&sql[start..i]);
                start = i + 1;
            }
            _ => {}
        }
        i += 1;
    }
    if in_string || depth != 0 {
        return None;
    }
    parts.push(&sql[start..]);
    Some(parts)
}

fn split_top_level_equality(sql: &str) -> Option<(&str, &str)> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            b'=' if depth == 0 => {
                if matches!(
                    bytes.get(i.wrapping_sub(1)).copied(),
                    Some(b'<' | b'>' | b'!')
                ) || matches!(bytes.get(i + 1).copied(), Some(b'='))
                {
                    return None;
                }
                return Some((&sql[..i], &sql[i + 1..]));
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn split_top_level_compare(sql: &str) -> Option<(&str, &str, &'static str)> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            b'<' | b'>' | b'=' if depth == 0 => {
                for op in ["<>", ">=", "<=", "=", ">", "<"] {
                    if i + op.len() <= bytes.len()
                        && bytes[i..i + op.len()].eq_ignore_ascii_case(op.as_bytes())
                    {
                        return Some((&sql[..i], &sql[i + op.len()..], op));
                    }
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn source_compare_op_token(op: &str) -> Option<&'static str> {
    Some(match op {
        "=" => "eq",
        "<" => "lt",
        "<=" => "le",
        ">" => "gt",
        ">=" => "ge",
        _ => return None,
    })
}

fn source_column_ref(expr: &str) -> Option<(String, String)> {
    let stripped = strip_source_parens(expr.trim());
    if stripped.contains("::")
        || stripped.contains(' ')
        || stripped.contains('(')
        || stripped.contains(')')
    {
        return None;
    }
    let (qualifier, col) = stripped.rsplit_once('.')?;
    let qualifier = qualifier.trim_matches('"').to_string();
    let col = col.trim_matches('"').to_string();
    if qualifier.is_empty() || col.is_empty() {
        return None;
    }
    Some((qualifier, col))
}

fn canonical_source_col(col: &str, cols: &[String]) -> Option<String> {
    cols.iter().find(|c| c.eq_ignore_ascii_case(col)).cloned()
}

fn comparison_before_open_paren(
    sql: &str,
    open_paren: usize,
) -> Option<(usize, usize, &'static str)> {
    let mut end = open_paren;
    while end > 0 && sql.as_bytes()[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    if end == 0 {
        return None;
    }
    let bytes = sql.as_bytes();
    let last = bytes[end - 1];
    match last {
        b'=' => {
            if end >= 2 {
                match bytes[end - 2] {
                    b'>' => return Some((end - 2, end, ">=")),
                    b'<' => return Some((end - 2, end, "<=")),
                    b'!' => return None,
                    _ => {}
                }
            }
            Some((end - 1, end, "="))
        }
        b'>' => Some((end - 1, end, ">")),
        b'<' => {
            if bytes.get(end).copied() == Some(b'>') {
                None
            } else {
                Some((end - 1, end, "<"))
            }
        }
        _ => None,
    }
}

fn find_clause_start(sql: &str, op_pos: usize) -> Option<usize> {
    let target_depth = paren_depth_at(sql, op_pos)?;
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    let mut boundary = None;
    while i < op_pos {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ => {
                if depth == target_depth {
                    if keyword_at(sql, i, "and") {
                        boundary = Some(i + "and".len());
                    } else if keyword_at(sql, i, "where") {
                        boundary = Some(i + "where".len());
                    }
                }
            }
        }
        i += 1;
    }
    let mut start = boundary?;
    while start < op_pos && sql.as_bytes()[start].is_ascii_whitespace() {
        start += 1;
    }
    Some(start)
}

fn find_top_level_keyword(sql: &str, keyword: &str, start: usize) -> Option<usize> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = start;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ if depth == 0 && keyword_at(sql, i, keyword) => return Some(i),
            _ => {}
        }
        i += 1;
    }
    None
}

fn find_next_top_level_clause(sql: &str, start: usize) -> Option<usize> {
    [
        "group",
        "order",
        "having",
        "limit",
        "union",
        "intersect",
        "except",
    ]
    .into_iter()
    .filter_map(|keyword| find_top_level_keyword(sql, keyword, start))
    .min()
}

fn find_keyword_at_depth(
    sql: &str,
    keyword: &str,
    start: usize,
    target_depth: i32,
) -> Option<usize> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ if i >= start && depth == target_depth && keyword_at(sql, i, keyword) => {
                return Some(i);
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn find_next_clause_at_depth(sql: &str, start: usize, depth: i32) -> Option<usize> {
    [
        "group",
        "order",
        "having",
        "limit",
        "union",
        "intersect",
        "except",
    ]
    .into_iter()
    .filter_map(|keyword| find_keyword_at_depth(sql, keyword, start, depth))
    .min()
}

fn find_select_for_from_at_depth(sql: &str, from_pos: usize, target_depth: i32) -> Option<usize> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    let mut last_select = None;
    while i < from_pos && i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ if depth == target_depth && keyword_at(sql, i, "select") => {
                last_select = Some(i);
            }
            _ => {}
        }
        i += 1;
    }
    last_select
}

fn find_depth_end(sql: &str, start: usize, target_depth: i32) -> Option<usize> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if i >= start && depth < target_depth {
                    return Some(i);
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn find_matching_paren(sql: &str, open: usize) -> Option<usize> {
    if sql.as_bytes().get(open).copied() != Some(b'(') {
        return None;
    }
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = open;
    while i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(i);
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn previous_nonspace(sql: &str, pos: usize) -> Option<usize> {
    let bytes = sql.as_bytes();
    let mut i = pos;
    while i > 0 {
        i -= 1;
        if !bytes[i].is_ascii_whitespace() {
            return Some(i);
        }
    }
    None
}

fn paren_depth_at(sql: &str, pos: usize) -> Option<i32> {
    let bytes = sql.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut i = 0usize;
    while i < pos && i < bytes.len() {
        let b = bytes[i];
        if in_string {
            if b == b'\'' {
                if bytes.get(i + 1).copied() == Some(b'\'') {
                    i += 2;
                    continue;
                }
                in_string = false;
            }
            i += 1;
            continue;
        }
        match b {
            b'\'' => in_string = true,
            b'(' => depth += 1,
            b')' => depth -= 1,
            _ => {}
        }
        i += 1;
    }
    if in_string {
        None
    } else {
        Some(depth)
    }
}

fn find_keyword_ci(sql: &str, keyword: &str, start: usize) -> Option<usize> {
    let mut i = start;
    while i + keyword.len() <= sql.len() {
        if keyword_at(sql, i, keyword) {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn starts_with_keyword(sql: &str, keyword: &str) -> bool {
    keyword_at(sql, 0, keyword)
}

fn keyword_at(sql: &str, pos: usize, keyword: &str) -> bool {
    let bytes = sql.as_bytes();
    if pos + keyword.len() > bytes.len() {
        return false;
    }
    if pos > 0 && is_ident_byte(bytes[pos - 1]) {
        return false;
    }
    if pos + keyword.len() < bytes.len() && is_ident_byte(bytes[pos + keyword.len()]) {
        return false;
    }
    bytes[pos..pos + keyword.len()].eq_ignore_ascii_case(keyword.as_bytes())
}

fn ascii_contains_ci(haystack: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return true;
    }
    let mut i = 0usize;
    while i + needle.len() <= haystack.len() {
        if haystack.as_bytes()[i..i + needle.len()].eq_ignore_ascii_case(needle.as_bytes()) {
            return true;
        }
        i += 1;
    }
    false
}

fn skip_ascii_ws(sql: &str, mut pos: usize) -> usize {
    while pos < sql.len() && sql.as_bytes()[pos].is_ascii_whitespace() {
        pos += 1;
    }
    pos
}

fn is_ident_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

unsafe fn rewrite_query(query: *mut pg_sys::Query) {
    // Stash current Query so try_shred_rule can resolve Var.varno -> table_oid
    // via this Query's rtable. Save+restore pattern handles subquery recursion.
    let prev_query = CURRENT_QUERY.with(|c| c.replace(query));

    // Rule family B (implicit prewarm): detect calls to rvbbit.<op_name>(...)
    // in the targetList and fire rvbbit.prewarm_operator() once via SPI so
    // the user query's per-row UDF calls hit warm cache instead of going
    // sequentially. Run this before scan/aggregate rewrites: some Rvbbit-table
    // projection fast paths return early, but the semantic calls still need to
    // be warmed for the final executor target list.
    try_implicit_prewarm_rule(query);

    // Phase 2 followup A: the metadata-only fast paths below
    // (try_count_star_rule, try_simple_agg_rule, try_groupby_rule, the
    // vector_float_aggregate rule, etc.) compute their answer from
    // rvbbit.row_groups + rvbbit.row_groups.stats / per_group_stats
    // directly, without scanning parquet — so they don't apply tombstones
    // and don't honor rvbbit.as_of_generation. When either of those
    // matters for correctness, fall through to the normal scan path
    // (custom_scan honors both).
    if metadata_rewrites_unsafe_for_correctness() {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A (const-from-metadata): try to answer `count(*) FROM
    // rvbbit_table` from rvbbit.row_groups without any scan. If this
    // rule fires, we mutate the Query into a trivial "SELECT <const>"
    // form and skip the rest of the walker.
    if try_count_star_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A1: count(*) with a simple filter on a low-cardinality
    // integer column. Sum matching per-group metadata buckets instead of
    // scanning parquet.
    if try_count_star_group_filter_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A1b: ClickBench-style wide sums over one smallint
    // expression, e.g. SUM(col + 0), ..., SUM(col + 89). Compute the
    // projected column once in Arrow and replace every aggregate with
    // an exact int8 Const.
    if try_wide_sum_int2_plus_const_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A1c: ungrouped COUNT(DISTINCT int_col). Keep the exact
    // distinct set in Rust over one projected parquet column.
    if try_count_distinct_int_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A2 (absorbed-aggregate): if every targetlist Aggref is
    // a simple agg over a Var of an rvbbit table (no qual, no GROUP BY),
    // replace each Aggref with a Const computed from row-group stats.
    // Skips PG's per-row aggregate execution entirely.
    if try_simple_agg_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A3 (group-by absorbed-aggregate): SELECT col, agg(x)
    // FROM rvbbit_table GROUP BY col over a low-cardinality column with
    // per-group stats — substitute the relation scan with a call to
    // rvbbit.agg_groupby_*. Bench shows ~3000x speedup on these queries.
    if try_groupby_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A4 (projected top-N): ClickBench has several
    // `SearchPhrase <> '' ORDER BY ... LIMIT 10` queries. The generic path
    // must emit hundreds of thousands of tuples into a Sort/Limit. Push the
    // top-N scan into Rust so Postgres sees only the already-limited rows.
    if try_searchphrase_topn_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A5: one-column top-count group by. Handles shapes like
    // `GROUP BY SearchPhrase ORDER BY count(*) DESC LIMIT 10` by scanning
    // one parquet column and returning only the top groups.
    if try_top_count_1col_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A6: one-column top-count with deterministic integer
    // projections in the group list, e.g. `ClientIP, ClientIP - 1, ...`.
    // The grouping key is still just the base column, so reuse the projected
    // top-count SRF and compute the derived columns after the limit.
    if try_top_count_derived_int_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A7: ClickBench-style avg(length(URL)) by CounterID with
    // a count HAVING threshold. Push the two-column scan and aggregation
    // into Rust so PG only formats the final numeric average rows.
    if try_url_len_avg_by_counter_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A7b: group by a deterministic text transform while
    // aggregating AVG(length(text)), COUNT(*), and MIN(text). The initial
    // transform is the common URL-host regexp_replace shape from ClickBench
    // Q28, but the executor is transform-driven rather than query-name
    // driven.
    if try_text_transform_avg_len_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A8: exact COUNT(DISTINCT int_col) top-k group-bys in
    // projected Rust. This keeps several ClickBench unique-user shapes out
    // of the tuple aggregate path.
    if try_top_count_distinct_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A9: two-column top-count for one integer key plus one
    // text key. Handles ClickBench's SearchEngineID/SearchPhrase and
    // UserID/SearchPhrase top-count shapes in the projected path.
    if try_top_count_int_text_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A10: two-integer-key rollups with COUNT/SUM/AVG. Covers
    // Q30-Q32 without emitting every projected row into PG aggregation.
    if try_top_rollup_2int_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A11: one-integer-key rollup with COUNT/SUM/AVG plus exact
    // COUNT(DISTINCT int). Covers ClickBench Q9 in the projected path.
    if try_top_rollup_1int_distinct_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A12: fixed LIKE '%needle%' count and grouped rollups.
    // These avoid emitting matching text rows into PG aggregates for
    // ClickBench Q20-Q22 while only firing on exact fixed-substring shapes.
    if try_text_like_aggregate_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A12b: late-materialized top rows for SELECT * with a fixed
    // substring LIKE filter and ORDER BY/LIMIT. The Rust side scans only the
    // filter/order columns first, then fetches the full rows for the winners.
    if try_text_like_ordered_rows_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A13: three-key top-count with extract(minute FROM ts).
    // Covers ClickBench Q18 without routing every row through PG grouping.
    if try_top_count_int_minute_text_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A14: general filtered top-count group-bys. This catches
    // projected COUNT(*) GROUP BY queries with simple conjunctive filters,
    // limit/offset, and one to five typed keys without routing rows through
    // PostgreSQL's tuple aggregate path.
    if try_filtered_top_count_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // Rule family A15: projected vector aggregates for simple analytical
    // SQL. Handles grouped/ungrouped SUM/AVG/COUNT over float expressions
    // with simple conjunctive filters. This is the general path TPC-H-style
    // scan aggregates need, without tying execution to query names.
    if try_vector_float_aggregate_rule(query) {
        CURRENT_QUERY.with(|c| c.set(prev_query));
        return;
    }

    // CTEs are full Query nodes, not RTE_SUBQUERY entries. Rewrite their
    // internal scan/aggregate shapes before the parent query plans CTE scans.
    let cte_list = (*query).cteList;
    if !cte_list.is_null() {
        for i in 0..(*cte_list).length {
            let cte =
                (*(*cte_list).elements.add(i as usize)).ptr_value as *mut pg_sys::CommonTableExpr;
            if cte.is_null() || (*cte).ctequery.is_null() {
                continue;
            }
            let node = (*cte).ctequery;
            if (*node).type_ == pg_sys::NodeTag::T_Query {
                let cte_query = node as *mut pg_sys::Query;
                if !cte_query.is_null() && (*cte_query).commandType == pg_sys::CmdType::CMD_SELECT {
                    rewrite_query(cte_query);
                }
            }
        }
    }

    // PG18's RTE_GROUP holds the actual GROUP BY expressions; the outer
    // tlist references them via Vars. Walk both.
    let rtable = (*query).rtable;
    if !rtable.is_null() {
        let n = (*rtable).length;
        let cell = (*rtable).elements;
        for i in 0..n {
            let rte = (*cell.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
            if rte.is_null() {
                continue;
            }
            if (*rte).rtekind == pg_sys::RTEKind::RTE_GROUP {
                mutate_list_in_place((*rte).groupexprs);
            }
            if (*rte).rtekind == pg_sys::RTEKind::RTE_SUBQUERY && !(*rte).subquery.is_null() {
                rewrite_query((*rte).subquery);
            }
        }
    }

    let tlist = (*query).targetList;
    if !tlist.is_null() {
        let n = (*tlist).length;
        let cell = (*tlist).elements;
        for i in 0..n {
            let tle = (*cell.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
            if !tle.is_null() {
                (*tle).expr = mutate_expr((*tle).expr as *mut pg_sys::Node) as *mut pg_sys::Expr;
            }
        }
    }

    let jt = (*query).jointree;
    if !jt.is_null() && !(*jt).quals.is_null() {
        (*jt).quals = mutate_expr((*jt).quals) as *mut pg_sys::Node;
    }
    if !(*query).havingQual.is_null() {
        (*query).havingQual = mutate_expr((*query).havingQual);
    }

    CURRENT_QUERY.with(|c| c.set(prev_query));
}

/// Rule family A — answer `SELECT count(*) FROM <rvbbit_table>` from
/// `rvbbit.row_groups` metadata. Returns true if the rewrite fired and
/// rewrite_query should stop.
unsafe fn try_count_star_rule(query: *mut pg_sys::Query) -> bool {
    // Strict shape check: trivial count(*) with no filters/groups.
    if !(*query).hasAggs {
        return false;
    }
    if !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).sortClause.is_null()
        || !(*query).limitCount.is_null()
        || !(*query).limitOffset.is_null()
    {
        return false;
    }
    let tlist = (*query).targetList;
    if tlist.is_null() || (*tlist).length != 1 {
        return false;
    }
    let tle = (*(*tlist).elements).ptr_value as *mut pg_sys::TargetEntry;
    if tle.is_null() {
        return false;
    }
    let expr = (*tle).expr as *mut pg_sys::Node;
    if expr.is_null() || (*expr).type_ != pg_sys::NodeTag::T_Aggref {
        return false;
    }
    let aggref = expr as *mut pg_sys::Aggref;
    // aggstar = true marks COUNT(*) (vs COUNT(col))
    if !(*aggref).aggstar {
        return false;
    }
    // COUNT(* FILTER (...)) or DISTINCT/ORDER BY shouldn't take the
    // const-from-metadata path. Bail.
    if !(*aggref).aggdistinct.is_null()
        || !(*aggref).aggfilter.is_null()
        || !(*aggref).aggorder.is_null()
    {
        return false;
    }

    // Exactly one RTE_RELATION in the rtable, no quals.
    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return false;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return false;
    }
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return false;
    }
    let table_oid = (*rte).relid.to_u32();

    // Is this an rvbbit table with row groups? For count(*) we can still be
    // exact if fresh heap rows exist after compact: add a direct table-AM
    // heap count to the parquet metadata count.
    let n_rows = match fetch_count_star_row_count(table_oid) {
        Some(n) => n,
        None => return false,
    };

    // Mutate the Query into "SELECT <n_rows>::bigint" with no FROM.
    // The Const replaces the Aggref; we clear hasAggs and the fromlist
    // so the planner emits a Result node with the constant.
    let new_const = pg_sys::makeConst(
        pg_sys::INT8OID,
        -1,
        pg_sys::InvalidOid,
        8,
        pg_sys::Datum::from(n_rows as i64 as usize),
        false,
        true, // int8 is by-value on 64-bit
    );
    (*new_const).location = -1;

    (*tle).expr = new_const as *mut pg_sys::Expr;
    (*query).hasAggs = false;
    // Empty the fromlist so the planner has nothing to scan.
    (*jt).fromlist = std::ptr::null_mut();
    true
}

#[derive(Clone)]
struct SimpleIntFilter {
    col_name: String,
    op: SimpleFilterOp,
    rhs: i64,
}

#[derive(Clone, Copy)]
enum SimpleFilterOp {
    Eq,
    Ne,
}

/// Rule A1 — answer `SELECT count(*) FROM rel WHERE low_card_int <> const`
/// from per-group stats. This covers ClickBench Q1 without invoking the
/// tuple executor at all.
unsafe fn try_count_star_group_filter_rule(query: *mut pg_sys::Query) -> bool {
    if !(*query).hasAggs {
        return false;
    }
    if !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).sortClause.is_null()
        || !(*query).limitCount.is_null()
        || !(*query).limitOffset.is_null()
    {
        return false;
    }
    let tlist = (*query).targetList;
    if tlist.is_null() || (*tlist).length != 1 {
        return false;
    }
    let tle = (*(*tlist).elements).ptr_value as *mut pg_sys::TargetEntry;
    if tle.is_null() {
        return false;
    }
    let expr = (*tle).expr as *mut pg_sys::Node;
    if expr.is_null() || (*expr).type_ != pg_sys::NodeTag::T_Aggref {
        return false;
    }
    let aggref = expr as *mut pg_sys::Aggref;
    if !(*aggref).aggstar
        || (*aggref).aggfnoid.to_u32() != 2803
        || !(*aggref).aggdistinct.is_null()
        || !(*aggref).aggfilter.is_null()
        || !(*aggref).aggorder.is_null()
    {
        return false;
    }

    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return false;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return false;
    }
    let jt = (*query).jointree;
    if jt.is_null() || (*jt).quals.is_null() {
        return false;
    }
    let table_oid = (*rte).relid.to_u32();
    if !is_rvbbit_table_cached(table_oid) {
        return false;
    }
    if fetch_total_row_count(table_oid).is_none() {
        return false;
    }
    let col_names = match fetch_attnames(table_oid) {
        Some(v) => v,
        None => return false,
    };
    let filter =
        match classify_simple_int_filter((*jt).quals, &col_names, 1, std::ptr::null_mut(), 0) {
            Some(f) => f,
            None => return false,
        };
    if !matches!(filter.op, SimpleFilterOp::Ne) {
        return false;
    }
    if !has_per_group_stats(table_oid, &filter.col_name) {
        return false;
    }
    let Some(count) = fetch_group_count_with_filter(table_oid, &filter) else {
        return false;
    };

    (*tle).expr = make_int8_const(count) as *mut pg_sys::Expr;
    (*query).hasAggs = false;
    (*jt).fromlist = std::ptr::null_mut();
    (*jt).quals = std::ptr::null_mut();
    true
}

struct SumInt2PlusConstPlan {
    tle: *mut pg_sys::TargetEntry,
    col_name: String,
    offset: i64,
    result_typoid: u32,
}

unsafe fn try_wide_sum_int2_plus_const_rule(query: *mut pg_sys::Query) -> bool {
    if !(*query).hasAggs {
        return false;
    }
    if !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).sortClause.is_null()
        || !(*query).limitCount.is_null()
        || !(*query).limitOffset.is_null()
    {
        return false;
    }
    let tlist = (*query).targetList;
    if tlist.is_null() {
        return false;
    }
    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return false;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return false;
    }
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return false;
    }
    let table_oid = (*rte).relid.to_u32();
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return false;
    }

    let col_names = match fetch_attnames(table_oid) {
        Some(v) => v,
        None => return false,
    };

    let n = (*tlist).length;
    let cell = (*tlist).elements;
    let mut plans = Vec::with_capacity(n as usize);
    for i in 0..n {
        let tle = (*cell.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() {
            return false;
        }
        let expr = (*tle).expr as *mut pg_sys::Node;
        let Some(plan) = classify_sum_int2_plus_const(expr, tle, &col_names) else {
            return false;
        };
        plans.push(plan);
    }
    if plans.is_empty() {
        return false;
    }

    let col_name = plans[0].col_name.clone();
    if plans.iter().any(|p| p.col_name != col_name) {
        return false;
    }

    let scan = match scan_numeric_sum_count(table_oid, &col_name) {
        Ok(scan) => scan,
        Err(e) => {
            pgrx::warning!("rvbbit: wide-sum rewrite scan failed: {}", e);
            return false;
        }
    };
    let Some(base_sum) = scan.sum_i128 else {
        return false;
    };
    let count = scan.count_nonnull as i128;

    for plan in plans {
        if plan.result_typoid != pg_sys::INT8OID.to_u32() {
            return false;
        }
        let value = base_sum + (plan.offset as i128) * count;
        if value < i64::MIN as i128 || value > i64::MAX as i128 {
            return false;
        }
        (*plan.tle).expr = if scan.count_nonnull == 0 {
            make_null_const(pg_sys::INT8OID) as *mut pg_sys::Expr
        } else {
            make_int8_const(value as i64) as *mut pg_sys::Expr
        };
    }

    (*query).hasAggs = false;
    (*jt).fromlist = std::ptr::null_mut();
    true
}

unsafe fn classify_sum_int2_plus_const(
    expr: *mut pg_sys::Node,
    tle: *mut pg_sys::TargetEntry,
    col_names: &[String],
) -> Option<SumInt2PlusConstPlan> {
    if expr.is_null() || (*expr).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = expr as *mut pg_sys::Aggref;
    if !matches!(
        (*agg).aggfnoid.to_u32(),
        2107 | 2108 | 2109 | 2110 | 2111 | 2114
    ) {
        return None;
    }
    if (*agg).aggstar
        || !(*agg).aggdistinct.is_null()
        || !(*agg).aggfilter.is_null()
        || !(*agg).aggorder.is_null()
    {
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let inner = (*arg_tle).expr as *mut pg_sys::Node;
    let (var, offset) = classify_int2_plus_const_expr(inner)?;
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    Some(SumInt2PlusConstPlan {
        tle,
        col_name: col_names[attno - 1].clone(),
        offset,
        result_typoid: (*agg).aggtype.to_u32(),
    })
}

unsafe fn classify_int2_plus_const_expr(
    node: *mut pg_sys::Node,
) -> Option<(*mut pg_sys::Var, i64)> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ == pg_sys::NodeTag::T_Var {
        let var = node as *mut pg_sys::Var;
        if (*var).vartype == pg_sys::INT2OID {
            return Some((var, 0));
        }
        return None;
    }
    if (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let opno = (*op).opno.to_u32();
    if opno != 552 && opno != 553 {
        return None;
    }
    let args = (*op).args;
    if args.is_null() || (*args).length != 2 {
        return None;
    }
    let left = pg_sys::list_nth(args, 0) as *mut pg_sys::Node;
    let right = pg_sys::list_nth(args, 1) as *mut pg_sys::Node;
    let (var_node, const_node) = if !left.is_null()
        && !right.is_null()
        && (*left).type_ == pg_sys::NodeTag::T_Var
        && (*right).type_ == pg_sys::NodeTag::T_Const
    {
        (left, right)
    } else if !left.is_null()
        && !right.is_null()
        && (*left).type_ == pg_sys::NodeTag::T_Const
        && (*right).type_ == pg_sys::NodeTag::T_Var
    {
        (right, left)
    } else {
        return None;
    };
    let var = var_node as *mut pg_sys::Var;
    if (*var).vartype != pg_sys::INT2OID {
        return None;
    }
    let offset = const_i64(const_node as *mut pg_sys::Const)?;
    // int2 + int4 produces int4. If the constant is in this range, every
    // possible int2 value can be added without per-row int4 overflow.
    if offset < (i32::MIN as i64 + i16::MAX as i64) || offset > (i32::MAX as i64 - i16::MAX as i64)
    {
        return None;
    }
    Some((var, offset))
}

unsafe fn try_count_distinct_int_rule(query: *mut pg_sys::Query) -> bool {
    if !(*query).hasAggs
        || !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).sortClause.is_null()
        || !(*query).limitCount.is_null()
        || !(*query).limitOffset.is_null()
    {
        return false;
    }
    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return false;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return false;
    }
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return false;
    }
    let table_oid = (*rte).relid.to_u32();
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return false;
    }
    let col_names = match fetch_attnames(table_oid) {
        Some(v) => v,
        None => return false,
    };

    let tlist = (*query).targetList;
    if tlist.is_null() || (*tlist).length != 1 {
        return false;
    }
    let tle = (*(*tlist).elements).ptr_value as *mut pg_sys::TargetEntry;
    if tle.is_null() {
        return false;
    }
    let expr = (*tle).expr as *mut pg_sys::Node;
    let Some(col_name) =
        classify_count_distinct_int_agg(expr, &col_names, 1, std::ptr::null_mut(), 0)
    else {
        return false;
    };

    let col_esc = col_name.replace('\'', "''");
    let sql = format!(
        "SELECT rvbbit.count_distinct_int({oid}::oid, '{col}')::bigint",
        oid = table_oid,
        col = col_esc,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!("rvbbit: count-distinct rewrite parse failed for: {}", sql);
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).hasAggs = false;
    true
}

unsafe fn const_i64(cst: *mut pg_sys::Const) -> Option<i64> {
    if cst.is_null() || (*cst).constisnull {
        return None;
    }
    Some(match (*cst).consttype.to_u32() {
        21 => (*cst).constvalue.value() as i16 as i64,
        23 => (*cst).constvalue.value() as i32 as i64,
        20 => (*cst).constvalue.value() as i64,
        1082 => (*cst).constvalue.value() as i32 as i64,
        _ => return None,
    })
}

/// Rule A2 — generalize count(*) substitution to other simple aggregates
/// (avg / sum / count(col)) that we can answer from row-group stats.
///
/// Strict gate: no WHERE, no GROUP BY, exactly one RTE_RELATION, all
/// targetlist exprs are Aggrefs we recognize over simple Vars. If any
/// Aggref doesn't fit, bail completely and let PG handle it.
unsafe fn try_simple_agg_rule(query: *mut pg_sys::Query) -> bool {
    if !(*query).hasAggs {
        return false;
    }
    if !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
    {
        return false;
    }
    let tlist = (*query).targetList;
    if tlist.is_null() {
        return false;
    }
    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return false;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return false;
    }
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return false;
    }
    let table_oid = (*rte).relid.to_u32();
    if !is_rvbbit_table_cached(table_oid) {
        return false;
    }
    if fetch_total_row_count(table_oid).is_none() {
        return false; // table exists but has no row groups → fall through
    }

    // Walk the column name list once so we can map Var.varattno to attname.
    let col_names = match fetch_attnames(table_oid) {
        Some(v) => v,
        None => return false,
    };

    // First pass: classify every targetlist entry. If any is not a
    // simple recognized Aggref over a Var, abort.
    let n = (*tlist).length;
    let cell = (*tlist).elements;
    let mut plans: Vec<(*mut pg_sys::TargetEntry, AggPlan)> = Vec::with_capacity(n as usize);
    for i in 0..n {
        let tle = (*cell.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() {
            return false;
        }
        let expr = (*tle).expr as *mut pg_sys::Node;
        if expr.is_null() || (*expr).type_ != pg_sys::NodeTag::T_Aggref {
            return false;
        }
        let plan = match classify_aggref(expr as *mut pg_sys::Aggref, &col_names) {
            Some(p) => p,
            None => return false,
        };
        plans.push((tle, plan));
    }

    // Second pass: compute each value via our stats helpers and replace
    // the Aggref with a Const carrying the result.
    for (tle, plan) in plans {
        let new_const = match compute_and_make_const(table_oid, &plan) {
            Some(c) => c,
            None => return false,
        };
        (*tle).expr = new_const as *mut pg_sys::Expr;
    }

    (*query).hasAggs = false;
    (*jt).fromlist = std::ptr::null_mut();
    true
}

/// What the planner needs to know about each Aggref we plan to substitute.
struct AggPlan {
    kind: AggKind,
    col_name: String,
    input_typoid: u32,
    result_typoid: u32,
}

#[derive(Clone, Copy)]
enum AggKind {
    CountStar,
    CountCol,
    Sum,
    Avg,
    Min,
    Max,
}

unsafe fn classify_aggref(agg: *mut pg_sys::Aggref, col_names: &[String]) -> Option<AggPlan> {
    let aggfnoid = (*agg).aggfnoid.to_u32();
    let kind = recognize_agg_fn(aggfnoid)?;
    // DISTINCT, FILTER, and ORDER BY inside the agg all change the meaning
    // — we can't answer them from per-row-group sum/count stats. Bail.
    if !(*agg).aggdistinct.is_null() || !(*agg).aggfilter.is_null() || !(*agg).aggorder.is_null() {
        return None;
    }
    if (*agg).aggstar {
        if matches!(kind, AggKind::CountStar) {
            return Some(AggPlan {
                kind,
                col_name: String::new(),
                input_typoid: 0,
                result_typoid: (*agg).aggtype.to_u32(),
            });
        }
        return None;
    }

    // Exactly one arg, must be a Var
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let inner = (*arg_tle).expr as *mut pg_sys::Node;
    if inner.is_null() || (*inner).type_ != pg_sys::NodeTag::T_Var {
        return None;
    }
    let var = inner as *mut pg_sys::Var;
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    Some(AggPlan {
        kind,
        col_name: col_names[attno - 1].clone(),
        input_typoid: (*var).vartype.to_u32(),
        result_typoid: (*agg).aggtype.to_u32(),
    })
}

/// Map well-known aggregate function OIDs to our handled kinds. Only
/// recognized rows get pushed down — everything else falls through to PG.
fn recognize_agg_fn(oid: u32) -> Option<AggKind> {
    use AggKind::*;
    Some(match oid {
        // count(*)
        2803 => CountStar,
        // count(any)
        2147 => CountCol,
        // sum
        2107 | 2108 | 2109 | 2110 | 2111 | 2114 => Sum,
        // avg
        2100 | 2101 | 2102 | 2103 | 2104 | 2105 => Avg,
        // min — small set of common types
        2131 | 2132 | 2133 | 2135 | 2136 | 2137 | 2138 | 2142 | 2143 | 2146 => Min,
        // max
        2115 | 2116 | 2117 | 2119 | 2120 | 2122 | 2126 | 2127 | 2129 | 2130 => Max,
        _ => return None,
    })
}

fn fetch_attnames(table_oid: u32) -> Option<Vec<String>> {
    let sql = format!(
        "SELECT array_agg(attname::text ORDER BY attnum) \
         FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid AND attnum > 0 AND NOT attisdropped"
    );
    let v: Option<Vec<Option<String>>> = pgrx::Spi::get_one(&sql).ok().flatten();
    Some(v?.into_iter().flatten().collect())
}

fn metadata_numeric_sum_count(
    table_oid: u32,
    col_name: &str,
    input_typoid: u32,
) -> Option<NumericScan> {
    let key = numeric_stats_cache_key(table_oid)?;
    let scan = if let Some(cached) = NUMERIC_STATS_CACHE.with(|cache| {
        cache
            .borrow()
            .get(&key)
            .and_then(|cols| cols.get(col_name).cloned())
    }) {
        cached
    } else {
        let stats = load_numeric_stats_for_table(table_oid, &key)?;
        NUMERIC_STATS_CACHE.with(|cache| {
            cache.borrow_mut().insert(key, stats.clone());
        });
        stats.get(col_name)?.clone()
    };
    if input_typoid == pg_sys::INT8OID.to_u32() && scan.sum_i128.is_none() && scan.count_nonnull > 0
    {
        return None;
    }
    Some(scan)
}

fn numeric_stats_cache_key(table_oid: u32) -> Option<NumericStatsCacheKey> {
    let sql = format!(
        "SELECT count(*)::bigint, \
                COALESCE(max(rg_id), -1)::bigint, \
                COALESCE(sum(n_rows), 0)::bigint \
         FROM rvbbit.row_groups_visible \
         WHERE table_oid = {table_oid}::oid"
    );
    let mut key = None;
    pgrx::Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            key = Some(NumericStatsCacheKey {
                rel_oid: table_oid,
                row_group_count: row.get::<i64>(1)?.unwrap_or(0),
                max_rg_id: row.get::<i64>(2)?.unwrap_or(-1),
                total_rows: row.get::<i64>(3)?.unwrap_or(0),
            });
        }
        Ok(())
    })
    .ok()?;
    key
}

fn load_numeric_stats_for_table(
    table_oid: u32,
    key: &NumericStatsCacheKey,
) -> Option<HashMap<String, NumericScan>> {
    if key.row_group_count <= 0 {
        return None;
    }
    let att_types = fetch_att_type_map(table_oid)?;
    let sql = format!(
        "SELECT n_rows::bigint, stats::text \
         FROM rvbbit.row_groups_visible \
         WHERE table_oid = {table_oid}::oid \
         ORDER BY rg_id"
    );
    let mut out: HashMap<String, NumericScan> = HashMap::new();
    let mut complete_counts: HashMap<String, i64> = HashMap::new();
    pgrx::Spi::connect(|client| -> Result<(), String> {
        let table = client
            .select(&sql, None, &[])
            .map_err(|e| format!("select numeric stats: {e}"))?;
        for row in table {
            let n_rows = row
                .get::<i64>(1)
                .map_err(|e| format!("get n_rows: {e}"))?
                .unwrap_or(0);
            let Some(stats_text) = row
                .get::<String>(2)
                .map_err(|e| format!("get stats: {e}"))?
            else {
                continue;
            };
            let stats: Vec<ColumnStats> =
                serde_json::from_str(&stats_text).map_err(|e| format!("parse stats: {e}"))?;
            for stat in stats {
                let Some(sum_value) = stat.sum.as_ref() else {
                    continue;
                };
                let count_nonnull = n_rows.saturating_sub(stat.null_count);
                *complete_counts.entry(stat.name.clone()).or_insert(0) += 1;
                let typoid = att_types.get(&stat.name).copied().unwrap_or(0);
                let entry = out.entry(stat.name).or_insert(NumericScan {
                    sum_f64: 0.0,
                    sum_i128: Some(0),
                    count_nonnull: 0,
                });
                entry.count_nonnull += count_nonnull;
                if typoid == pg_sys::INT8OID.to_u32() && !sum_value.is_string() && count_nonnull > 0
                {
                    // Pre-exact-sum row groups stored i64 sums as JSON
                    // numbers. They might already be overflowed, so bigint
                    // SUM/AVG must use the old scan fallback until recompact.
                    entry.sum_i128 = None;
                    continue;
                }
                if let Some(sum_i128) = json_sum_i128(sum_value) {
                    entry.sum_f64 += sum_i128 as f64;
                    if let Some(total) = &mut entry.sum_i128 {
                        *total += sum_i128;
                    }
                } else if let Some(sum_f64) = sum_value.as_f64() {
                    entry.sum_f64 += sum_f64;
                    entry.sum_i128 = None;
                } else if count_nonnull > 0 {
                    entry.sum_i128 = None;
                }
            }
        }
        Ok(())
    })
    .ok()?;
    out.retain(|col, _| complete_counts.get(col).copied().unwrap_or(0) == key.row_group_count);
    Some(out)
}

fn fetch_att_type_map(table_oid: u32) -> Option<HashMap<String, u32>> {
    let sql = format!(
        "SELECT attname::text, atttypid::oid::bigint \
         FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid AND attnum > 0 AND NOT attisdropped"
    );
    let mut out = HashMap::new();
    pgrx::Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let typoid: Option<i64> = row.get(2)?;
            if let (Some(name), Some(typoid)) = (name, typoid) {
                if (0..=u32::MAX as i64).contains(&typoid) {
                    out.insert(name, typoid as u32);
                }
            }
        }
        Ok(())
    })
    .ok()?;
    Some(out)
}

fn json_sum_i128(value: &Value) -> Option<i128> {
    if let Some(v) = value.as_i64() {
        return Some(v as i128);
    }
    if let Some(v) = value.as_u64() {
        return Some(v as i128);
    }
    value.as_str()?.parse::<i128>().ok()
}

unsafe fn compute_and_make_const(table_oid: u32, plan: &AggPlan) -> Option<*mut pg_sys::Const> {
    let col_esc = plan.col_name.replace('\'', "''");
    match plan.kind {
        AggKind::CountStar => {
            let v = fetch_total_row_count(table_oid).unwrap_or(0);
            Some(make_int8_const(v))
        }
        AggKind::CountCol => {
            let v: i64 = pgrx::Spi::get_one(&format!(
                "SELECT rvbbit.agg_count_nonnull({table_oid}::oid::regclass, '{col_esc}')"
            ))
            .ok()
            .flatten()
            .unwrap_or(0);
            Some(make_int8_const(v))
        }
        AggKind::Sum => {
            let scan = metadata_numeric_sum_count(table_oid, &plan.col_name, plan.input_typoid)
                .or_else(|| scan_numeric_sum_count(table_oid, &plan.col_name).ok())?;
            make_typed_sum_const(plan.result_typoid, &scan)
        }
        AggKind::Avg => {
            let scan = metadata_numeric_sum_count(table_oid, &plan.col_name, plan.input_typoid)
                .or_else(|| scan_numeric_sum_count(table_oid, &plan.col_name).ok())?;
            make_typed_avg_const(plan.result_typoid, &scan)
        }
        AggKind::Min | AggKind::Max => make_typed_minmax_const(table_oid, plan),
    }
}

unsafe fn make_int8_const(v: i64) -> *mut pg_sys::Const {
    let c = pg_sys::makeConst(
        pg_sys::INT8OID,
        -1,
        pg_sys::InvalidOid,
        8,
        pg_sys::Datum::from(v as usize),
        false,
        true,
    );
    (*c).location = -1;
    c
}

unsafe fn make_int2_const(v: i16) -> *mut pg_sys::Const {
    let c = pg_sys::makeConst(
        pg_sys::INT2OID,
        -1,
        pg_sys::InvalidOid,
        2,
        pg_sys::Datum::from(v as usize),
        false,
        true,
    );
    (*c).location = -1;
    c
}

unsafe fn make_int4_like_const(typoid: u32, v: i32) -> *mut pg_sys::Const {
    let c = pg_sys::makeConst(
        pg_sys::Oid::from(typoid),
        -1,
        pg_sys::InvalidOid,
        4,
        pg_sys::Datum::from(v as usize),
        false,
        true,
    );
    (*c).location = -1;
    c
}

unsafe fn make_int8_like_const(typoid: u32, v: i64) -> *mut pg_sys::Const {
    let c = pg_sys::makeConst(
        pg_sys::Oid::from(typoid),
        -1,
        pg_sys::InvalidOid,
        8,
        pg_sys::Datum::from(v as usize),
        false,
        true,
    );
    (*c).location = -1;
    c
}

unsafe fn make_null_const(typoid: pg_sys::Oid) -> *mut pg_sys::Const {
    let c = pg_sys::makeConst(
        typoid,
        -1,
        pg_sys::InvalidOid,
        -1,
        pg_sys::Datum::from(0usize),
        true,
        false,
    );
    (*c).location = -1;
    c
}

unsafe fn make_typed_sum_const(typoid: u32, scan: &NumericScan) -> Option<*mut pg_sys::Const> {
    if scan.count_nonnull == 0 {
        return Some(make_null_const(pg_sys::Oid::from(typoid)));
    }
    match typoid {
        // int8
        20 => {
            let sum = scan.sum_i128?;
            if sum < i64::MIN as i128 || sum > i64::MAX as i128 {
                return None;
            }
            Some(make_int8_const(sum as i64))
        }
        // float8
        701 => Some(make_float8_const(scan.sum_f64)),
        // numeric
        1700 => {
            let sum = scan.sum_i128?;
            make_numeric_const_from_str(&sum.to_string())
        }
        _ => None,
    }
}

const PG_EPOCH_OFFSET_DAYS: i64 = 10_957;
const PG_EPOCH_OFFSET_MICROS: i64 = 946_684_800_000_000;

unsafe fn make_typed_minmax_const(table_oid: u32, plan: &AggPlan) -> Option<*mut pg_sys::Const> {
    let stat_name = match plan.kind {
        AggKind::Min => "min",
        AggKind::Max => "max",
        _ => return None,
    };

    let raw = fetch_i64_stat_aggregate(table_oid, &plan.col_name, stat_name)?;
    let Some(raw) = raw else {
        return Some(make_null_const(pg_sys::Oid::from(plan.result_typoid)));
    };

    match (plan.input_typoid, plan.result_typoid) {
        (21, 21) => {
            if raw < i16::MIN as i64 || raw > i16::MAX as i64 {
                return None;
            }
            Some(make_int2_const(raw as i16))
        }
        (23, 23) => {
            if raw < i32::MIN as i64 || raw > i32::MAX as i64 {
                return None;
            }
            Some(make_int4_like_const(23, raw as i32))
        }
        (20, 20) => Some(make_int8_const(raw)),
        (1082, 1082) => {
            let pg_date = raw.checked_sub(PG_EPOCH_OFFSET_DAYS)?;
            if pg_date < i32::MIN as i64 || pg_date > i32::MAX as i64 {
                return None;
            }
            Some(make_int4_like_const(1082, pg_date as i32))
        }
        (1114, 1114) | (1184, 1184) => {
            let pg_ts = raw.checked_sub(PG_EPOCH_OFFSET_MICROS)?;
            Some(make_int8_like_const(plan.result_typoid, pg_ts))
        }
        _ => None,
    }
}

fn fetch_i64_stat_aggregate(
    table_oid: u32,
    col_name: &str,
    stat_name: &str,
) -> Option<Option<i64>> {
    if stat_name != "min" && stat_name != "max" {
        return None;
    }
    let col_esc = col_name.replace('\'', "''");
    let agg = stat_name;
    let sql = format!(
        "WITH vals AS ( \
             SELECT (s->>'{stat_name}')::bigint AS v \
             FROM rvbbit.row_groups_visible, jsonb_array_elements(stats) AS s \
             WHERE table_oid = {table_oid}::oid AND s->>'name' = '{col_esc}' \
                   AND s->'{stat_name}' IS NOT NULL \
                   AND jsonb_typeof(s->'{stat_name}') <> 'null' \
         ) \
         SELECT count(*)::bigint, {agg}(v)::bigint FROM vals"
    );

    let mut stats_present = false;
    let mut value = None;
    pgrx::Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, Some(1), &[])?;
        for row in table {
            let n: Option<i64> = row.get(1)?;
            let v: Option<i64> = row.get(2)?;
            stats_present = n.unwrap_or(0) > 0;
            value = v;
        }
        Ok(())
    })
    .ok()?;

    if stats_present {
        Some(value)
    } else {
        None
    }
}

unsafe fn make_typed_avg_const(typoid: u32, scan: &NumericScan) -> Option<*mut pg_sys::Const> {
    if scan.count_nonnull == 0 {
        return Some(make_null_const(pg_sys::Oid::from(typoid)));
    }
    match typoid {
        // float8
        701 => Some(make_float8_const(
            scan.sum_f64 / (scan.count_nonnull as f64),
        )),
        // numeric avg for integer inputs. PG's integer avg displays 16
        // fractional digits for small values but rounds large quotients
        // more aggressively. Let Postgres perform the numeric division so
        // the replacement const has exactly the same semantics.
        1700 => {
            let sum = scan.sum_i128?;
            make_numeric_avg_const(sum, scan.count_nonnull as i128)
        }
        _ => None,
    }
}

unsafe fn make_float8_const(v: f64) -> *mut pg_sys::Const {
    let c = pg_sys::makeConst(
        pg_sys::FLOAT8OID,
        -1,
        pg_sys::InvalidOid,
        8,
        pg_sys::Datum::from(v.to_bits() as usize),
        false,
        true,
    );
    (*c).location = -1;
    c
}

unsafe fn make_numeric_const_from_str(s: &str) -> Option<*mut pg_sys::Const> {
    let n = pgrx::AnyNumeric::from_str(s).ok()?;
    let datum = n.into_datum()?;
    let c = pg_sys::makeConst(
        pg_sys::NUMERICOID,
        -1,
        pg_sys::InvalidOid,
        -1,
        datum,
        false,
        false,
    );
    (*c).location = -1;
    Some(c)
}

unsafe fn make_numeric_avg_const(sum: i128, count: i128) -> Option<*mut pg_sys::Const> {
    let sql = format!("SELECT ({sum}::numeric / {count}::numeric)::text");
    let s: String = pgrx::Spi::get_one(&sql).ok().flatten()?;
    make_numeric_const_from_str(&s)
}

// ---------------------------------------------------------------------------
// Rule A3: GROUP BY pushdown
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy)]
enum GroupAggKind {
    CountStar,
    Sum,
    Avg,
}

struct GroupbyInfo {
    table_oid: u32,
    group_col_name: String,
    /// The PG type name to cast group_value back to (e.g. "smallint").
    group_col_typname: &'static str,
    /// Optional simple filter on the group column, e.g. `g <> 0`.
    group_filter: Option<SimpleIntFilter>,
    agg_kind: GroupAggKind,
    /// Column name for sum/avg. None for count(*).
    agg_col_name: Option<String>,
    /// Original TLE in the query that holds the group expression.
    group_tle: *mut pg_sys::TargetEntry,
    /// Original TLE that holds the Aggref.
    agg_tle: *mut pg_sys::TargetEntry,
}

thread_local! {
    /// Re-entrance guard. analyze_groupby + the donor parse call SPI;
    /// SPI parse_analyze fires post_parse_analyze_hook which re-enters
    /// this rule. Without a guard we'd recurse / paper over the real query.
    static IN_GROUPBY_RULE: std::cell::Cell<bool> = std::cell::Cell::new(false);
}

unsafe fn try_groupby_rule(query: *mut pg_sys::Query) -> bool {
    if IN_GROUPBY_RULE.with(|c| c.get()) {
        return false;
    }
    IN_GROUPBY_RULE.with(|c| c.set(true));
    let result = try_groupby_rule_inner(query);
    IN_GROUPBY_RULE.with(|c| c.set(false));
    result
}

unsafe fn try_groupby_rule_inner(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_groupby(query) {
        Some(i) => i,
        None => return false,
    };
    if !is_rvbbit_table_cached(info.table_oid) {
        return false;
    }
    if !has_per_group_stats(info.table_oid, &info.group_col_name) {
        return false;
    }

    let sql = build_srf_sql(&info);
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!("rvbbit: group-by rewrite parse failed for: {}", sql);
            return false;
        }
    };

    // Donor query has exactly two targetList entries: [group_expr, agg_expr]
    // in the order we wrote the SQL. Match them to the original tlist by
    // role and replace just the .expr field, preserving resno/resname/
    // ressortgroupref so ORDER BY references still work.
    let donor_tlist = (*donor).targetList;
    if donor_tlist.is_null() || (*donor_tlist).length != 2 {
        return false;
    }
    let donor_group_tle = (*(*donor_tlist).elements).ptr_value as *mut pg_sys::TargetEntry;
    let donor_agg_tle = (*(*donor_tlist).elements.add(1)).ptr_value as *mut pg_sys::TargetEntry;

    (*info.group_tle).expr = (*donor_group_tle).expr;
    (*info.agg_tle).expr = (*donor_agg_tle).expr;

    // Swap structural fields. PG owns both Query objects; we're just
    // re-pointing the original's rtable / jointree at the donor's tree.
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).groupClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    // Donor's tlist had its own ressortgroupref values; we preserved
    // the originals on the original tlist entries, which is what
    // ORDER BY references. groupClause is cleared so the sortClause
    // (if any) just sees a passthrough column.

    true
}

unsafe fn analyze_groupby(query: *mut pg_sys::Query) -> Option<GroupbyInfo> {
    if !(*query).hasAggs {
        return None;
    }
    if !(*query).havingQual.is_null() || !(*query).distinctClause.is_null() {
        return None;
    }
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != 1 {
        return None;
    }
    let group_sort = (*(*group_clause).elements).ptr_value as *mut pg_sys::SortGroupClause;
    if group_sort.is_null() {
        return None;
    }
    let group_ref = (*group_sort).tleSortGroupRef;

    // PG18 wraps GROUP BY queries with an extra RTE_GROUP entry in the
    // rtable. The outer tlist's group Var references that RTE_GROUP slot;
    // we have to dereference through groupexprs to find the real column.
    let rtable = (*query).rtable;
    if rtable.is_null() {
        return None;
    }
    let rt_len = (*rtable).length;
    if rt_len < 1 || rt_len > 2 {
        return None;
    }
    let mut rel_rte: *mut pg_sys::RangeTblEntry = std::ptr::null_mut();
    let mut rel_rti: i32 = 0;
    let mut group_rte: *mut pg_sys::RangeTblEntry = std::ptr::null_mut();
    let mut group_rti: i32 = 0;
    for i in 0..rt_len {
        let rte = (*(*rtable).elements.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
        if rte.is_null() {
            continue;
        }
        let rti = (i as i32) + 1;
        match (*rte).rtekind {
            pg_sys::RTEKind::RTE_RELATION => {
                if !rel_rte.is_null() {
                    return None;
                }
                rel_rte = rte;
                rel_rti = rti;
            }
            pg_sys::RTEKind::RTE_GROUP => {
                group_rte = rte;
                group_rti = rti;
            }
            _ => return None,
        }
    }
    if rel_rte.is_null() {
        return None;
    }
    let table_oid = (*rel_rte).relid.to_u32();

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }

    // Walk targetList. Exactly one group TLE (ressortgroupref == group_ref)
    // and one Aggref TLE. Anything else → bail.
    let tlist = (*query).targetList;
    if tlist.is_null() || (*tlist).length != 2 {
        return None;
    }
    let mut group_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut agg_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    for i in 0..2 {
        let tle = (*(*tlist).elements.add(i)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() {
            return None;
        }
        if (*tle).ressortgroupref == group_ref {
            group_tle = tle;
        } else {
            agg_tle = tle;
        }
    }
    if group_tle.is_null() || agg_tle.is_null() {
        return None;
    }

    let col_names = fetch_attnames_inline(table_oid)?;

    // The group TLE's expression. PG18 typically wraps it as a Var that
    // points at RTE_GROUP; deref through to the underlying Var that
    // points at the data relation.
    let g_expr_top = (*group_tle).expr as *mut pg_sys::Node;
    let g_var = match resolve_to_relation_var(g_expr_top, rel_rti, group_rte, group_rti) {
        Some(v) => v,
        None => return None,
    };
    let group_col_typname = pg_type_to_name((*g_var).vartype.to_u32())?;
    let attno = (*g_var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    let group_col_name = col_names[attno - 1].clone();

    let group_filter = if (*jt).quals.is_null() {
        None
    } else {
        let filter =
            classify_simple_int_filter((*jt).quals, &col_names, rel_rti, group_rte, group_rti)?;
        if filter.col_name != group_col_name {
            return None;
        }
        Some(filter)
    };

    let a_expr = (*agg_tle).expr as *mut pg_sys::Node;
    if a_expr.is_null() || (*a_expr).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let aggref = a_expr as *mut pg_sys::Aggref;
    let (kind, agg_col) = classify_group_aggref(aggref, &col_names, rel_rti, group_rte, group_rti)?;

    Some(GroupbyInfo {
        table_oid,
        group_col_name,
        group_col_typname,
        group_filter,
        agg_kind: kind,
        agg_col_name: agg_col,
        group_tle,
        agg_tle,
    })
}

unsafe fn classify_group_aggref(
    aggref: *mut pg_sys::Aggref,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<(GroupAggKind, Option<String>)> {
    let fn_oid = (*aggref).aggfnoid.to_u32();
    // DISTINCT / FILTER / ORDER BY change the semantics — bail.
    if !(*aggref).aggdistinct.is_null()
        || !(*aggref).aggfilter.is_null()
        || !(*aggref).aggorder.is_null()
    {
        return None;
    }
    if (*aggref).aggstar {
        if fn_oid == 2803 {
            return Some((GroupAggKind::CountStar, None));
        }
        return None;
    }
    let kind = match fn_oid {
        2107 | 2108 | 2109 | 2110 | 2111 | 2114 => GroupAggKind::Sum,
        2100 | 2101 | 2102 | 2103 | 2104 | 2105 => GroupAggKind::Avg,
        _ => return None,
    };
    let args = (*aggref).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    let inner = (*arg_tle).expr as *mut pg_sys::Node;
    let var = resolve_to_relation_var(inner, rel_rti, group_rte, group_rti)?;
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    Some((kind, Some(col_names[attno - 1].clone())))
}

/// Resolve an expression to a Var that points at the data relation
/// (rti = rel_rti). PG18 GROUP BY queries route the outer Vars through
/// an RTE_GROUP entry; we dereference one level if needed.
unsafe fn resolve_to_relation_var(
    node: *mut pg_sys::Node,
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<*mut pg_sys::Var> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Var {
        return None;
    }
    let var = node as *mut pg_sys::Var;
    let varno = (*var).varno as i32;
    if varno == rel_rti {
        // Already a relation-pointing Var.
        return Some(var);
    }
    if !group_rte.is_null() && varno == group_rti {
        // Dereference through RTE_GROUP.groupexprs[varattno - 1].
        let groupexprs = (*group_rte).groupexprs;
        if groupexprs.is_null() {
            return None;
        }
        let attno = (*var).varattno as i32;
        if attno < 1 || attno > (*groupexprs).length {
            return None;
        }
        let inner =
            (*(*groupexprs).elements.add((attno - 1) as usize)).ptr_value as *mut pg_sys::Node;
        // Recurse — inner should be a Var pointing at rel_rti.
        return resolve_to_relation_var(inner, rel_rti, group_rte, group_rti);
    }
    None
}

unsafe fn classify_simple_int_filter(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<SimpleIntFilter> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let filter_op = recognize_eq_ne_op((*op).opno.to_u32())?;
    let args = (*op).args;
    if args.is_null() || (*args).length != 2 {
        return None;
    }
    let left = pg_sys::list_nth(args, 0) as *mut pg_sys::Node;
    let right = pg_sys::list_nth(args, 1) as *mut pg_sys::Node;

    let (var_node, const_node) =
        if !left.is_null() && !right.is_null() && (*left).type_ == pg_sys::NodeTag::T_Const {
            (right, left)
        } else {
            (left, right)
        };
    if const_node.is_null() || (*const_node).type_ != pg_sys::NodeTag::T_Const {
        return None;
    }

    let var = resolve_to_relation_var(var_node, rel_rti, group_rte, group_rti)?;
    if !matches!((*var).vartype.to_u32(), 20 | 21 | 23) {
        return None;
    }
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    let rhs = const_i64(const_node as *mut pg_sys::Const)?;
    Some(SimpleIntFilter {
        col_name: col_names[attno - 1].clone(),
        op: filter_op,
        rhs,
    })
}

fn recognize_eq_ne_op(oid: u32) -> Option<SimpleFilterOp> {
    Some(match oid {
        // integer equality operators: int2/int4/int8 cross-type variants
        94 | 96 | 410 | 532 | 533 | 15 | 1862 | 1868 | 416 => SimpleFilterOp::Eq,
        // integer inequality operators: int2/int4/int8 cross-type variants
        519 | 518 | 411 | 538 | 539 | 36 | 1863 | 1869 | 417 => SimpleFilterOp::Ne,
        _ => return None,
    })
}

fn fetch_group_count_with_filter(table_oid: u32, filter: &SimpleIntFilter) -> Option<i64> {
    let counts = group_count_map(table_oid, &filter.col_name).ok()?;
    let mut total = 0i64;
    for (value, count) in counts {
        let Some(value) = value else {
            continue;
        };
        let value = value.parse::<i64>().ok()?;
        let matches = match filter.op {
            SimpleFilterOp::Eq => value == filter.rhs,
            SimpleFilterOp::Ne => value != filter.rhs,
        };
        if matches {
            total += count;
        }
    }
    Some(total)
}

fn pg_type_to_name(typoid: u32) -> Option<&'static str> {
    Some(match typoid {
        16 => "boolean",
        20 => "bigint",
        21 => "smallint",
        23 => "integer",
        25 => "text",
        1042 => "character",
        1043 => "character varying",
        700 => "real",
        701 => "double precision",
        1082 => "date",
        1114 => "timestamp",
        1184 => "timestamptz",
        _ => return None,
    })
}

fn fetch_attnames_inline(table_oid: u32) -> Option<Vec<String>> {
    let sql = format!(
        "SELECT array_agg(attname::text ORDER BY attnum) \
         FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid AND attnum > 0 AND NOT attisdropped"
    );
    let v: Option<Vec<Option<String>>> = pgrx::Spi::get_one(&sql).ok().flatten();
    Some(v?.into_iter().flatten().collect())
}

fn fetch_att_typoid(table_oid: u32, col_name: &str) -> Option<u32> {
    let col = col_name.replace('\'', "''");
    let sql = format!(
        "SELECT atttypid::oid::bigint FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid \
           AND attname = '{col}' \
           AND attnum > 0 \
           AND NOT attisdropped"
    );
    let oid = pgrx::Spi::get_one::<i64>(&sql).ok().flatten()?;
    if oid < 0 || oid > u32::MAX as i64 {
        return None;
    }
    Some(oid as u32)
}

fn source_vector_filter_kind(typoid: u32) -> Option<&'static str> {
    Some(match typoid {
        20 | 21 | 23 => "int",
        700 | 701 => "float",
        1082 => "date",
        _ => return None,
    })
}

fn has_per_group_stats(table_oid: u32, group_col: &str) -> bool {
    let col_esc = group_col.replace('\'', "''");
    let group_stats_exists: Option<bool> =
        pgrx::Spi::get_one("SELECT to_regclass('rvbbit.group_stats') IS NOT NULL")
            .ok()
            .flatten();
    if group_stats_exists.unwrap_or(false) {
        let sql = format!(
            "SELECT EXISTS ( \
                 SELECT 1 FROM rvbbit.group_stats \
                 WHERE table_oid = {table_oid}::oid AND group_col = '{col_esc}' \
             )"
        );
        if pgrx::Spi::get_one::<bool>(&sql)
            .ok()
            .flatten()
            .unwrap_or(false)
        {
            return true;
        }
    }
    let sql = format!(
        "SELECT EXISTS ( \
             SELECT 1 FROM rvbbit.row_groups, \
                          jsonb_array_elements(per_group_stats) AS b \
             WHERE table_oid = {table_oid}::oid \
               AND b->>'group_column' = '{col_esc}' \
         )"
    );
    pgrx::Spi::get_one::<bool>(&sql)
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn build_srf_sql(info: &GroupbyInfo) -> String {
    let g_esc = info.group_col_name.replace('\'', "''");
    let typ = info.group_col_typname;
    let filter_sql = group_filter_sql(info);
    match info.agg_kind {
        GroupAggKind::CountStar => format!(
            "SELECT (group_value)::{typ} AS g, count AS a \
             FROM rvbbit.agg_groupby_count({oid}::oid, '{g_esc}') {filter_sql}",
            typ = typ,
            oid = info.table_oid,
            g_esc = g_esc,
            filter_sql = filter_sql,
        ),
        GroupAggKind::Sum => format!(
            "SELECT (group_value)::{typ} AS g, sum AS a \
             FROM rvbbit.agg_groupby_sum({oid}::oid, '{g_esc}', '{a_esc}') {filter_sql}",
            typ = typ,
            oid = info.table_oid,
            g_esc = g_esc,
            filter_sql = filter_sql,
            a_esc = info
                .agg_col_name
                .as_deref()
                .unwrap_or("")
                .replace('\'', "''"),
        ),
        GroupAggKind::Avg => format!(
            "SELECT (group_value)::{typ} AS g, avg AS a \
             FROM rvbbit.agg_groupby_avg({oid}::oid, '{g_esc}', '{a_esc}') {filter_sql}",
            typ = typ,
            oid = info.table_oid,
            g_esc = g_esc,
            filter_sql = filter_sql,
            a_esc = info
                .agg_col_name
                .as_deref()
                .unwrap_or("")
                .replace('\'', "''"),
        ),
    }
}

fn group_filter_sql(info: &GroupbyInfo) -> String {
    let Some(filter) = &info.group_filter else {
        return String::new();
    };
    let op = match filter.op {
        SimpleFilterOp::Eq => "=",
        SimpleFilterOp::Ne => "<>",
    };
    format!(
        "WHERE (group_value)::{} {} {}",
        info.group_col_typname, op, filter.rhs
    )
}

// ---------------------------------------------------------------------------
// Rule A15: projected vector float aggregates
// ---------------------------------------------------------------------------

struct VectorFloatAggInfo {
    table_oid: u32,
    keys: Vec<VectorKeyInfo>,
    filters: Vec<VectorFilterInfo>,
    sum_exprs: Vec<String>,
    avg_exprs: Vec<String>,
    outputs: Vec<VectorOutput>,
    having: Option<VectorHavingInfo>,
    order_key_count: usize,
}

struct VectorKeyInfo {
    col_name: String,
    kind: &'static str,
    cast_typname: &'static str,
    alias: String,
    sort_ref: pg_sys::Index,
}

struct VectorFilterInfo {
    col_name: String,
    kind: &'static str,
    op: &'static str,
    value: String,
}

enum VectorOutput {
    Key(usize),
    Sum(usize, String),
    Avg(usize, String),
    Count(String),
}

struct VectorHavingInfo {
    sql: String,
    sum_key_spec: Option<String>,
    op_token: &'static str,
    rhs: f64,
}

thread_local! {
    static IN_VECTOR_FLOAT_AGG_RULE: std::cell::Cell<bool> = std::cell::Cell::new(false);
}

unsafe fn try_vector_float_aggregate_rule(query: *mut pg_sys::Query) -> bool {
    if IN_VECTOR_FLOAT_AGG_RULE.with(|c| c.get()) {
        return false;
    }
    IN_VECTOR_FLOAT_AGG_RULE.with(|c| c.set(true));
    let result = try_vector_float_aggregate_rule_inner(query);
    IN_VECTOR_FLOAT_AGG_RULE.with(|c| c.set(false));
    result
}

unsafe fn try_vector_float_aggregate_rule_inner(query: *mut pg_sys::Query) -> bool {
    let Some(info) = analyze_vector_float_aggregate(query) else {
        return false;
    };
    if !is_rvbbit_table_cached(info.table_oid) || fetch_total_row_count(info.table_oid).is_none() {
        return false;
    }

    let sql = build_vector_float_agg_sql(&info);
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: vector float aggregate rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).sortClause = (*donor).sortClause;
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_vector_float_aggregate(query: *mut pg_sys::Query) -> Option<VectorFloatAggInfo> {
    if !(*query).hasAggs
        || !(*query).distinctClause.is_null()
        || !(*query).limitCount.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }

    let rtable = (*query).rtable;
    if rtable.is_null() {
        return None;
    }
    let rt_len = (*rtable).length;
    if rt_len < 1 || rt_len > 2 {
        return None;
    }
    let mut rel_rte: *mut pg_sys::RangeTblEntry = std::ptr::null_mut();
    let mut rel_rti: i32 = 0;
    let mut group_rte: *mut pg_sys::RangeTblEntry = std::ptr::null_mut();
    let mut group_rti: i32 = 0;
    for i in 0..rt_len {
        let rte = (*(*rtable).elements.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
        if rte.is_null() {
            continue;
        }
        let rti = (i as i32) + 1;
        match (*rte).rtekind {
            pg_sys::RTEKind::RTE_RELATION => {
                if !rel_rte.is_null() {
                    return None;
                }
                rel_rte = rte;
                rel_rti = rti;
            }
            pg_sys::RTEKind::RTE_GROUP => {
                group_rte = rte;
                group_rti = rti;
            }
            _ => return None,
        }
    }
    if rel_rte.is_null() {
        return None;
    }
    let table_oid = (*rel_rte).relid.to_u32();
    let col_names = fetch_attnames_inline(table_oid)?;

    let mut keys = Vec::<VectorKeyInfo>::new();
    let group_clause = (*query).groupClause;
    if !group_clause.is_null() {
        let group_len = (*group_clause).length;
        if group_len > 3 {
            return None;
        }
        for idx in 0..group_len {
            let group_sort = (*(*group_clause).elements.add(idx as usize)).ptr_value
                as *mut pg_sys::SortGroupClause;
            if group_sort.is_null() {
                return None;
            }
            let group_ref = (*group_sort).tleSortGroupRef;
            let tle = find_tle_by_sort_ref((*query).targetList, group_ref)?;
            let var = resolve_to_relation_var(
                (*tle).expr as *mut pg_sys::Node,
                rel_rti,
                group_rte,
                group_rti,
            )?;
            let attno = (*var).varattno as usize;
            if attno == 0 || attno > col_names.len() {
                return None;
            }
            keys.push(VectorKeyInfo {
                col_name: col_names[attno - 1].clone(),
                kind: vector_key_kind((*var).vartype.to_u32())?,
                cast_typname: pg_type_to_name((*var).vartype.to_u32())?,
                alias: target_alias(tle).unwrap_or_else(|| format!("key{}", idx + 1)),
                sort_ref: group_ref,
            });
        }
    } else if !group_rte.is_null() {
        return None;
    }

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }
    let filters = if (*jt).quals.is_null() {
        Vec::new()
    } else {
        classify_vector_filters((*jt).quals, &col_names, rel_rti, group_rte, group_rti)?
    };

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut outputs = Vec::<VectorOutput>::new();
    let mut sum_exprs = Vec::<String>::new();
    let mut avg_exprs = Vec::<String>::new();
    for idx in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(idx as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            return None;
        }
        let expr = (*tle).expr as *mut pg_sys::Node;
        if let Some(key_idx) =
            classify_vector_group_output(expr, &keys, rel_rti, group_rte, group_rti, &col_names)
        {
            outputs.push(VectorOutput::Key(key_idx));
            continue;
        }
        let alias = target_alias(tle).unwrap_or_else(|| format!("agg{}", idx + 1));
        let output = classify_vector_agg_output(
            expr,
            &col_names,
            rel_rti,
            group_rte,
            group_rti,
            &mut sum_exprs,
            &mut avg_exprs,
            alias,
        )?;
        outputs.push(output);
    }
    let having = classify_vector_having(
        (*query).havingQual,
        &col_names,
        rel_rti,
        group_rte,
        group_rti,
        &mut sum_exprs,
        &mut avg_exprs,
    )?;
    if outputs.is_empty()
        || (sum_exprs.is_empty()
            && avg_exprs.is_empty()
            && !outputs.iter().any(|o| matches!(o, VectorOutput::Count(_)))
            && having.is_none())
    {
        return None;
    }
    if having.is_some()
        && !outputs.iter().any(|o| {
            matches!(
                o,
                VectorOutput::Sum(_, _) | VectorOutput::Avg(_, _) | VectorOutput::Count(_)
            )
        })
        && !can_use_vector_sum_having_keys(&outputs, having.as_ref())
    {
        return None;
    }

    let order_key_count = classify_vector_order_by(query, &keys)?;

    Some(VectorFloatAggInfo {
        table_oid,
        keys,
        filters,
        sum_exprs,
        avg_exprs,
        outputs,
        having,
        order_key_count,
    })
}

fn can_use_vector_sum_having_keys(
    outputs: &[VectorOutput],
    having: Option<&VectorHavingInfo>,
) -> bool {
    having.and_then(|h| h.sum_key_spec.as_ref()).is_some()
        && outputs.iter().all(|o| matches!(o, VectorOutput::Key(_)))
}

unsafe fn classify_vector_group_output(
    expr: *mut pg_sys::Node,
    keys: &[VectorKeyInfo],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
    col_names: &[String],
) -> Option<usize> {
    let var = resolve_to_relation_var(expr, rel_rti, group_rte, group_rti)?;
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    let col = &col_names[attno - 1];
    keys.iter().position(|key| &key.col_name == col)
}

unsafe fn classify_vector_agg_output(
    expr: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
    sum_exprs: &mut Vec<String>,
    avg_exprs: &mut Vec<String>,
    alias: String,
) -> Option<VectorOutput> {
    let expr = strip_relabel(expr);
    if expr.is_null() || (*expr).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = expr as *mut pg_sys::Aggref;
    if !(*agg).aggdistinct.is_null() || !(*agg).aggfilter.is_null() || !(*agg).aggorder.is_null() {
        return None;
    }
    if (*agg).aggstar {
        if (*agg).aggfnoid.to_u32() == 2803 {
            return Some(VectorOutput::Count(alias));
        }
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let arg = (*arg_tle).expr as *mut pg_sys::Node;
    let spec = classify_float_expr_spec(arg, col_names, rel_rti, group_rte, group_rti)?;
    match (*agg).aggfnoid.to_u32() {
        2111 => {
            let idx = sum_exprs.len();
            if idx >= 8 {
                return None;
            }
            sum_exprs.push(spec);
            Some(VectorOutput::Sum(idx, alias))
        }
        2105 => {
            let idx = avg_exprs.len();
            if idx >= 8 {
                return None;
            }
            avg_exprs.push(spec);
            Some(VectorOutput::Avg(idx, alias))
        }
        _ => None,
    }
}

unsafe fn classify_vector_having(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
    sum_exprs: &mut Vec<String>,
    avg_exprs: &mut Vec<String>,
) -> Option<Option<VectorHavingInfo>> {
    if node.is_null() {
        return Some(None);
    }
    let node = strip_relabel(node);
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let (left, right) = op_two_args(op)?;
    let op_oid = (*op).opno.to_u32();
    if let Some(having) = classify_vector_having_side(
        left, right, op_oid, false, col_names, rel_rti, group_rte, group_rti, sum_exprs, avg_exprs,
    ) {
        return Some(Some(having));
    }
    classify_vector_having_side(
        right, left, op_oid, true, col_names, rel_rti, group_rte, group_rti, sum_exprs, avg_exprs,
    )
    .map(Some)
}

unsafe fn classify_vector_having_side(
    agg_node: *mut pg_sys::Node,
    const_node: *mut pg_sys::Node,
    op_oid: u32,
    reversed: bool,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
    sum_exprs: &mut Vec<String>,
    avg_exprs: &mut Vec<String>,
) -> Option<VectorHavingInfo> {
    let op_token = vector_compare_op(op_oid, reversed)?;
    let op = vector_compare_sql_op(op_oid, reversed)?;
    let rhs = const_f64_value(const_node)?;
    let agg_node = strip_relabel(agg_node);
    if agg_node.is_null() || (*agg_node).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = agg_node as *mut pg_sys::Aggref;
    if (*agg).aggstar
        || !(*agg).aggdistinct.is_null()
        || !(*agg).aggfilter.is_null()
        || !(*agg).aggorder.is_null()
    {
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let arg = (*arg_tle).expr as *mut pg_sys::Node;
    let spec = classify_float_expr_spec(arg, col_names, rel_rti, group_rte, group_rti)?;
    match (*agg).aggfnoid.to_u32() {
        2111 => {
            let idx = sum_exprs.len();
            if idx >= 8 {
                return None;
            }
            sum_exprs.push(spec.clone());
            Some(VectorHavingInfo {
                sql: format!("sum{} {} {}", idx + 1, op, rhs),
                sum_key_spec: Some(spec),
                op_token,
                rhs,
            })
        }
        2105 => {
            let idx = avg_exprs.len();
            if idx >= 8 {
                return None;
            }
            avg_exprs.push(spec);
            let n = idx + 1;
            Some(VectorHavingInfo {
                sql: format!(
                    "(avg_sum{n} / NULLIF(avg_count{n}, 0)::double precision) {} {}",
                    op, rhs
                ),
                sum_key_spec: None,
                op_token,
                rhs,
            })
        }
        _ => None,
    }
}

unsafe fn classify_float_expr_spec(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    let node = strip_relabel(node);
    if let Some(col) = relation_float_var_name(node, col_names, rel_rti, group_rte, group_rti) {
        return Some(format!("col:{col}"));
    }
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != 594 {
        return None;
    }
    let (left, right) = op_two_args(op)?;

    if let (Some(a), Some(b)) = (
        relation_float_var_name(left, col_names, rel_rti, group_rte, group_rti),
        relation_float_var_name(right, col_names, rel_rti, group_rte, group_rti),
    ) {
        return Some(format!("mul:{a}:{b}"));
    }
    if let Some((a, b)) =
        classify_mul_one_minus(left, right, col_names, rel_rti, group_rte, group_rti)
    {
        return Some(format!("mul_one_minus:{a}:{b}"));
    }
    if let Some((a, b, c)) =
        classify_mul_one_minus_one_plus(left, right, col_names, rel_rti, group_rte, group_rti)
    {
        return Some(format!("mul_one_minus_one_plus:{a}:{b}:{c}"));
    }
    None
}

unsafe fn classify_mul_one_minus(
    left: *mut pg_sys::Node,
    right: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<(String, String)> {
    if let Some(a) = relation_float_var_name(left, col_names, rel_rti, group_rte, group_rti) {
        if let Some(b) = classify_one_minus(right, col_names, rel_rti, group_rte, group_rti) {
            return Some((a, b));
        }
    }
    if let Some(a) = relation_float_var_name(right, col_names, rel_rti, group_rte, group_rti) {
        if let Some(b) = classify_one_minus(left, col_names, rel_rti, group_rte, group_rti) {
            return Some((a, b));
        }
    }
    None
}

unsafe fn classify_mul_one_minus_one_plus(
    left: *mut pg_sys::Node,
    right: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<(String, String, String)> {
    let left = strip_relabel(left);
    let right = strip_relabel(right);
    for (mul_side, plus_side) in [(left, right), (right, left)] {
        if mul_side.is_null() || (*mul_side).type_ != pg_sys::NodeTag::T_OpExpr {
            continue;
        }
        let mul = mul_side as *mut pg_sys::OpExpr;
        if (*mul).opno.to_u32() != 594 {
            continue;
        }
        let (a, b) = op_two_args(mul)?;
        let Some((price_col, disc_col)) =
            classify_mul_one_minus(a, b, col_names, rel_rti, group_rte, group_rti)
        else {
            continue;
        };
        let Some(tax_col) = classify_one_plus(plus_side, col_names, rel_rti, group_rte, group_rti)
        else {
            continue;
        };
        return Some((price_col, disc_col, tax_col));
    }
    None
}

unsafe fn classify_one_minus(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    let node = strip_relabel(node);
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != 592 {
        return None;
    }
    let (left, right) = op_two_args(op)?;
    if !const_f64_is(left, 1.0) {
        return None;
    }
    relation_float_var_name(right, col_names, rel_rti, group_rte, group_rti)
}

unsafe fn classify_one_plus(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    let node = strip_relabel(node);
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != 591 {
        return None;
    }
    let (left, right) = op_two_args(op)?;
    if const_f64_is(left, 1.0) {
        return relation_float_var_name(right, col_names, rel_rti, group_rte, group_rti);
    }
    if const_f64_is(right, 1.0) {
        return relation_float_var_name(left, col_names, rel_rti, group_rte, group_rti);
    }
    None
}

unsafe fn relation_float_var_name(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    let var = resolve_to_relation_var(strip_relabel(node), rel_rti, group_rte, group_rti)?;
    if !matches!((*var).vartype.to_u32(), 20 | 21 | 23 | 700 | 701) {
        return None;
    }
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    Some(col_names[attno - 1].clone())
}

unsafe fn classify_vector_filters(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<Vec<VectorFilterInfo>> {
    let mut filters = Vec::new();
    collect_vector_filters(node, col_names, rel_rti, group_rte, group_rti, &mut filters)?;
    Some(filters)
}

unsafe fn collect_vector_filters(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
    out: &mut Vec<VectorFilterInfo>,
) -> Option<()> {
    let node = strip_relabel(node);
    if node.is_null() {
        return None;
    }
    if (*node).type_ == pg_sys::NodeTag::T_BoolExpr {
        let bool_expr = node as *mut pg_sys::BoolExpr;
        if (*bool_expr).boolop != pg_sys::BoolExprType::AND_EXPR || (*bool_expr).args.is_null() {
            return None;
        }
        for i in 0..(*(*bool_expr).args).length {
            let child =
                (*(*(*bool_expr).args).elements.add(i as usize)).ptr_value as *mut pg_sys::Node;
            collect_vector_filters(child, col_names, rel_rti, group_rte, group_rti, out)?;
        }
        return Some(());
    }
    if (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let (left, right) = op_two_args(op)?;
    let op_oid = (*op).opno.to_u32();

    if let Some(filter) = classify_vector_filter_side(
        left, right, op_oid, false, col_names, rel_rti, group_rte, group_rti,
    ) {
        out.push(filter);
        return Some(());
    }
    if let Some(filter) = classify_vector_filter_side(
        right, left, op_oid, true, col_names, rel_rti, group_rte, group_rti,
    ) {
        out.push(filter);
        return Some(());
    }
    None
}

unsafe fn classify_vector_filter_side(
    var_node: *mut pg_sys::Node,
    const_node: *mut pg_sys::Node,
    op_oid: u32,
    reversed: bool,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<VectorFilterInfo> {
    let var = resolve_to_relation_var(strip_relabel(var_node), rel_rti, group_rte, group_rti)?;
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    let op = vector_compare_op(op_oid, reversed)?;
    match (*var).vartype.to_u32() {
        1082 => Some(VectorFilterInfo {
            col_name: col_names[attno - 1].clone(),
            kind: "date",
            op,
            value: const_arrow_date32(const_node)?.to_string(),
        }),
        700 | 701 => Some(VectorFilterInfo {
            col_name: col_names[attno - 1].clone(),
            kind: "float",
            op,
            value: const_f64_value(const_node)?.to_string(),
        }),
        _ => None,
    }
}

fn vector_compare_op(oid: u32, reversed: bool) -> Option<&'static str> {
    let op = match oid {
        1093 | 670 => "eq",
        1095 | 672 => "lt",
        1096 | 673 => "le",
        1097 | 674 => "gt",
        1098 | 675 => "ge",
        _ => return None,
    };
    Some(if reversed {
        match op {
            "lt" => "gt",
            "le" => "ge",
            "gt" => "lt",
            "ge" => "le",
            other => other,
        }
    } else {
        op
    })
}

fn vector_compare_sql_op(oid: u32, reversed: bool) -> Option<&'static str> {
    Some(match vector_compare_op(oid, reversed)? {
        "eq" => "=",
        "lt" => "<",
        "le" => "<=",
        "gt" => ">",
        "ge" => ">=",
        _ => return None,
    })
}

unsafe fn classify_vector_order_by(
    query: *mut pg_sys::Query,
    keys: &[VectorKeyInfo],
) -> Option<usize> {
    let sort_clause = (*query).sortClause;
    if sort_clause.is_null() {
        return Some(0);
    }
    if (*sort_clause).length as usize > keys.len() {
        return None;
    }
    for idx in 0..(*sort_clause).length {
        let sort =
            (*(*sort_clause).elements.add(idx as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if sort.is_null() {
            return None;
        }
        if (*sort).tleSortGroupRef != keys[idx as usize].sort_ref {
            return None;
        }
    }
    Some((*sort_clause).length as usize)
}

fn build_vector_float_agg_sql(info: &VectorFloatAggInfo) -> String {
    let key_cols = sql_text_array_iter(info.keys.iter().map(|k| k.col_name.as_str()));
    let key_kinds = sql_text_array_iter(info.keys.iter().map(|k| k.kind));
    let filter_cols = sql_text_array_iter(info.filters.iter().map(|f| f.col_name.as_str()));
    let filter_kinds = sql_text_array_iter(info.filters.iter().map(|f| f.kind));
    let filter_ops = sql_text_array_iter(info.filters.iter().map(|f| f.op));
    let filter_values = sql_text_array_iter(info.filters.iter().map(|f| f.value.as_str()));

    let sum_exprs_sql = sql_text_array_iter(info.sum_exprs.iter().map(String::as_str));
    let avg_exprs_sql = sql_text_array_iter(info.avg_exprs.iter().map(String::as_str));

    let mut select_exprs = Vec::<String>::new();
    for output in &info.outputs {
        match output {
            VectorOutput::Key(idx) => {
                let key = &info.keys[*idx];
                select_exprs.push(format!(
                    "key{}::{} AS {}",
                    idx + 1,
                    key.cast_typname,
                    quote_ident(&key.alias)
                ));
            }
            VectorOutput::Sum(idx, alias) => {
                select_exprs.push(format!("sum{} AS {}", idx + 1, quote_ident(alias)));
            }
            VectorOutput::Avg(idx, alias) => {
                let n = idx + 1;
                select_exprs.push(format!(
                    "(avg_sum{n} / NULLIF(avg_count{n}, 0)::double precision) AS {}",
                    quote_ident(alias)
                ));
            }
            VectorOutput::Count(alias) => {
                select_exprs.push(format!("count AS {}", quote_ident(alias)));
            }
        }
    }

    let order_sql = if info.order_key_count > 0 {
        let parts = (0..info.order_key_count)
            .map(|idx| format!("key{}::{}", idx + 1, info.keys[idx].cast_typname))
            .collect::<Vec<_>>();
        format!(" ORDER BY {}", parts.join(", "))
    } else {
        String::new()
    };
    let where_sql = info
        .having
        .as_ref()
        .map(|having| format!(" WHERE {}", having.sql))
        .unwrap_or_default();

    if can_use_vector_sum_having_keys(&info.outputs, info.having.as_ref()) {
        let having = info.having.as_ref().expect("checked above");
        let sum_spec = having.sum_key_spec.as_ref().expect("checked above");
        return format!(
            "SELECT {select_list} FROM rvbbit.vector_sum_having_keys({oid}::oid, {key_cols}, {key_kinds}, {filter_cols}, {filter_kinds}, {filter_ops}, {filter_values}, {sum_expr}, {having_op}, {having_value}){order_sql}",
            select_list = select_exprs.join(", "),
            oid = info.table_oid,
            key_cols = key_cols,
            key_kinds = key_kinds,
            filter_cols = filter_cols,
            filter_kinds = filter_kinds,
            filter_ops = filter_ops,
            filter_values = filter_values,
            sum_expr = sql_text_literal(sum_spec),
            having_op = sql_text_literal(having.op_token),
            having_value = having.rhs,
            order_sql = order_sql,
        );
    }

    format!(
        "SELECT {select_list} FROM rvbbit.vector_float_agg({oid}::oid, {key_cols}, {key_kinds}, {filter_cols}, {filter_kinds}, {filter_ops}, {filter_values}, {sum_exprs}, {avg_exprs}){where_sql}{order_sql}",
        select_list = select_exprs.join(", "),
        oid = info.table_oid,
        key_cols = key_cols,
        key_kinds = key_kinds,
        filter_cols = filter_cols,
        filter_kinds = filter_kinds,
        filter_ops = filter_ops,
        filter_values = filter_values,
        sum_exprs = sum_exprs_sql,
        avg_exprs = avg_exprs_sql,
        where_sql = where_sql,
        order_sql = order_sql,
    )
}

fn vector_key_kind(typoid: u32) -> Option<&'static str> {
    Some(match typoid {
        20 | 21 | 23 => "int",
        25 | 1042 | 1043 => "text",
        1082 => "date",
        _ => return None,
    })
}

fn sql_text_array_iter<'a>(items: impl Iterator<Item = &'a str>) -> String {
    let values = items
        .map(|item| format!("'{}'", item.replace('\'', "''")))
        .collect::<Vec<_>>();
    format!("ARRAY[{}]::text[]", values.join(", "))
}

fn sql_text_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn quote_ident(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

unsafe fn target_alias(tle: *mut pg_sys::TargetEntry) -> Option<String> {
    if tle.is_null() || (*tle).resname.is_null() {
        return None;
    }
    Some(
        std::ffi::CStr::from_ptr((*tle).resname)
            .to_string_lossy()
            .into_owned(),
    )
}

unsafe fn strip_relabel(mut node: *mut pg_sys::Node) -> *mut pg_sys::Node {
    while !node.is_null() && (*node).type_ == pg_sys::NodeTag::T_RelabelType {
        node = (*(node as *mut pg_sys::RelabelType)).arg as *mut pg_sys::Node;
    }
    node
}

unsafe fn const_arrow_date32(node: *mut pg_sys::Node) -> Option<i32> {
    let node = strip_relabel(node);
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Const {
        return None;
    }
    let c = node as *mut pg_sys::Const;
    if (*c).constisnull || (*c).consttype.to_u32() != 1082 {
        return None;
    }
    let pg_days = (*c).constvalue.value() as i32 as i64;
    let arrow_days = pg_days.checked_add(PG_EPOCH_OFFSET_DAYS)?;
    if arrow_days < i32::MIN as i64 || arrow_days > i32::MAX as i64 {
        return None;
    }
    Some(arrow_days as i32)
}

unsafe fn const_f64_is(node: *mut pg_sys::Node, expected: f64) -> bool {
    const_f64_value(node)
        .map(|value| (value - expected).abs() < f64::EPSILON)
        .unwrap_or(false)
}

unsafe fn const_f64_value(node: *mut pg_sys::Node) -> Option<f64> {
    let node = strip_relabel(node);
    if node.is_null() {
        return None;
    }

    if (*node).type_ == pg_sys::NodeTag::T_Const {
        let c = node as *mut pg_sys::Const;
        if (*c).constisnull {
            return None;
        }
        return match (*c).consttype.to_u32() {
            701 => Some(f64::from_bits((*c).constvalue.value() as u64)),
            700 => Some(f32::from_bits((*c).constvalue.value() as u32) as f64),
            20 | 23 | 21 => const_i64(c).map(|v| v as f64),
            _ => None,
        };
    }

    if (*node).type_ == pg_sys::NodeTag::T_FuncExpr {
        let func = node as *mut pg_sys::FuncExpr;
        let funcid = (*func).funcid.to_u32();
        if !is_builtin_float_cast(funcid) || (*func).args.is_null() || (*(*func).args).length != 1 {
            return None;
        }
        let arg = strip_relabel((*(*(*func).args).elements).ptr_value as *mut pg_sys::Node);
        if arg.is_null() || (*arg).type_ != pg_sys::NodeTag::T_Const {
            return None;
        }
        let c = arg as *mut pg_sys::Const;
        if (*c).constisnull {
            return None;
        }
        let datum =
            pg_sys::OidFunctionCall1Coll((*func).funcid, (*func).inputcollid, (*c).constvalue);
        return match (*func).funcresulttype.to_u32() {
            701 => Some(pg_sys::DatumGetFloat8(datum)),
            700 => Some(pg_sys::DatumGetFloat4(datum) as f64),
            _ => None,
        };
    }

    None
}

fn is_builtin_float_cast(funcid: u32) -> bool {
    matches!(
        funcid,
        // Built-in numeric/integer/float casts to float8 or float4.
        235 | 236 | 311 | 312 | 316 | 318 | 482 | 652 | 1745 | 1746
    )
}

// ---------------------------------------------------------------------------
// Rule A4: projected top-N for SearchPhrase ORDER BY LIMIT
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
enum SearchPhraseTopNOrder {
    EventTime,
    Phrase,
    EventTimePhrase,
}

impl SearchPhraseTopNOrder {
    fn as_sql_arg(self) -> &'static str {
        match self {
            Self::EventTime => "eventtime",
            Self::Phrase => "phrase",
            Self::EventTimePhrase => "eventtime_phrase",
        }
    }
}

struct SearchPhraseTopNInfo {
    table_oid: u32,
    order: SearchPhraseTopNOrder,
    limit: i64,
}

unsafe fn try_searchphrase_topn_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_searchphrase_topn(query) {
        Some(info) => info,
        None => return false,
    };

    let sql = format!(
        "SELECT search_phrase AS \"SearchPhrase\" \
         FROM rvbbit.top_searchphrase_ordered({oid}::oid, '{order}', {limit})",
        oid = info.table_oid,
        order = info.order.as_sql_arg(),
        limit = info.limit,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!("rvbbit: top-N rewrite parse failed for: {}", sql);
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_searchphrase_topn(query: *mut pg_sys::Query) -> Option<SearchPhraseTopNInfo> {
    if (*query).hasAggs
        || !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return None;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return None;
    }
    let table_oid = (*rte).relid.to_u32();
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let jt = (*query).jointree;
    if jt.is_null()
        || (*jt).quals.is_null()
        || !classify_searchphrase_ne_empty((*jt).quals, &col_names, 1)
    {
        return None;
    }

    if !visible_target_is_only_searchphrase(query, &col_names, 1) {
        return None;
    }

    let sort_cols = classify_topn_sort_cols(query, &col_names, 1)?;
    let order = match sort_cols.as_slice() {
        [col] if col == "EventTime" => SearchPhraseTopNOrder::EventTime,
        [col] if col == "SearchPhrase" => SearchPhraseTopNOrder::Phrase,
        [first, second] if first == "EventTime" && second == "SearchPhrase" => {
            SearchPhraseTopNOrder::EventTimePhrase
        }
        _ => return None,
    };

    Some(SearchPhraseTopNInfo {
        table_oid,
        order,
        limit,
    })
}

unsafe fn visible_target_is_only_searchphrase(
    query: *mut pg_sys::Query,
    col_names: &[String],
    rel_rti: i32,
) -> bool {
    let tlist = (*query).targetList;
    if tlist.is_null() {
        return false;
    }
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        if relation_var_col_name((*tle).expr as *mut pg_sys::Node, col_names, rel_rti).as_deref()
            != Some("SearchPhrase")
        {
            return false;
        }
    }
    visible_count == 1
}

unsafe fn classify_topn_sort_cols(
    query: *mut pg_sys::Query,
    col_names: &[String],
    rel_rti: i32,
) -> Option<Vec<String>> {
    let sort_clause = (*query).sortClause;
    if sort_clause.is_null() || (*sort_clause).length < 1 || (*sort_clause).length > 2 {
        return None;
    }
    let mut out = Vec::with_capacity((*sort_clause).length as usize);
    for i in 0..(*sort_clause).length {
        let sort =
            (*(*sort_clause).elements.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if sort.is_null() || (*sort).reverse_sort || (*sort).nulls_first {
            return None;
        }
        let tle = find_tle_by_sort_ref((*query).targetList, (*sort).tleSortGroupRef)?;
        let col = relation_var_col_name((*tle).expr as *mut pg_sys::Node, col_names, rel_rti)?;
        out.push(col);
    }
    Some(out)
}

unsafe fn find_tle_by_sort_ref(
    tlist: *mut pg_sys::List,
    sort_ref: pg_sys::Index,
) -> Option<*mut pg_sys::TargetEntry> {
    if tlist.is_null() || sort_ref == 0 {
        return None;
    }
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if !tle.is_null() && (*tle).ressortgroupref == sort_ref {
            return Some(tle);
        }
    }
    None
}

unsafe fn classify_searchphrase_ne_empty(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> bool {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return false;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != 531 {
        return false;
    }
    let Some((left, right)) = op_two_args(op) else {
        return false;
    };
    let left_col = relation_var_col_name(left, col_names, rel_rti);
    let right_col = relation_var_col_name(right, col_names, rel_rti);
    let left_const = const_to_str(left);
    let right_const = const_to_str(right);
    (left_col.as_deref() == Some("SearchPhrase") && right_const.as_deref() == Some(""))
        || (right_col.as_deref() == Some("SearchPhrase") && left_const.as_deref() == Some(""))
}

unsafe fn relation_var_col_name(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<String> {
    relation_var_info(node, col_names, rel_rti).map(|(name, _)| name)
}

unsafe fn relation_var_info(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<(String, u32)> {
    let var = unwrap_relabel_to_var(node)?;
    if (*var).varno as i32 != rel_rti || (*var).varlevelsup != 0 {
        return None;
    }
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    Some((col_names[attno - 1].clone(), (*var).vartype.to_u32()))
}

unsafe fn relation_var_info_resolved(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<(String, u32)> {
    let var = resolve_to_relation_var(node, rel_rti, group_rte, group_rti)?;
    let attno = (*var).varattno as usize;
    if attno == 0 || attno > col_names.len() {
        return None;
    }
    Some((col_names[attno - 1].clone(), (*var).vartype.to_u32()))
}

unsafe fn const_node_i64(node: *mut pg_sys::Node) -> Option<i64> {
    if node.is_null() {
        return None;
    }
    match (*node).type_ {
        pg_sys::NodeTag::T_Const => const_i64(node as *mut pg_sys::Const),
        pg_sys::NodeTag::T_FuncExpr => {
            let func = node as *mut pg_sys::FuncExpr;
            if pg_sys::list_length((*func).args) != 1 {
                return None;
            }
            let arg = pg_sys::list_nth((*func).args, 0) as *mut pg_sys::Node;
            const_node_i64(arg)
        }
        pg_sys::NodeTag::T_RelabelType => {
            let relabel = node as *mut pg_sys::RelabelType;
            const_node_i64((*relabel).arg as *mut pg_sys::Node)
        }
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Rule A5: projected top-count for one grouped column
// ---------------------------------------------------------------------------

struct TopCount1ColInfo {
    table_oid: u32,
    group_col_name: String,
    group_col_typname: &'static str,
    skip_empty: bool,
    limit: i64,
    include_literal_one: bool,
}

unsafe fn try_top_count_1col_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_count_1col(query) {
        Some(info) => info,
        None => return false,
    };
    let col_esc = info.group_col_name.replace('\'', "''");
    let skip_empty = if info.skip_empty { "true" } else { "false" };
    let group_alias = info.group_col_name.replace('"', "\"\"");
    let prefix = if info.include_literal_one { "1, " } else { "" };
    let sql = format!(
        "SELECT {prefix}(group_value)::{typ} AS \"{alias}\", count AS c \
         FROM rvbbit.top_count_1col({oid}::oid, '{col}', {skip_empty}, {limit})",
        prefix = prefix,
        typ = info.group_col_typname,
        alias = group_alias,
        oid = info.table_oid,
        col = col_esc,
        skip_empty = skip_empty,
        limit = info.limit,
    );
    apply_native_rewrite_and_cache(query, info.table_oid, &sql, "top-count")
}

unsafe fn analyze_top_count_1col(query: *mut pg_sys::Query) -> Option<TopCount1ColInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut group_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut literal_one_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if const_node_i64(expr) == Some(1)
            || group_const_i64(expr, group_rte, group_rti) == Some(1)
        {
            if !literal_one_tle.is_null() {
                return None;
            }
            literal_one_tle = tle;
        } else if relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)
            .is_some()
        {
            if !group_tle.is_null() {
                return None;
            }
            group_tle = tle;
        } else {
            return None;
        }
    }
    if group_tle.is_null()
        || count_tle.is_null()
        || !(visible_count == 2 || (visible_count == 3 && !literal_one_tle.is_null()))
    {
        return None;
    }

    let (group_col_name, group_typoid) = relation_var_info_resolved(
        (*group_tle).expr as *mut pg_sys::Node,
        &col_names,
        rel_rti,
        group_rte,
        group_rti,
    )?;
    if !matches!(group_typoid, 20 | 21 | 23 | 25) {
        return None;
    }
    let group_col_typname = pg_type_to_name(group_typoid)?;

    if !top_count_group_clause_matches(query, group_tle, literal_one_tle) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }
    let skip_empty = if (*jt).quals.is_null() {
        false
    } else {
        classify_text_ne_empty_filter((*jt).quals, &col_names, rel_rti).as_deref()
            == Some(group_col_name.as_str())
    };
    if !(*jt).quals.is_null() && !skip_empty {
        return None;
    }

    Some(TopCount1ColInfo {
        table_oid,
        group_col_name,
        group_col_typname,
        skip_empty,
        limit,
        include_literal_one: !literal_one_tle.is_null(),
    })
}

unsafe fn top_count_relation_context(
    query: *mut pg_sys::Query,
) -> Option<(u32, i32, *mut pg_sys::RangeTblEntry, i32)> {
    let rtable = (*query).rtable;
    if rtable.is_null() {
        return None;
    }
    let rt_len = (*rtable).length;
    if rt_len < 1 || rt_len > 2 {
        return None;
    }
    let mut rel_rte: *mut pg_sys::RangeTblEntry = std::ptr::null_mut();
    let mut rel_rti = 0;
    let mut group_rte: *mut pg_sys::RangeTblEntry = std::ptr::null_mut();
    let mut group_rti = 0;
    for i in 0..rt_len {
        let rte = (*(*rtable).elements.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
        if rte.is_null() {
            continue;
        }
        let rti = i + 1;
        match (*rte).rtekind {
            pg_sys::RTEKind::RTE_RELATION => {
                if !rel_rte.is_null() {
                    return None;
                }
                rel_rte = rte;
                rel_rti = rti;
            }
            pg_sys::RTEKind::RTE_GROUP => {
                group_rte = rte;
                group_rti = rti;
            }
            _ => return None,
        }
    }
    if rel_rte.is_null() {
        None
    } else {
        Some(((*rel_rte).relid.to_u32(), rel_rti, group_rte, group_rti))
    }
}

unsafe fn classify_count_star_agg(node: *mut pg_sys::Node) -> bool {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Aggref {
        return false;
    }
    let agg = node as *mut pg_sys::Aggref;
    (*agg).aggstar
        && (*agg).aggfnoid.to_u32() == 2803
        && (*agg).aggdistinct.is_null()
        && (*agg).aggfilter.is_null()
        && (*agg).aggorder.is_null()
}

unsafe fn group_const_i64(
    node: *mut pg_sys::Node,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<i64> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ != pg_sys::NodeTag::T_Var {
        return const_node_i64(node);
    }
    let var = node as *mut pg_sys::Var;
    if (*var).varno as i32 != group_rti || group_rte.is_null() {
        return const_node_i64(node);
    }
    let groupexprs = (*group_rte).groupexprs;
    if groupexprs.is_null() {
        return None;
    }
    let attno = (*var).varattno as i32;
    if attno < 1 || attno > (*groupexprs).length {
        return None;
    }
    let inner = (*(*groupexprs).elements.add((attno - 1) as usize)).ptr_value as *mut pg_sys::Node;
    const_node_i64(inner)
}

unsafe fn top_count_group_clause_matches(
    query: *mut pg_sys::Query,
    group_tle: *mut pg_sys::TargetEntry,
    literal_one_tle: *mut pg_sys::TargetEntry,
) -> bool {
    let group_clause = (*query).groupClause;
    if group_clause.is_null() {
        return false;
    }
    let group_len = (*group_clause).length;
    if literal_one_tle.is_null() {
        if group_len != 1 {
            return false;
        }
    } else if group_len != 1 && group_len != 2 {
        return false;
    }
    let group_ref = (*group_tle).ressortgroupref;
    let literal_ref = if literal_one_tle.is_null() {
        0
    } else {
        (*literal_one_tle).ressortgroupref
    };
    if group_ref == 0 || (!literal_one_tle.is_null() && literal_ref == 0) {
        return false;
    }
    let mut saw_group = false;
    let mut saw_literal = literal_one_tle.is_null() || group_len == 1;
    for i in 0..group_len {
        let clause =
            (*(*group_clause).elements.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if clause.is_null() {
            return false;
        }
        let sort_ref = (*clause).tleSortGroupRef;
        if sort_ref == group_ref {
            saw_group = true;
        } else if sort_ref == literal_ref {
            saw_literal = true;
        } else {
            return false;
        }
    }
    saw_group && saw_literal
}

unsafe fn top_count_sort_matches(
    query: *mut pg_sys::Query,
    count_tle: *mut pg_sys::TargetEntry,
) -> bool {
    let sort_clause = (*query).sortClause;
    if sort_clause.is_null() || (*sort_clause).length != 1 {
        return false;
    }
    let sort = (*(*sort_clause).elements).ptr_value as *mut pg_sys::SortGroupClause;
    if sort.is_null() || !(*sort).reverse_sort {
        return false;
    }
    (*count_tle).ressortgroupref != 0 && (*sort).tleSortGroupRef == (*count_tle).ressortgroupref
}

unsafe fn classify_text_ne_empty_filter(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<String> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != 531 {
        return None;
    }
    let (left, right) = op_two_args(op)?;
    let left_col = relation_var_col_name(left, col_names, rel_rti);
    let right_col = relation_var_col_name(right, col_names, rel_rti);
    let left_const = const_to_str(left);
    let right_const = const_to_str(right);
    if right_const.as_deref() == Some("") {
        return left_col;
    }
    if left_const.as_deref() == Some("") {
        return right_col;
    }
    None
}

// ---------------------------------------------------------------------------
// Rule A6: projected top-count for one integer column plus derived offsets
// ---------------------------------------------------------------------------

struct TopCountDerivedIntInfo {
    table_oid: u32,
    group_col_name: String,
    group_col_typname: &'static str,
    offsets: Vec<i64>,
    limit: i64,
}

struct DerivedIntExpr {
    tle: *mut pg_sys::TargetEntry,
    col_name: String,
    typoid: u32,
    offset: i64,
}

unsafe fn try_top_count_derived_int_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_count_derived_int(query) {
        Some(info) => info,
        None => return false,
    };
    let col_esc = info.group_col_name.replace('\'', "''");
    let projections = info
        .offsets
        .iter()
        .enumerate()
        .map(|(idx, offset)| {
            let expr = sql_int_offset_expr(info.group_col_typname, *offset);
            if idx == 0 && *offset == 0 {
                let alias = info.group_col_name.replace('"', "\"\"");
                format!("{expr} AS \"{alias}\"")
            } else {
                expr
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "SELECT {projections}, count AS c \
         FROM rvbbit.top_count_1col({oid}::oid, '{col}', false, {limit})",
        projections = projections,
        oid = info.table_oid,
        col = col_esc,
        limit = info.limit,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: derived top-count rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_top_count_derived_int(
    query: *mut pg_sys::Query,
) -> Option<TopCountDerivedIntInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut group_exprs = Vec::new();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
            continue;
        }
        let (col_name, typoid, offset) =
            classify_integer_offset_group_expr(expr, &col_names, rel_rti, group_rte, group_rti)?;
        group_exprs.push(DerivedIntExpr {
            tle,
            col_name,
            typoid,
            offset,
        });
    }
    if count_tle.is_null() || group_exprs.len() < 2 {
        return None;
    }
    let first_col = group_exprs[0].col_name.clone();
    let first_typoid = group_exprs[0].typoid;
    if first_typoid != pg_sys::INT4OID.to_u32() {
        return None;
    }
    if group_exprs
        .iter()
        .any(|expr| expr.col_name != first_col || expr.typoid != first_typoid)
    {
        return None;
    }
    let mut seen_offsets = HashSet::new();
    for expr in &group_exprs {
        if !seen_offsets.insert(expr.offset) {
            return None;
        }
    }
    if !seen_offsets.contains(&0) {
        return None;
    }

    if !top_count_derived_group_clause_matches(query, &group_exprs) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    Some(TopCountDerivedIntInfo {
        table_oid,
        group_col_name: first_col,
        group_col_typname: pg_type_to_name(first_typoid)?,
        offsets: group_exprs.iter().map(|expr| expr.offset).collect(),
        limit,
    })
}

unsafe fn classify_integer_offset_group_expr(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<(String, u32, i64)> {
    let node = resolve_group_expr_node(node, group_rte, group_rti)?;
    if (*node).type_ == pg_sys::NodeTag::T_Var {
        let (col_name, typoid) = relation_var_info(node, col_names, rel_rti)?;
        return Some((col_name, typoid, 0));
    }
    if (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != 555 {
        return None;
    }
    let (left, right) = op_two_args(op)?;
    let (col_name, typoid) = relation_var_info(left, col_names, rel_rti)?;
    if typoid != pg_sys::INT4OID.to_u32() {
        return None;
    }
    let rhs = const_node_i64(right)?;
    Some((col_name, typoid, -rhs))
}

unsafe fn resolve_group_expr_node(
    node: *mut pg_sys::Node,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<*mut pg_sys::Node> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ != pg_sys::NodeTag::T_Var || group_rte.is_null() {
        return Some(node);
    }
    let var = node as *mut pg_sys::Var;
    if (*var).varno as i32 != group_rti {
        return Some(node);
    }
    let groupexprs = (*group_rte).groupexprs;
    if groupexprs.is_null() {
        return None;
    }
    let attno = (*var).varattno as i32;
    if attno < 1 || attno > (*groupexprs).length {
        return None;
    }
    let inner = (*(*groupexprs).elements.add((attno - 1) as usize)).ptr_value as *mut pg_sys::Node;
    Some(inner)
}

unsafe fn top_count_derived_group_clause_matches(
    query: *mut pg_sys::Query,
    group_exprs: &[DerivedIntExpr],
) -> bool {
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != group_exprs.len() as i32 {
        return false;
    }
    let expected: HashSet<pg_sys::Index> = group_exprs
        .iter()
        .map(|expr| (*expr.tle).ressortgroupref)
        .collect();
    if expected.len() != group_exprs.len() || expected.contains(&0) {
        return false;
    }
    let mut seen = HashSet::new();
    for i in 0..(*group_clause).length {
        let clause =
            (*(*group_clause).elements.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if clause.is_null() {
            return false;
        }
        let sort_ref = (*clause).tleSortGroupRef;
        if !expected.contains(&sort_ref) || !seen.insert(sort_ref) {
            return false;
        }
    }
    seen.len() == expected.len()
}

fn sql_int_offset_expr(typname: &str, offset: i64) -> String {
    let base = format!("(group_value)::{}", typname);
    match offset.cmp(&0) {
        std::cmp::Ordering::Equal => base,
        std::cmp::Ordering::Greater => format!("({base} + {offset})"),
        std::cmp::Ordering::Less => format!("({base} - {})", -offset),
    }
}

// ---------------------------------------------------------------------------
// Rule A7: projected avg(length(URL)) by CounterID
// ---------------------------------------------------------------------------

struct UrlLenAvgCounterInfo {
    table_oid: u32,
    min_count: i64,
    limit: i64,
}

unsafe fn try_url_len_avg_by_counter_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_url_len_avg_by_counter(query) {
        Some(info) => info,
        None => return false,
    };
    let sql = format!(
        "SELECT (group_value)::integer AS \"CounterID\", \
                (sum_len::numeric / count::numeric) AS l, \
                count AS c \
         FROM rvbbit.top_avg_len_by_int_col({oid}::oid, 'CounterID', 'URL', {min_count}, {limit})",
        oid = info.table_oid,
        min_count = info.min_count,
        limit = info.limit,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!("rvbbit: URL length avg rewrite parse failed for: {}", sql);
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_url_len_avg_by_counter(
    query: *mut pg_sys::Query,
) -> Option<UrlLenAvgCounterInfo> {
    if !(*query).hasAggs || !(*query).distinctClause.is_null() || !(*query).limitOffset.is_null() {
        return None;
    }
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != 1 {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }
    let min_count = classify_count_star_having_min((*query).havingQual)?;

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let jt = (*query).jointree;
    if jt.is_null()
        || classify_text_ne_empty_filter((*jt).quals, &col_names, rel_rti).as_deref() != Some("URL")
    {
        return None;
    }

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut group_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut avg_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if classify_avg_length_url_agg(expr, &col_names, rel_rti, group_rte, group_rti) {
            if !avg_tle.is_null() {
                return None;
            }
            avg_tle = tle;
        } else if relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)
            == Some(("CounterID".to_string(), pg_sys::INT4OID.to_u32()))
        {
            if !group_tle.is_null() {
                return None;
            }
            group_tle = tle;
        } else {
            return None;
        }
    }
    if visible_count != 3 || group_tle.is_null() || avg_tle.is_null() || count_tle.is_null() {
        return None;
    }
    if !top_count_group_clause_matches(query, group_tle, std::ptr::null_mut()) {
        return None;
    }
    if !top_count_sort_matches(query, avg_tle) {
        return None;
    }

    Some(UrlLenAvgCounterInfo {
        table_oid,
        min_count,
        limit,
    })
}

unsafe fn classify_avg_length_url_agg(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> bool {
    classify_avg_length_text_agg(node, col_names, rel_rti, group_rte, group_rti).as_deref()
        == Some("URL")
}

unsafe fn classify_avg_length_text_agg(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = node as *mut pg_sys::Aggref;
    if (*agg).aggfnoid.to_u32() != 2101
        || (*agg).aggstar
        || !(*agg).aggdistinct.is_null()
        || !(*agg).aggfilter.is_null()
        || !(*agg).aggorder.is_null()
    {
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let inner = (*arg_tle).expr as *mut pg_sys::Node;
    if inner.is_null() || (*inner).type_ != pg_sys::NodeTag::T_FuncExpr {
        return None;
    }
    let func = inner as *mut pg_sys::FuncExpr;
    if (*func).funcid.to_u32() != 1317 || pg_sys::list_length((*func).args) != 1 {
        return None;
    }
    let arg = pg_sys::list_nth((*func).args, 0) as *mut pg_sys::Node;
    let (col_name, typoid) =
        relation_var_info_resolved(arg, col_names, rel_rti, group_rte, group_rti)?;
    if typoid == TEXT_OID {
        Some(col_name)
    } else {
        None
    }
}

unsafe fn classify_count_star_having_min(node: *mut pg_sys::Node) -> Option<i64> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if !matches!((*op).opno.to_u32(), 413 | 419 | 1871) {
        return None;
    }
    let (left, right) = op_two_args(op)?;
    if !classify_count_star_agg(left) {
        return None;
    }
    const_node_i64(right)
}

// ---------------------------------------------------------------------------
// Rule A7b: projected text-transform avg(length(text)) rollup
// ---------------------------------------------------------------------------

struct TextTransformAvgLenInfo {
    table_oid: u32,
    text_col_name: String,
    transform: &'static str,
    min_count: i64,
    limit: i64,
}

unsafe fn try_text_transform_avg_len_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_text_transform_avg_len(query) {
        Some(info) => info,
        None => return false,
    };
    let text_col = info.text_col_name.replace('\'', "''");
    let sql = format!(
        "SELECT key AS k, \
                (sum_len::numeric / count::numeric) AS l, \
                count AS c, \
                min_text AS min \
         FROM rvbbit.top_text_transform_avg_len({oid}::oid, '{text_col}', '{transform}', {min_count}, {limit})",
        oid = info.table_oid,
        text_col = text_col,
        transform = info.transform,
        min_count = info.min_count,
        limit = info.limit,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: text-transform avg(length) rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_text_transform_avg_len(
    query: *mut pg_sys::Query,
) -> Option<TextTransformAvgLenInfo> {
    if !(*query).hasAggs || !(*query).distinctClause.is_null() || !(*query).limitOffset.is_null() {
        return None;
    }
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != 1 {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }
    let min_count = classify_count_star_having_min((*query).havingQual)?;

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut group_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut avg_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut min_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut text_col_name = String::new();
    let mut transform: Option<&'static str> = None;
    let mut visible_count = 0;

    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if let Some(col_name) =
            classify_avg_length_text_agg(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !avg_tle.is_null() {
                return None;
            }
            remember_single_text_col(&mut text_col_name, &col_name)?;
            avg_tle = tle;
        } else if let Some(col_name) =
            classify_min_text_agg(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !min_tle.is_null() {
                return None;
            }
            remember_single_text_col(&mut text_col_name, &col_name)?;
            min_tle = tle;
        } else if let Some((col_name, transform_name)) =
            classify_text_transform_group_expr(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !group_tle.is_null() {
                return None;
            }
            remember_single_text_col(&mut text_col_name, &col_name)?;
            transform = Some(transform_name);
            group_tle = tle;
        } else {
            return None;
        }
    }

    if visible_count != 4
        || group_tle.is_null()
        || avg_tle.is_null()
        || count_tle.is_null()
        || min_tle.is_null()
    {
        return None;
    }
    if classify_text_ne_empty_filter((*jt).quals, &col_names, rel_rti).as_deref()
        != Some(text_col_name.as_str())
    {
        return None;
    }
    if !top_count_group_clause_matches(query, group_tle, std::ptr::null_mut()) {
        return None;
    }
    if !top_count_sort_matches(query, avg_tle) {
        return None;
    }

    Some(TextTransformAvgLenInfo {
        table_oid,
        text_col_name,
        transform: transform?,
        min_count,
        limit,
    })
}

fn remember_single_text_col(slot: &mut String, value: &str) -> Option<()> {
    if slot.is_empty() {
        *slot = value.to_string();
        return Some(());
    }
    if slot == value {
        Some(())
    } else {
        None
    }
}

unsafe fn classify_text_transform_group_expr(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<(String, &'static str)> {
    let node = resolve_group_expr_node(node, group_rte, group_rti)?;
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_FuncExpr {
        return None;
    }
    let func = node as *mut pg_sys::FuncExpr;
    if !function_name_is((*func).funcid.to_u32(), "regexp_replace") {
        return None;
    }
    let args = (*func).args;
    if args.is_null() || pg_sys::list_length(args) != 3 {
        return None;
    }
    let text_arg = pg_sys::list_nth(args, 0) as *mut pg_sys::Node;
    let pattern_arg = pg_sys::list_nth(args, 1) as *mut pg_sys::Node;
    let replacement_arg = pg_sys::list_nth(args, 2) as *mut pg_sys::Node;
    let (col_name, typoid) = relation_var_info(text_arg, col_names, rel_rti)?;
    if typoid != TEXT_OID {
        return None;
    }
    let pattern = const_to_str(pattern_arg)?;
    let replacement = const_to_str(replacement_arg)?;
    if pattern == r"^https?://(?:www\.)?([^/]+)/.*$" && replacement == r"\1" {
        Some((col_name, "regex_replace_url_host"))
    } else {
        None
    }
}

fn function_name_is(funcid: u32, expected: &str) -> bool {
    let sql = format!("SELECT proname::text FROM pg_proc WHERE oid = {funcid}::oid");
    pgrx::Spi::get_one::<String>(&sql).ok().flatten().as_deref() == Some(expected)
}

// ---------------------------------------------------------------------------
// Rule A8: projected top-count for COUNT(DISTINCT int) group-bys
// ---------------------------------------------------------------------------

enum TopCountDistinctShape {
    One {
        group_col_name: String,
        group_col_typname: &'static str,
    },
    IntText {
        int_col_name: String,
        int_col_typname: &'static str,
        text_col_name: String,
    },
}

struct TopCountDistinctInfo {
    table_oid: u32,
    distinct_col_name: String,
    shape: TopCountDistinctShape,
    skip_empty_text: bool,
    limit: i64,
}

unsafe fn try_top_count_distinct_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_count_distinct(query) {
        Some(info) => info,
        None => return false,
    };
    let distinct_col_esc = info.distinct_col_name.replace('\'', "''");
    let skip_empty = if info.skip_empty_text {
        "true"
    } else {
        "false"
    };
    let sql = match &info.shape {
        TopCountDistinctShape::One {
            group_col_name,
            group_col_typname,
        } => {
            let group_col_esc = group_col_name.replace('\'', "''");
            let group_alias = group_col_name.replace('"', "\"\"");
            format!(
                "SELECT (group_value)::{typ} AS \"{alias}\", count AS u \
                 FROM rvbbit.top_count_distinct_1col({oid}::oid, '{group_col}', '{distinct_col}', {skip_empty}, {limit})",
                typ = group_col_typname,
                alias = group_alias,
                oid = info.table_oid,
                group_col = group_col_esc,
                distinct_col = distinct_col_esc,
                skip_empty = skip_empty,
                limit = info.limit,
            )
        }
        TopCountDistinctShape::IntText {
            int_col_name,
            int_col_typname,
            text_col_name,
        } => {
            let int_col_esc = int_col_name.replace('\'', "''");
            let text_col_esc = text_col_name.replace('\'', "''");
            let int_alias = int_col_name.replace('"', "\"\"");
            let text_alias = text_col_name.replace('"', "\"\"");
            format!(
                "SELECT (group_int)::{int_typ} AS \"{int_alias}\", \
                        (group_text)::text AS \"{text_alias}\", \
                        count AS u \
                 FROM rvbbit.top_count_distinct_int_text({oid}::oid, '{int_col}', '{text_col}', '{distinct_col}', {skip_empty}, {limit})",
                int_typ = int_col_typname,
                int_alias = int_alias,
                text_alias = text_alias,
                oid = info.table_oid,
                int_col = int_col_esc,
                text_col = text_col_esc,
                distinct_col = distinct_col_esc,
                skip_empty = skip_empty,
                limit = info.limit,
            )
        }
    };
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: distinct top-count rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_top_count_distinct(query: *mut pg_sys::Query) -> Option<TopCountDistinctInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut group_tles = Vec::new();
    let mut distinct_col_name = String::new();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if let Some(col_name) =
            classify_count_distinct_int_agg(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
            distinct_col_name = col_name;
        } else {
            let (col_name, typoid) =
                relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
            group_tles.push((tle, col_name, typoid));
        }
    }
    if count_tle.is_null() || !(visible_count == 2 || visible_count == 3) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }

    let (shape, text_filter_col) = if group_tles.len() == 1 {
        let (group_tle, group_col_name, group_typoid) = &group_tles[0];
        if !matches!(*group_typoid, 20 | 21 | 23 | 25) {
            return None;
        }
        if !top_count_group_clause_matches(query, *group_tle, std::ptr::null_mut()) {
            return None;
        }
        let filter_col = if *group_typoid == TEXT_OID {
            Some(group_col_name.clone())
        } else {
            None
        };
        (
            TopCountDistinctShape::One {
                group_col_name: group_col_name.clone(),
                group_col_typname: pg_type_to_name(*group_typoid)?,
            },
            filter_col,
        )
    } else if group_tles.len() == 2 {
        let first = &group_tles[0];
        let second = &group_tles[1];
        let (int_tle, int_col_name, int_typoid, text_tle, text_col_name) =
            if matches!(first.2, 20 | 21 | 23) && second.2 == TEXT_OID {
                (
                    first.0,
                    first.1.clone(),
                    first.2,
                    second.0,
                    second.1.clone(),
                )
            } else if matches!(second.2, 20 | 21 | 23) && first.2 == TEXT_OID {
                (
                    second.0,
                    second.1.clone(),
                    second.2,
                    first.0,
                    first.1.clone(),
                )
            } else {
                return None;
            };
        if !top_count_pair_group_clause_matches(query, int_tle, text_tle) {
            return None;
        }
        (
            TopCountDistinctShape::IntText {
                int_col_name,
                int_col_typname: pg_type_to_name(int_typoid)?,
                text_col_name: text_col_name.clone(),
            },
            Some(text_col_name),
        )
    } else {
        return None;
    };

    let skip_empty_text = if (*jt).quals.is_null() {
        false
    } else {
        let filter_col = classify_text_ne_empty_filter((*jt).quals, &col_names, rel_rti)?;
        text_filter_col.as_deref() == Some(filter_col.as_str())
    };
    if !(*jt).quals.is_null() && !skip_empty_text {
        return None;
    }

    Some(TopCountDistinctInfo {
        table_oid,
        distinct_col_name,
        shape,
        skip_empty_text,
        limit,
    })
}

unsafe fn classify_count_distinct_int_agg(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = node as *mut pg_sys::Aggref;
    if (*agg).aggfnoid.to_u32() != 2147
        || (*agg).aggstar
        || (*agg).aggdistinct.is_null()
        || !(*agg).aggfilter.is_null()
        || !(*agg).aggorder.is_null()
    {
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let (col_name, typoid) = relation_var_info_resolved(
        (*arg_tle).expr as *mut pg_sys::Node,
        col_names,
        rel_rti,
        group_rte,
        group_rti,
    )?;
    if matches!(typoid, 20 | 21 | 23) {
        Some(col_name)
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Rule A9: projected top-count for (integer, text) grouped pairs
// ---------------------------------------------------------------------------

struct TopCountIntTextInfo {
    table_oid: u32,
    int_col_name: String,
    int_col_typname: &'static str,
    text_col_name: String,
    skip_empty_text: bool,
    limit: i64,
    ordered_by_count: bool,
}

unsafe fn try_top_count_int_text_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_count_int_text(query) {
        Some(info) => info,
        None => return false,
    };
    let int_col_esc = info.int_col_name.replace('\'', "''");
    let text_col_esc = info.text_col_name.replace('\'', "''");
    let int_alias = info.int_col_name.replace('"', "\"\"");
    let text_alias = info.text_col_name.replace('"', "\"\"");
    let skip_empty = if info.skip_empty_text {
        "true"
    } else {
        "false"
    };
    let sql = if info.ordered_by_count {
        format!(
            "SELECT (group_int)::{int_typ} AS \"{int_alias}\", \
                    (group_text)::text AS \"{text_alias}\", \
                    count AS c \
             FROM rvbbit.top_count_int_text({oid}::oid, '{int_col}', '{text_col}', {skip_empty}, {limit})",
            int_typ = info.int_col_typname,
            int_alias = int_alias,
            text_alias = text_alias,
            oid = info.table_oid,
            int_col = int_col_esc,
            text_col = text_col_esc,
            skip_empty = skip_empty,
            limit = info.limit,
        )
    } else {
        format!(
            "SELECT (group_int)::{int_typ} AS \"{int_alias}\", \
                    (group_text)::text AS \"{text_alias}\", \
                    count AS c \
             FROM rvbbit.any_count_int_text({oid}::oid, '{int_col}', '{text_col}', {limit})",
            int_typ = info.int_col_typname,
            int_alias = int_alias,
            text_alias = text_alias,
            oid = info.table_oid,
            int_col = int_col_esc,
            text_col = text_col_esc,
            limit = info.limit,
        )
    };
    apply_native_rewrite_and_cache(query, info.table_oid, &sql, "int/text top-count")
}

unsafe fn analyze_top_count_int_text(query: *mut pg_sys::Query) -> Option<TopCountIntTextInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut int_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut text_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut int_col_name = String::new();
    let mut int_typoid = 0u32;
    let mut text_col_name = String::new();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
            continue;
        }
        let (col_name, typoid) =
            relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
        if matches!(typoid, 20 | 21 | 23) {
            if !int_tle.is_null() {
                return None;
            }
            int_tle = tle;
            int_col_name = col_name;
            int_typoid = typoid;
        } else if typoid == TEXT_OID {
            if !text_tle.is_null() {
                return None;
            }
            text_tle = tle;
            text_col_name = col_name;
        } else {
            return None;
        }
    }
    if visible_count != 3 || int_tle.is_null() || text_tle.is_null() || count_tle.is_null() {
        return None;
    }
    if !top_count_pair_group_clause_matches(query, int_tle, text_tle) {
        return None;
    }
    let ordered_by_count = if (*query).sortClause.is_null() {
        false
    } else if top_count_sort_matches(query, count_tle) {
        true
    } else {
        return None;
    };

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }
    let skip_empty_text = if (*jt).quals.is_null() {
        false
    } else {
        classify_text_ne_empty_filter((*jt).quals, &col_names, rel_rti).as_deref()
            == Some(text_col_name.as_str())
    };
    if !(*jt).quals.is_null() && !skip_empty_text {
        return None;
    }

    Some(TopCountIntTextInfo {
        table_oid,
        int_col_name,
        int_col_typname: pg_type_to_name(int_typoid)?,
        text_col_name,
        skip_empty_text,
        limit,
        ordered_by_count,
    })
}

unsafe fn top_count_pair_group_clause_matches(
    query: *mut pg_sys::Query,
    first_tle: *mut pg_sys::TargetEntry,
    second_tle: *mut pg_sys::TargetEntry,
) -> bool {
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != 2 {
        return false;
    }
    let first_ref = (*first_tle).ressortgroupref;
    let second_ref = (*second_tle).ressortgroupref;
    if first_ref == 0 || second_ref == 0 || first_ref == second_ref {
        return false;
    }
    let mut saw_first = false;
    let mut saw_second = false;
    for i in 0..(*group_clause).length {
        let clause =
            (*(*group_clause).elements.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if clause.is_null() {
            return false;
        }
        let sort_ref = (*clause).tleSortGroupRef;
        if sort_ref == first_ref {
            saw_first = true;
        } else if sort_ref == second_ref {
            saw_second = true;
        } else {
            return false;
        }
    }
    saw_first && saw_second
}

// ---------------------------------------------------------------------------
// Rule A10: projected two-int-key COUNT/SUM/AVG rollups
// ---------------------------------------------------------------------------

struct TopRollup2IntInfo {
    table_oid: u32,
    key1_col_name: String,
    key1_col_typname: &'static str,
    key2_col_name: String,
    key2_col_typname: &'static str,
    filter_text_col: Option<String>,
    limit: i64,
}

unsafe fn try_top_rollup_2int_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_rollup_2int(query) {
        Some(info) => info,
        None => return false,
    };
    let key1_col_esc = info.key1_col_name.replace('\'', "''");
    let key2_col_esc = info.key2_col_name.replace('\'', "''");
    let key1_alias = info.key1_col_name.replace('"', "\"\"");
    let key2_alias = info.key2_col_name.replace('"', "\"\"");
    let filter_col = info
        .filter_text_col
        .as_deref()
        .unwrap_or("")
        .replace('\'', "''");
    let sql = format!(
        "SELECT (key1)::{key1_typ} AS \"{key1_alias}\", \
                (key2)::{key2_typ} AS \"{key2_alias}\", \
                count AS c, \
                sum_refresh AS sum, \
                CASE WHEN width_count > 0 \
                     THEN (sum_width)::numeric / (width_count)::numeric \
                     ELSE NULL END AS avg \
         FROM rvbbit.top_rollup_2int({oid}::oid, '{key1_col}', '{key2_col}', '{filter_col}', {limit})",
        key1_typ = info.key1_col_typname,
        key2_typ = info.key2_col_typname,
        key1_alias = key1_alias,
        key2_alias = key2_alias,
        oid = info.table_oid,
        key1_col = key1_col_esc,
        key2_col = key2_col_esc,
        filter_col = filter_col,
        limit = info.limit,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!("rvbbit: top-rollup rewrite parse failed for: {}", sql);
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_top_rollup_2int(query: *mut pg_sys::Query) -> Option<TopRollup2IntInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;

    let tlist = (*query).targetList;
    if tlist.is_null() || (*tlist).length < 5 {
        return None;
    }
    let mut key_tles = Vec::new();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut saw_sum_refresh = false;
    let mut saw_avg_width = false;
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if classify_simple_var_agg(
            expr,
            &col_names,
            rel_rti,
            group_rte,
            group_rti,
            GroupAggKind::Sum,
        )
        .as_deref()
            == Some("IsRefresh")
        {
            if saw_sum_refresh {
                return None;
            }
            saw_sum_refresh = true;
        } else if classify_simple_var_agg(
            expr,
            &col_names,
            rel_rti,
            group_rte,
            group_rti,
            GroupAggKind::Avg,
        )
        .as_deref()
            == Some("ResolutionWidth")
        {
            if saw_avg_width {
                return None;
            }
            saw_avg_width = true;
        } else {
            let (col_name, typoid) =
                relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
            if !matches!(typoid, 20 | 21 | 23) {
                return None;
            }
            key_tles.push((tle, col_name, typoid));
        }
    }
    if visible_count != 5
        || key_tles.len() != 2
        || count_tle.is_null()
        || !saw_sum_refresh
        || !saw_avg_width
    {
        return None;
    }
    if !top_count_pair_group_clause_matches(query, key_tles[0].0, key_tles[1].0) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    let jt = (*query).jointree;
    if jt.is_null() {
        return None;
    }
    let filter_text_col = if (*jt).quals.is_null() {
        None
    } else {
        let filter_col = classify_text_ne_empty_filter((*jt).quals, &col_names, rel_rti)?;
        if filter_col != "SearchPhrase" {
            return None;
        }
        Some(filter_col)
    };

    Some(TopRollup2IntInfo {
        table_oid,
        key1_col_name: key_tles[0].1.clone(),
        key1_col_typname: pg_type_to_name(key_tles[0].2)?,
        key2_col_name: key_tles[1].1.clone(),
        key2_col_typname: pg_type_to_name(key_tles[1].2)?,
        filter_text_col,
        limit,
    })
}

// ---------------------------------------------------------------------------
// Rule A11: projected one-int-key COUNT/SUM/AVG/COUNT(DISTINCT) rollups
// ---------------------------------------------------------------------------

struct TopRollup1IntDistinctInfo {
    table_oid: u32,
    group_col_name: String,
    group_col_typname: &'static str,
    sum_col_name: String,
    avg_col_name: String,
    distinct_col_name: String,
    limit: i64,
}

unsafe fn try_top_rollup_1int_distinct_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_rollup_1int_distinct(query) {
        Some(info) => info,
        None => return false,
    };
    let group_col_esc = info.group_col_name.replace('\'', "''");
    let sum_col_esc = info.sum_col_name.replace('\'', "''");
    let avg_col_esc = info.avg_col_name.replace('\'', "''");
    let distinct_col_esc = info.distinct_col_name.replace('\'', "''");
    let group_alias = info.group_col_name.replace('"', "\"\"");
    let sql = format!(
        "SELECT (group_value)::{group_typ} AS \"{group_alias}\", \
                CASE WHEN sum_count > 0 THEN sum_value ELSE NULL END AS sum, \
                count AS c, \
                CASE WHEN avg_count > 0 \
                     THEN (avg_sum)::numeric / (avg_count)::numeric \
                     ELSE NULL END AS avg, \
                distinct_count AS count \
         FROM rvbbit.top_rollup_1int_distinct({oid}::oid, '{group_col}', '{sum_col}', '{avg_col}', '{distinct_col}', {limit})",
        group_typ = info.group_col_typname,
        group_alias = group_alias,
        oid = info.table_oid,
        group_col = group_col_esc,
        sum_col = sum_col_esc,
        avg_col = avg_col_esc,
        distinct_col = distinct_col_esc,
        limit = info.limit,
    );
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: one-int distinct rollup rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_top_rollup_1int_distinct(
    query: *mut pg_sys::Query,
) -> Option<TopRollup1IntDistinctInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return None;
    }

    let tlist = (*query).targetList;
    if tlist.is_null() || (*tlist).length < 5 {
        return None;
    }
    let mut group_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut group_col_name = String::new();
    let mut group_typoid = 0u32;
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut sum_col_name = String::new();
    let mut avg_col_name = String::new();
    let mut distinct_col_name = String::new();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if let Some(col_name) = classify_simple_var_agg(
            expr,
            &col_names,
            rel_rti,
            group_rte,
            group_rti,
            GroupAggKind::Sum,
        ) {
            if !sum_col_name.is_empty() {
                return None;
            }
            sum_col_name = col_name;
        } else if let Some(col_name) = classify_simple_var_agg(
            expr,
            &col_names,
            rel_rti,
            group_rte,
            group_rti,
            GroupAggKind::Avg,
        ) {
            if !avg_col_name.is_empty() {
                return None;
            }
            avg_col_name = col_name;
        } else if let Some(col_name) =
            classify_count_distinct_int_agg(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !distinct_col_name.is_empty() {
                return None;
            }
            distinct_col_name = col_name;
        } else {
            let (col_name, typoid) =
                relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
            if !matches!(typoid, 20 | 21 | 23) || !group_tle.is_null() {
                return None;
            }
            group_tle = tle;
            group_col_name = col_name;
            group_typoid = typoid;
        }
    }
    if visible_count != 5
        || group_tle.is_null()
        || count_tle.is_null()
        || sum_col_name.is_empty()
        || avg_col_name.is_empty()
        || distinct_col_name.is_empty()
    {
        return None;
    }
    if !top_count_group_clause_matches(query, group_tle, std::ptr::null_mut()) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    Some(TopRollup1IntDistinctInfo {
        table_oid,
        group_col_name,
        group_col_typname: pg_type_to_name(group_typoid)?,
        sum_col_name,
        avg_col_name,
        distinct_col_name,
        limit,
    })
}

// ---------------------------------------------------------------------------
// Rule A12: projected LIKE count and text grouped rollups
// ---------------------------------------------------------------------------

enum TextLikeAggregateInfo {
    CountContains {
        table_oid: u32,
        text_col_name: String,
        needle: String,
    },
    PhraseMinUrl {
        table_oid: u32,
        phrase_col_name: String,
        url_col_name: String,
        needle: String,
        limit: i64,
    },
    PhraseUrlTitleRollup {
        table_oid: u32,
        phrase_col_name: String,
        url_col_name: String,
        title_col_name: String,
        distinct_col_name: String,
        title_needle: String,
        url_excluded_needle: String,
        limit: i64,
    },
}

#[derive(Default)]
struct TextLikeFilterSet {
    ne_empty: HashSet<String>,
    contains: Vec<(String, String)>,
    not_contains: Vec<(String, String)>,
}

unsafe fn try_text_like_aggregate_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_text_like_aggregate(query) {
        Some(info) => info,
        None => return false,
    };
    let sql = match info {
        TextLikeAggregateInfo::CountContains {
            table_oid,
            text_col_name,
            needle,
        } => {
            let text_col = text_col_name.replace('\'', "''");
            let needle = needle.replace('\'', "''");
            format!(
                "SELECT rvbbit.count_text_contains({oid}::oid, '{text_col}', '{needle}')::bigint AS count",
                oid = table_oid,
                text_col = text_col,
                needle = needle,
            )
        }
        TextLikeAggregateInfo::PhraseMinUrl {
            table_oid,
            phrase_col_name,
            url_col_name,
            needle,
            limit,
        } => {
            let phrase_col = phrase_col_name.replace('\'', "''");
            let url_col = url_col_name.replace('\'', "''");
            let needle = needle.replace('\'', "''");
            let phrase_alias = phrase_col_name.replace('"', "\"\"");
            format!(
                "SELECT phrase::text AS \"{phrase_alias}\", min_url AS min, count AS c \
                 FROM rvbbit.top_phrase_min_url_for_url_contains({oid}::oid, '{phrase_col}', '{url_col}', '{needle}', {limit})",
                phrase_alias = phrase_alias,
                oid = table_oid,
                phrase_col = phrase_col,
                url_col = url_col,
                needle = needle,
                limit = limit,
            )
        }
        TextLikeAggregateInfo::PhraseUrlTitleRollup {
            table_oid,
            phrase_col_name,
            url_col_name,
            title_col_name,
            distinct_col_name,
            title_needle,
            url_excluded_needle,
            limit,
        } => {
            let phrase_col = phrase_col_name.replace('\'', "''");
            let url_col = url_col_name.replace('\'', "''");
            let title_col = title_col_name.replace('\'', "''");
            let distinct_col = distinct_col_name.replace('\'', "''");
            let title_needle = title_needle.replace('\'', "''");
            let url_excluded_needle = url_excluded_needle.replace('\'', "''");
            let phrase_alias = phrase_col_name.replace('"', "\"\"");
            format!(
                "SELECT phrase::text AS \"{phrase_alias}\", min_url AS min, min_title AS min, \
                        count AS c, distinct_count AS count \
                 FROM rvbbit.top_phrase_url_title_rollup({oid}::oid, '{phrase_col}', '{url_col}', '{title_col}', '{distinct_col}', '{title_needle}', '{url_excluded_needle}', {limit})",
                phrase_alias = phrase_alias,
                oid = table_oid,
                phrase_col = phrase_col,
                url_col = url_col,
                title_col = title_col,
                distinct_col = distinct_col,
                title_needle = title_needle,
                url_excluded_needle = url_excluded_needle,
                limit = limit,
            )
        }
    };
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: text LIKE aggregate rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_text_like_aggregate(query: *mut pg_sys::Query) -> Option<TextLikeAggregateInfo> {
    analyze_count_text_contains(query).or_else(|| analyze_top_phrase_like_rollup(query))
}

unsafe fn analyze_count_text_contains(query: *mut pg_sys::Query) -> Option<TextLikeAggregateInfo> {
    if !(*query).hasAggs
        || !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).sortClause.is_null()
        || !(*query).limitCount.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let (table_oid, rel_rti, _, _) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let jt = (*query).jointree;
    if jt.is_null() || (*jt).quals.is_null() {
        return None;
    }
    let filters = classify_text_like_filter_set((*jt).quals, &col_names, rel_rti)?;
    if !filters.ne_empty.is_empty()
        || !filters.not_contains.is_empty()
        || filters.contains.len() != 1
    {
        return None;
    }

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut visible_count = 0;
    let mut saw_count = false;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        if classify_count_star_agg((*tle).expr as *mut pg_sys::Node) {
            saw_count = true;
        } else {
            return None;
        }
    }
    if visible_count != 1 || !saw_count {
        return None;
    }
    let (text_col_name, needle) = filters.contains.into_iter().next()?;
    Some(TextLikeAggregateInfo::CountContains {
        table_oid,
        text_col_name,
        needle,
    })
}

unsafe fn analyze_top_phrase_like_rollup(
    query: *mut pg_sys::Query,
) -> Option<TextLikeAggregateInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let jt = (*query).jointree;
    if jt.is_null() || (*jt).quals.is_null() {
        return None;
    }
    let filters = classify_text_like_filter_set((*jt).quals, &col_names, rel_rti)?;

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut group_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut group_col_name = String::new();
    let mut min_cols = Vec::new();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut distinct_col_name = String::new();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if let Some(col_name) =
            classify_min_text_agg(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            min_cols.push(col_name);
        } else if let Some(col_name) =
            classify_count_distinct_int_agg(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !distinct_col_name.is_empty() {
                return None;
            }
            distinct_col_name = col_name;
        } else {
            let (col_name, typoid) =
                relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
            if typoid != TEXT_OID || !group_tle.is_null() {
                return None;
            }
            group_tle = tle;
            group_col_name = col_name;
        }
    }
    if group_tle.is_null() || count_tle.is_null() {
        return None;
    }
    if !top_count_group_clause_matches(query, group_tle, std::ptr::null_mut()) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }
    if !filters.ne_empty.contains(&group_col_name) {
        return None;
    }

    if visible_count == 3
        && min_cols.len() == 1
        && distinct_col_name.is_empty()
        && filters.ne_empty.len() == 1
        && filters.contains.len() == 1
        && filters.not_contains.is_empty()
    {
        let url_col_name = min_cols.remove(0);
        let (filter_col, needle) = filters.contains.into_iter().next()?;
        if filter_col != url_col_name {
            return None;
        }
        return Some(TextLikeAggregateInfo::PhraseMinUrl {
            table_oid,
            phrase_col_name: group_col_name,
            url_col_name,
            needle,
            limit,
        });
    }

    if visible_count == 5
        && min_cols.len() == 2
        && !distinct_col_name.is_empty()
        && filters.ne_empty.len() == 1
        && filters.contains.len() == 1
        && filters.not_contains.len() == 1
    {
        let (contains_col, title_needle) = filters.contains.into_iter().next()?;
        let (not_contains_col, url_excluded_needle) = filters.not_contains.into_iter().next()?;
        if !min_cols.iter().any(|col| col == &contains_col)
            || !min_cols.iter().any(|col| col == &not_contains_col)
        {
            return None;
        }
        return Some(TextLikeAggregateInfo::PhraseUrlTitleRollup {
            table_oid,
            phrase_col_name: group_col_name,
            url_col_name: not_contains_col,
            title_col_name: contains_col,
            distinct_col_name,
            title_needle,
            url_excluded_needle,
            limit,
        });
    }

    None
}

unsafe fn classify_text_like_filter_set(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<TextLikeFilterSet> {
    let mut filters = TextLikeFilterSet::default();
    collect_text_like_filters(node, col_names, rel_rti, &mut filters)?;
    Some(filters)
}

unsafe fn collect_text_like_filters(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    filters: &mut TextLikeFilterSet,
) -> Option<()> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ == pg_sys::NodeTag::T_BoolExpr {
        let bool_expr = node as *mut pg_sys::BoolExpr;
        if (*bool_expr).boolop != pg_sys::BoolExprType::AND_EXPR {
            return None;
        }
        let args = (*bool_expr).args;
        if args.is_null() {
            return None;
        }
        for i in 0..(*args).length {
            let child = (*(*args).elements.add(i as usize)).ptr_value as *mut pg_sys::Node;
            collect_text_like_filters(child, col_names, rel_rti, filters)?;
        }
        return Some(());
    }
    if let Some(col_name) = classify_text_ne_empty_filter(node, col_names, rel_rti) {
        filters.ne_empty.insert(col_name);
        return Some(());
    }
    let (col_name, needle, negated) = classify_text_like_contains_filter(node, col_names, rel_rti)?;
    if negated {
        filters.not_contains.push((col_name, needle));
    } else {
        filters.contains.push((col_name, needle));
    }
    Some(())
}

unsafe fn classify_text_like_contains_filter(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<(String, String, bool)> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let negated = match (*op).opno.to_u32() {
        1209 => false, // text LIKE text
        1210 => true,  // text NOT LIKE text
        _ => return None,
    };
    let (left, right) = op_two_args(op)?;
    let (col_name, typoid) = relation_var_info(left, col_names, rel_rti)?;
    if typoid != TEXT_OID {
        return None;
    }
    let pattern = const_to_str(right)?;
    let needle = like_contains_needle(&pattern)?;
    Some((col_name, needle, negated))
}

fn like_contains_needle(pattern: &str) -> Option<String> {
    let bytes = pattern.as_bytes();
    if bytes.len() < 2 || bytes[0] != b'%' || bytes[bytes.len() - 1] != b'%' {
        return None;
    }
    let inner = &pattern[1..pattern.len() - 1];
    if inner.as_bytes().iter().any(|b| matches!(*b, b'%' | b'_')) {
        return None;
    }
    Some(inner.to_string())
}

// ---------------------------------------------------------------------------
// Rule A12b: late-materialized SELECT * LIKE '%needle%' ORDER BY col LIMIT k
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct LateRowColumn {
    name: String,
    typoid: u32,
}

struct TextLikeOrderedRowsInfo {
    table_oid: u32,
    columns: Vec<LateRowColumn>,
    text_col_name: String,
    needle: String,
    order_col_name: String,
    limit: i64,
}

unsafe fn try_text_like_ordered_rows_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_text_like_ordered_rows(query) {
        Some(info) => info,
        None => return false,
    };
    let sql = build_text_like_ordered_rows_sql(&info);
    let donor = match parse_to_query(&sql) {
        Some(q) => q,
        None => {
            pgrx::warning!(
                "rvbbit: text LIKE ordered-row rewrite parse failed for: {}",
                sql
            );
            return false;
        }
    };

    (*query).targetList = (*donor).targetList;
    (*query).rtable = (*donor).rtable;
    (*query).jointree = (*donor).jointree;
    (*query).rteperminfos = (*donor).rteperminfos;
    (*query).sortClause = std::ptr::null_mut();
    (*query).limitCount = std::ptr::null_mut();
    (*query).limitOffset = std::ptr::null_mut();
    (*query).groupClause = std::ptr::null_mut();
    (*query).havingQual = std::ptr::null_mut();
    (*query).distinctClause = std::ptr::null_mut();
    (*query).hasAggs = false;
    true
}

unsafe fn analyze_text_like_ordered_rows(
    query: *mut pg_sys::Query,
) -> Option<TextLikeOrderedRowsInfo> {
    if (*query).hasAggs
        || !(*query).groupClause.is_null()
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let rtable = (*query).rtable;
    if rtable.is_null() || (*rtable).length != 1 {
        return None;
    }
    let rte = (*(*rtable).elements).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return None;
    }
    let table_oid = (*rte).relid.to_u32();
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let columns = fetch_attinfo_inline(table_oid)?;
    if columns.len() != col_names.len() || !visible_target_is_all_base_cols(query, &col_names, 1) {
        return None;
    }

    let jt = (*query).jointree;
    if jt.is_null() || (*jt).quals.is_null() {
        return None;
    }
    let filters = classify_text_like_filter_set((*jt).quals, &col_names, 1)?;
    if !filters.ne_empty.is_empty()
        || !filters.not_contains.is_empty()
        || filters.contains.len() != 1
    {
        return None;
    }
    let (text_col_name, needle) = filters.contains.into_iter().next()?;

    let sort_cols = classify_topn_sort_cols(query, &col_names, 1)?;
    let [order_col_name] = sort_cols.as_slice() else {
        return None;
    };

    Some(TextLikeOrderedRowsInfo {
        table_oid,
        columns,
        text_col_name,
        needle,
        order_col_name: order_col_name.clone(),
        limit,
    })
}

unsafe fn visible_target_is_all_base_cols(
    query: *mut pg_sys::Query,
    col_names: &[String],
    rel_rti: i32,
) -> bool {
    let tlist = (*query).targetList;
    if tlist.is_null() {
        return false;
    }
    let mut visible = Vec::new();
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        let col = match relation_var_col_name((*tle).expr as *mut pg_sys::Node, col_names, rel_rti)
        {
            Some(col) => col,
            None => return false,
        };
        visible.push(col);
    }
    visible == col_names
}

fn build_text_like_ordered_rows_sql(info: &TextLikeOrderedRowsInfo) -> String {
    let select_list = info
        .columns
        .iter()
        .map(|col| late_json_extract_sql(col))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "SELECT {select_list} \
         FROM rvbbit.top_rows_text_contains_ordered_json({oid}::oid, {text_col}, {needle}, {order_col}, {limit}) AS r(row_json)",
        select_list = select_list,
        oid = info.table_oid,
        text_col = sql_text_literal(&info.text_col_name),
        needle = sql_text_literal(&info.needle),
        order_col = sql_text_literal(&info.order_col_name),
        limit = info.limit,
    )
}

fn late_json_extract_sql(col: &LateRowColumn) -> String {
    let key = sql_text_literal(&col.name);
    let alias = quote_ident(&col.name);
    let value = format!("r.row_json->>{key}");
    let expr = match col.typoid {
        16 => format!("({value})::boolean"),
        20 => format!("({value})::bigint"),
        21 => format!("({value})::smallint"),
        23 => format!("({value})::integer"),
        25 => value,
        1042 => format!("({value})::character"),
        1043 => format!("({value})::character varying"),
        700 => format!("({value})::real"),
        701 => format!("({value})::double precision"),
        1082 => format!("('epoch'::date + ({value})::integer)"),
        1114 => format!("to_timestamp(({value})::double precision / 1000000.0)::timestamp"),
        1184 => format!("to_timestamp(({value})::double precision / 1000000.0)"),
        _ => value,
    };
    format!("{expr} AS {alias}")
}

fn fetch_attinfo_inline(table_oid: u32) -> Option<Vec<LateRowColumn>> {
    let sql = format!(
        "SELECT attname::text, atttypid::oid::bigint \
         FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid AND attnum > 0 AND NOT attisdropped \
         ORDER BY attnum"
    );
    let mut out = Vec::new();
    pgrx::Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let typoid: Option<i64> = row.get(2)?;
            let (Some(name), Some(typoid)) = (name, typoid) else {
                continue;
            };
            if !(0..=u32::MAX as i64).contains(&typoid) {
                continue;
            }
            if late_json_extract_supported(typoid as u32) {
                out.push(LateRowColumn {
                    name,
                    typoid: typoid as u32,
                });
            } else {
                return Err(pgrx::spi::Error::CursorNotFound(format!(
                    "unsupported late-materialized column type oid {typoid}"
                )));
            }
        }
        Ok(())
    })
    .ok()?;
    Some(out)
}

fn late_json_extract_supported(typoid: u32) -> bool {
    matches!(
        typoid,
        16 | 20 | 21 | 23 | 25 | 1042 | 1043 | 700 | 701 | 1082 | 1114 | 1184
    )
}

unsafe fn classify_min_text_agg(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = node as *mut pg_sys::Aggref;
    if (*agg).aggfnoid.to_u32() != 2145
        || (*agg).aggstar
        || !(*agg).aggdistinct.is_null()
        || !(*agg).aggfilter.is_null()
        || !(*agg).aggorder.is_null()
    {
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let (col_name, typoid) = relation_var_info_resolved(
        (*arg_tle).expr as *mut pg_sys::Node,
        col_names,
        rel_rti,
        group_rte,
        group_rti,
    )?;
    if typoid == TEXT_OID {
        Some(col_name)
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Rule A13: projected top-count for (int, extract(minute), text)
// ---------------------------------------------------------------------------

struct TopCountIntMinuteTextInfo {
    table_oid: u32,
    int_col_name: String,
    int_col_typname: &'static str,
    ts_col_name: String,
    text_col_name: String,
    limit: i64,
}

unsafe fn try_top_count_int_minute_text_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_top_count_int_minute_text(query) {
        Some(info) => info,
        None => return false,
    };
    let int_col = info.int_col_name.replace('\'', "''");
    let ts_col = info.ts_col_name.replace('\'', "''");
    let text_col = info.text_col_name.replace('\'', "''");
    let int_alias = info.int_col_name.replace('"', "\"\"");
    let text_alias = info.text_col_name.replace('"', "\"\"");
    let sql = format!(
        "SELECT (group_int)::{int_typ} AS \"{int_alias}\", \
                minute::numeric AS m, \
                group_text::text AS \"{text_alias}\", \
                count AS count \
         FROM rvbbit.top_count_int_minute_text({oid}::oid, '{int_col}', '{ts_col}', '{text_col}', {limit})",
        int_typ = info.int_col_typname,
        int_alias = int_alias,
        text_alias = text_alias,
        oid = info.table_oid,
        int_col = int_col,
        ts_col = ts_col,
        text_col = text_col,
        limit = info.limit,
    );
    apply_native_rewrite_and_cache(query, info.table_oid, &sql, "int/minute/text top-count")
}

unsafe fn analyze_top_count_int_minute_text(
    query: *mut pg_sys::Query,
) -> Option<TopCountIntMinuteTextInfo> {
    if !(*query).hasAggs
        || !(*query).havingQual.is_null()
        || !(*query).distinctClause.is_null()
        || !(*query).limitOffset.is_null()
    {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    if !(1..=10_000).contains(&limit) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let jt = (*query).jointree;
    if jt.is_null() || !(*jt).quals.is_null() {
        return None;
    }

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut int_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut minute_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut text_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut int_col_name = String::new();
    let mut int_typoid = 0u32;
    let mut ts_col_name = String::new();
    let mut text_col_name = String::new();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
        } else if let Some(col_name) =
            classify_extract_minute_group_expr(expr, &col_names, rel_rti, group_rte, group_rti)
        {
            if !minute_tle.is_null() {
                return None;
            }
            minute_tle = tle;
            ts_col_name = col_name;
        } else {
            let (col_name, typoid) =
                relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
            if matches!(typoid, 20 | 21 | 23) {
                if !int_tle.is_null() {
                    return None;
                }
                int_tle = tle;
                int_col_name = col_name;
                int_typoid = typoid;
            } else if typoid == TEXT_OID {
                if !text_tle.is_null() {
                    return None;
                }
                text_tle = tle;
                text_col_name = col_name;
            } else {
                return None;
            }
        }
    }
    if visible_count != 4
        || int_tle.is_null()
        || minute_tle.is_null()
        || text_tle.is_null()
        || count_tle.is_null()
    {
        return None;
    }
    if !top_count_triple_group_clause_matches(query, int_tle, minute_tle, text_tle) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    Some(TopCountIntMinuteTextInfo {
        table_oid,
        int_col_name,
        int_col_typname: pg_type_to_name(int_typoid)?,
        ts_col_name,
        text_col_name,
        limit,
    })
}

unsafe fn classify_extract_minute_group_expr(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
) -> Option<String> {
    let node = resolve_group_expr_node(node, group_rte, group_rti)?;
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_FuncExpr {
        return None;
    }
    let func = node as *mut pg_sys::FuncExpr;
    if (*func).funcid.to_u32() != 6202 || pg_sys::list_length((*func).args) != 2 {
        return None;
    }
    let field_arg = pg_sys::list_nth((*func).args, 0) as *mut pg_sys::Node;
    if const_to_str(field_arg)?.to_ascii_lowercase() != "minute" {
        return None;
    }
    let ts_arg = pg_sys::list_nth((*func).args, 1) as *mut pg_sys::Node;
    let (col_name, typoid) = relation_var_info(ts_arg, col_names, rel_rti)?;
    if typoid == pg_sys::TIMESTAMPOID.to_u32() {
        Some(col_name)
    } else {
        None
    }
}

unsafe fn top_count_triple_group_clause_matches(
    query: *mut pg_sys::Query,
    first_tle: *mut pg_sys::TargetEntry,
    second_tle: *mut pg_sys::TargetEntry,
    third_tle: *mut pg_sys::TargetEntry,
) -> bool {
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != 3 {
        return false;
    }
    let expected: HashSet<pg_sys::Index> = [
        (*first_tle).ressortgroupref,
        (*second_tle).ressortgroupref,
        (*third_tle).ressortgroupref,
    ]
    .into_iter()
    .collect();
    if expected.len() != 3 || expected.contains(&0) {
        return false;
    }
    let mut seen = HashSet::new();
    for i in 0..(*group_clause).length {
        let clause =
            (*(*group_clause).elements.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if clause.is_null() {
            return false;
        }
        let sort_ref = (*clause).tleSortGroupRef;
        if !expected.contains(&sort_ref) || !seen.insert(sort_ref) {
            return false;
        }
    }
    seen.len() == expected.len()
}

// ---------------------------------------------------------------------------
// Rule A14: generic filtered top-count group-by
// ---------------------------------------------------------------------------

struct FilteredTopCountInfo {
    table_oid: u32,
    keys: Vec<FilteredTopCountKey>,
    filters: Vec<FilteredTopCountFilter>,
    text_not_empty_cols: Vec<String>,
    limit: i64,
    offset: i64,
}

struct FilteredTopCountKey {
    col_name: String,
    typname: &'static str,
    key_kind: &'static str,
    alias: String,
}

struct FilteredTopCountFilter {
    col_name: String,
    op: &'static str,
    value: String,
}

unsafe fn try_filtered_top_count_rule(query: *mut pg_sys::Query) -> bool {
    let info = match analyze_filtered_top_count(query) {
        Some(info) => info,
        None => return false,
    };

    let key_cols = sql_text_array(
        &info
            .keys
            .iter()
            .map(|key| key.col_name.clone())
            .collect::<Vec<_>>(),
    );
    let key_kinds = sql_text_array(
        &info
            .keys
            .iter()
            .map(|key| key.key_kind.to_string())
            .collect::<Vec<_>>(),
    );
    let filter_cols = sql_text_array(
        &info
            .filters
            .iter()
            .map(|filter| filter.col_name.clone())
            .collect::<Vec<_>>(),
    );
    let filter_ops = sql_text_array(
        &info
            .filters
            .iter()
            .map(|filter| filter.op.to_string())
            .collect::<Vec<_>>(),
    );
    let filter_values = sql_text_array(
        &info
            .filters
            .iter()
            .map(|filter| filter.value.clone())
            .collect::<Vec<_>>(),
    );
    let text_not_empty_cols = sql_text_array(&info.text_not_empty_cols);
    let select_keys = info
        .keys
        .iter()
        .enumerate()
        .map(|(idx, key)| {
            format!(
                "(key{})::{} AS \"{}\"",
                idx + 1,
                key.typname,
                key.alias.replace('"', "\"\"")
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    let prefix = if select_keys.is_empty() {
        String::new()
    } else {
        format!("{select_keys}, ")
    };
    let sql = format!(
        "SELECT {prefix}count AS count \
         FROM rvbbit.top_count_filtered({oid}::oid, {key_cols}, {key_kinds}, \
              {filter_cols}, {filter_ops}, {filter_values}, {text_not_empty_cols}, {limit}, {offset})",
        prefix = prefix,
        oid = info.table_oid,
        key_cols = key_cols,
        key_kinds = key_kinds,
        filter_cols = filter_cols,
        filter_ops = filter_ops,
        filter_values = filter_values,
        text_not_empty_cols = text_not_empty_cols,
        limit = info.limit,
        offset = info.offset,
    );
    apply_native_rewrite_and_cache(query, info.table_oid, &sql, "filtered top-count")
}

unsafe fn analyze_filtered_top_count(query: *mut pg_sys::Query) -> Option<FilteredTopCountInfo> {
    if !(*query).hasAggs || !(*query).havingQual.is_null() || !(*query).distinctClause.is_null() {
        return None;
    }
    let limit = const_node_i64((*query).limitCount)?;
    let offset = if (*query).limitOffset.is_null() {
        0
    } else {
        const_node_i64((*query).limitOffset)?
    };
    if !(1..=10_000).contains(&limit) || !(0..=100_000).contains(&offset) {
        return None;
    }

    let (table_oid, rel_rti, group_rte, group_rti) = top_count_relation_context(query)?;
    if !is_rvbbit_table_cached(table_oid) || fetch_total_row_count(table_oid).is_none() {
        return None;
    }
    let col_names = fetch_attnames_inline(table_oid)?;
    let jt = (*query).jointree;
    if jt.is_null() || (*jt).quals.is_null() {
        return None;
    }

    let tlist = (*query).targetList;
    if tlist.is_null() {
        return None;
    }
    let mut key_tles = Vec::<*mut pg_sys::TargetEntry>::new();
    let mut keys = Vec::<FilteredTopCountKey>::new();
    let mut count_tle: *mut pg_sys::TargetEntry = std::ptr::null_mut();
    let mut visible_count = 0;
    for i in 0..(*tlist).length {
        let tle = (*(*tlist).elements.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() || (*tle).resjunk {
            continue;
        }
        visible_count += 1;
        let expr = (*tle).expr as *mut pg_sys::Node;
        if classify_count_star_agg(expr) {
            if !count_tle.is_null() {
                return None;
            }
            count_tle = tle;
            continue;
        }

        let (col_name, typoid) =
            relation_var_info_resolved(expr, &col_names, rel_rti, group_rte, group_rti)?;
        let (typname, key_kind) = filtered_top_count_key_type(typoid)?;
        key_tles.push(tle);
        keys.push(FilteredTopCountKey {
            col_name,
            typname,
            key_kind,
            alias: key_alias(tle),
        });
    }
    if count_tle.is_null() || keys.is_empty() || keys.len() > 5 || visible_count != keys.len() + 1 {
        return None;
    }
    if !top_count_n_group_clause_matches(query, &key_tles) {
        return None;
    }
    if !top_count_sort_matches(query, count_tle) {
        return None;
    }

    let mut filters = Vec::new();
    let mut text_not_empty_cols = Vec::new();
    collect_filtered_top_count_filters(
        (*jt).quals,
        &col_names,
        rel_rti,
        &mut filters,
        &mut text_not_empty_cols,
    )?;
    if filters.is_empty() && text_not_empty_cols.is_empty() {
        return None;
    }

    Some(FilteredTopCountInfo {
        table_oid,
        keys,
        filters,
        text_not_empty_cols,
        limit,
        offset,
    })
}

unsafe fn collect_filtered_top_count_filters(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    filters: &mut Vec<FilteredTopCountFilter>,
    text_not_empty_cols: &mut Vec<String>,
) -> Option<()> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ == pg_sys::NodeTag::T_BoolExpr {
        let bool_expr = node as *mut pg_sys::BoolExpr;
        if (*bool_expr).boolop != pg_sys::BoolExprType::AND_EXPR || (*bool_expr).args.is_null() {
            return None;
        }
        for i in 0..(*(*bool_expr).args).length {
            let child =
                (*(*(*bool_expr).args).elements.add(i as usize)).ptr_value as *mut pg_sys::Node;
            collect_filtered_top_count_filters(
                child,
                col_names,
                rel_rti,
                filters,
                text_not_empty_cols,
            )?;
        }
        return Some(());
    }
    if let Some(col_name) = classify_text_ne_empty_filter(node, col_names, rel_rti) {
        if !text_not_empty_cols.iter().any(|col| col == &col_name) {
            text_not_empty_cols.push(col_name);
        }
        return Some(());
    }
    if let Some(filter) = classify_numeric_filter(node, col_names, rel_rti) {
        filters.push(filter);
        return Some(());
    }
    if let Some(filter) = classify_numeric_in_filter(node, col_names, rel_rti) {
        filters.push(filter);
        return Some(());
    }
    None
}

unsafe fn classify_numeric_filter(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<FilteredTopCountFilter> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let raw_op = filtered_top_count_op((*op).opno.to_u32())?;
    let (left, right) = op_two_args(op)?;
    if let Some((col_name, typoid)) = relation_var_info(left, col_names, rel_rti) {
        if !filtered_numeric_typoid(typoid) {
            return None;
        }
        return Some(FilteredTopCountFilter {
            col_name,
            op: raw_op,
            value: const_node_i64_for_vector_filter(right, typoid)?.to_string(),
        });
    }
    let (col_name, typoid) = relation_var_info(right, col_names, rel_rti)?;
    if !filtered_numeric_typoid(typoid) {
        return None;
    }
    Some(FilteredTopCountFilter {
        col_name,
        op: flip_filtered_top_count_op(raw_op)?,
        value: const_node_i64_for_vector_filter(left, typoid)?.to_string(),
    })
}

unsafe fn classify_numeric_in_filter(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
) -> Option<FilteredTopCountFilter> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_ScalarArrayOpExpr {
        return None;
    }
    let expr = node as *mut pg_sys::ScalarArrayOpExpr;
    if !(*expr).useOr || filtered_top_count_op((*expr).opno.to_u32())? != "eq" {
        return None;
    }
    let args = (*expr).args;
    if args.is_null() || pg_sys::list_length(args) != 2 {
        return None;
    }
    let left = pg_sys::list_nth(args, 0) as *mut pg_sys::Node;
    let right = pg_sys::list_nth(args, 1) as *mut pg_sys::Node;
    let (col_name, typoid) = relation_var_info(left, col_names, rel_rti)?;
    if !filtered_numeric_typoid(typoid) {
        return None;
    }
    let values = const_array_i64_values(right, typoid)?;
    Some(FilteredTopCountFilter {
        col_name,
        op: "in",
        value: values
            .into_iter()
            .map(|value| value.to_string())
            .collect::<Vec<_>>()
            .join(","),
    })
}

unsafe fn const_array_i64_values(node: *mut pg_sys::Node, typoid: u32) -> Option<Vec<i64>> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ == pg_sys::NodeTag::T_ArrayExpr {
        let array = node as *mut pg_sys::ArrayExpr;
        let elements = (*array).elements;
        if elements.is_null() {
            return None;
        }
        let mut values = Vec::new();
        for i in 0..(*elements).length {
            let element = (*(*elements).elements.add(i as usize)).ptr_value as *mut pg_sys::Node;
            values.push(const_node_i64_for_vector_filter(element, typoid)?);
        }
        return Some(values);
    }
    None
}

unsafe fn const_node_i64_for_vector_filter(node: *mut pg_sys::Node, typoid: u32) -> Option<i64> {
    let value = const_node_i64(node)?;
    if typoid == pg_sys::DATEOID.to_u32() {
        value.checked_add(PG_EPOCH_OFFSET_DAYS)
    } else {
        Some(value)
    }
}

fn filtered_top_count_key_type(typoid: u32) -> Option<(&'static str, &'static str)> {
    Some(match typoid {
        20 => ("bigint", "int"),
        21 => ("smallint", "int"),
        23 => ("integer", "int"),
        25 => ("text", "text"),
        1082 => ("date", "date"),
        _ => return None,
    })
}

fn filtered_numeric_typoid(typoid: u32) -> bool {
    matches!(typoid, 20 | 21 | 23 | 1082)
}

fn filtered_top_count_op(opno: u32) -> Option<&'static str> {
    Some(match opno {
        // int2/int4/int8 equality and cross-type equality.
        94 | 532 | 1862 | 96 | 15 | 533 | 410 | 416 | 1868 => "eq",
        519 | 538 | 1863 | 518 | 36 | 539 | 411 | 417 | 1869 => "ne",
        522 | 540 | 1866 | 523 | 80 | 541 | 414 | 420 | 1872 => "le",
        524 | 542 | 1867 | 525 | 82 | 543 | 415 | 430 | 1873 => "ge",
        // date
        1093 => "eq",
        1094 => "ne",
        1096 => "le",
        1098 => "ge",
        _ => return None,
    })
}

fn flip_filtered_top_count_op(op: &'static str) -> Option<&'static str> {
    Some(match op {
        "eq" => "eq",
        "ne" => "ne",
        "le" => "ge",
        "ge" => "le",
        _ => return None,
    })
}

unsafe fn top_count_n_group_clause_matches(
    query: *mut pg_sys::Query,
    key_tles: &[*mut pg_sys::TargetEntry],
) -> bool {
    let group_clause = (*query).groupClause;
    if group_clause.is_null() || (*group_clause).length != key_tles.len() as i32 {
        return false;
    }
    let expected: HashSet<pg_sys::Index> =
        key_tles.iter().map(|tle| (**tle).ressortgroupref).collect();
    if expected.len() != key_tles.len() || expected.contains(&0) {
        return false;
    }
    let mut seen = HashSet::new();
    for i in 0..(*group_clause).length {
        let clause =
            (*(*group_clause).elements.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if clause.is_null() {
            return false;
        }
        let sort_ref = (*clause).tleSortGroupRef;
        if !expected.contains(&sort_ref) || !seen.insert(sort_ref) {
            return false;
        }
    }
    seen.len() == expected.len()
}

unsafe fn key_alias(tle: *mut pg_sys::TargetEntry) -> String {
    if tle.is_null() {
        return "key".to_string();
    }
    let ptr = (*tle).resname;
    if ptr.is_null() {
        return "key".to_string();
    }
    std::ffi::CStr::from_ptr(ptr).to_string_lossy().into_owned()
}

fn sql_text_array(values: &[String]) -> String {
    if values.is_empty() {
        return "ARRAY[]::text[]".to_string();
    }
    format!(
        "ARRAY[{}]::text[]",
        values
            .iter()
            .map(|value| format!("'{}'", value.replace('\'', "''")))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

unsafe fn classify_simple_var_agg(
    node: *mut pg_sys::Node,
    col_names: &[String],
    rel_rti: i32,
    group_rte: *mut pg_sys::RangeTblEntry,
    group_rti: i32,
    expected_kind: GroupAggKind,
) -> Option<String> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Aggref {
        return None;
    }
    let agg = node as *mut pg_sys::Aggref;
    if (*agg).aggstar
        || !(*agg).aggdistinct.is_null()
        || !(*agg).aggfilter.is_null()
        || !(*agg).aggorder.is_null()
    {
        return None;
    }
    let kind = match (*agg).aggfnoid.to_u32() {
        2107 | 2108 | 2109 | 2110 | 2111 | 2114 => GroupAggKind::Sum,
        2100 | 2101 | 2102 | 2103 | 2104 | 2105 => GroupAggKind::Avg,
        _ => return None,
    };
    if std::mem::discriminant(&kind) != std::mem::discriminant(&expected_kind) {
        return None;
    }
    let args = (*agg).args;
    if args.is_null() || (*args).length != 1 {
        return None;
    }
    let arg_tle = (*(*args).elements).ptr_value as *mut pg_sys::TargetEntry;
    if arg_tle.is_null() {
        return None;
    }
    let (col_name, _) = relation_var_info_resolved(
        (*arg_tle).expr as *mut pg_sys::Node,
        col_names,
        rel_rti,
        group_rte,
        group_rti,
    )?;
    Some(col_name)
}

/// Parse a SQL string into a fully-analyzed Query tree. Returns the
/// first Query in the result list. None on any error.
unsafe fn parse_to_query(sql: &str) -> Option<*mut pg_sys::Query> {
    let cstr = match std::ffi::CString::new(sql) {
        Ok(c) => c,
        Err(_) => return None,
    };
    let parsetree_list = pg_sys::raw_parser(cstr.as_ptr(), pg_sys::RawParseMode::RAW_PARSE_DEFAULT);
    if parsetree_list.is_null() || (*parsetree_list).length != 1 {
        return None;
    }
    let raw_stmt = (*(*parsetree_list).elements).ptr_value as *mut pg_sys::RawStmt;
    if raw_stmt.is_null() {
        return None;
    }
    let queries = pg_sys::pg_analyze_and_rewrite_fixedparams(
        raw_stmt,
        cstr.as_ptr(),
        std::ptr::null(),
        0,
        std::ptr::null_mut(),
    );
    if queries.is_null() || (*queries).length == 0 {
        return None;
    }
    let q = (*(*queries).elements).ptr_value as *mut pg_sys::Query;
    if q.is_null() {
        return None;
    }
    Some(q)
}

// ---------------------------------------------------------------------------
// Rule B: implicit prewarm for semantic operator calls
// ---------------------------------------------------------------------------

thread_local! {
    /// funcid → operator name (when this is an rvbbit op wrapper).
    /// Per-backend cache; loaded lazily from pg_proc + rvbbit.operators.
    static RVBBIT_OP_FN_CACHE: RefCell<HashMap<u32, Option<String>>> = RefCell::new(HashMap::new());
}

/// Default cap on rows we'll auto-prewarm. A million-row LLM operator
/// call without a user-asked cap would burn hours of provider time and
/// dollars — refuse to auto-trigger past this. Users with bigger
/// tables call rvbbit.prewarm_operator() explicitly.
const DEFAULT_IMPLICIT_PREWARM_MAX_ROWS: i64 = 250_000;

fn implicit_prewarm_max_rows() -> i64 {
    std::env::var("RVBBIT_IMPLICIT_PREWARM_MAX_ROWS")
        .ok()
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(DEFAULT_IMPLICIT_PREWARM_MAX_ROWS)
}

fn implicit_prewarm_max_concurrent() -> i32 {
    std::env::var("RVBBIT_IMPLICIT_PREWARM_MAX_CONCURRENT")
        .ok()
        .and_then(|s| s.parse::<i32>().ok())
        .unwrap_or(32)
}

/// Runtime kill-switch for the implicit prewarm rewrite. Default ON.
/// Disable per-session with `SET rvbbit.implicit_prewarm = off`, or globally
/// with the RVBBIT_IMPLICIT_PREWARM=off env var (for shared_preload contexts).
/// Only consulted once we know a query actually has semantic ops to warm, so
/// non-semantic queries never pay for it.
fn implicit_prewarm_enabled() -> bool {
    if matches!(
        std::env::var("RVBBIT_IMPLICIT_PREWARM")
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "0" | "off" | "false" | "no"
    ) {
        return false;
    }
    match pgrx::Spi::get_one::<String>(
        "SELECT nullif(current_setting('rvbbit.implicit_prewarm', true), '')",
    ) {
        Ok(Some(v)) => !matches!(v.to_ascii_lowercase().as_str(), "0" | "off" | "false" | "no"),
        _ => true,
    }
}

unsafe fn try_implicit_prewarm_rule(query: *mut pg_sys::Query) {
    if (*query).commandType != pg_sys::CmdType::CMD_SELECT {
        return;
    }
    // Bail only on shapes where a semantic op's inputs can't be safely warmed
    // over a single base relation: CTEs, sub-selects in expressions, set-ops,
    // window functions, row locking. Aggregates / GROUP BY / DISTINCT / HAVING
    // are fine — the operator is evaluated per base row *before* any of those
    // apply, so we warm it over the relation regardless of the outer shape
    // (e.g. `SELECT count(*) FROM t WHERE about(x)>0.5`,
    //  `SELECT region, avg(about(blurb,'t')) FROM t GROUP BY region`).
    if !(*query).cteList.is_null()
        || !(*query).setOperations.is_null()
        || !(*query).rowMarks.is_null()
        || (*query).hasWindowFuncs
        || (*query).hasSubLinks
    {
        return;
    }
    let jt = (*query).jointree;
    if jt.is_null() {
        return;
    }
    // Require exactly one base relation. A GROUP BY query also carries an
    // RTE_GROUP entry (PG16+) which we skip; a join (>1 RTE_RELATION) or any
    // subquery / function / VALUES scan bails.
    let rtable = (*query).rtable;
    if rtable.is_null() {
        return;
    }
    let mut found_oid: Option<u32> = None;
    for i in 0..(*rtable).length {
        let rte = (*(*rtable).elements.add(i as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
        if rte.is_null() {
            continue;
        }
        match (*rte).rtekind {
            pg_sys::RTEKind::RTE_RELATION => {
                if found_oid.replace((*rte).relid.to_u32()).is_some() {
                    return;
                }
            }
            pg_sys::RTEKind::RTE_GROUP => {}
            _ => return,
        }
    }
    let table_oid = match found_oid {
        Some(oid) => oid,
        None => return,
    };

    let calls = collect_rvbbit_op_calls(query, table_oid);
    // Phase 1: collect semantic ops in the WHERE quals too. Previously this was
    // a hard bail ("not prewarmed yet"); now we warm them over the relation so
    // the per-row filter Postgres applies resolves from cache instead of
    // calling the backend per row (the about()-in-WHERE timeout).
    let mut where_calls: Vec<(String, Vec<String>)> = Vec::new();
    if !(*jt).quals.is_null() {
        walk_for_op_calls(query, table_oid, (*jt).quals, &mut where_calls);
    }
    if calls.is_empty() && where_calls.is_empty() {
        return;
    }
    if !implicit_prewarm_enabled() {
        return;
    }
    if sort_clause_contains_rvbbit_op(query, table_oid) {
        pgrx::debug1!(
            "rvbbit: skipping implicit prewarm — ORDER BY depends on a semantic operator result"
        );
        return;
    }

    let table_name = match fetch_qualified_name(table_oid) {
        Some(n) => n,
        None => return,
    };

    // Safety cap — estimate the warmed row count and bail if too high.
    // Parameterized LIMIT/OFFSET values are not visible in the parse hook.
    // For the common no-WHERE shape, fall back to warming the whole relation
    // only when the relation estimate itself is under the cap; otherwise skip
    // and let explicit prewarm handle the user's chosen bound.
    let est_rows = estimate_relation_rows(table_oid);
    let cap = implicit_prewarm_max_rows();
    // Warm the whole relation (capped) when the warm set can't be bounded by a
    // constant LIMIT on the base rows: a semantic op in WHERE runs on every
    // scanned row, and under aggregation / GROUP BY / DISTINCT the LIMIT bounds
    // grouped/distinct *output*, not the base rows the operator evaluates over.
    let warm_whole_relation = !where_calls.is_empty()
        || (*query).hasAggs
        || !(*query).groupClause.is_null()
        || !(*query).distinctClause.is_null();
    let (effective_rows, from_tail) = if warm_whole_relation {
        // Strip the WHERE/grouping from the prewarm query — including the
        // original WHERE would re-trigger the operator and recurse through this
        // very parse hook. Rely on the row cap to bound the warm set.
        (est_rows, format!("FROM {table_name}"))
    } else {
        let limit_clause = implicit_prewarm_limit_clause(query);
        if let Some(limit_clause) = limit_clause {
            let effective_rows = limit_clause.effective_rows(est_rows);
            let from_tail = implicit_prewarm_from_tail(query)
                .unwrap_or_else(|| format!("FROM {table_name}{}", limit_clause.sql_suffix()));
            (effective_rows, from_tail)
        } else {
            if !(*jt).quals.is_null() {
                pgrx::debug1!(
                    "rvbbit: skipping implicit prewarm — non-constant LIMIT/OFFSET with WHERE \
                     cannot be safely replayed without bound parameters"
                );
                return;
            }
            pgrx::debug1!(
                "rvbbit: implicit prewarm using full relation estimate because LIMIT/OFFSET \
                 is not a constant expression"
            );
            (est_rows, format!("FROM {table_name}"))
        }
    };
    if effective_rows == 0 {
        return;
    }
    if effective_rows > cap {
        // Visible (not debug1) so a slow large query explains itself: this is the
        // common "why is this timing out" case — prewarm is skipped and the
        // operator runs per-row. Raise the cap or pre-filter to a smaller set.
        pgrx::notice!(
            "rvbbit: semantic prewarm skipped — estimated {effective_rows} rows exceeds cap {cap}; \
             running per-row (slow). Raise rvbbit.implicit_prewarm cap via \
             RVBBIT_IMPLICIT_PREWARM_MAX_ROWS, pre-filter to fewer rows, or call \
             rvbbit.prewarm_operator(...) explicitly."
        );
        return;
    }

    let max_conc = implicit_prewarm_max_concurrent();

    // Dedupe — multiple identical calls (e.g. rvbbit.foo(x), rvbbit.foo(x))
    // only need one prewarm.
    let mut seen: HashSet<(String, Vec<String>)> = HashSet::new();
    for (op_name, arg_frags) in calls.into_iter().chain(where_calls.into_iter()) {
        if !seen.insert((op_name.clone(), arg_frags.clone())) {
            continue;
        }
        // arg_frags are ready-to-use SQL expressions, one per operator input:
        // a quoted column ident for a Var arg, a typed literal for a Const arg
        // (e.g. rvbbit.about(observed, 'topic') -> ["observed", 'topic'::text]).
        // Each is aliased to the operator's arg_name so prewarm_operator builds
        // the same inputs.<arg> jsonb (and thus the same input_hash) the per-row
        // call would, guaranteeing the L2 cache hit.
        let arg_names = match fetch_op_arg_names(&op_name) {
            Some(n) if n.len() == arg_frags.len() => n,
            _ => continue,
        };
        let select_cols = arg_frags
            .iter()
            .zip(arg_names.iter())
            .map(|(frag, name)| format!("{frag} AS \"{name}\""))
            .collect::<Vec<_>>()
            .join(", ");
        let op_literal = sql_literal(&op_name);
        // Use a uniquely-tagged dollar-quote so we don't collide with
        // any literal $$ in operator names.
        let prewarm_sql = format!(
            "SELECT * FROM rvbbit.prewarm_operator(\
                 {op_literal}, \
                 $rvbbitprewarm$SELECT {select_cols} {from_tail}$rvbbitprewarm$, \
                 {max_conc})"
        );
        if let Err(err) = pgrx::Spi::run(&prewarm_sql) {
            pgrx::debug1!("rvbbit: implicit prewarm failed for {op_name}: {err}");
        }
    }
}

unsafe fn sort_clause_contains_rvbbit_op(query: *mut pg_sys::Query, table_oid: u32) -> bool {
    let sort_clause = (*query).sortClause;
    if sort_clause.is_null() {
        return false;
    }
    let n = (*sort_clause).length;
    let cell = (*sort_clause).elements;
    for i in 0..n {
        let sgc = (*cell.add(i as usize)).ptr_value as *mut pg_sys::SortGroupClause;
        if sgc.is_null() {
            continue;
        }
        let tle = target_entry_for_sort_group_ref((*query).targetList, (*sgc).tleSortGroupRef);
        if tle.is_null() {
            continue;
        }
        let mut calls = Vec::new();
        walk_for_op_calls(
            query,
            table_oid,
            (*tle).expr as *mut pg_sys::Node,
            &mut calls,
        );
        if !calls.is_empty() {
            return true;
        }
    }
    false
}

unsafe fn target_entry_for_sort_group_ref(
    tlist: *mut pg_sys::List,
    sort_group_ref: pg_sys::Index,
) -> *mut pg_sys::TargetEntry {
    if tlist.is_null() || sort_group_ref == 0 {
        return std::ptr::null_mut();
    }
    let n = (*tlist).length;
    let cell = (*tlist).elements;
    for i in 0..n {
        let tle = (*cell.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if !tle.is_null() && (*tle).ressortgroupref == sort_group_ref {
            return tle;
        }
    }
    std::ptr::null_mut()
}

unsafe fn implicit_prewarm_from_tail(query: *mut pg_sys::Query) -> Option<String> {
    let sql = statement_source_sql(query)?;
    let from_pos = find_top_level_keyword(&sql, "from", 0)?;
    let tail = sql[from_pos..].trim();
    if tail.is_empty() {
        None
    } else {
        Some(tail.trim_end_matches(';').trim().to_string())
    }
}

unsafe fn statement_source_sql(query: *mut pg_sys::Query) -> Option<String> {
    if query.is_null() {
        return None;
    }
    CURRENT_SOURCE_SQL.with(|cell| {
        let source = cell.borrow();
        let source = source.as_ref()?;
        let len = source.len();
        let start = if (*query).stmt_location >= 0 {
            ((*query).stmt_location as usize).min(len)
        } else {
            0
        };
        let end = if (*query).stmt_len > 0 {
            start.saturating_add((*query).stmt_len as usize).min(len)
        } else {
            len
        };
        source.get(start..end).map(|s| s.trim().to_string())
    })
}

fn sql_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[derive(Debug, Clone, Copy)]
struct ImplicitPrewarmLimit {
    limit: Option<i64>,
    offset: Option<i64>,
}

impl ImplicitPrewarmLimit {
    fn effective_rows(self, estimated_relation_rows: i64) -> i64 {
        match self.limit {
            Some(limit) => limit.max(0),
            None => {
                let offset = self.offset.unwrap_or(0).max(0);
                estimated_relation_rows.saturating_sub(offset).max(0)
            }
        }
    }

    fn sql_suffix(self) -> String {
        let mut out = String::new();
        if let Some(limit) = self.limit {
            out.push_str(" LIMIT ");
            out.push_str(&limit.max(0).to_string());
        }
        if let Some(offset) = self.offset {
            out.push_str(" OFFSET ");
            out.push_str(&offset.max(0).to_string());
        }
        out
    }
}

unsafe fn implicit_prewarm_limit_clause(query: *mut pg_sys::Query) -> Option<ImplicitPrewarmLimit> {
    let limit = if (*query).limitCount.is_null() {
        None
    } else {
        Some(const_node_i64((*query).limitCount)?)
    };
    let offset = if (*query).limitOffset.is_null() {
        None
    } else {
        Some(const_node_i64((*query).limitOffset)?)
    };
    Some(ImplicitPrewarmLimit { limit, offset })
}

/// Walk the targetList recursively for FuncExpr calls to rvbbit
/// operators. `rvbbit.sentiment(body)->>'label'` is an OpExpr wrapping
/// a FuncExpr, so we descend through OpExpr / FuncExpr / CoerceViaIO
/// / RelabelType / List args. Returns `(op_name, [col_name_per_arg])`
/// per recognized call.
unsafe fn collect_rvbbit_op_calls(
    query: *mut pg_sys::Query,
    table_oid: u32,
) -> Vec<(String, Vec<String>)> {
    let mut out = Vec::new();
    let tlist = (*query).targetList;
    if tlist.is_null() {
        return out;
    }
    let n = (*tlist).length;
    let cell = (*tlist).elements;
    for i in 0..n {
        let tle = (*cell.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
        if tle.is_null() {
            continue;
        }
        walk_for_op_calls(query, table_oid, (*tle).expr as *mut pg_sys::Node, &mut out);
    }
    out
}

unsafe fn walk_for_op_calls(
    query: *mut pg_sys::Query,
    table_oid: u32,
    node: *mut pg_sys::Node,
    out: &mut Vec<(String, Vec<String>)>,
) {
    if node.is_null() {
        return;
    }
    // First, try to classify this exact node as an op call.
    if let Some(call) = classify_op_call(query, table_oid, node) {
        out.push(call);
        // Don't descend further into this FuncExpr's args — the args
        // are the op's inputs, not other op calls.
        return;
    }
    // Otherwise, descend into child expressions.
    match (*node).type_ {
        pg_sys::NodeTag::T_OpExpr => {
            let op = node as *mut pg_sys::OpExpr;
            walk_list_for_op_calls(query, table_oid, (*op).args, out);
        }
        pg_sys::NodeTag::T_FuncExpr => {
            let fe = node as *mut pg_sys::FuncExpr;
            walk_list_for_op_calls(query, table_oid, (*fe).args, out);
        }
        pg_sys::NodeTag::T_CoerceViaIO => {
            let cv = node as *mut pg_sys::CoerceViaIO;
            walk_for_op_calls(query, table_oid, (*cv).arg as *mut pg_sys::Node, out);
        }
        pg_sys::NodeTag::T_RelabelType => {
            let rt = node as *mut pg_sys::RelabelType;
            walk_for_op_calls(query, table_oid, (*rt).arg as *mut pg_sys::Node, out);
        }
        pg_sys::NodeTag::T_BoolExpr => {
            let be = node as *mut pg_sys::BoolExpr;
            walk_list_for_op_calls(query, table_oid, (*be).args, out);
        }
        pg_sys::NodeTag::T_CaseExpr => {
            let ce = node as *mut pg_sys::CaseExpr;
            walk_for_op_calls(query, table_oid, (*ce).arg as *mut pg_sys::Node, out);
            walk_list_for_op_calls(query, table_oid, (*ce).args, out);
            walk_for_op_calls(query, table_oid, (*ce).defresult as *mut pg_sys::Node, out);
        }
        pg_sys::NodeTag::T_Aggref => {
            // Aggref.args is a List of TargetEntry (e.g. avg(rvbbit.about(x,'t'))).
            // Descend into each entry's expr so a semantic op nested in an
            // aggregate is found — the op runs per base row before the aggregate
            // applies, so it warms over the relation just like a bare SELECT op.
            let agg = node as *mut pg_sys::Aggref;
            let args = (*agg).args;
            if !args.is_null() {
                let n = (*args).length;
                let cell = (*args).elements;
                for i in 0..n {
                    let tle = (*cell.add(i as usize)).ptr_value as *mut pg_sys::TargetEntry;
                    if !tle.is_null() {
                        walk_for_op_calls(query, table_oid, (*tle).expr as *mut pg_sys::Node, out);
                    }
                }
            }
        }
        _ => { /* unknown node — stop descending */ }
    }
}

unsafe fn walk_list_for_op_calls(
    query: *mut pg_sys::Query,
    table_oid: u32,
    list: *mut pg_sys::List,
    out: &mut Vec<(String, Vec<String>)>,
) {
    if list.is_null() {
        return;
    }
    let n = (*list).length;
    let cell = (*list).elements;
    for i in 0..n {
        let node = (*cell.add(i as usize)).ptr_value as *mut pg_sys::Node;
        walk_for_op_calls(query, table_oid, node, out);
    }
}

unsafe fn classify_op_call(
    query: *mut pg_sys::Query,
    table_oid: u32,
    node: *mut pg_sys::Node,
) -> Option<(String, Vec<String>)> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_FuncExpr {
        return None;
    }
    let fe = node as *mut pg_sys::FuncExpr;
    let op_name = rvbbit_op_for_funcid((*fe).funcid.to_u32())?;
    // Operator wrapper functions take user-args + a trailing jsonb opts
    // (DEFAULT). Find out how many leading args are real inputs.
    let arg_count = fetch_op_arg_count(&op_name)?;
    let args = (*fe).args;
    if args.is_null() {
        return None;
    }
    let total_args = (*args).length as usize;
    if total_args < arg_count {
        return None;
    }

    let mut arg_frags: Vec<String> = Vec::with_capacity(arg_count);
    for i in 0..arg_count {
        let arg = pg_sys::list_nth(args, i as i32) as *mut pg_sys::Node;
        if arg.is_null() {
            return None;
        }
        arg_frags.push(render_op_arg(query, table_oid, arg)?);
    }
    Some((op_name, arg_frags))
}

/// Render one operator-input argument to a SQL fragment usable in the prewarm
/// SELECT: a quoted column ident for a Var on the driving relation, or a typed
/// literal for a Const. Returns None for anything else (the op is then skipped,
/// matching the prior column-only behavior). Const support is what lets
/// operators with constant args (rvbbit.about(col, 'topic')) prewarm at all.
unsafe fn render_op_arg(
    query: *mut pg_sys::Query,
    table_oid: u32,
    node: *mut pg_sys::Node,
) -> Option<String> {
    let inner = peel_casts(node);
    if inner.is_null() {
        return None;
    }
    match (*inner).type_ {
        pg_sys::NodeTag::T_Var => {
            let var = inner as *mut pg_sys::Var;
            let varno = (*var).varno as i32;
            let rtable = (*query).rtable;
            if rtable.is_null() || varno < 1 || varno > (*rtable).length {
                return None;
            }
            let rte = pg_sys::list_nth(rtable, varno - 1) as *mut pg_sys::RangeTblEntry;
            if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
                return None;
            }
            if (*rte).relid.to_u32() != table_oid {
                return None;
            }
            let attno = (*var).varattno as i32;
            if attno < 1 {
                return None;
            }
            Some(format!("\"{}\"", fetch_attname(table_oid, attno)?))
        }
        pg_sys::NodeTag::T_Const => {
            let c = inner as *mut pg_sys::Const;
            if (*c).constisnull {
                return Some("NULL".to_string());
            }
            let typ = (*c).consttype;
            let text = const_output_text((*c).constvalue, typ);
            let typename = const_type_name(typ)?;
            // Typed literal so the prewarm projection serializes to the same
            // jsonb value (and input_hash) the per-row call produces.
            Some(format!("{}::{}", sql_literal(&text), typename))
        }
        _ => None,
    }
}

/// Peel implicit-cast wrappers (RelabelType / CoerceViaIO) to reach the inner
/// Var or Const that parse-analyze wrapped.
unsafe fn peel_casts(mut node: *mut pg_sys::Node) -> *mut pg_sys::Node {
    loop {
        if node.is_null() {
            return node;
        }
        match (*node).type_ {
            pg_sys::NodeTag::T_RelabelType => {
                node = (*(node as *mut pg_sys::RelabelType)).arg as *mut pg_sys::Node;
            }
            pg_sys::NodeTag::T_CoerceViaIO => {
                node = (*(node as *mut pg_sys::CoerceViaIO)).arg as *mut pg_sys::Node;
            }
            _ => return node,
        }
    }
}

unsafe fn const_output_text(datum: pg_sys::Datum, typoid: pg_sys::Oid) -> String {
    let mut typoutput = pg_sys::InvalidOid;
    let mut typisvarlena = false;
    pg_sys::getTypeOutputInfo(typoid, &mut typoutput, &mut typisvarlena);
    let cstr = pg_sys::OidOutputFunctionCall(typoutput, datum);
    if cstr.is_null() {
        return String::new();
    }
    let out = std::ffi::CStr::from_ptr(cstr).to_string_lossy().into_owned();
    pg_sys::pfree(cstr as *mut std::ffi::c_void);
    out
}

unsafe fn const_type_name(typoid: pg_sys::Oid) -> Option<String> {
    let cstr = pg_sys::format_type_be(typoid);
    if cstr.is_null() {
        return None;
    }
    let name = std::ffi::CStr::from_ptr(cstr).to_string_lossy().into_owned();
    pg_sys::pfree(cstr as *mut std::ffi::c_void);
    Some(name)
}

unsafe fn unwrap_relabel_to_var(node: *mut pg_sys::Node) -> Option<*mut pg_sys::Var> {
    if node.is_null() {
        return None;
    }
    let n = match (*node).type_ {
        pg_sys::NodeTag::T_Var => node,
        pg_sys::NodeTag::T_RelabelType => {
            let rt = node as *mut pg_sys::RelabelType;
            (*rt).arg as *mut pg_sys::Node
        }
        _ => return None,
    };
    if (*n).type_ != pg_sys::NodeTag::T_Var {
        return None;
    }
    Some(n as *mut pg_sys::Var)
}

fn rvbbit_op_for_funcid(funcid: u32) -> Option<String> {
    RVBBIT_OP_FN_CACHE.with(|c| {
        if let Some(cached) = c.borrow().get(&funcid) {
            return cached.clone();
        }
        let result = lookup_rvbbit_op_name(funcid);
        c.borrow_mut().insert(funcid, result.clone());
        result
    })
}

fn lookup_rvbbit_op_name(funcid: u32) -> Option<String> {
    // Before CREATE EXTENSION (e.g. during initdb post-bootstrap, when
    // shared_preload_libraries loads us while the system catalogs are
    // still being built) rvbbit.operators does not exist yet. A bare
    // reference would ereport, and that longjmps straight past the .ok()
    // below — fatal during initdb. Gate on to_regclass first: it returns
    // NULL for a missing relation instead of erroring.
    let catalog_present =
        pgrx::Spi::get_one::<bool>("SELECT to_regclass('rvbbit.operators') IS NOT NULL")
            .ok()
            .flatten()
            .unwrap_or(false);
    if !catalog_present {
        return None;
    }
    // Must be in the rvbbit schema AND have a matching operators row.
    let sql = format!(
        "SELECT p.proname::text \
         FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid \
         WHERE p.oid = {funcid}::oid AND n.nspname = 'rvbbit' \
           AND EXISTS (SELECT 1 FROM rvbbit.operators o WHERE o.name = p.proname::text)"
    );
    pgrx::Spi::get_one::<String>(&sql).ok().flatten()
}

fn fetch_op_arg_count(op_name: &str) -> Option<usize> {
    let arg_names = fetch_op_arg_names(op_name)?;
    Some(arg_names.len())
}

fn fetch_op_arg_names(op_name: &str) -> Option<Vec<String>> {
    let esc = op_name.replace('\'', "''");
    let sql = format!("SELECT arg_names FROM rvbbit.operators WHERE name = '{esc}'");
    let v: Option<Vec<Option<String>>> = pgrx::Spi::get_one(&sql).ok().flatten();
    Some(v?.into_iter().flatten().collect())
}

fn fetch_attname(table_oid: u32, attno: i32) -> Option<String> {
    let sql = format!(
        "SELECT attname::text FROM pg_attribute \
         WHERE attrelid = {table_oid}::oid AND attnum = {attno} AND NOT attisdropped"
    );
    pgrx::Spi::get_one::<String>(&sql).ok().flatten()
}

fn fetch_qualified_name(table_oid: u32) -> Option<String> {
    let sql = format!("SELECT {table_oid}::oid::regclass::text");
    pgrx::Spi::get_one::<String>(&sql).ok().flatten()
}

fn resolve_relation_oid(rel_name: &str) -> Option<u32> {
    let esc = rel_name.replace('\'', "''");
    let sql = format!("SELECT to_regclass('{esc}')::oid::bigint");
    let oid = pgrx::Spi::get_one::<i64>(&sql).ok().flatten()?;
    if oid < 0 || oid > u32::MAX as i64 {
        return None;
    }
    Some(oid as u32)
}

fn estimate_relation_rows(table_oid: u32) -> i64 {
    // For rvbbit tables, prefer the exact row count from row_groups.
    let from_rg = format!(
        "SELECT coalesce(sum(n_rows), 0)::bigint \
         FROM rvbbit.row_groups_visible WHERE table_oid = {table_oid}::oid"
    );
    if let Ok(Some(n)) = pgrx::Spi::get_one::<i64>(&from_rg) {
        if n > 0 {
            return n;
        }
    }
    // Fall back to the planner estimate (cheap, possibly stale).
    let sql =
        format!("SELECT coalesce(reltuples, 0)::bigint FROM pg_class WHERE oid = {table_oid}::oid");
    pgrx::Spi::get_one::<i64>(&sql).ok().flatten().unwrap_or(0)
}

/// Exact count for the trivial `SELECT count(*) FROM rel` rewrite.
///
/// Fully compacted benchmark tables take the metadata-only path. If a retained
/// heap has been dirtied after an acceleration refresh, the heap is the source
/// of truth and already contains every visible row, so use a direct heap count
/// instead of adding parquet rows and double-counting. More complex rewrites
/// still require a clean authoritative parquet view via `fetch_total_row_count`.
fn fetch_count_star_row_count(table_oid: u32) -> Option<i64> {
    let row_group_rows = fetch_row_group_row_count(table_oid)?;
    if heap_relation_size(table_oid).unwrap_or(0) == 0 {
        return Some(row_group_rows);
    }
    if clean_shadow_heap_retained(table_oid) {
        return Some(row_group_rows);
    }
    if shadow_heap_retained(table_oid) {
        return unsafe { heap_visible_row_count(table_oid) };
    }
    None
}

/// Look up the total row count from rvbbit.row_groups for a fully compacted
/// rvbbit-managed table. Returns None for non-rvbbit tables, tables with no
/// row groups, or tables with live heap/delta rows that parquet-only rewrites
/// would otherwise miss.
fn fetch_total_row_count(table_oid: u32) -> Option<i64> {
    // Only run if it's an rvbbit table — avoid SPI for every count(*).
    // is_rvbbit check is itself a SPI roundtrip; we cache the answer.
    if !is_rvbbit_table_cached(table_oid) {
        return None;
    }
    let n = fetch_row_group_row_count(table_oid)?;
    if heap_relation_size(table_oid).unwrap_or(0) == 0 {
        return Some(n);
    }
    if clean_shadow_heap_retained(table_oid) {
        return Some(n);
    }
    if unsafe { heap_visible_row_count(table_oid) }.unwrap_or(1) == 0 {
        Some(n)
    } else {
        None
    }
}

fn clean_shadow_heap_retained(table_oid: u32) -> bool {
    let sql = format!(
        "SELECT coalesce(shadow_heap_retained AND NOT shadow_heap_dirty, false) \
         FROM rvbbit.tables WHERE table_oid = {table_oid}::oid"
    );
    pgrx::Spi::get_one::<bool>(&sql)
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn shadow_heap_retained(table_oid: u32) -> bool {
    let sql = format!(
        "SELECT coalesce(shadow_heap_retained, false) \
         FROM rvbbit.tables WHERE table_oid = {table_oid}::oid"
    );
    pgrx::Spi::get_one::<bool>(&sql)
        .ok()
        .flatten()
        .unwrap_or(false)
}

fn fetch_row_group_row_count(table_oid: u32) -> Option<i64> {
    if !is_rvbbit_table_cached(table_oid) {
        return None;
    }
    let sql = format!(
        "SELECT coalesce(sum(n_rows), 0)::bigint \
         FROM rvbbit.row_groups_visible WHERE table_oid = {table_oid}::oid"
    );
    let result: Result<Option<i64>, _> = pgrx::Spi::get_one(&sql);
    let n = result.ok().flatten().unwrap_or(0);
    if n > 0 {
        Some(n)
    } else {
        None
    }
}

fn heap_relation_size(table_oid: u32) -> Option<i64> {
    let sql = format!("SELECT pg_relation_size({table_oid}::oid)::bigint");
    pgrx::Spi::get_one::<i64>(&sql).ok().flatten()
}

fn force_heap_scan_enabled() -> bool {
    guc_setting("rvbbit.force_heap_scan")
        .as_deref()
        .map(|value| setting_enabled(value, false))
        .unwrap_or(false)
}

/// Phase 2 followup A: are any of the metadata-only fast path rewrites
/// unsafe to apply right now? Returns true when:
///   - rvbbit.as_of_generation is set to a positive value (historical
///     reads need row-group narrowing the metadata path can't do); OR
///   - any rvbbit table has at least one tombstone (metadata counts
///     don't subtract tombstones).
///
/// Conservative: a query that references no tombstoned table still
/// falls through when ANY rvbbit table has tombstones. That's the
/// trade-off for keeping the check to a single cheap EXISTS query
/// instead of walking the rtable per call. A more precise per-rtable
/// check is a future refinement.
fn metadata_rewrites_unsafe_for_correctness() -> bool {
    if current_source_has_asof_timestamp_directive() {
        return true;
    }
    // GUC check: free (direct GetConfigOption).
    if let Some(val) = guc_setting("rvbbit.as_of_generation") {
        if let Ok(g) = val.trim().parse::<i64>() {
            if g > 0 {
                return true;
            }
        }
    }
    if guc_setting("rvbbit.as_of_timestamp").is_some_and(|v| !v.trim().is_empty()) {
        return true;
    }
    // Existence guard: PG parses BOTH branches of a CASE at plan time, so
    // a single CASE-guarded EXISTS still fails when rvbbit.delete_log
    // doesn't exist yet (CREATE EXTENSION's first CREATE TABLE runs the
    // planner hook before rvbbit.delete_log is created). Two separate
    // SPI calls sidestep that — the first never references the table.
    let table_exists: Option<bool> =
        pgrx::Spi::get_one("SELECT to_regclass('rvbbit.delete_log') IS NOT NULL")
            .ok()
            .flatten();
    if !table_exists.unwrap_or(false) {
        return false;
    }
    let has_tombstones: Option<bool> =
        pgrx::Spi::get_one("SELECT EXISTS(SELECT 1 FROM rvbbit.delete_log)")
            .ok()
            .flatten();
    if has_tombstones.unwrap_or(false) {
        return true;
    }
    // Phase 2 ObjectStore: metadata fast paths read rvbbit.row_groups
    // stats and per_group_stats columns, then return without scanning
    // parquet. Those stats are accurate for the LOCAL parquet — but if
    // the file lives on a cold tier (cold_url IS NOT NULL), the row
    // group is unreachable via the native scan path anyway, and the
    // metadata fast path would return stats from a file the operator
    // intended to be served via the in-process DataFusion route.
    // Conservative: any cold row group anywhere disables metadata
    // fast paths. Same one-EXISTS overhead pattern as the tombstone
    // check above.
    let has_cold: Option<bool> = pgrx::Spi::get_one(
        "SELECT EXISTS(SELECT 1 FROM rvbbit.row_groups WHERE cold_url IS NOT NULL)",
    )
    .ok()
    .flatten();
    has_cold.unwrap_or(false)
}

fn current_source_has_asof_timestamp_directive() -> bool {
    CURRENT_SOURCE_SQL.with(|cell| {
        cell.borrow()
            .as_deref()
            .map(crate::time_travel::has_as_of_timestamp_directive)
            .unwrap_or(false)
    })
}

fn setting_enabled(value: &str, default: bool) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return default;
    }
    !matches!(
        trimmed.to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off" | "disabled"
    )
}

fn guc_setting(name: &str) -> Option<String> {
    let cname = CString::new(name).ok()?;
    let ptr = unsafe { pg_sys::GetConfigOption(cname.as_ptr(), true, false) };
    if ptr.is_null() {
        None
    } else {
        Some(unsafe { CStr::from_ptr(ptr).to_string_lossy().into_owned() })
    }
}

unsafe fn heap_visible_row_count(table_oid: u32) -> Option<i64> {
    let rel = pg_sys::table_open(
        pg_sys::Oid::from(table_oid),
        pg_sys::AccessShareLock as pg_sys::LOCKMODE,
    );
    if rel.is_null() {
        return None;
    }

    let snapshot = {
        let active = pg_sys::GetActiveSnapshot();
        if active.is_null() {
            pg_sys::GetTransactionSnapshot()
        } else {
            active
        }
    };
    let scan = pg_sys::table_beginscan(rel, snapshot, 0, std::ptr::null_mut());
    if scan.is_null() {
        pg_sys::table_close(rel, pg_sys::AccessShareLock as pg_sys::LOCKMODE);
        return None;
    }
    let slot = pg_sys::MakeSingleTupleTableSlot((*rel).rd_att, pg_sys::table_slot_callbacks(rel));
    if slot.is_null() {
        pg_sys::table_endscan(scan);
        pg_sys::table_close(rel, pg_sys::AccessShareLock as pg_sys::LOCKMODE);
        return None;
    }

    let mut count = 0i64;
    while pg_sys::table_scan_getnextslot(scan, pg_sys::ScanDirection::ForwardScanDirection, slot) {
        count += 1;
        pg_sys::ExecClearTuple(slot);
    }
    pg_sys::ExecDropSingleTupleTableSlot(slot);
    pg_sys::table_endscan(scan);
    pg_sys::table_close(rel, pg_sys::AccessShareLock as pg_sys::LOCKMODE);
    Some(count)
}

thread_local! {
    static IS_RVBBIT_CACHE: RefCell<HashMap<u32, bool>> = RefCell::new(HashMap::new());
}

fn is_rvbbit_table_cached(oid: u32) -> bool {
    if let Some(&v) = IS_RVBBIT_CACHE
        .with(|c| c.borrow().get(&oid).copied())
        .as_ref()
    {
        return v;
    }
    let sql = format!(
        "SELECT (a.amname = 'rvbbit') \
         FROM pg_class c JOIN pg_am a ON c.relam = a.oid \
         WHERE c.oid = {oid}::oid"
    );
    let result: Result<Option<bool>, _> = pgrx::Spi::get_one(&sql);
    let v = result.ok().flatten().unwrap_or(false);
    IS_RVBBIT_CACHE.with(|c| c.borrow_mut().insert(oid, v));
    v
}

unsafe fn mutate_list_in_place(list: *mut pg_sys::List) {
    if list.is_null() {
        return;
    }
    let n = (*list).length;
    let cell = (*list).elements;
    for i in 0..n {
        let p = cell.add(i as usize);
        let node = (*p).ptr_value as *mut pg_sys::Node;
        let new_node = mutate_expr(node);
        if new_node != node {
            (*p).ptr_value = new_node as *mut c_void;
        }
    }
}

unsafe fn mutate_expr(node: *mut pg_sys::Node) -> *mut pg_sys::Node {
    if node.is_null() {
        return node;
    }
    rewrite_sublink_query(node);
    let descended =
        pg_sys::expression_tree_mutator_impl(node, Some(mutator_cb), std::ptr::null_mut());
    try_shred_rule(descended).unwrap_or(descended)
}

unsafe fn rewrite_sublink_query(node: *mut pg_sys::Node) {
    if (*node).type_ != pg_sys::NodeTag::T_SubLink {
        return;
    }
    let sublink = node as *mut pg_sys::SubLink;
    let subselect = (*sublink).subselect;
    if subselect.is_null() || (*subselect).type_ != pg_sys::NodeTag::T_Query {
        return;
    }
    let query = subselect as *mut pg_sys::Query;
    if !query.is_null() && (*query).commandType == pg_sys::CmdType::CMD_SELECT {
        rewrite_query(query);
    }
}

#[pg_guard]
unsafe extern "C-unwind" fn mutator_cb(
    node: *mut pg_sys::Node,
    _ctx: *mut c_void,
) -> *mut pg_sys::Node {
    mutate_expr(node)
}

/// Top-level shred matcher. Two shapes:
///
/// 1. Bare path: `Var [-> Const ...] ->> Const` returning text.
///    Matched against a TEXT shred.
///
/// 2. Cast: `(Var [-> Const ...] ->> Const)::T` returning T.
///    Matched against a shred of type T. The cast wrapper is replaced
///    along with the inner path.
unsafe fn try_shred_rule(node: *mut pg_sys::Node) -> Option<*mut pg_sys::Node> {
    // Case 1: bare jsonb path returning text.
    if let Some((varno, src_attnum, path)) = extract_jsonb_path(node) {
        let table_oid = varno_to_table_oid(varno)?;
        if let Some(m) = find_shred(table_oid, src_attnum, &path, TEXT_OID) {
            return Some(make_substitute_var(node, &m));
        }
    }
    // Case 2: wrapped in a cast to a typed column.
    if let Some((inner, output_typoid)) = unwrap_cast(node) {
        if let Some((varno, src_attnum, path)) = extract_jsonb_path(inner) {
            let table_oid = varno_to_table_oid(varno)?;
            if let Some(m) = find_shred(table_oid, src_attnum, &path, output_typoid) {
                return Some(make_substitute_var(node, &m));
            }
        }
    }
    None
}

/// Walk inward through chained `->` ops to extract (varno, src_var_attnum, [keys]).
/// The chain ends at a leaf `->>` (which returns text). Returns None if
/// the pattern doesn't match (e.g. arg isn't a Var, key isn't a Const text).
unsafe fn extract_jsonb_path(node: *mut pg_sys::Node) -> Option<(i32, i16, Vec<String>)> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    if (*op).opno.to_u32() != JSONB_OBJECT_FIELD_TEXT_OP {
        return None;
    }
    let (a1, a2) = op_two_args(op)?;
    let leaf = const_to_str(a2)?;
    let mut path = vec![leaf];

    let mut current = a1;
    loop {
        if (*current).type_ == pg_sys::NodeTag::T_Var {
            let var = current as *mut pg_sys::Var;
            path.reverse();
            return Some(((*var).varno, (*var).varattno, path));
        }
        if (*current).type_ != pg_sys::NodeTag::T_OpExpr {
            return None;
        }
        let op = current as *mut pg_sys::OpExpr;
        if (*op).opno.to_u32() != JSONB_OBJECT_FIELD_OP {
            return None;
        }
        let (inner_a1, inner_a2) = op_two_args(op)?;
        let key = const_to_str(inner_a2)?;
        path.push(key);
        current = inner_a1;
    }
}

/// If `node` is a cast expression, return (inner_arg, output_typoid).
/// Handles CoerceViaIO (`text::int`) and FuncExpr with explicit cast format.
unsafe fn unwrap_cast(node: *mut pg_sys::Node) -> Option<(*mut pg_sys::Node, u32)> {
    if node.is_null() {
        return None;
    }
    match (*node).type_ {
        pg_sys::NodeTag::T_CoerceViaIO => {
            let cvi = node as *mut pg_sys::CoerceViaIO;
            Some(((*cvi).arg as *mut pg_sys::Node, (*cvi).resulttype.to_u32()))
        }
        pg_sys::NodeTag::T_FuncExpr => {
            let fe = node as *mut pg_sys::FuncExpr;
            // Only single-arg function calls of cast shape.
            if pg_sys::list_length((*fe).args) != 1 {
                return None;
            }
            // Skip non-cast functions (we don't want to replace arbitrary
            // FuncExprs even if their arg is a jsonb path).
            if (*fe).funcformat != pg_sys::CoercionForm::COERCE_EXPLICIT_CAST
                && (*fe).funcformat != pg_sys::CoercionForm::COERCE_IMPLICIT_CAST
            {
                return None;
            }
            let arg = pg_sys::list_nth((*fe).args, 0) as *mut pg_sys::Node;
            Some((arg, (*fe).funcresulttype.to_u32()))
        }
        pg_sys::NodeTag::T_RelabelType => {
            let rt = node as *mut pg_sys::RelabelType;
            Some(((*rt).arg as *mut pg_sys::Node, (*rt).resulttype.to_u32()))
        }
        _ => None,
    }
}

/// Find a shred for the given (table, src_attnum, path, dst_type) tuple.
/// Loads from rvbbit.shreds on first access per backend.
unsafe fn find_shred(
    table_oid: u32,
    src_attnum: i16,
    path: &[String],
    dst_typoid: u32,
) -> Option<ShredEntry> {
    ensure_loaded(table_oid)?;
    SHRED_CACHE.with(|c| {
        c.borrow().get(&table_oid).and_then(|entries| {
            entries
                .iter()
                .find(|e| {
                    e.src_attnum == src_attnum
                        && e.dst_typoid == dst_typoid
                        && e.path.len() == path.len()
                        && e.path.iter().zip(path.iter()).all(|(a, b)| a == b)
                })
                .cloned()
        })
    })
}

/// Populate the shred cache for one table if it isn't there yet.
/// Safe to call from inside the rewriter: the SPI used to load fires
/// our hook recursively, but the hook's IN_REWRITER guard makes those
/// nested calls no-op.
unsafe fn ensure_loaded(table_oid: u32) -> Option<()> {
    let present = SHRED_CACHE.with(|c| c.borrow().contains_key(&table_oid));
    if present {
        return Some(());
    }
    let entries = load_for_table(table_oid).unwrap_or_default();
    SHRED_CACHE.with(|c| c.borrow_mut().insert(table_oid, entries));
    Some(())
}

/// SPI-load the shreds for `table_oid`, pre-resolving src_attnum and
/// dst_attnum from pg_attribute so the rewriter doesn't have to do that
/// per query.
fn load_for_table(table_oid: u32) -> Option<Vec<ShredEntry>> {
    let mut out = Vec::new();
    let result: Result<(), pgrx::spi::Error> = pgrx::Spi::connect(|client| {
        let sql = format!(
            "SELECT \
                attsrc.attnum::int  AS src_attnum, \
                s.path              AS path, \
                attdst.attnum::int  AS dst_attnum, \
                s.data_type         AS data_type \
             FROM rvbbit.shreds s \
             JOIN pg_attribute attsrc ON attsrc.attrelid = s.table_oid \
                                     AND attsrc.attname = s.src_column \
                                     AND attsrc.attnum > 0 \
             JOIN pg_attribute attdst ON attdst.attrelid = s.table_oid \
                                     AND attdst.attname = s.column_name \
                                     AND attdst.attnum > 0 \
             WHERE s.table_oid = {table_oid}::oid"
        );
        let table = client.select(&sql, None, &[])?;
        for row in table {
            let src_attnum: Option<i32> = row.get(1)?;
            let path: Option<Vec<Option<String>>> = row.get(2)?;
            let dst_attnum: Option<i32> = row.get(3)?;
            let data_type: Option<String> = row.get(4)?;
            if let (Some(s), Some(p), Some(d), Some(t)) = (src_attnum, path, dst_attnum, data_type)
            {
                let path_strs: Vec<String> = p.into_iter().flatten().collect();
                let dst_typoid = typname_to_oid(&t);
                out.push(ShredEntry {
                    src_attnum: s as i16,
                    path: path_strs,
                    dst_attnum: d as i16,
                    dst_typoid,
                });
            }
        }
        Ok(())
    });
    if result.is_err() {
        return None;
    }
    Some(out)
}

fn typname_to_oid(s: &str) -> u32 {
    match s {
        "text" => 25,
        "int4" | "integer" | "int" => 23,
        "int8" | "bigint" => 20,
        "int2" | "smallint" => 21,
        "float4" | "real" => 700,
        "float8" | "double precision" => 701,
        "numeric" => 1700,
        "jsonb" => 3802,
        "bool" | "boolean" => 16,
        other => {
            pgrx::log!(
                "rvbbit rewriter: unknown data_type '{}' in rvbbit.shreds",
                other
            );
            0
        }
    }
}

/// Translate a Var's varno + the current Query's rtable into the table_oid
/// it ultimately references.
unsafe fn varno_to_table_oid(varno: i32) -> Option<u32> {
    let query = CURRENT_QUERY.with(|c| c.get());
    if query.is_null() {
        return None;
    }
    let rtable = (*query).rtable;
    if rtable.is_null() {
        return None;
    }
    let n = (*rtable).length;
    let idx = varno - 1;
    if idx < 0 || idx >= n {
        return None;
    }
    let rte = (*(*rtable).elements.add(idx as usize)).ptr_value as *mut pg_sys::RangeTblEntry;
    if rte.is_null() || (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return None;
    }
    let oid = (*rte).relid.to_u32();
    if oid == 0 {
        None
    } else {
        Some(oid)
    }
}

unsafe fn make_substitute_var(
    src_node: *mut pg_sys::Node,
    mapping: &ShredEntry,
) -> *mut pg_sys::Node {
    // We need a Var pointing at the shred column. Preserve the source
    // varno (relation reference) — for the cast-wrapped case the
    // "varno" we want is the varno of the inner Var, not anything from
    // the outer cast node. So dig in to find it.
    let varno = innermost_varno(src_node).unwrap_or(1);
    let collid = if mapping.dst_typoid == TEXT_OID {
        // Default collation OID — required for GROUP BY / hash on text
        // to know which collation to use.
        pg_sys::Oid::from(100u32)
    } else {
        pg_sys::InvalidOid
    };
    let new_var = pg_sys::makeVar(
        varno,
        mapping.dst_attnum,
        pg_sys::Oid::from(mapping.dst_typoid),
        -1,
        collid,
        0,
    );
    (*new_var).location = node_location(src_node);
    new_var as *mut pg_sys::Node
}

unsafe fn innermost_varno(node: *mut pg_sys::Node) -> Option<i32> {
    if node.is_null() {
        return None;
    }
    match (*node).type_ {
        pg_sys::NodeTag::T_Var => Some((*(node as *mut pg_sys::Var)).varno as i32),
        pg_sys::NodeTag::T_OpExpr => {
            let op = node as *mut pg_sys::OpExpr;
            let a1 = pg_sys::list_nth((*op).args, 0) as *mut pg_sys::Node;
            innermost_varno(a1)
        }
        pg_sys::NodeTag::T_CoerceViaIO => {
            innermost_varno((*(node as *mut pg_sys::CoerceViaIO)).arg as *mut pg_sys::Node)
        }
        pg_sys::NodeTag::T_FuncExpr => {
            let fe = node as *mut pg_sys::FuncExpr;
            if pg_sys::list_length((*fe).args) == 0 {
                return None;
            }
            let a0 = pg_sys::list_nth((*fe).args, 0) as *mut pg_sys::Node;
            innermost_varno(a0)
        }
        pg_sys::NodeTag::T_RelabelType => {
            innermost_varno((*(node as *mut pg_sys::RelabelType)).arg as *mut pg_sys::Node)
        }
        _ => None,
    }
}

unsafe fn node_location(node: *mut pg_sys::Node) -> i32 {
    if node.is_null() {
        return -1;
    }
    match (*node).type_ {
        pg_sys::NodeTag::T_Var => (*(node as *mut pg_sys::Var)).location,
        pg_sys::NodeTag::T_OpExpr => (*(node as *mut pg_sys::OpExpr)).location,
        pg_sys::NodeTag::T_CoerceViaIO => (*(node as *mut pg_sys::CoerceViaIO)).location,
        pg_sys::NodeTag::T_FuncExpr => (*(node as *mut pg_sys::FuncExpr)).location,
        pg_sys::NodeTag::T_RelabelType => (*(node as *mut pg_sys::RelabelType)).location,
        _ => -1,
    }
}

unsafe fn op_two_args(op: *mut pg_sys::OpExpr) -> Option<(*mut pg_sys::Node, *mut pg_sys::Node)> {
    let args = (*op).args;
    if args.is_null() || pg_sys::list_length(args) != 2 {
        return None;
    }
    let a1 = pg_sys::list_nth(args, 0) as *mut pg_sys::Node;
    let a2 = pg_sys::list_nth(args, 1) as *mut pg_sys::Node;
    if a1.is_null() || a2.is_null() {
        return None;
    }
    Some((a1, a2))
}

unsafe fn const_to_str(node: *mut pg_sys::Node) -> Option<String> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Const {
        return None;
    }
    let c = node as *mut pg_sys::Const;
    if (*c).consttype.to_u32() != TEXT_OID || (*c).constisnull {
        return None;
    }
    let datum = (*c).constvalue;
    let raw_varlena = datum.cast_mut_ptr::<pg_sys::varlena>();
    if raw_varlena.is_null() {
        return None;
    }
    let detoasted = pg_sys::pg_detoast_datum(raw_varlena);
    let header = std::ptr::read_unaligned(detoasted as *const u32);
    let bytes = if header & 0x01 == 0x01 {
        let len = ((header & 0xff) >> 1) as usize - 1;
        let data_ptr = (detoasted as *const u8).add(1);
        std::slice::from_raw_parts(data_ptr, len)
    } else {
        let total_len = (header >> 2) as usize;
        let data_len = total_len - 4;
        let data_ptr = (detoasted as *const u8).add(4);
        std::slice::from_raw_parts(data_ptr, data_len)
    };
    let s = std::str::from_utf8(bytes).ok()?.to_string();
    if detoasted != raw_varlena {
        pg_sys::pfree(detoasted as *mut _);
    }
    Some(s)
}
