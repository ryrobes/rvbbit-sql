//! Phase 2c: CustomScan implementation.
//!
//! Lifecycle (PG callback order):
//!
//!   1. Planner: `PlanCustomPath`            (path -> Plan node)
//!   2. Executor init: `CreateCustomScanState`   (Plan -> ScanState)
//!   3. Executor start: `BeginCustomScan`     (open parquet readers)
//!   4. Executor run:   `ExecCustomScan` * N  (return one slot per call)
//!   5. Executor cleanup: `EndCustomScan`     (close readers)
//!
//! Plus `ReScanCustomScan` (rewind for cursor reuse) and
//! `ExplainCustomScan` (EXPLAIN output).
//!
//! Phase 2c MVP scope:
//!   - Returns ALL rows from ALL row groups (no row-group pruning yet)
//!   - Returns ALL columns from each row group (no projection pushdown
//!     yet — planner's PathTarget tells us what to keep, but we'd have
//!     to walk it and translate to a ProjectionMask; deferred)
//!   - No predicate pushdown (planner filters on top of our output)
//!   - No parallel scan
//!
//! All of these are layered on top later without changing the scan
//! state structure.

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::c_char;
use std::sync::Arc;

use rvbbit_storage::metadata::{ColumnStats, TextSketch};
use rvbbit_storage::row_group::RowGroupReader;
use arrow::array::{
    Array, BinaryArray, BooleanArray, Date32Array, Float32Array, Float64Array, Int16Array,
    Int32Array, Int64Array, RecordBatch, StringArray, TimestampMicrosecondArray,
};
use arrow::datatypes::DataType;
use parquet::arrow::arrow_reader::ParquetRecordBatchReader;
use pgrx::pg_guard;
use pgrx::pg_sys;

const SCAN_LAYOUT: &str = "scan";
const CLUSTER_LAYOUT_PREFIX: &str = "cluster:";

thread_local! {
    static SCAN_BATCH_CACHE: RefCell<ScanBatchCache> =
        RefCell::new(ScanBatchCache::default());
}

// --- Method tables ---------------------------------------------------------

#[repr(transparent)]
pub(crate) struct ScanMethodsSync(pub pg_sys::CustomScanMethods);
unsafe impl Sync for ScanMethodsSync {}

#[repr(transparent)]
pub(crate) struct ExecMethodsSync(pub pg_sys::CustomExecMethods);
unsafe impl Sync for ExecMethodsSync {}

pub(crate) static RVBBIT_SCAN_METHODS: ScanMethodsSync =
    ScanMethodsSync(pg_sys::CustomScanMethods {
        CustomName: c"RvbbitParquetScan".as_ptr() as *const c_char,
        CreateCustomScanState: Some(create_custom_scan_state),
    });

pub(crate) static RVBBIT_EXEC_METHODS: ExecMethodsSync =
    ExecMethodsSync(pg_sys::CustomExecMethods {
        CustomName: c"RvbbitParquetScan".as_ptr() as *const c_char,
        BeginCustomScan: Some(begin_custom_scan),
        ExecCustomScan: Some(exec_custom_scan),
        EndCustomScan: Some(end_custom_scan),
        ReScanCustomScan: Some(rescan_custom_scan),
        MarkPosCustomScan: None,
        RestrPosCustomScan: None,
        EstimateDSMCustomScan: None,
        InitializeDSMCustomScan: None,
        ReInitializeDSMCustomScan: None,
        InitializeWorkerCustomScan: None,
        ShutdownCustomScan: None,
        ExplainCustomScan: Some(explain_custom_scan),
    });

// --- Convert chosen path → plan node ---------------------------------------

#[pg_guard]
pub(crate) unsafe extern "C-unwind" fn plan_custom_path(
    _root: *mut pg_sys::PlannerInfo,
    rel: *mut pg_sys::RelOptInfo,
    best_path: *mut pg_sys::CustomPath,
    tlist: *mut pg_sys::List,
    clauses: *mut pg_sys::List,
    _custom_plans: *mut pg_sys::List,
) -> *mut pg_sys::Plan {
    let cscan_ptr =
        pg_sys::palloc0(std::mem::size_of::<pg_sys::CustomScan>()) as *mut pg_sys::CustomScan;
    let cscan = &mut *cscan_ptr;

    cscan.scan.plan.type_ = pg_sys::NodeTag::T_CustomScan;
    cscan.scan.plan.targetlist = tlist;
    cscan.scan.plan.qual = pgrx::pg_sys::extract_actual_clauses(clauses, false);
    cscan.scan.scanrelid = (*rel).relid;

    cscan.flags = (*best_path).flags;
    cscan.custom_plans = std::ptr::null_mut();
    cscan.custom_exprs = std::ptr::null_mut();
    // Pass the table OID through from path to scan.
    cscan.custom_private = (*best_path).custom_private;
    cscan.custom_scan_tlist = std::ptr::null_mut();
    cscan.custom_relids = std::ptr::null_mut();
    cscan.methods = &RVBBIT_SCAN_METHODS.0;

    cscan_ptr as *mut pg_sys::Plan
}

// --- Per-execution state (Rust-owned, pointer stashed in CustomScanState) ---

#[repr(C)]
struct RvbbitScanStateExt {
    /// Must be first — PG treats this whole allocation as a CustomScanState.
    css: pg_sys::CustomScanState,
    /// List<Integer> carrying the table OID; populated from the CustomScan's
    /// custom_private in create_custom_scan_state, consumed by begin.
    oid_list: *mut pg_sys::List,
    /// Boxed because PG palloc memory + Rust-owned Vec/Option don't mix.
    /// We Box::leak in begin and Box::from_raw in end.
    rust_state_ptr: *mut RustScanState,
}

struct RustScanState {
    /// Row group descriptors discovered in begin_custom_scan.
    row_groups: Vec<RowGroupEntry>,
    row_group_layout: String,
    rg_idx: usize,
    pruned_row_groups: usize,
    /// Current reader iterating batches within the current row group.
    current_reader: Option<ParquetRecordBatchReader>,
    /// Decoded batches served from the per-backend scan cache for the current
    /// row group/projection.
    current_cached_batches: Option<Vec<RecordBatch>>,
    current_cached_batch_idx: usize,
    /// Accumulates decoded batches while streaming a row group so future scans
    /// of the same projection can skip parquet decode.
    current_cache_key: Option<BatchCacheKey>,
    current_cache_accum: Vec<RecordBatch>,
    /// Current batch we're emitting rows from.
    current_batch: Option<RecordBatch>,
    row_in_batch: usize,
    /// The scan slot's value/isnull arrays are reused row-by-row. Initialize
    /// untouched columns to NULL once, then per-row writes only touch projected
    /// columns.
    slot_nulls_initialized: bool,
    /// Parameterized nested-loop/subquery scans can rescan the same parquet
    /// projection hundreds of times with only the outer scalar changing. Keep
    /// decoded batches for those scans inside the query's executor state.
    cached_batches: Vec<RecordBatch>,
    cache_complete: bool,
    cached_batch_idx: usize,
    eq_index: Option<RuntimeEqIndex>,
    indexed_row_refs: Vec<CachedRowRef>,
    indexed_row_ref_idx: usize,
    indexed_lookup_dirty: bool,
    indexed_lookup_active: bool,
    /// PG tuple descriptor (column oids/types) for the relation.
    /// Cached so we don't fetch from slot every row.
    pg_attrs: Vec<PgAttr>,
    /// Attribute numbers (1-based) actually referenced by the query
    /// (targetlist + qual). Anything not in here gets NULL in the slot
    /// without ever being read from parquet — the projection pushdown win.
    needed_attnums: Vec<i32>,
    /// One typed reader per needed column, rebuilt when current_batch is
    /// replaced. Lets fill_slot do a single enum-dispatch per cell
    /// instead of an Arc<dyn Array> downcast on every row.
    column_readers: Vec<NeededColumn>,
    /// Predicates recognized at plan time that can be evaluated on
    /// Arrow data BEFORE tuple materialization. PG's ExecQual still
    /// runs as a safety net on rows we emit, so semantics are unchanged
    /// even if we miss subtle corner cases.
    pushed_quals: Vec<PushedQual>,
    pushed_expr: Option<PushExpr>,
    qual_fully_pushed: bool,
    /// Per-pushed-qual, a typed reader into the current batch — same
    /// shape as `column_readers` but indexed by pushed_quals position.
    qual_readers: Vec<ColumnReader>,
    qual_rhs_readers: Vec<ColumnReader>,
    /// Pushed quals whose RHS is an outer/parameter expression. On each
    /// rescan we evaluate the expression once and update the corresponding
    /// `pushed_quals` value, so correlated scans stay vectorized.
    dynamic_quals: Vec<DynamicPushedQual>,
    dynamic_quals_dirty: bool,
}

/// (attnum_idx, reader). attnum_idx is 0-based into pg_attrs.
struct NeededColumn {
    attnum_idx: usize,
    reader: ColumnReader,
}

/// Typed read into the currently-active Arrow batch.
/// Pointers are valid only as long as `current_batch` lives — rebuilt
/// every time a new batch is pulled. The owning `current_batch` field on
/// RustScanState keeps the Arrow buffers alive while we hold pointers
/// into them.
enum ColumnReader {
    Int16(*const Int16Array),
    Int32(*const Int32Array),
    Date32(*const Date32Array),
    Date32Int32(*const Int32Array),
    Int64(*const Int64Array),
    Float32(*const Float32Array),
    Float64(*const Float64Array),
    Bool(*const BooleanArray),
    Utf8 {
        arr: *const StringArray,
        is_jsonb: bool,
    },
    Binary(*const BinaryArray),
    TimestampMicros(*const TimestampMicrosecondArray),
    /// Column requested by query but absent from this row group — always NULL.
    Missing,
}

/// A simple `Var <op> Const` predicate the planner found in the qual
/// list and we can evaluate directly on Arrow column data, without
/// going through PG's per-row qual evaluation.
struct PushedQual {
    /// 1-based PG attnum into pg_attrs.
    attnum: i32,
    op: PushOp,
    value: PushVal,
}

