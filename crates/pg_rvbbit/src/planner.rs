//! Phase 2c: planner integration for transparent reads.
//!
//! Architecture (designed with future SQL rewriting in mind):
//!
//!     User query
//!         |
//!         v
//!     [Postgres parser]
//!         |
//!         v
//!     [post_parse_analyze_hook]  <-- Phase 5: REWRITE Query tree using
//!         |                          rvbbit.shreds (e.g. replace
//!         v                          `response->>'foo'` with Var pointing
//!     [Postgres planner]              at the typed shred column).
//!         |
//!         v
//!     [set_rel_pathlist_hook]    <-- THIS FILE. For each base relation,
//!         |                          if it's an rvbbit table with row
//!         v                          groups, add a CustomPath that the
//!     [Postgres executor]            planner can choose.
//!
//! Phase 2c (this file) only handles the read-path replacement. The
//! rewriter layer is its own hook; it consumes the same `rvbbit.shreds`
//! catalog but operates entirely on Query trees, never touching this
//! file. Clean separation.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::ffi::{CStr, CString, c_char};

use pgrx::pg_guard;
use pgrx::pg_sys;

use crate::{custom_scan, router};

static mut PREV_REL_PATHLIST_HOOK: pg_sys::set_rel_pathlist_hook_type = None;
static mut PREV_GET_RELATION_INFO_HOOK: pg_sys::get_relation_info_hook_type = None;
static mut PREV_PLANNER_HOOK: pg_sys::planner_hook_type = None;

const FIRST_NORMAL_OBJECT_ID: u32 = 16384;

thread_local! {
    static IN_HOOK: Cell<bool> = const { Cell::new(false) };
    static IS_RVBBIT_CACHE: RefCell<HashMap<u32, bool>> = RefCell::new(HashMap::new());
    static ROW_GROUPS_CACHE: RefCell<HashMap<u32, i64>> = RefCell::new(HashMap::new());
    /// Cached `(sum_n_rows, sum_n_bytes)` aggregates per table. Filled on
    /// first planner pass over an rvbbit table, persists for the backend's
    /// lifetime. Invalidated locally when this backend runs compact() (see
    /// `invalidate_planner_aggregates`). Cross-backend writes (another
    /// session compacts the table) leave the cache slightly stale until
    /// this backend restarts — that means a slightly off cost estimate for
    /// new row groups, not a correctness issue, so it's an acceptable
    /// tradeoff for saving 2 SPI queries per plan.
    static AGG_CACHE: RefCell<HashMap<u32, (f64, f64)>> = RefCell::new(HashMap::new());
}

/// Drop the per-table planner aggregate cache. Called from compact() so
/// the same backend immediately sees its own writes reflected in plan
/// row estimates.
pub fn invalidate_planner_aggregates(oid: u32) {
    AGG_CACHE.with(|c| c.borrow_mut().remove(&oid));
}

/// Drop ALL cached aggregate estimates (cross-backend epoch flush).
pub fn invalidate_planner_aggregates_all() {
    AGG_CACHE.with(|c| c.borrow_mut().clear());
}

/// Wrapper to make `CustomPathMethods` (which contains raw fn pointers)
/// usable in a `static`. Static items in Rust must be `Sync`; raw pointers
/// aren't `Sync` by default. The methods table is read-only after init
/// and PG only reads it from inside callbacks we control, so it's safe.
#[repr(transparent)]
pub(crate) struct PathMethodsSync(pub pg_sys::CustomPathMethods);
unsafe impl Sync for PathMethodsSync {}

pub(crate) static RVBBIT_PATH_METHODS: PathMethodsSync =
    PathMethodsSync(pg_sys::CustomPathMethods {
        CustomName: c"RvbbitParquetScan".as_ptr() as *const c_char,
        PlanCustomPath: Some(custom_scan::plan_custom_path),
        ReparameterizeCustomPathByChild: None,
    });