enum PushExpr {
    Qual(usize),
    And(Vec<PushExpr>),
    Or(Vec<PushExpr>),
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PushOp {
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
    In,
    /// PG LIKE / NOT LIKE / ILIKE / NOT ILIKE. Pattern lives in
    /// PushVal::Text; the negation + case sensitivity is baked into
    /// the op variant so eval is a single match arm.
    Like,
    NotLike,
    ILike,
    NotILike,
}

/// Constant value extracted from a Const node. We type the variants by
/// the comparison family so the per-row check is a tight match arm.
#[derive(Clone)]
enum PushVal {
    Null,
    I64(i64),
    F64(f64),
    Bool(bool),
    Text(String),
    I64Set(Vec<i64>),
    F64Set(Vec<f64>),
    BoolSet(Vec<bool>),
    TextSet(Vec<String>),
    Column(i32),
}

#[derive(Default)]
struct PushedQualPlan {
    quals: Vec<PushedQual>,
    expr: Option<PushExpr>,
    dynamic_quals: Vec<DynamicPushedQual>,
    fully_pushed: bool,
}

struct DynamicPushedQual {
    qual_idx: usize,
    expr_state: *mut pg_sys::ExprState,
    typoid: pg_sys::Oid,
}

struct RuntimeEqIndex {
    qual_indices: Vec<usize>,
    map: HashMap<RuntimeEqKey, Vec<CachedRowRef>>,
}

#[derive(Clone, Copy)]
struct CachedRowRef {
    batch_idx: usize,
    row: usize,
}

#[derive(Clone, Copy, Hash, PartialEq, Eq)]
struct RuntimeEqKey {
    len: u8,
    values: [i64; 4],
}

struct RowGroupEntry {
    path: String,
    stats: HashMap<String, PruneStats>,
}

#[derive(Clone, Hash, PartialEq, Eq)]
struct BatchCacheKey {
    path: String,
    file_len: u64,
    file_mtime_nanos: u128,
    projection: String,
}

struct BatchCacheEntry {
    batches: Vec<RecordBatch>,
    bytes: usize,
}

#[derive(Default)]
struct ScanBatchCache {
    entries: HashMap<BatchCacheKey, BatchCacheEntry>,
    bytes: usize,
}

struct PruneStats {
    min: Option<serde_json::Value>,
    max: Option<serde_json::Value>,
    text_sketch_b64: Option<String>,
}

struct PgAttr {
    name: String,
    typoid: pg_sys::Oid,
    typmod: i32,
}

// --- CreateCustomScanState -------------------------------------------------

#[pg_guard]
unsafe extern "C-unwind" fn create_custom_scan_state(
    cscan: *mut pg_sys::CustomScan,
) -> *mut pg_sys::Node {
    let ext_ptr =
        pg_sys::palloc0(std::mem::size_of::<RvbbitScanStateExt>()) as *mut RvbbitScanStateExt;
    let ext = &mut *ext_ptr;

    ext.css.ss.ps.type_ = pg_sys::NodeTag::T_CustomScanState;
    ext.css.flags = (*cscan).flags;
    ext.css.methods = &RVBBIT_EXEC_METHODS.0;
    ext.rust_state_ptr = std::ptr::null_mut();
    // Stash custom_private (the table-oid list) in our own field rather
    // than css.custom_ps — custom_ps is reserved for child PlanState ptrs
    // and the executor walks it as such.
    ext.oid_list = (*cscan).custom_private;

    ext_ptr as *mut pg_sys::Node
}

// --- BeginCustomScan: open parquet readers ---------------------------------

#[pg_guard]
unsafe extern "C-unwind" fn begin_custom_scan(
    node: *mut pg_sys::CustomScanState,
    _estate: *mut pg_sys::EState,
    _eflags: i32,
) {
    let ext = node as *mut RvbbitScanStateExt;

    let oid_list = (*ext).oid_list;
    if oid_list.is_null() {
        pgrx::error!("rvbbit custom scan: missing table oid in custom_private");
    }
    let table_oid = pg_sys::list_nth_int(oid_list, 0) as u32;

    // Extract the tuple descriptor — tells us what columns PG expects.
    let slot = (*node).ss.ss_ScanTupleSlot;
    let tupdesc = (*slot).tts_tupleDescriptor;
    let natts = (*tupdesc).natts as usize;
    let mut attrs = Vec::with_capacity(natts);
    for i in 0..(*tupdesc).natts {
        let attr = pgrx::pg_sys::TupleDescAttr(tupdesc, i);
        let name = std::ffi::CStr::from_ptr((*attr).attname.data.as_ptr())
            .to_string_lossy()
            .into_owned();
        attrs.push(PgAttr {
            name,
            typoid: (*attr).atttypid,
            typmod: (*attr).atttypmod,
        });
    }

    // Projection pushdown: walk the plan's targetlist + qual for Var
    // references, collect attnums. Anything not in the set won't be
    // read from parquet and will be NULL in the slot.
    let cscan = (*node).ss.ps.plan as *mut pg_sys::CustomScan;
    let needed_attnums = collect_needed_attnums(cscan, attrs.len() as i32);

    let qual = (*(*node).ss.ps.plan).qual;
    let pushed_plan = analyze_qual(qual, (*cscan).scan.scanrelid, &mut (*node).ss.ps);
    let needs_row_group_stats = pushed_plan_can_prune_row_groups(&pushed_plan);

    // Fetch row group paths via SPI. Stats are large once text sketches exist,
    // so keep true scans on the cheap path and load stats only when a pushed
    // predicate can actually use them for row-group pruning. If the clustered
    // variant can prune enough row groups, scan that copy; otherwise use the
    // scan-friendly canonical layout.
    let (row_groups, row_group_layout) =
        match fetch_best_row_group_paths(table_oid, &attrs, &pushed_plan, needs_row_group_stats) {
            Ok(result) => result,
            Err(e) => pgrx::error!("rvbbit custom scan: row group lookup failed: {}", e),
        };

    let rust_state = Box::new(RustScanState {
        row_groups,
        row_group_layout,
        rg_idx: 0,
        pruned_row_groups: 0,
        current_reader: None,
        current_cached_batches: None,
        current_cached_batch_idx: 0,
        current_cache_key: None,
        current_cache_accum: Vec::new(),
        current_batch: None,
        row_in_batch: 0,
        slot_nulls_initialized: false,
        cached_batches: Vec::new(),
        cache_complete: false,
        cached_batch_idx: 0,
        eq_index: None,
        indexed_row_refs: Vec::new(),
        indexed_row_ref_idx: 0,
        indexed_lookup_dirty: true,
        indexed_lookup_active: false,
        pg_attrs: attrs,
        needed_attnums,
        column_readers: Vec::new(),
        pushed_quals: pushed_plan.quals,
        pushed_expr: pushed_plan.expr,
        qual_fully_pushed: pushed_plan.fully_pushed,
        qual_readers: Vec::new(),
        qual_rhs_readers: Vec::new(),
        dynamic_quals: pushed_plan.dynamic_quals,
        dynamic_quals_dirty: true,
    });
    (*ext).rust_state_ptr = Box::into_raw(rust_state);
}

/// Walk the qual list and build a conservative predicate tree from the
/// clauses we can evaluate directly on Arrow data. Anything unsupported is
/// left for PG's ExecQual to handle.
unsafe fn analyze_qual(
    qual: *mut pg_sys::List,
    scan_varno: pg_sys::Index,
    parent: *mut pg_sys::PlanState,
) -> PushedQualPlan {
    let mut plan = PushedQualPlan::default();
    if qual.is_null() {
        plan.fully_pushed = true;
        return plan;
    }
    let mut conjuncts = Vec::new();
    let mut fully_pushed = true;
    let len = (*qual).length;
    for i in 0..len {
        let node = pg_sys::list_nth(qual, i) as *mut pg_sys::Node;
        let mut full_node_plan = PushedQualPlan::default();
        if let Some(mut expr) =
            recognize_push_expr(node, scan_varno, parent, &mut full_node_plan, true)
        {
            let offset = plan.quals.len();
            offset_push_expr(&mut expr, offset);
            for dq in &mut full_node_plan.dynamic_quals {
                dq.qual_idx += offset;
            }
            plan.quals.extend(full_node_plan.quals);
            plan.dynamic_quals.extend(full_node_plan.dynamic_quals);
            conjuncts.push(expr);
            continue;
        }

        fully_pushed = false;
        if let Some(expr) = recognize_push_expr(node, scan_varno, parent, &mut plan, false) {
            conjuncts.push(expr);
        }
    }
    plan.expr = combine_push_expr(PushExpr::And, conjuncts);
    plan.fully_pushed = fully_pushed && plan.dynamic_quals.is_empty();
    plan
}

fn pushed_plan_can_prune_row_groups(plan: &PushedQualPlan) -> bool {
    if plan.dynamic_quals.is_empty() {
        plan.quals.iter().any(pushed_qual_can_prune_row_group)
    } else {
        // The executor currently disables row-group pruning when runtime
        // outer-param quals are present because their values can change.
        false
    }
}

fn pushed_qual_can_prune_row_group(q: &PushedQual) -> bool {
    if matches!(q.value, PushVal::Column(_)) {
        return false;
    }
    !matches!(q.op, PushOp::NotLike | PushOp::NotILike)
}

unsafe fn recognize_push_expr(
    node: *mut pg_sys::Node,
    scan_varno: pg_sys::Index,
    parent: *mut pg_sys::PlanState,
    plan: &mut PushedQualPlan,
    require_complete_or_branch: bool,
) -> Option<PushExpr> {
    if node.is_null() {
        return None;
    }
    match (*node).type_ {
        pg_sys::NodeTag::T_BoolExpr => {
            let bool_expr = node as *mut pg_sys::BoolExpr;
            let args = (*bool_expr).args;
            if args.is_null() {
                return None;
            }
            let mut children = Vec::new();
            let len = (*args).length;
            match (*bool_expr).boolop {
                pg_sys::BoolExprType::AND_EXPR => {
                    for i in 0..len {
                        let child = pg_sys::list_nth(args, i) as *mut pg_sys::Node;
                        if let Some(expr) = recognize_push_expr(
                            child,
                            scan_varno,
                            parent,
                            plan,
                            require_complete_or_branch,
                        ) {
                            children.push(expr);
                        } else if require_complete_or_branch {
                            return None;
                        }
                    }
                    combine_push_expr(PushExpr::And, children)
                }
                pg_sys::BoolExprType::OR_EXPR => {
                    for i in 0..len {
                        let child = pg_sys::list_nth(args, i) as *mut pg_sys::Node;
                        let expr = recognize_push_expr(child, scan_varno, parent, plan, true)?;
                        children.push(expr);
                    }
                    combine_push_expr(PushExpr::Or, children)
                }
                _ => None,
            }
        }
        pg_sys::NodeTag::T_OpExpr => push_one_qual(
            try_recognize_clause(node, scan_varno, parent, plan),
            &mut plan.quals,
        ),
        pg_sys::NodeTag::T_ScalarArrayOpExpr => push_one_qual(
            try_recognize_scalar_array_clause(node, scan_varno),
            &mut plan.quals,
        ),
        _ if require_complete_or_branch => None,
        _ => None,
    }
}

fn push_one_qual(pq: Option<PushedQual>, quals: &mut Vec<PushedQual>) -> Option<PushExpr> {
    let pq = pq?;
    let idx = quals.len();
    quals.push(pq);
    Some(PushExpr::Qual(idx))
}

fn combine_push_expr(
    ctor: fn(Vec<PushExpr>) -> PushExpr,
    mut children: Vec<PushExpr>,
) -> Option<PushExpr> {
    match children.len() {
        0 => None,
        1 => Some(children.remove(0)),
        _ => Some(ctor(children)),
    }
}

fn offset_push_expr(expr: &mut PushExpr, offset: usize) {
    match expr {
        PushExpr::Qual(idx) => *idx += offset,
        PushExpr::And(children) | PushExpr::Or(children) => {
            for child in children {
                offset_push_expr(child, offset);
            }
        }
    }
}

unsafe fn try_recognize_clause(
    node: *mut pg_sys::Node,
    scan_varno: pg_sys::Index,
    parent: *mut pg_sys::PlanState,
    plan: &mut PushedQualPlan,
) -> Option<PushedQual> {
    if node.is_null() {
        return None;
    }
    if (*node).type_ != pg_sys::NodeTag::T_OpExpr {
        return None;
    }
    let op = node as *mut pg_sys::OpExpr;
    let opno = (*op).opno.to_u32();
    let push_op = recognize_op(opno)?;
    let args = (*op).args;
    if args.is_null() || (*args).length != 2 {
        return None;
    }
    let arg0 = strip_coercion(pg_sys::list_nth(args, 0) as *mut pg_sys::Node);
    let arg1 = strip_coercion(pg_sys::list_nth(args, 1) as *mut pg_sys::Node);

    // Either scan-Var <op> Const, Const <op> scan-Var, or scan-Var <op>
    // outer expression. Normalize to scan-Var-first by flipping the operator
    // where needed. LIKE/ILIKE don't commute, so 'const' op 'var' is not
    // recognizable for those.
    let arg0_scan_var = scan_var(arg0, scan_varno);
    let arg1_scan_var = scan_var(arg1, scan_varno);

    if let Some(var) = arg0_scan_var {
        if let Some(rhs_var) = arg1_scan_var {
            return Some(PushedQual {
                attnum: (*var).varattno as i32,
                op: push_op,
                value: PushVal::Column((*rhs_var).varattno as i32),
            });
        }
        if (*arg1).type_ == pg_sys::NodeTag::T_Const {
            return const_pushed_qual(var, arg1, push_op);
        }
        if !contains_scan_var(arg1, scan_varno) {
            return dynamic_pushed_qual(var, arg1, push_op, parent, plan);
        }
        return None;
    }

    if let Some(var) = arg1_scan_var {
        let op_normalized = flip_op(push_op)?;
        if (*arg0).type_ == pg_sys::NodeTag::T_Const {
            return const_pushed_qual(var, arg0, op_normalized);
        }
        if !contains_scan_var(arg0, scan_varno) {
            return dynamic_pushed_qual(var, arg0, op_normalized, parent, plan);
        }
    }

    None
}

unsafe fn const_pushed_qual(
    var: *mut pg_sys::Var,
    const_node: *mut pg_sys::Node,
    op: PushOp,
) -> Option<PushedQual> {
    let cst = const_node as *mut pg_sys::Const;
    if (*cst).constisnull {
        return None; // NULL semantics — leave to PG
    }
    let value = extract_const_value((*cst).consttype.to_u32(), (*cst).constvalue)?;
    Some(PushedQual {
        attnum: (*var).varattno as i32,
        op,
        value,
    })
}

unsafe fn dynamic_pushed_qual(
    var: *mut pg_sys::Var,
    value_expr: *mut pg_sys::Node,
    op: PushOp,
    parent: *mut pg_sys::PlanState,
    plan: &mut PushedQualPlan,
) -> Option<PushedQual> {
    if value_expr.is_null() || parent.is_null() {
        return None;
    }
    let typoid = pg_sys::exprType(value_expr);
    if !is_supported_push_typoid(typoid.to_u32()) {
        return None;
    }
    let expr_state = pg_sys::ExecInitExpr(value_expr as *mut pg_sys::Expr, parent);
    if expr_state.is_null() {
        return None;
    }
    let qual_idx = plan.quals.len();
    plan.dynamic_quals.push(DynamicPushedQual {
        qual_idx,
        expr_state,
        typoid,
    });
    Some(PushedQual {
        attnum: (*var).varattno as i32,
        op,
        value: PushVal::Null,
    })
}

unsafe fn try_recognize_scalar_array_clause(
    node: *mut pg_sys::Node,
    scan_varno: pg_sys::Index,
) -> Option<PushedQual> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_ScalarArrayOpExpr {
        return None;
    }
    let expr = node as *mut pg_sys::ScalarArrayOpExpr;
    if !(*expr).useOr || recognize_op((*expr).opno.to_u32())? != PushOp::Eq {
        return None;
    }
    let args = (*expr).args;
    if args.is_null() || (*args).length != 2 {
        return None;
    }
    let left = strip_coercion(pg_sys::list_nth(args, 0) as *mut pg_sys::Node);
    let right = strip_coercion(pg_sys::list_nth(args, 1) as *mut pg_sys::Node);
    let var = scan_var(left, scan_varno)?;
    let value = extract_array_value_set(right)?;
    Some(PushedQual {
        attnum: (*var).varattno as i32,
        op: PushOp::In,
        value,
    })
}

unsafe fn strip_coercion(mut node: *mut pg_sys::Node) -> *mut pg_sys::Node {
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

unsafe fn scan_var(node: *mut pg_sys::Node, scan_varno: pg_sys::Index) -> Option<*mut pg_sys::Var> {
    if node.is_null() || (*node).type_ != pg_sys::NodeTag::T_Var {
        return None;
    }
    let var = node as *mut pg_sys::Var;
    if (*var).varno == scan_varno as i32 && (*var).varlevelsup == 0 && (*var).varattno > 0 {
        Some(var)
    } else {
        None
    }
}

unsafe fn contains_scan_var(node: *mut pg_sys::Node, scan_varno: pg_sys::Index) -> bool {
    if node.is_null() {
        return false;
    }
    let mut bms: *mut pg_sys::Bitmapset = std::ptr::null_mut();
    pg_sys::pull_varattnos(node, scan_varno, &mut bms);
    !bms.is_null()
}

fn is_supported_push_typoid(typoid: u32) -> bool {
    matches!(
        typoid,
        16 | 19 | 20 | 21 | 23 | 25 | 700 | 701 | 1042 | 1043 | 1082
    )
}

/// PG operator OIDs for the comparison ops we recognize. Source:
/// src/include/catalog/pg_operator.dat.
fn recognize_op(opno: u32) -> Option<PushOp> {
    use PushOp::*;
    Some(match opno {
        // int4
        96 => Eq,
        97 => Lt,
        521 => Gt,
        523 => Le,
        525 => Ge,
        // int8
        410 => Eq,
        412 => Lt,
        413 => Gt,
        414 => Le,
        415 => Ge,
        // int2
        94 => Eq,
        95 => Lt,
        520 => Gt,
        522 => Le,
        524 => Ge,
        // float4
        620 => Eq,
        622 => Lt,
        623 => Gt,
        624 => Le,
        625 => Ge,
        // float8
        670 => Eq,
        672 => Lt,
        674 => Gt,
        673 => Le,
        675 => Ge,
        // bool
        91 => Eq,
        // date
        1093 => Eq,
        1095 => Lt,
        1096 => Le,
        1097 => Gt,
        1098 => Ge,
        // text / varchar equality
        98 => Eq, // texteq
        // text LIKE / NOT LIKE / ILIKE / NOT ILIKE (PG operator OIDs)
        1209 => Like,
        1210 => NotLike,
        1226 => ILike,
        1227 => NotILike,
        // int24 / int42 / int48 / int84 cross-type
        15 => Eq,
        37 => Lt,
        76 => Gt,
        80 => Le,
        82 => Ge,
        416 => Eq,
        418 => Lt,
        419 => Gt,
        420 => Le,
        430 => Ge,
        474 => Eq,
        534 => Lt,
        535 => Gt,
        540 => Le,
        542 => Ge,
        _ => return None,
    })
}

fn flip_op(op: PushOp) -> Option<PushOp> {
    use PushOp::*;
    // LIKE/ILIKE aren't commutative — we only push when Var is on the
    // left ("col LIKE 'pattern'"). Return None for "'pattern' LIKE col"
    // to bail out of pushdown for that clause.
    Some(match op {
        Lt => Gt,
        Le => Ge,
        Gt => Lt,
        Ge => Le,
        Eq => Eq,
        In | Like | NotLike | ILike | NotILike => return None,
    })
}

unsafe fn extract_const_value(typoid: u32, datum: pg_sys::Datum) -> Option<PushVal> {
    Some(match typoid {
        21 => PushVal::I64(datum.value() as i16 as i64),
        23 => PushVal::I64(datum.value() as i32 as i64),
        20 => PushVal::I64(datum.value() as i64),
        1082 => PushVal::I64(datum.value() as i32 as i64),
        700 => PushVal::F64(f32::from_bits(datum.value() as u32) as f64),
        701 => PushVal::F64(f64::from_bits(datum.value() as u64)),
        16 => PushVal::Bool((datum.value() as u8) != 0),
        // text(25) / varchar(1043) / bpchar(1042) / name(19) — datum is
        // a varlena pointer. text_to_cstring handles detoasting.
        25 | 1043 | 1042 | 19 => {
            let varlena = datum.cast_mut_ptr::<pg_sys::varlena>();
            if varlena.is_null() {
                return None;
            }
            let cstr_ptr = pg_sys::text_to_cstring(varlena as *const pg_sys::text);
            if cstr_ptr.is_null() {
                return None;
            }
            let s = std::ffi::CStr::from_ptr(cstr_ptr)
                .to_string_lossy()
                .into_owned();
            pg_sys::pfree(cstr_ptr as *mut _);
            PushVal::Text(s)
        }
        _ => return None,
    })
}

unsafe fn refresh_dynamic_quals(state: &mut RustScanState, node: *mut pg_sys::CustomScanState) {
    if !state.dynamic_quals_dirty {
        return;
    }
    state.dynamic_quals_dirty = false;
    if state.dynamic_quals.is_empty() {
        return;
    }
    let ps = &mut (*node).ss.ps;
    let econtext = ps.ps_ExprContext;
    if econtext.is_null() {
        return;
    }
    for dq in &state.dynamic_quals {
        if dq.qual_idx >= state.pushed_quals.len() {
            continue;
        }
        let mut is_null = false;
        let datum = pg_sys::ExecEvalExpr(dq.expr_state, econtext, &mut is_null);
        state.pushed_quals[dq.qual_idx].value = if is_null {
            PushVal::Null
        } else {
            extract_const_value(dq.typoid.to_u32(), datum).unwrap_or(PushVal::Null)
        };
    }
}

unsafe fn extract_array_value_set(node: *mut pg_sys::Node) -> Option<PushVal> {
    if node.is_null() {
        return None;
    }
    match (*node).type_ {
        pg_sys::NodeTag::T_ArrayExpr => {
            let array = node as *mut pg_sys::ArrayExpr;
            let elements = (*array).elements;
            if elements.is_null() {
                return Some(PushVal::I64Set(Vec::new()));
            }
            let mut values = Vec::new();
            for i in 0..(*elements).length {
                let elem = strip_coercion(pg_sys::list_nth(elements, i) as *mut pg_sys::Node);
                if elem.is_null() || (*elem).type_ != pg_sys::NodeTag::T_Const {
                    return None;
                }
                let cst = elem as *mut pg_sys::Const;
                if (*cst).constisnull {
                    continue;
                }
                values.push(extract_const_value(
                    (*cst).consttype.to_u32(),
                    (*cst).constvalue,
                )?);
            }
            push_values_to_set(values)
        }
        pg_sys::NodeTag::T_Const => {
            let cst = node as *mut pg_sys::Const;
            if (*cst).constisnull {
                return None;
            }
            let elem_oid = pg_sys::get_element_type((*cst).consttype);
            if elem_oid.to_u32() == 0 {
                return None;
            }
            let any_array = pg_sys::DatumGetAnyArrayP((*cst).constvalue);
            if any_array.is_null() {
                return None;
            }
            let mut elems: *mut pg_sys::Datum = std::ptr::null_mut();
            let mut nulls: *mut bool = std::ptr::null_mut();
            let mut nelems: std::ffi::c_int = 0;
            pg_sys::deconstruct_array_builtin(
                any_array as *mut pg_sys::ArrayType,
                elem_oid,
                &mut elems,
                &mut nulls,
                &mut nelems,
            );
            let mut values = Vec::with_capacity(nelems as usize);
            for i in 0..nelems {
                if !nulls.is_null() && *nulls.add(i as usize) {
                    continue;
                }
                values.push(extract_const_value(
                    elem_oid.to_u32(),
                    *elems.add(i as usize),
                )?);
            }
            if !elems.is_null() {
                pg_sys::pfree(elems as *mut _);
            }
            if !nulls.is_null() {
                pg_sys::pfree(nulls as *mut _);
            }
            push_values_to_set(values)
        }
        _ => None,
    }
}

fn push_values_to_set(values: Vec<PushVal>) -> Option<PushVal> {
    let mut i64s = Vec::new();
    let mut f64s = Vec::new();
    let mut bools = Vec::new();
    let mut texts = Vec::new();
    for value in values {
        match value {
            PushVal::I64(v) if f64s.is_empty() && bools.is_empty() && texts.is_empty() => {
                i64s.push(v);
            }
            PushVal::F64(v) if i64s.is_empty() && bools.is_empty() && texts.is_empty() => {
                f64s.push(v);
            }
            PushVal::Bool(v) if i64s.is_empty() && f64s.is_empty() && texts.is_empty() => {
                bools.push(v);
            }
            PushVal::Text(v) if i64s.is_empty() && f64s.is_empty() && bools.is_empty() => {
                texts.push(v);
            }
            _ => return None,
        }
    }
    if !texts.is_empty() {
        Some(PushVal::TextSet(texts))
    } else if !i64s.is_empty() {
        Some(PushVal::I64Set(i64s))
    } else if !f64s.is_empty() {
        Some(PushVal::F64Set(f64s))
    } else if !bools.is_empty() {
        Some(PushVal::BoolSet(bools))
    } else {
        Some(PushVal::I64Set(Vec::new()))
    }
}

/// PG LIKE pattern matcher. `%` matches any (possibly empty) sequence,
/// `_` matches a single char, everything else literal. No backslash
/// escapes for v0 (PG users hardly ever rely on them).
///
/// Fast paths recognize the four ubiquitous shapes that account for
/// the vast majority of real-world LIKE patterns — equality,
/// startsWith, endsWith, contains. Everything else falls to the
/// general iterative backtrack matcher.
fn like_match(haystack: &str, pattern: &str, case_insensitive: bool) -> bool {
    // Case-sensitive fast path: no allocation, no copy.
    if !case_insensitive {
        if let Some(result) = like_fast_path(haystack, pattern) {
            return result;
        }
        return like_match_inner(haystack.as_bytes(), pattern.as_bytes());
    }
    // Case-insensitive: lowercase both, then try fast paths against the
    // lowered forms.
    let h = haystack.to_lowercase();
    let p = pattern.to_lowercase();
    if let Some(result) = like_fast_path(&h, &p) {
        return result;
    }
    like_match_inner(h.as_bytes(), p.as_bytes())
}

/// Returns Some(result) if `pattern` matches one of the fast-path
/// shapes; None to fall through to the general matcher.
fn like_fast_path(haystack: &str, pattern: &str) -> Option<bool> {
    let pb = pattern.as_bytes();
    // No wildcards → equality
    if !pb.contains(&b'%') && !pb.contains(&b'_') {
        return Some(haystack == pattern);
    }
    // %foo% → contains. No other wildcards in the interior.
    if pb.len() >= 2 && pb[0] == b'%' && pb[pb.len() - 1] == b'%' {
        let inner = &pattern[1..pattern.len() - 1];
        let ib = inner.as_bytes();
        if !ib.contains(&b'%') && !ib.contains(&b'_') {
            return Some(haystack.contains(inner));
        }
    }
    // foo% → starts_with
    if pb.len() >= 1 && pb[pb.len() - 1] == b'%' {
        let prefix = &pattern[..pattern.len() - 1];
        let pb2 = prefix.as_bytes();
        if !pb2.contains(&b'%') && !pb2.contains(&b'_') {
            return Some(haystack.starts_with(prefix));
        }
    }
    // %foo → ends_with
    if pb.len() >= 1 && pb[0] == b'%' {
        let suffix = &pattern[1..];
        let pb2 = suffix.as_bytes();
        if !pb2.contains(&b'%') && !pb2.contains(&b'_') {
            return Some(haystack.ends_with(suffix));
        }
    }
    None
}

fn like_match_inner(s: &[u8], p: &[u8]) -> bool {
    // Classic iterative wildcard match with backtrack on `%`.
    let (mut si, mut pi) = (0usize, 0usize);
    let (mut star_si, mut star_pi) = (None::<usize>, None::<usize>);
    while si < s.len() {
        if pi < p.len() && (p[pi] == b'_' || p[pi] == s[si]) {
            si += 1;
            pi += 1;
        } else if pi < p.len() && p[pi] == b'%' {
            star_pi = Some(pi);
            star_si = Some(si);
            pi += 1;
        } else if let (Some(spi), Some(ssi)) = (star_pi, star_si) {
            pi = spi + 1;
            si = ssi + 1;
            star_si = Some(si);
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == b'%' {
        pi += 1;
    }
    pi == p.len()
}

/// True if the pushed predicate passes for `row` of the current batch.
/// Returning false means PG won't see this row at all — the row stays
/// inside our scan, never crosses the slot boundary.
unsafe fn pushed_expr_pass(
    qual_readers: &[ColumnReader],
    qual_rhs_readers: &[ColumnReader],
    pushed: &[PushedQual],
    expr: &PushExpr,
    row: usize,
) -> bool {
    match expr {
        PushExpr::Qual(idx) => {
            let (Some(q), Some(rdr)) = (pushed.get(*idx), qual_readers.get(*idx)) else {
                return true;
            };
            let rhs = qual_rhs_readers.get(*idx).unwrap_or(&ColumnReader::Missing);
            eval_one_qual(rdr, rhs, q, row)
        }
        PushExpr::And(children) => children
            .iter()
            .all(|child| pushed_expr_pass(qual_readers, qual_rhs_readers, pushed, child, row)),
        PushExpr::Or(children) => children
            .iter()
            .any(|child| pushed_expr_pass(qual_readers, qual_rhs_readers, pushed, child, row)),
    }
}

unsafe fn eval_one_qual(
    reader: &ColumnReader,
    rhs_reader: &ColumnReader,
    q: &PushedQual,
    row: usize,
) -> bool {
    use PushOp::*;
    use PushVal::*;
    // PG SQL semantics: any predicate on NULL is NULL → row excluded.
    macro_rules! cmp {
        ($l:expr, $r:expr) => {
            match q.op {
                Lt => $l < $r,
                Le => $l <= $r,
                Gt => $l > $r,
                Ge => $l >= $r,
                Eq => $l == $r,
                In => return false,
                // LIKE family is only meaningful on text columns; for
                // numeric/bool the qual is nonsense — exclude the row.
                Like | NotLike | ILike | NotILike => return false,
            }
        };
    }
    match (reader, &q.value) {
        (_, Null) => false,
        (_, Column(_)) => match (
            i64_key_from_reader(reader, row),
            i64_key_from_reader(rhs_reader, row),
        ) {
            (Some(lhs), Some(rhs)) => compare_i64(q.op, lhs, rhs),
            _ => true,
        },
        (ColumnReader::Int16(p), I64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as i64, rhs)
            }
        }
        (ColumnReader::Int16(p), I64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&(a.value(row) as i64))
        }
        (ColumnReader::Int32(p), I64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as i64, rhs)
            }
        }
        (ColumnReader::Int32(p), I64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&(a.value(row) as i64))
        }
        (ColumnReader::Date32(p), I64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!((a.value(row) - PG_EPOCH_OFFSET_DAYS) as i64, rhs)
            }
        }
        (ColumnReader::Date32(p), I64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&((a.value(row) - PG_EPOCH_OFFSET_DAYS) as i64))
        }
        (ColumnReader::Date32Int32(p), I64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!((a.value(row) - PG_EPOCH_OFFSET_DAYS) as i64, rhs)
            }
        }
        (ColumnReader::Date32Int32(p), I64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&((a.value(row) - PG_EPOCH_OFFSET_DAYS) as i64))
        }
        (ColumnReader::Int64(p), I64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row), rhs)
            }
        }
        (ColumnReader::Int64(p), I64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&a.value(row))
        }
        (ColumnReader::Int16(p), F64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as f64, rhs)
            }
        }
        (ColumnReader::Int32(p), F64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as f64, rhs)
            }
        }
        (ColumnReader::Int64(p), F64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as f64, rhs)
            }
        }
        (ColumnReader::Float32(p), F64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as f64, rhs)
            }
        }
        (ColumnReader::Float32(p), F64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&(a.value(row) as f64))
        }
        (ColumnReader::Float64(p), F64(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row), rhs)
            }
        }
        (ColumnReader::Float64(p), F64Set(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&a.value(row))
        }
        (ColumnReader::Float32(p), I64(rhs)) => {
            let rhs = *rhs as f64;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row) as f64, rhs)
            }
        }
        (ColumnReader::Float64(p), I64(rhs)) => {
            let rhs = *rhs as f64;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row), rhs)
            }
        }
        (ColumnReader::Bool(p), Bool(rhs)) => {
            let rhs = *rhs;
            let a = &**p;
            if a.is_null(row) {
                false
            } else {
                cmp!(a.value(row), rhs)
            }
        }
        (ColumnReader::Bool(p), BoolSet(rhs)) => {
            let a = &**p;
            !a.is_null(row) && rhs.contains(&a.value(row))
        }
        // Text comparisons / LIKE / ILIKE against a Utf8 column.
        (ColumnReader::Utf8 { arr, .. }, Text(rhs)) => {
            let a = &**arr;
            if a.is_null(row) {
                return false;
            }
            let s = a.value(row);
            let rhs = rhs.as_str();
            match q.op {
                Eq => s == rhs,
                Lt => s < rhs,
                Le => s <= rhs,
                Gt => s > rhs,
                Ge => s >= rhs,
                In => return false,
                Like => like_match(s, rhs, false),
                NotLike => !like_match(s, rhs, false),
                ILike => like_match(s, rhs, true),
                NotILike => !like_match(s, rhs, true),
            }
        }
        (ColumnReader::Utf8 { arr, .. }, TextSet(rhs)) => {
            let a = &**arr;
            if a.is_null(row) {
                return false;
            }
            let s = a.value(row);
            rhs.iter().any(|candidate| candidate == s)
        }
        // Anything else — be conservative, fall through and let PG ExecQual
        // re-evaluate (we return true to keep the row in the candidate set).
        _ => true,
    }
}