/// Install the planner hooks. Called from `_PG_init`.
pub unsafe fn register_hooks() {
    // get_relation_info_hook fires BEFORE the planner generates any base
    // paths. We use it to override the stale rel->rows/pages estimates so
    // the eventual SeqScan path's cost reflects the real (post-compact)
    // size of the relation. Then set_rel_pathlist_hook adds our custom
    // path with a competitive cost and the planner picks it naturally.
    PREV_GET_RELATION_INFO_HOOK = pg_sys::get_relation_info_hook;
    pg_sys::get_relation_info_hook = Some(rvbbit_get_relation_info_hook);

    PREV_REL_PATHLIST_HOOK = pg_sys::set_rel_pathlist_hook;
    pg_sys::set_rel_pathlist_hook = Some(rvbbit_set_rel_pathlist_hook);

    PREV_PLANNER_HOOK = pg_sys::planner_hook;
    pg_sys::planner_hook = Some(rvbbit_planner_hook);

    pg_sys::RegisterCustomScanMethods(&custom_scan::RVBBIT_SCAN_METHODS.0);
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_planner_hook(
    parse: *mut pg_sys::Query,
    query_string: *const c_char,
    cursor_options: std::ffi::c_int,
    bound_params: pg_sys::ParamListInfo,
) -> *mut pg_sys::PlannedStmt {
    let _asof_scope = crate::time_travel::planner_scope(query_string);
    if crate::pg_context::nonsystem_view_access_restricted() {
        return call_next_planner(parse, query_string, cursor_options, bound_params);
    }
    // Snapshot the native+vortex route selection at planner entry, before
    // standard_planner's internal route re-computation can clobber the global flag.
    // add_rvbbit_path reads this captured value to stash into the scan node.
    router::set_native_vortex_plan_captured(router::native_vortex_route_selected());
    if force_heap_scan_enabled() {
        return call_next_planner(parse, query_string, cursor_options, bound_params);
    }
    if router::pg_rowstore_route_selected() {
        let planned = call_next_planner(parse, query_string, cursor_options, bound_params);
        router::set_pg_rowstore_route_selected(false);
        return planned;
    }

    let disable_nestloop = !IN_HOOK.with(|f| f.get()) && query_has_join_heavy_rvbbit(parse);
    if !disable_nestloop {
        return call_next_planner(parse, query_string, cursor_options, bound_params);
    }

    // Stash the value to restore in a thread-local so an xact/subxact abort
    // callback can put it back if standard_planner longjmps past the restore
    // below (ffi-02 — otherwise nestloop stays globally disabled for the whole
    // pooled session).
    let saved = pg_sys::enable_nestloop;
    SAVED_NESTLOOP.with(|s| s.set(Some(saved)));
    pg_sys::enable_nestloop = false;
    let planned = call_next_planner(parse, query_string, cursor_options, bound_params);
    pg_sys::enable_nestloop = saved;
    SAVED_NESTLOOP.with(|s| s.set(None));
    planned
}

thread_local! {
    /// Set while the planner hook has temporarily forced `enable_nestloop=false`
    /// for a join-heavy rvbbit query. Restored on the normal path; restored by
    /// `reset_nestloop_guard()` from the abort callback if planning errored.
    static SAVED_NESTLOOP: std::cell::Cell<Option<bool>> = const { std::cell::Cell::new(None) };
}

/// Restore `enable_nestloop` if the planner hook had overridden it and the
/// restore was skipped by a longjmp. Called from the xact/subxact abort
/// callbacks. Must not panic/ereport.
pub(crate) unsafe fn reset_nestloop_guard() {
    SAVED_NESTLOOP.with(|s| {
        if let Some(v) = s.take() {
            pg_sys::enable_nestloop = v;
        }
    });
}

unsafe fn call_next_planner(
    parse: *mut pg_sys::Query,
    query_string: *const c_char,
    cursor_options: std::ffi::c_int,
    bound_params: pg_sys::ParamListInfo,
) -> *mut pg_sys::PlannedStmt {
    if let Some(prev) = PREV_PLANNER_HOOK {
        prev(parse, query_string, cursor_options, bound_params)
    } else {
        pg_sys::standard_planner(parse, query_string, cursor_options, bound_params)
    }
}

unsafe fn query_has_join_heavy_rvbbit(query: *mut pg_sys::Query) -> bool {
    if query.is_null() || (*query).commandType != pg_sys::CmdType::CMD_SELECT {
        return false;
    }
    count_rvbbit_rtes((*query).rtable) >= 3
}

unsafe fn query_allows_rvbbit_file_scan(root: *mut pg_sys::PlannerInfo) -> bool {
    if root.is_null() || (*root).parse.is_null() {
        return false;
    }
    let query = (*root).parse;
    if (*query).commandType != pg_sys::CmdType::CMD_SELECT {
        return false;
    }
    // Row-locking queries need real heap TIDs. A CustomScan over accelerator
    // files cannot satisfy UPDATE/DELETE/FOR UPDATE tuple identity.
    (*query).rowMarks.is_null()
}

unsafe fn count_rvbbit_rtes(rtable: *mut pg_sys::List) -> usize {
    if rtable.is_null() {
        return 0;
    }
    let mut count = 0usize;
    for i in 0..(*rtable).length {
        let rte = pg_sys::list_nth(rtable, i) as *mut pg_sys::RangeTblEntry;
        if rte.is_null() {
            continue;
        }
        match (*rte).rtekind {
            pg_sys::RTEKind::RTE_RELATION => {
                let oid = (*rte).relid.to_u32();
                if oid >= FIRST_NORMAL_OBJECT_ID && is_rvbbit_table(oid) {
                    count += 1;
                }
            }
            pg_sys::RTEKind::RTE_SUBQUERY => {
                if query_has_join_heavy_rvbbit((*rte).subquery) {
                    count += 3;
                }
            }
            _ => {}
        }
    }
    count
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_get_relation_info_hook(
    root: *mut pg_sys::PlannerInfo,
    relation_oid: pg_sys::Oid,
    inhparent: bool,
    rel: *mut pg_sys::RelOptInfo,
) {
    if let Some(prev) = PREV_GET_RELATION_INFO_HOOK {
        prev(root, relation_oid, inhparent, rel);
    }
    if rel.is_null() {
        return;
    }
    if crate::pg_context::nonsystem_view_access_restricted() {
        return;
    }
    let oid_u32 = relation_oid.to_u32();
    if oid_u32 < FIRST_NORMAL_OBJECT_ID {
        return;
    }
    if IN_HOOK.with(|f| f.get()) {
        return;
    }
    if !query_allows_rvbbit_file_scan(root) {
        return;
    }
    if !is_rvbbit_table(oid_u32) {
        return;
    }
    // Standby (hot standby in recovery): acceleration files aren't WAL-shipped and
    // can't be maintained on a read-only node, but the heap rows ARE replicated
    // (rvbbit = heap). Skip acceleration so PostgreSQL plans a plain heap scan —
    // correct, just unaccelerated — instead of erroring on a missing row-group file.
    if pg_sys::RecoveryInProgress() {
        return;
    }
    if force_heap_scan_enabled() || router::pg_rowstore_route_selected() {
        return;
    }
    if !as_of_generation_enabled() && !parquet_authoritative_for_oid(oid_u32) {
        return;
    }
    // Our replacement scan is not parallel-aware yet. If PG keeps heap
    // partial paths around for a grouped query, the final plan can bypass
    // parquet entirely and read stale heap residue after export_to_parquet.
    (*rel).consider_parallel = false;
    let total_rows = sum_row_group_rows(oid_u32);
    if total_rows > 0.0 {
        (*rel).rows = total_rows;
        (*rel).tuples = total_rows;
        // Estimate pages from total parquet bytes / BLCKSZ. Doesn't matter
        // a lot for cost — what matters is that the seqscan path computed
        // later doesn't think the relation is empty.
        let total_bytes = sum_row_group_bytes(oid_u32);
        let est_pages = (total_bytes / 8192.0).ceil() as u32;
        (*rel).pages = est_pages.max(1);
    }
}

#[pg_guard]
unsafe extern "C-unwind" fn rvbbit_set_rel_pathlist_hook(
    root: *mut pg_sys::PlannerInfo,
    rel: *mut pg_sys::RelOptInfo,
    rti: pg_sys::Index,
    rte: *mut pg_sys::RangeTblEntry,
) {
    if let Some(prev) = PREV_REL_PATHLIST_HOOK {
        prev(root, rel, rti, rte);
    }

    if rte.is_null() || rel.is_null() {
        return;
    }
    if crate::pg_context::nonsystem_view_access_restricted() {
        return;
    }
    if (*rte).rtekind != pg_sys::RTEKind::RTE_RELATION {
        return;
    }

    let oid_u32 = (*rte).relid.to_u32();
    if oid_u32 < FIRST_NORMAL_OBJECT_ID {
        return;
    }
    if IN_HOOK.with(|f| f.get()) {
        return;
    }
    if !query_allows_rvbbit_file_scan(root) {
        return;
    }
    if !is_rvbbit_table(oid_u32) {
        return;
    }
    // Standby (hot standby in recovery): acceleration files aren't WAL-shipped and
    // can't be maintained on a read-only node, but the heap rows ARE replicated
    // (rvbbit = heap). Skip acceleration so PostgreSQL plans a plain heap scan —
    // correct, just unaccelerated — instead of erroring on a missing row-group file.
    if pg_sys::RecoveryInProgress() {
        return;
    }
    if force_heap_scan_enabled() || router::pg_rowstore_route_selected() {
        return;
    }
    if !as_of_generation_enabled() && !parquet_authoritative_for_oid(oid_u32) {
        return;
    }

    let n_rgs = count_row_groups(oid_u32);
    if n_rgs == 0 {
        return;
    }

    let total_rows = sum_row_group_rows(oid_u32);
    let est_rows = clamp_custom_scan_rows((*rel).rows, total_rows);
    (*rel).tuples = total_rows;
    (*rel).consider_parallel = false;

    // Only wipe heap paths when parquet is authoritative: either the legacy
    // heap was truncated, or the retained heap is marked clean by the
    // acceleration refresh machinery. A dirty retained heap remains the SQL
    // source of truth and must be planned by PostgreSQL normally.
    (*rel).pathlist = std::ptr::null_mut();
    (*rel).partial_pathlist = std::ptr::null_mut();
    (*rel).cheapest_total_path = std::ptr::null_mut();
    (*rel).cheapest_startup_path = std::ptr::null_mut();
    (*rel).cheapest_unique_path = std::ptr::null_mut();
    (*rel).cheapest_parameterized_paths = std::ptr::null_mut();

    let total_bytes = sum_row_group_bytes(oid_u32);
    add_rvbbit_path(rel, oid_u32, est_rows, total_rows, total_bytes);
}

/// Allocate a CustomPath in PG memory and register it with the planner.
unsafe fn add_rvbbit_path(
    rel: *mut pg_sys::RelOptInfo,
    table_oid: u32,
    est_rows: f64,
    total_rows: f64,
    total_bytes: f64,
) {
    let path_ptr =
        pg_sys::palloc0(std::mem::size_of::<pg_sys::CustomPath>()) as *mut pg_sys::CustomPath;
    let path = &mut *path_ptr;

    path.path.type_ = pg_sys::NodeTag::T_CustomPath;
    path.path.pathtype = pg_sys::NodeTag::T_CustomScan;
    path.path.parent = rel;
    path.path.pathtarget = (*rel).reltarget;
    path.path.param_info = std::ptr::null_mut();
    path.path.parallel_aware = false;
    path.path.parallel_safe = false;
    path.path.parallel_workers = 0;
    path.path.rows = est_rows;
    // There is no heap path left after compaction, so this path can carry
    // realistic cost information. Join planning depends on these numbers:
    // pretending filtered parquet scans are zero-cost/full-row scans pushes
    // PostgreSQL toward huge join cardinalities and unnecessary JIT.
    path.path.startup_cost = 0.0;
    path.path.total_cost = parquet_scan_cost(total_rows, total_bytes, est_rows);
    path.path.pathkeys = std::ptr::null_mut();

    // CUSTOMPATH_SUPPORT_PROJECTION = 0x0004. Tells the planner this path
    // can handle output projection itself, so it doesn't insert a Result
    // node above us — which (we suspect) is what's resolving every Var to
    // attribute 1 regardless of what was requested.
    path.flags = 0x0004;
    path.custom_paths = std::ptr::null_mut();
    // Stash the table OID in custom_private as a List of Integer so
    // PlanCustomPath can recover it. Element 1 carries the native+vortex route
    // selection captured HERE at plan time (where the rewriter's flag is still
    // reliably set) — it then rides the plan node into begin_custom_scan, immune
    // to the execution-time route re-computation that clobbers the global flag.
    let native_vortex = crate::router::native_vortex_plan_captured() as i32;
    let oid_list = pg_sys::lappend_int(std::ptr::null_mut(), table_oid as i32);
    let oid_list = pg_sys::lappend_int(oid_list, native_vortex);
    path.custom_private = oid_list;
    path.methods = &RVBBIT_PATH_METHODS.0;

    pg_sys::add_path(rel, path_ptr as *mut pg_sys::Path);
}

unsafe fn parquet_scan_cost(total_rows: f64, total_bytes: f64, est_rows: f64) -> f64 {
    let pages = (total_bytes / 8192.0).ceil().max(1.0);
    let input_rows = total_rows.max(est_rows).max(1.0);
    // Parquet scans avoid heap tuple visibility and read fewer projected
    // bytes, but they still pay IO, decode, and output tuple costs.
    let io = pages * pg_sys::seq_page_cost * 0.25;
    let decode = input_rows * pg_sys::cpu_tuple_cost * 0.20;
    let output = est_rows.max(1.0) * pg_sys::cpu_tuple_cost;
    (io + decode + output).max(1.0)
}

fn clamp_custom_scan_rows(pg_est_rows: f64, total_rows: f64) -> f64 {
    if total_rows <= 1.0 {
        return pg_est_rows.max(1.0);
    }
    // After compact, PostgreSQL's heap stats can be absent or stale because
    // the heap is empty and parquet is authoritative. Avoid one-row estimates
    // for full row-group scans; those make nested loops rescan parquet.
    pg_est_rows.max(total_rows * 0.05).min(total_rows).max(1.0)
}

fn force_heap_scan_enabled() -> bool {
    guc_setting("rvbbit.force_heap_scan")
        .map(|value| setting_enabled(&value, false))
        .unwrap_or(false)
}

fn as_of_generation_enabled() -> bool {
    crate::time_travel::active_as_of_enabled()
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

/// Cached lookup: is this relation enabled in the rvbbit acceleration registry?
fn is_rvbbit_table(oid: u32) -> bool {
    if let Some(cached) = IS_RVBBIT_CACHE.with(|c| c.borrow().get(&oid).copied()) {
        return cached;
    }
    IN_HOOK.with(|f| f.set(true));
    let result: Result<Option<bool>, _> = pgrx::Spi::get_one(&format!(
        "SELECT EXISTS ( \
             SELECT 1 \
             FROM rvbbit.tables t \
             JOIN pg_class c ON c.oid = t.table_oid \
             WHERE t.table_oid = {oid}::oid \
               AND coalesce(t.acceleration_enabled, true) \
         )"
    ));
    IN_HOOK.with(|f| f.set(false));
    let is = result.ok().flatten().unwrap_or(false);
    IS_RVBBIT_CACHE.with(|c| c.borrow_mut().insert(oid, is));
    is
}

/// Latest-view parquet scans are correct only when the heap is empty
/// (legacy compact) or the retained heap has not been mutated since the
/// last acceleration refresh. Historical AS OF reads are handled by the
/// caller and intentionally bypass this latest-view check.
fn parquet_authoritative_for_oid(oid: u32) -> bool {
    IN_HOOK.with(|f| f.set(true));
    let result: Result<Option<bool>, _> = pgrx::Spi::get_one(&format!(
        "SELECT pg_relation_size(t.table_oid) = 0 \
                OR rvbbit.shadow_heap_clean_retained(t.table_oid) \
         FROM rvbbit.tables t \
         WHERE t.table_oid = {oid}::oid"
    ));
    IN_HOOK.with(|f| f.set(false));
    result.ok().flatten().unwrap_or(false)
}

/// How many row groups does this table have? Not cached because compact()
/// changes it.
fn count_row_groups(oid: u32) -> i64 {
    IN_HOOK.with(|f| f.set(true));
    let n: Result<Option<i64>, _> = pgrx::Spi::get_one(&format!(
        "SELECT count(*) FROM rvbbit.row_groups_visible WHERE table_oid = {oid}::oid"
    ));
    IN_HOOK.with(|f| f.set(false));
    n.ok().flatten().unwrap_or(0)
}

fn sum_row_group_rows(oid: u32) -> f64 {
    aggregate_for_oid(oid).0
}

fn sum_row_group_bytes(oid: u32) -> f64 {
    aggregate_for_oid(oid).1
}

/// Fetch `(sum_n_rows, sum_n_bytes)` for a relation, using the
/// backend-local cache. The two values are paired because the planner
/// always asks for both on the same plan, and one SPI returning two
/// columns is meaningfully cheaper than two SPIs.
fn aggregate_for_oid(oid: u32) -> (f64, f64) {
    crate::custom_scan::refresh_caches_if_stale();
    if let Some(cached) = AGG_CACHE.with(|c| c.borrow().get(&oid).copied()) {
        return cached;
    }
    IN_HOOK.with(|f| f.set(true));
    let mut rows = 0i64;
    let mut bytes = 0i64;
    let _ = pgrx::Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            &format!(
                "SELECT coalesce(sum(n_rows), 0)::bigint, \
                        coalesce(sum(n_bytes), 0)::bigint \
                 FROM rvbbit.row_groups_visible WHERE table_oid = {oid}::oid"
            ),
            None,
            &[],
        )?;
        for row in table {
            rows = row.get::<i64>(1)?.unwrap_or(0);
            bytes = row.get::<i64>(2)?.unwrap_or(0);
        }
        Ok(())
    });
    IN_HOOK.with(|f| f.set(false));
    let pair = (rows as f64, bytes as f64);
    AGG_CACHE.with(|c| c.borrow_mut().insert(oid, pair));
    pair
}