/// Return false when row-group min/max stats prove at least one pushed
/// AND-clause cannot match any row in this group. Anything uncertain stays
/// on the scan path; PG's ExecQual remains the final authority.
fn row_group_may_satisfy(
    row_group: &RowGroupEntry,
    pg_attrs: &[PgAttr],
    pushed_quals: &[PushedQual],
    pushed_expr: Option<&PushExpr>,
) -> bool {
    let Some(expr) = pushed_expr else {
        return true;
    };
    !row_group_expr_impossible(row_group, pg_attrs, pushed_quals, expr)
}

fn row_group_expr_impossible(
    row_group: &RowGroupEntry,
    pg_attrs: &[PgAttr],
    pushed_quals: &[PushedQual],
    expr: &PushExpr,
) -> bool {
    match expr {
        PushExpr::Qual(idx) => pushed_quals
            .get(*idx)
            .is_some_and(|q| row_group_clause_impossible(row_group, pg_attrs, q)),
        PushExpr::And(children) => children
            .iter()
            .any(|child| row_group_expr_impossible(row_group, pg_attrs, pushed_quals, child)),
        PushExpr::Or(children) => children
            .iter()
            .all(|child| row_group_expr_impossible(row_group, pg_attrs, pushed_quals, child)),
    }
}

fn row_group_clause_impossible(
    row_group: &RowGroupEntry,
    pg_attrs: &[PgAttr],
    q: &PushedQual,
) -> bool {
    if q.attnum <= 0 {
        return false;
    }
    let attr_idx = (q.attnum - 1) as usize;
    let Some(attr) = pg_attrs.get(attr_idx) else {
        return false;
    };
    let Some(stats) = row_group.stats.get(&attr.name) else {
        return false;
    };
    match &q.value {
        PushVal::Null => true,
        PushVal::I64(rhs) => {
            let (Some(min), Some(max)) = (&stats.min, &stats.max) else {
                return false;
            };
            if let Some((min, max)) = json_i64_bounds(min, max) {
                let (min, max) = if attr.typoid == pg_sys::DATEOID {
                    (
                        min - PG_EPOCH_OFFSET_DAYS as i64,
                        max - PG_EPOCH_OFFSET_DAYS as i64,
                    )
                } else {
                    (min, max)
                };
                return i64_clause_impossible(q.op, min, max, *rhs);
            }
            if let Some((min, max)) = json_f64_bounds(min, max) {
                return f64_clause_impossible(q.op, min, max, *rhs as f64);
            }
            false
        }
        PushVal::F64(rhs) => {
            let (Some(min), Some(max)) = (&stats.min, &stats.max) else {
                return false;
            };
            let Some((min, max)) = json_f64_bounds(min, max) else {
                return false;
            };
            f64_clause_impossible(q.op, min, max, *rhs)
        }
        PushVal::Bool(rhs) => {
            let (Some(min), Some(max)) = (&stats.min, &stats.max) else {
                return false;
            };
            let Some((min, max)) = json_bool_bounds(min, max) else {
                return false;
            };
            bool_clause_impossible(q.op, min, max, *rhs)
        }
        PushVal::I64Set(rhs) => {
            let (Some(min), Some(max)) = (&stats.min, &stats.max) else {
                return false;
            };
            if let Some((min, max)) = json_i64_bounds(min, max) {
                let (min, max) = if attr.typoid == pg_sys::DATEOID {
                    (
                        min - PG_EPOCH_OFFSET_DAYS as i64,
                        max - PG_EPOCH_OFFSET_DAYS as i64,
                    )
                } else {
                    (min, max)
                };
                return rhs
                    .iter()
                    .all(|value| i64_clause_impossible(PushOp::Eq, min, max, *value));
            }
            false
        }
        PushVal::F64Set(rhs) => {
            let (Some(min), Some(max)) = (&stats.min, &stats.max) else {
                return false;
            };
            let Some((min, max)) = json_f64_bounds(min, max) else {
                return false;
            };
            rhs.iter()
                .all(|value| f64_clause_impossible(PushOp::Eq, min, max, *value))
        }
        PushVal::BoolSet(rhs) => {
            let (Some(min), Some(max)) = (&stats.min, &stats.max) else {
                return false;
            };
            let Some((min, max)) = json_bool_bounds(min, max) else {
                return false;
            };
            rhs.iter()
                .all(|value| bool_clause_impossible(PushOp::Eq, min, max, *value))
        }
        PushVal::Text(rhs) => text_clause_impossible(
            q.op,
            stats.min.as_ref(),
            stats.max.as_ref(),
            stats.text_sketch_b64.as_deref(),
            attr.typoid != pg_sys::BPCHAROID,
            rhs,
        ),
        PushVal::TextSet(rhs) => rhs.iter().all(|value| {
            text_clause_impossible(
                PushOp::Eq,
                stats.min.as_ref(),
                stats.max.as_ref(),
                stats.text_sketch_b64.as_deref(),
                attr.typoid != pg_sys::BPCHAROID,
                value,
            )
        }),
        PushVal::Column(_) => false,
    }
}

fn json_i64_bounds(min: &serde_json::Value, max: &serde_json::Value) -> Option<(i64, i64)> {
    Some((json_i64(min)?, json_i64(max)?))
}

fn json_i64(v: &serde_json::Value) -> Option<i64> {
    v.as_i64()
        .or_else(|| v.as_u64().and_then(|u| i64::try_from(u).ok()))
}

fn json_f64_bounds(min: &serde_json::Value, max: &serde_json::Value) -> Option<(f64, f64)> {
    Some((min.as_f64()?, max.as_f64()?))
}

fn json_bool_bounds(min: &serde_json::Value, max: &serde_json::Value) -> Option<(bool, bool)> {
    Some((min.as_bool()?, max.as_bool()?))
}

fn json_text_bounds<'a>(
    min: &'a serde_json::Value,
    max: &'a serde_json::Value,
) -> Option<(&'a str, &'a str)> {
    Some((min.as_str()?, max.as_str()?))
}

fn i64_clause_impossible(op: PushOp, min: i64, max: i64, rhs: i64) -> bool {
    use PushOp::*;
    match op {
        Eq => rhs < min || rhs > max,
        Lt => min >= rhs,
        Le => min > rhs,
        Gt => max <= rhs,
        Ge => max < rhs,
        In | Like | NotLike | ILike | NotILike => false,
    }
}

fn compare_i64(op: PushOp, lhs: i64, rhs: i64) -> bool {
    match op {
        PushOp::Eq => lhs == rhs,
        PushOp::Lt => lhs < rhs,
        PushOp::Le => lhs <= rhs,
        PushOp::Gt => lhs > rhs,
        PushOp::Ge => lhs >= rhs,
        PushOp::In | PushOp::Like | PushOp::NotLike | PushOp::ILike | PushOp::NotILike => true,
    }
}

fn f64_clause_impossible(op: PushOp, min: f64, max: f64, rhs: f64) -> bool {
    use PushOp::*;
    if !min.is_finite() || !max.is_finite() || !rhs.is_finite() {
        return false;
    }
    match op {
        Eq => rhs < min || rhs > max,
        Lt => min >= rhs,
        Le => min > rhs,
        Gt => max <= rhs,
        Ge => max < rhs,
        In | Like | NotLike | ILike | NotILike => false,
    }
}

fn bool_clause_impossible(op: PushOp, min: bool, max: bool, rhs: bool) -> bool {
    match op {
        PushOp::Eq => (rhs as u8) < (min as u8) || (rhs as u8) > (max as u8),
        _ => false,
    }
}

fn text_clause_impossible(
    op: PushOp,
    min: Option<&serde_json::Value>,
    max: Option<&serde_json::Value>,
    sketch_b64: Option<&str>,
    use_value_sketch: bool,
    rhs: &str,
) -> bool {
    match op {
        PushOp::Eq => {
            let Some((min, max)) = min
                .zip(max)
                .and_then(|(min, max)| json_text_bounds(min, max))
            else {
                if use_value_sketch {
                    return sketch_b64
                        .and_then(TextSketch::from_b64)
                        .is_some_and(|sketch| !sketch.may_contain_value(rhs));
                }
                return false;
            };
            if rhs < min || rhs > max {
                return true;
            }
            use_value_sketch
                && sketch_b64
                    .and_then(TextSketch::from_b64)
                    .is_some_and(|sketch| !sketch.may_contain_value(rhs))
        }
        PushOp::Like | PushOp::ILike => {
            let Some(sketch) = sketch_b64.and_then(TextSketch::from_b64) else {
                return false;
            };
            like_pattern_impossible_for_sketch(&sketch, rhs, matches!(op, PushOp::ILike))
        }
        _ => false,
    }
}

fn like_pattern_impossible_for_sketch(
    sketch: &TextSketch,
    pattern: &str,
    case_insensitive: bool,
) -> bool {
    let required = required_like_trigrams(pattern);
    !required.is_empty()
        && required
            .iter()
            .any(|trigram| !sketch.may_contain_trigram(trigram, case_insensitive))
}

fn required_like_trigrams(pattern: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut literal = String::new();
    for ch in pattern.chars() {
        if ch == '%' || ch == '_' {
            push_literal_trigrams(&literal, &mut out);
            literal.clear();
        } else {
            literal.push(ch);
        }
    }
    push_literal_trigrams(&literal, &mut out);
    out
}

fn push_literal_trigrams(literal: &str, out: &mut Vec<String>) {
    let bytes = literal.as_bytes();
    if bytes.len() < 3 {
        return;
    }
    for window in bytes.windows(3) {
        if let Ok(s) = std::str::from_utf8(window) {
            out.push(s.to_string());
        }
    }
}

/// Build a typed reader per needed column for this batch. Called whenever
/// `state.current_batch` is replaced. Does one downcast per column;
/// `fill_slot_from_batch` then issues only enum dispatches per row.
unsafe fn rebuild_column_readers(state: &mut RustScanState) {
    let batch = match state.current_batch.as_ref() {
        Some(b) => b,
        None => {
            state.column_readers.clear();
            state.qual_readers.clear();
            state.qual_rhs_readers.clear();
            return;
        }
    };
    state.qual_readers = build_qual_readers_for_batch(batch, &state.pg_attrs, &state.pushed_quals);
    state.qual_rhs_readers =
        build_qual_rhs_readers_for_batch(batch, &state.pg_attrs, &state.pushed_quals);
    state.column_readers =
        build_column_readers_for_batch(batch, &state.pg_attrs, &state.needed_attnums);
}

unsafe fn build_qual_readers_for_batch(
    batch: &RecordBatch,
    pg_attrs: &[PgAttr],
    pushed_quals: &[PushedQual],
) -> Vec<ColumnReader> {
    let mut out = Vec::with_capacity(pushed_quals.len());
    let schema = batch.schema();
    for q in pushed_quals {
        let col_idx = (q.attnum - 1) as usize;
        if col_idx >= pg_attrs.len() {
            out.push(ColumnReader::Missing);
            continue;
        }
        let attr = &pg_attrs[col_idx];
        match schema.index_of(&attr.name) {
            Ok(i) => out.push(make_reader_for(batch.column(i), attr)),
            Err(_) => out.push(ColumnReader::Missing),
        }
    }
    out
}

unsafe fn build_qual_rhs_readers_for_batch(
    batch: &RecordBatch,
    pg_attrs: &[PgAttr],
    pushed_quals: &[PushedQual],
) -> Vec<ColumnReader> {
    let mut out = Vec::with_capacity(pushed_quals.len());
    let schema = batch.schema();
    for q in pushed_quals {
        let PushVal::Column(attnum) = &q.value else {
            out.push(ColumnReader::Missing);
            continue;
        };
        let col_idx = (*attnum - 1) as usize;
        let Some(attr) = pg_attrs.get(col_idx) else {
            out.push(ColumnReader::Missing);
            continue;
        };
        match schema.index_of(&attr.name) {
            Ok(i) => out.push(make_reader_for(batch.column(i), attr)),
            Err(_) => out.push(ColumnReader::Missing),
        }
    }
    out
}

unsafe fn build_column_readers_for_batch(
    batch: &RecordBatch,
    pg_attrs: &[PgAttr],
    needed_attnums: &[i32],
) -> Vec<NeededColumn> {
    let mut out = Vec::with_capacity(needed_attnums.len());
    let schema = batch.schema();
    for &attnum in needed_attnums {
        let col_idx = (attnum - 1) as usize;
        let attr = &pg_attrs[col_idx];
        let arrow_col_idx = match schema.index_of(&attr.name) {
            Ok(i) => i,
            Err(_) => {
                out.push(NeededColumn {
                    attnum_idx: col_idx,
                    reader: ColumnReader::Missing,
                });
                continue;
            }
        };
        let array = batch.column(arrow_col_idx);
        let reader = make_reader_for(array, attr);
        out.push(NeededColumn {
            attnum_idx: col_idx,
            reader,
        });
    }
    out
}

unsafe fn make_reader_for(array: &Arc<dyn Array>, attr: &PgAttr) -> ColumnReader {
    match array.data_type() {
        DataType::Int16 => {
            ColumnReader::Int16(array.as_any().downcast_ref::<Int16Array>().unwrap() as *const _)
        }
        DataType::Date32 => {
            ColumnReader::Date32(array.as_any().downcast_ref::<Date32Array>().unwrap() as *const _)
        }
        DataType::Int32 => {
            let a = array.as_any().downcast_ref::<Int32Array>().unwrap() as *const _;
            if attr.typoid == pg_sys::DATEOID {
                ColumnReader::Date32Int32(a)
            } else {
                ColumnReader::Int32(a)
            }
        }
        DataType::Int64 => {
            ColumnReader::Int64(array.as_any().downcast_ref::<Int64Array>().unwrap() as *const _)
        }
        DataType::Float32 => {
            ColumnReader::Float32(array.as_any().downcast_ref::<Float32Array>().unwrap() as *const _)
        }
        DataType::Float64 => {
            ColumnReader::Float64(array.as_any().downcast_ref::<Float64Array>().unwrap() as *const _)
        }
        DataType::Boolean => {
            ColumnReader::Bool(array.as_any().downcast_ref::<BooleanArray>().unwrap() as *const _)
        }
        DataType::Utf8 => ColumnReader::Utf8 {
            arr: array.as_any().downcast_ref::<StringArray>().unwrap() as *const _,
            is_jsonb: attr.typoid == pg_sys::JSONBOID,
        },
        DataType::Binary => {
            ColumnReader::Binary(array.as_any().downcast_ref::<BinaryArray>().unwrap() as *const _)
        }
        DataType::Timestamp(_, _) => ColumnReader::TimestampMicros(
            array
                .as_any()
                .downcast_ref::<TimestampMicrosecondArray>()
                .unwrap() as *const _,
        ),
        other => {
            pgrx::error!(
                "rvbbit: unsupported arrow type {:?} for column '{}' (oid {})",
                other,
                attr.name,
                attr.typoid.to_u32()
            );
        }
    }
}

unsafe fn prepare_indexed_lookup(state: &mut RustScanState) {
    if !state.cache_complete || !state.indexed_lookup_dirty {
        return;
    }
    state.indexed_lookup_dirty = false;
    state.indexed_lookup_active = false;
    state.indexed_row_refs.clear();
    state.indexed_row_ref_idx = 0;

    if state.eq_index.is_none() {
        let index = build_runtime_eq_index(state);
        state.eq_index = index;
    }

    let Some(index) = state.eq_index.as_ref() else {
        return;
    };
    let Some(key) = runtime_eq_lookup_key(&state.pushed_quals, &index.qual_indices) else {
        return;
    };
    state.indexed_lookup_active = true;
    if let Some(rows) = index.map.get(&key) {
        state.indexed_row_refs = rows.clone();
    }
}

unsafe fn build_runtime_eq_index(state: &RustScanState) -> Option<RuntimeEqIndex> {
    let pushed_expr = state.pushed_expr.as_ref()?;
    let qual_indices = state
        .dynamic_quals
        .iter()
        .filter_map(|dq| {
            let q = state.pushed_quals.get(dq.qual_idx)?;
            (q.op == PushOp::Eq
                && q.attnum > 0
                && push_expr_contains_qual(pushed_expr, dq.qual_idx))
            .then_some(dq.qual_idx)
        })
        .take(4)
        .collect::<Vec<_>>();
    if qual_indices.is_empty() {
        return None;
    }

    let attrs = qual_indices
        .iter()
        .filter_map(|qual_idx| {
            let q = state.pushed_quals.get(*qual_idx)?;
            let attr_idx = (q.attnum - 1) as usize;
            state.pg_attrs.get(attr_idx)
        })
        .collect::<Vec<_>>();
    if attrs.len() != qual_indices.len() {
        return None;
    }

    let mut map: HashMap<RuntimeEqKey, Vec<CachedRowRef>> = HashMap::new();
    for (batch_idx, batch) in state.cached_batches.iter().enumerate() {
        let schema = batch.schema();
        let readers = attrs
            .iter()
            .filter_map(|attr| {
                let col_idx = schema.index_of(&attr.name).ok()?;
                Some(make_reader_for(batch.column(col_idx), attr))
            })
            .collect::<Vec<_>>();
        if readers.len() != attrs.len() {
            continue;
        }
        for row in 0..batch.num_rows() {
            if let Some(key) = runtime_eq_row_key(&readers, row) {
                map.entry(key)
                    .or_default()
                    .push(CachedRowRef { batch_idx, row });
            }
        }
    }
    Some(RuntimeEqIndex { qual_indices, map })
}

fn push_expr_contains_qual(expr: &PushExpr, qual_idx: usize) -> bool {
    match expr {
        PushExpr::Qual(idx) => *idx == qual_idx,
        PushExpr::And(children) | PushExpr::Or(children) => children
            .iter()
            .any(|child| push_expr_contains_qual(child, qual_idx)),
    }
}

unsafe fn i64_key_from_reader(reader: &ColumnReader, row: usize) -> Option<i64> {
    match reader {
        ColumnReader::Int16(p) => {
            let a = &**p;
            (!a.is_null(row)).then(|| a.value(row) as i64)
        }
        ColumnReader::Int32(p) => {
            let a = &**p;
            (!a.is_null(row)).then(|| a.value(row) as i64)
        }
        ColumnReader::Date32(p) => {
            let a = &**p;
            (!a.is_null(row)).then(|| (a.value(row) - PG_EPOCH_OFFSET_DAYS) as i64)
        }
        ColumnReader::Date32Int32(p) => {
            let a = &**p;
            (!a.is_null(row)).then(|| (a.value(row) - PG_EPOCH_OFFSET_DAYS) as i64)
        }
        ColumnReader::Int64(p) => {
            let a = &**p;
            (!a.is_null(row)).then(|| a.value(row))
        }
        _ => None,
    }
}

fn runtime_eq_lookup_key(quals: &[PushedQual], qual_indices: &[usize]) -> Option<RuntimeEqKey> {
    if qual_indices.is_empty() || qual_indices.len() > 4 {
        return None;
    }
    let mut key = RuntimeEqKey {
        len: qual_indices.len() as u8,
        values: [0; 4],
    };
    for (idx, qual_idx) in qual_indices.iter().enumerate() {
        let q = quals.get(*qual_idx)?;
        let PushVal::I64(value) = q.value else {
            return None;
        };
        key.values[idx] = value;
    }
    Some(key)
}

unsafe fn runtime_eq_row_key(readers: &[ColumnReader], row: usize) -> Option<RuntimeEqKey> {
    if readers.is_empty() || readers.len() > 4 {
        return None;
    }
    let mut key = RuntimeEqKey {
        len: readers.len() as u8,
        values: [0; 4],
    };
    for (idx, reader) in readers.iter().enumerate() {
        key.values[idx] = i64_key_from_reader(reader, row)?;
    }
    Some(key)
}

fn batch_cache_limit_bytes() -> usize {
    std::env::var("RVBBIT_SCAN_BATCH_CACHE_MB")
        .ok()
        .and_then(|raw| raw.parse::<usize>().ok())
        .unwrap_or(256)
        .saturating_mul(1024 * 1024)
}

fn batch_cache_key(path: &str, col_names: &[String]) -> BatchCacheKey {
    let (file_len, file_mtime_nanos) = std::fs::metadata(path)
        .ok()
        .map(|metadata| {
            let mtime = metadata
                .modified()
                .ok()
                .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|duration| duration.as_nanos())
                .unwrap_or(0);
            (metadata.len(), mtime)
        })
        .unwrap_or((0, 0));
    BatchCacheKey {
        path: path.to_string(),
        file_len,
        file_mtime_nanos,
        projection: col_names.join("\u{1f}"),
    }
}

fn batch_cache_get(key: &BatchCacheKey) -> Option<Vec<RecordBatch>> {
    SCAN_BATCH_CACHE.with(|cache| {
        cache
            .borrow()
            .entries
            .get(key)
            .map(|entry| entry.batches.clone())
    })
}

fn batch_cache_put(key: BatchCacheKey, batches: Vec<RecordBatch>) {
    if batches.is_empty() {
        return;
    }
    let limit = batch_cache_limit_bytes();
    if limit == 0 {
        return;
    }
    let bytes = batches
        .iter()
        .map(RecordBatch::get_array_memory_size)
        .sum::<usize>();
    if bytes > limit {
        return;
    }

    SCAN_BATCH_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        if let Some(existing) = cache.entries.remove(&key) {
            cache.bytes = cache.bytes.saturating_sub(existing.bytes);
        }
        while cache.bytes.saturating_add(bytes) > limit {
            let Some(victim) = cache.entries.keys().next().cloned() else {
                break;
            };
            if let Some(existing) = cache.entries.remove(&victim) {
                cache.bytes = cache.bytes.saturating_sub(existing.bytes);
            }
        }
        cache.bytes = cache.bytes.saturating_add(bytes);
        cache
            .entries
            .insert(key, BatchCacheEntry { batches, bytes });
    });
}

fn finish_current_batch_cache(state: &mut RustScanState) {
    let Some(key) = state.current_cache_key.take() else {
        state.current_cache_accum.clear();
        return;
    };
    let batches = std::mem::take(&mut state.current_cache_accum);
    batch_cache_put(key, batches);
}

/// Walk the scan plan's targetlist and qual to find which base-relation
/// attribute numbers the query actually references.
unsafe fn collect_needed_attnums(cscan: *mut pg_sys::CustomScan, natts: i32) -> Vec<i32> {
    let scan_varno: u32 = (*cscan).scan.scanrelid;
    let mut bms: *mut pg_sys::Bitmapset = std::ptr::null_mut();

    pg_sys::pull_varattnos(
        (*cscan).scan.plan.targetlist as *mut pg_sys::Node,
        scan_varno,
        &mut bms,
    );
    pg_sys::pull_varattnos(
        (*cscan).scan.plan.qual as *mut pg_sys::Node,
        scan_varno,
        &mut bms,
    );

    let first_low = pg_sys::FirstLowInvalidHeapAttributeNumber as i32;
    let mut attnums = Vec::new();
    let mut x: i32 = -1;
    loop {
        x = pg_sys::bms_next_member(bms, x);
        if x < 0 {
            break;
        }
        let attnum = x + first_low;
        if attnum > 0 && attnum <= natts {
            attnums.push(attnum);
        }
    }
    // Whole-row references show up as attnum 0, which pull_varattnos
    // encodes specially. If we got any, fall back to "all columns".
    if attnums.is_empty() {
        // Fall back: read at least one tiny column so parquet still yields
        // batches with row counts (e.g. count(*) with no Vars referenced).
        // Use the first column unconditionally.
        if natts > 0 {
            attnums.push(1);
        }
    }
    attnums.sort();
    attnums
}

unsafe fn emit_indexed_row(
    node: *mut pg_sys::CustomScanState,
    state: &mut RustScanState,
    scan_slot: *mut pg_sys::TupleTableSlot,
) -> *mut pg_sys::TupleTableSlot {
    while state.indexed_row_ref_idx < state.indexed_row_refs.len() {
        let row_ref = state.indexed_row_refs[state.indexed_row_ref_idx];
        state.indexed_row_ref_idx += 1;
        let Some(batch) = state.cached_batches.get(row_ref.batch_idx).cloned() else {
            continue;
        };
        let qual_readers =
            build_qual_readers_for_batch(&batch, &state.pg_attrs, &state.pushed_quals);
        let qual_rhs_readers =
            build_qual_rhs_readers_for_batch(&batch, &state.pg_attrs, &state.pushed_quals);
        if let Some(expr) = &state.pushed_expr {
            if !pushed_expr_pass(
                &qual_readers,
                &qual_rhs_readers,
                &state.pushed_quals,
                expr,
                row_ref.row,
            ) {
                continue;
            }
        }

        let column_readers =
            build_column_readers_for_batch(&batch, &state.pg_attrs, &state.needed_attnums);
        fill_slot_from_batch(scan_slot, row_ref.row, &column_readers);
        pg_sys::ExecStoreVirtualTuple(scan_slot);

        let ps = &(*node).ss.ps;
        let econtext = ps.ps_ExprContext;
        (*econtext).ecxt_scantuple = scan_slot;

        let qual = ps.qual;
        if !state.qual_fully_pushed && !qual.is_null() && !pg_sys::ExecQual(qual, econtext) {
            continue;
        }

        let proj_info = ps.ps_ProjInfo;
        if !proj_info.is_null() {
            return pg_sys::ExecProject(proj_info);
        }
        return scan_slot;
    }
    std::ptr::null_mut()
}

// --- ExecCustomScan: return one tuple per call -----------------------------

#[pg_guard]
unsafe extern "C-unwind" fn exec_custom_scan(
    node: *mut pg_sys::CustomScanState,
) -> *mut pg_sys::TupleTableSlot {
    let ext = node as *mut RvbbitScanStateExt;
    let state = &mut *(*ext).rust_state_ptr;
    let scan_slot = (*node).ss.ss_ScanTupleSlot;

    pg_sys::ExecClearTuple(scan_slot);
    if !state.slot_nulls_initialized {
        initialize_slot_nulls(scan_slot, state.pg_attrs.len());
        state.slot_nulls_initialized = true;
    }
    refresh_dynamic_quals(state, node);
    prepare_indexed_lookup(state);
    if state.indexed_lookup_active {
        return emit_indexed_row(node, state, scan_slot);
    }

    loop {
        // Need a current batch with rows remaining?
        if let Some(batch) = &state.current_batch {
            if state.row_in_batch < batch.num_rows() {
                // Predicate pushdown: skip rows we can prove fail the
                // pushed qual without ever materializing them. PG's
                // ExecQual still runs below as a safety net for anything
                // we couldn't recognize.
                if let Some(expr) = &state.pushed_expr {
                    if !pushed_expr_pass(
                        &state.qual_readers,
                        &state.qual_rhs_readers,
                        &state.pushed_quals,
                        expr,
                        state.row_in_batch,
                    ) {
                        state.row_in_batch += 1;
                        continue;
                    }
                }

                fill_slot_from_batch(scan_slot, state.row_in_batch, &state.column_readers);
                state.row_in_batch += 1;
                pg_sys::ExecStoreVirtualTuple(scan_slot);

                // CustomScan doesn't get ExecScan()'s wrapper, so WE
                // apply qual and projection. Without ExecQual every
                // WHERE clause is silently dropped (returns all rows).
                // Without ExecProject every column read goes to
                // tts_values[0] regardless of attnum (returns wrong cols).
                let ps = &(*node).ss.ps;
                let econtext = ps.ps_ExprContext;
                (*econtext).ecxt_scantuple = scan_slot;

                let qual = ps.qual;
                if !state.qual_fully_pushed && !qual.is_null() && !pg_sys::ExecQual(qual, econtext)
                {
                    // This row didn't pass WHERE; skip it.
                    continue;
                }

                let proj_info = ps.ps_ProjInfo;
                if !proj_info.is_null() {
                    return pg_sys::ExecProject(proj_info);
                }
                return scan_slot;
            }
        }

        // Current batch exhausted: pull the next one from the current reader.
        if !state.dynamic_quals.is_empty() && state.cache_complete {
            if state.cached_batch_idx < state.cached_batches.len() {
                state.current_batch = Some(state.cached_batches[state.cached_batch_idx].clone());
                state.cached_batch_idx += 1;
                state.row_in_batch = 0;
                rebuild_column_readers(state);
                continue;
            }
            return std::ptr::null_mut();
        }

        let pulled = if let Some(batches) = state.current_cached_batches.as_ref() {
            if state.current_cached_batch_idx < batches.len() {
                let batch = batches[state.current_cached_batch_idx].clone();
                state.current_cached_batch_idx += 1;
                Some(Ok(batch))
            } else {
                None
            }
        } else if let Some(reader) = state.current_reader.as_mut() {
            reader.next()
        } else {
            None
        };

        match pulled {
            Some(Ok(batch)) => {
                if state.current_cache_key.is_some() {
                    state.current_cache_accum.push(batch.clone());
                }
                if !state.dynamic_quals.is_empty() && !state.cache_complete {
                    state.cached_batches.push(batch.clone());
                }
                state.current_batch = Some(batch);
                state.row_in_batch = 0;
                rebuild_column_readers(state);
                continue;
            }
            Some(Err(e)) => {
                pgrx::error!("rvbbit: parquet read error: {}", e);
            }
            None => {
                if state.current_reader.is_some() {
                    finish_current_batch_cache(state);
                }
                // Current reader exhausted: open the next row group.
                state.current_batch = None;
                state.current_reader = None;
                state.current_cached_batches = None;
                state.current_cached_batch_idx = 0;
                while state.dynamic_quals.is_empty()
                    && state.rg_idx < state.row_groups.len()
                    && !row_group_may_satisfy(
                        &state.row_groups[state.rg_idx],
                        &state.pg_attrs,
                        &state.pushed_quals,
                        state.pushed_expr.as_ref(),
                    )
                {
                    state.pruned_row_groups += 1;
                    state.rg_idx += 1;
                }
                if state.rg_idx >= state.row_groups.len() {
                    if !state.dynamic_quals.is_empty() {
                        state.cache_complete = true;
                        state.cached_batch_idx = state.cached_batches.len();
                        state.indexed_lookup_dirty = false;
                        state.indexed_lookup_active = false;
                    }
                    return std::ptr::null_mut(); // EOF
                }
                let path_str = state.row_groups[state.rg_idx].path.clone();
                let path = std::path::Path::new(&path_str);
                // Projection pushdown: only read columns the query touches.
                let col_names: Vec<String> = state
                    .needed_attnums
                    .iter()
                    .map(|&attnum| state.pg_attrs[(attnum - 1) as usize].name.clone())
                    .collect();
                let cache_key = batch_cache_key(&path_str, &col_names);
                if let Some(batches) = batch_cache_get(&cache_key) {
                    state.current_cached_batches = Some(batches);
                    state.current_cached_batch_idx = 0;
                    state.rg_idx += 1;
                    continue;
                }
                let col_refs: Vec<&str> = col_names.iter().map(String::as_str).collect();
                let reader = match RowGroupReader::open_projected(path, &col_refs) {
                    Ok(r) => r,
                    Err(e) => pgrx::error!("rvbbit: opening {}: {}", path.display(), e),
                };
                state.current_reader = Some(reader);
                state.current_cache_key = Some(cache_key);
                state.current_cache_accum.clear();
                state.rg_idx += 1;
            }
        }
    }
}

fn fetch_best_row_group_paths(
    table_oid: u32,
    pg_attrs: &[PgAttr],
    pushed_plan: &PushedQualPlan,
    include_stats: bool,
) -> Result<(Vec<RowGroupEntry>, String), String> {
    if !include_stats {
        return Ok((
            fetch_row_group_paths(table_oid, include_stats, None)?,
            SCAN_LAYOUT.to_string(),
        ));
    }

    let mut best_variant: Option<(Vec<RowGroupEntry>, String, usize, bool)> = None;
    for layout in fetch_variant_layouts(table_oid)? {
        let clustered = fetch_row_group_paths(table_oid, true, Some(&layout))?;
        if clustered.is_empty() {
            continue;
        }
        let pruned = clustered
            .iter()
            .filter(|rg| {
                !row_group_may_satisfy(rg, pg_attrs, &pushed_plan.quals, pushed_plan.expr.as_ref())
            })
            .count();
        if should_use_cluster_layout(clustered.len(), pruned) {
            let kept = clustered.len().saturating_sub(pruned);
            let matches_filter = layout_matches_pushed_filter(&layout, pg_attrs, pushed_plan);
            let replace =
                best_variant
                    .as_ref()
                    .is_none_or(|(_, _, best_kept, best_matches_filter)| {
                        kept < *best_kept
                            || (kept == *best_kept && matches_filter && !*best_matches_filter)
                    });
            if replace {
                best_variant = Some((clustered, layout, kept, matches_filter));
            }
        }
    }
    if let Some((row_groups, layout, _, _)) = best_variant {
        return Ok((row_groups, layout));
    }

    Ok((
        fetch_row_group_paths(table_oid, include_stats, None)?,
        SCAN_LAYOUT.to_string(),
    ))
}

fn should_use_cluster_layout(total_groups: usize, pruned_groups: usize) -> bool {
    if total_groups == 0 || pruned_groups == 0 {
        return false;
    }
    let threshold = std::env::var("RVBBIT_CLUSTER_MIN_PRUNE_PCT")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(20)
        .min(100);
    pruned_groups * 100 >= total_groups * threshold
}

fn layout_matches_pushed_filter(
    layout: &str,
    pg_attrs: &[PgAttr],
    pushed_plan: &PushedQualPlan,
) -> bool {
    let Some(column) = layout.strip_prefix(CLUSTER_LAYOUT_PREFIX) else {
        return false;
    };
    pushed_plan.quals.iter().any(|qual| {
        let attnum = qual.attnum;
        if attnum <= 0 {
            return false;
        }
        pg_attrs
            .get((attnum - 1) as usize)
            .is_some_and(|attr| attr.name == column)
    })
}

fn fetch_variant_layouts(table_oid: u32) -> Result<Vec<String>, String> {
    let prefix = CLUSTER_LAYOUT_PREFIX.replace('\'', "''");
    let mut layouts = Vec::new();
    pgrx::Spi::connect(|client| -> Result<(), String> {
        let table = client
            .select(
                &format!(
                    "SELECT DISTINCT layout FROM rvbbit.row_group_variants \
                     WHERE table_oid = {table_oid}::oid AND layout LIKE '{prefix}%' \
                     ORDER BY layout"
                ),
                None,
                &[],
            )
            .map_err(|e| format!("SPI select variant layouts: {e}"))?;
        for row in table {
            if let Some(layout) = row
                .get::<String>(1)
                .map_err(|e| format!("SPI get variant layout: {e}"))?
            {
                layouts.push(layout);
            }
        }
        Ok(())
    })
    .map_err(|e| format!("Spi::connect variant layouts: {e}"))?;
    Ok(layouts)
}

fn fetch_row_group_paths(
    table_oid: u32,
    include_stats: bool,
    variant_layout: Option<&str>,
) -> Result<Vec<RowGroupEntry>, String> {
    let mut out = Vec::new();
    pgrx::Spi::connect(|client| -> Result<(), String> {
        let select_sql = if let Some(layout) = variant_layout {
            let layout = layout.replace('\'', "''");
            if include_stats {
                format!(
                    "SELECT path, stats::text FROM rvbbit.row_group_variants \
                     WHERE table_oid = {table_oid}::oid AND layout = '{layout}' \
                     ORDER BY rg_id"
                )
            } else {
                format!(
                    "SELECT path, NULL::text FROM rvbbit.row_group_variants \
                     WHERE table_oid = {table_oid}::oid AND layout = '{layout}' \
                     ORDER BY rg_id"
                )
            }
        } else if include_stats {
            format!(
                "SELECT path, stats::text FROM rvbbit.row_groups \
                 WHERE table_oid = {table_oid}::oid \
                 ORDER BY rg_id"
            )
        } else {
            format!(
                "SELECT path, NULL::text FROM rvbbit.row_groups \
                 WHERE table_oid = {table_oid}::oid \
                 ORDER BY rg_id"
            )
        };
        let table = client
            .select(&select_sql, None, &[])
            .map_err(|e| format!("SPI select: {e}"))?;
        for row in table {
            let path: Option<String> = row.get(1).map_err(|e| format!("SPI get: {e}"))?;
            if let Some(p) = path {
                let stats_text: Option<String> =
                    row.get(2).map_err(|e| format!("SPI get stats: {e}"))?;
                out.push(RowGroupEntry {
                    path: p,
                    stats: parse_prune_stats(stats_text.as_deref()),
                });
            }
        }
        Ok(())
    })
    .map_err(|e| format!("Spi::connect: {e}"))?;
    Ok(out)
}

fn parse_prune_stats(stats_text: Option<&str>) -> HashMap<String, PruneStats> {
    let Some(stats_text) = stats_text else {
        return HashMap::new();
    };
    let Ok(stats) = serde_json::from_str::<Vec<ColumnStats>>(stats_text) else {
        return HashMap::new();
    };
    stats
        .into_iter()
        .filter_map(|s| {
            if s.min.is_none() && s.max.is_none() && s.text_sketch_b64.is_none() {
                None
            } else {
                Some((
                    s.name,
                    PruneStats {
                        min: s.min,
                        max: s.max,
                        text_sketch_b64: s.text_sketch_b64,
                    },
                ))
            }
        })
        .collect()
}

// --- Arrow → Datum conversion ----------------------------------------------

const PG_EPOCH_OFFSET_MICROS: i64 = 946_684_800_000_000;
const PG_EPOCH_OFFSET_DAYS: i32 = 10_957;

unsafe fn fill_slot_from_batch(
    slot: *mut pg_sys::TupleTableSlot,
    row: usize,
    column_readers: &[NeededColumn],
) {
    let values = (*slot).tts_values;
    let nulls = (*slot).tts_isnull;

    for col in column_readers {
        let col_idx = col.attnum_idx;
        let (datum, was_null) = read_via(&col.reader, row);
        *values.add(col_idx) = datum;
        *nulls.add(col_idx) = was_null;
    }
}

unsafe fn initialize_slot_nulls(slot: *mut pg_sys::TupleTableSlot, n_attrs: usize) {
    let values = (*slot).tts_values;
    let nulls = (*slot).tts_isnull;
    for col_idx in 0..n_attrs {
        *values.add(col_idx) = pg_sys::Datum::from(0usize);
        *nulls.add(col_idx) = true;
    }
}

/// Resolve one cell of the current batch. Pointer dispatch is the only
/// per-row cost — no Arc<dyn Array> downcast, no schema lookup, no
/// branchy type matching against an Arrow DataType enum.
unsafe fn read_via(reader: &ColumnReader, row: usize) -> (pg_sys::Datum, bool) {
    match reader {
        ColumnReader::Missing => (pg_sys::Datum::from(0usize), true),
        ColumnReader::Int16(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                (pg_sys::Datum::from(a.value(row) as i64 as usize), false)
            }
        }
        ColumnReader::Int32(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                (pg_sys::Datum::from(a.value(row) as i64 as usize), false)
            }
        }
        ColumnReader::Date32(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                let pg_date = a.value(row) - PG_EPOCH_OFFSET_DAYS;
                (pg_sys::Datum::from(pg_date as i64 as usize), false)
            }
        }
        ColumnReader::Date32Int32(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                let pg_date = a.value(row) - PG_EPOCH_OFFSET_DAYS;
                (pg_sys::Datum::from(pg_date as i64 as usize), false)
            }
        }
        ColumnReader::Int64(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                (pg_sys::Datum::from(a.value(row) as usize), false)
            }
        }
        ColumnReader::Float32(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                (pg_sys::Datum::from(a.value(row).to_bits() as usize), false)
            }
        }
        ColumnReader::Float64(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                (pg_sys::Datum::from(a.value(row).to_bits() as usize), false)
            }
        }
        ColumnReader::Bool(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                (pg_sys::Datum::from(a.value(row) as usize), false)
            }
        }
        ColumnReader::Utf8 { arr, is_jsonb } => {
            let a = &**arr;
            if a.is_null(row) {
                return (pg_sys::Datum::from(0usize), true);
            }
            let s = a.value(row);
            if *is_jsonb {
                let mut buf = Vec::with_capacity(s.len() + 1);
                buf.extend_from_slice(s.as_bytes());
                buf.push(0);
                type CUnwindPGFn =
                    unsafe extern "C-unwind" fn(pg_sys::FunctionCallInfo) -> pg_sys::Datum;
                let jsonb_in: CUnwindPGFn = std::mem::transmute(pg_sys::jsonb_in as *const ());
                let datum = pg_sys::DirectFunctionCall1Coll(
                    Some(jsonb_in),
                    pg_sys::InvalidOid,
                    pg_sys::Datum::from(buf.as_ptr() as usize),
                );
                drop(buf);
                (datum, false)
            } else {
                let text_ptr =
                    pg_sys::cstring_to_text_with_len(s.as_ptr() as *const i8, s.len() as i32);
                (pg_sys::Datum::from(text_ptr as usize), false)
            }
        }
        ColumnReader::Binary(p) => {
            let a = &**p;
            if a.is_null(row) {
                return (pg_sys::Datum::from(0usize), true);
            }
            let bytes = a.value(row);
            let total_len = bytes.len() + 4;
            let varlena = pg_sys::palloc(total_len) as *mut u8;
            std::ptr::write_unaligned(varlena as *mut u32, (total_len as u32) << 2);
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), varlena.add(4), bytes.len());
            (pg_sys::Datum::from(varlena as usize), false)
        }
        ColumnReader::TimestampMicros(p) => {
            let a = &**p;
            if a.is_null(row) {
                (pg_sys::Datum::from(0usize), true)
            } else {
                let pg_ts = a.value(row) - PG_EPOCH_OFFSET_MICROS;
                (pg_sys::Datum::from(pg_ts as usize), false)
            }
        }
    }
}

#[allow(dead_code)]
unsafe fn arrow_to_datum_legacy(
    array: &Arc<dyn Array>,
    row: usize,
    attr: &PgAttr,
) -> (pg_sys::Datum, bool) {
    match array.data_type() {
        DataType::Int64 => {
            let v = array
                .as_any()
                .downcast_ref::<Int64Array>()
                .unwrap()
                .value(row);
            (pg_sys::Datum::from(v as usize), false)
        }
        DataType::Int32 => {
            let v = array
                .as_any()
                .downcast_ref::<Int32Array>()
                .unwrap()
                .value(row);
            let v = if attr.typoid == pg_sys::DATEOID {
                v - PG_EPOCH_OFFSET_DAYS
            } else {
                v
            };
            (pg_sys::Datum::from(v as i64 as usize), false)
        }
        DataType::Date32 => {
            let v = array
                .as_any()
                .downcast_ref::<Date32Array>()
                .unwrap()
                .value(row)
                - PG_EPOCH_OFFSET_DAYS;
            (pg_sys::Datum::from(v as i64 as usize), false)
        }
        DataType::Int16 => {
            let v = array
                .as_any()
                .downcast_ref::<arrow::array::Int16Array>()
                .unwrap()
                .value(row);
            (pg_sys::Datum::from(v as i64 as usize), false)
        }
        DataType::Float64 => {
            let v = array
                .as_any()
                .downcast_ref::<arrow::array::Float64Array>()
                .unwrap()
                .value(row);
            // PG float8 datum: bit-cast f64 → u64 → Datum.
            (pg_sys::Datum::from(v.to_bits() as usize), false)
        }
        DataType::Float32 => {
            let v = array
                .as_any()
                .downcast_ref::<arrow::array::Float32Array>()
                .unwrap()
                .value(row);
            // PG float4 datum: bit-cast f32 → u32 → Datum (low bits).
            (pg_sys::Datum::from(v.to_bits() as usize), false)
        }
        DataType::Boolean => {
            let v = array
                .as_any()
                .downcast_ref::<arrow::array::BooleanArray>()
                .unwrap()
                .value(row);
            (pg_sys::Datum::from(v as usize), false)
        }
        DataType::Utf8 => {
            let s = array
                .as_any()
                .downcast_ref::<StringArray>()
                .unwrap()
                .value(row);
            // For jsonb-typed columns, pass the parquet text directly to
            // PG's jsonb_in via DirectFunctionCall1 — ONE parse instead of
            // serde_json::from_str -> JsonB::to_string -> jsonb_in's parse.
            //
            // pgrx wraps pg_sys::jsonb_in as an "extern Rust" fn (its
            // pg_guard adapter), but DirectFunctionCall1Coll wants an
            // "extern C-unwind" fn pointer. The underlying calling
            // convention is identical (it's a PGFunction); transmute to
            // line up the ABI.
            if attr.typoid == pg_sys::JSONBOID {
                let mut buf = Vec::with_capacity(s.len() + 1);
                buf.extend_from_slice(s.as_bytes());
                buf.push(0);
                type CUnwindPGFn =
                    unsafe extern "C-unwind" fn(pg_sys::FunctionCallInfo) -> pg_sys::Datum;
                let jsonb_in: CUnwindPGFn = std::mem::transmute(pg_sys::jsonb_in as *const ());
                let datum = pg_sys::DirectFunctionCall1Coll(
                    Some(jsonb_in),
                    pg_sys::InvalidOid,
                    pg_sys::Datum::from(buf.as_ptr() as usize),
                );
                drop(buf);
                return (datum, false);
            }
            let text_ptr =
                pg_sys::cstring_to_text_with_len(s.as_ptr() as *const i8, s.len() as i32);
            (pg_sys::Datum::from(text_ptr as usize), false)
        }
        DataType::Binary => {
            // Phase 4c: parquet stores PG jsonb body bytes (no header).
            // Reconstruct a 4-byte-header varlena and return as datum.
            // The destination column is expected to be jsonb (or bytea).
            let bytes = array
                .as_any()
                .downcast_ref::<BinaryArray>()
                .unwrap()
                .value(row);
            let total_len = bytes.len() + 4; // VARHDRSZ
            let varlena = pg_sys::palloc(total_len) as *mut u8;
            std::ptr::write_unaligned(varlena as *mut u32, (total_len as u32) << 2);
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), varlena.add(4), bytes.len());
            (pg_sys::Datum::from(varlena as usize), false)
        }
        DataType::Timestamp(_, _) => {
            // Arrow Timestamp(Microsecond) = micros since UNIX epoch.
            // PG timestamptz = micros since 2000-01-01 UTC.
            let v = array
                .as_any()
                .downcast_ref::<TimestampMicrosecondArray>()
                .unwrap()
                .value(row);
            let pg_ts = v - PG_EPOCH_OFFSET_MICROS;
            (pg_sys::Datum::from(pg_ts as usize), false)
        }
        other => {
            pgrx::error!(
                "rvbbit: unsupported arrow type {:?} for column '{}' (oid {})",
                other,
                attr.name,
                attr.typoid.to_u32()
            );
        }
    }
}

// --- EndCustomScan, ReScan, Explain ---------------------------------------

#[pg_guard]
unsafe extern "C-unwind" fn end_custom_scan(node: *mut pg_sys::CustomScanState) {
    let ext = node as *mut RvbbitScanStateExt;
    if !(*ext).rust_state_ptr.is_null() {
        // Reclaim Rust-owned state. Vec/Option drops cleanly.
        drop(Box::from_raw((*ext).rust_state_ptr));
        (*ext).rust_state_ptr = std::ptr::null_mut();
    }
}

#[pg_guard]
unsafe extern "C-unwind" fn rescan_custom_scan(node: *mut pg_sys::CustomScanState) {
    let ext = node as *mut RvbbitScanStateExt;
    if (*ext).rust_state_ptr.is_null() {
        return;
    }
    let state = &mut *(*ext).rust_state_ptr;
    state.rg_idx = 0;
    state.pruned_row_groups = 0;
    state.current_reader = None;
    state.current_cached_batches = None;
    state.current_cached_batch_idx = 0;
    state.current_cache_key = None;
    state.current_cache_accum.clear();
    state.current_batch = None;
    state.row_in_batch = 0;
    state.cached_batch_idx = 0;
    state.indexed_row_refs.clear();
    state.indexed_row_ref_idx = 0;
    state.indexed_lookup_dirty = true;
    state.indexed_lookup_active = false;
    state.column_readers.clear();
    state.qual_readers.clear();
    state.qual_rhs_readers.clear();
    state.dynamic_quals_dirty = true;
}

#[pg_guard]
unsafe extern "C-unwind" fn explain_custom_scan(
    node: *mut pg_sys::CustomScanState,
    _ancestors: *mut pg_sys::List,
    es: *mut pg_sys::ExplainState,
) {
    let ext = node as *mut RvbbitScanStateExt;
    if (*ext).rust_state_ptr.is_null() {
        return;
    }
    let state = &*(*ext).rust_state_ptr;
    let label = std::ffi::CString::new("Rvbbit Layout").unwrap();
    let value = std::ffi::CString::new(state.row_group_layout.as_str()).unwrap();
    pg_sys::ExplainPropertyText(label.as_ptr(), value.as_ptr(), es);
    let label = std::ffi::CString::new("Row Groups").unwrap();
    pg_sys::ExplainPropertyInteger(
        label.as_ptr(),
        std::ptr::null(),
        state.row_groups.len() as i64,
        es,
    );
    let label = std::ffi::CString::new("Pruned Row Groups").unwrap();
    pg_sys::ExplainPropertyInteger(
        label.as_ptr(),
        std::ptr::null(),
        state.pruned_row_groups as i64,
        es,
    );
}
