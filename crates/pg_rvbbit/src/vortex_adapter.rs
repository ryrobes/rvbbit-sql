//! Vortex embedded-reader adapter — Phase 0 of `docs/NATIVE_VORTEX_PLAN.md`.
//!
//! This is the ONLY module that imports `vortex::*` for the *read* path, so the
//! pre-1.0 Vortex library-API churn is contained here — when we bump `vortex`,
//! only this file should need to change. Everything else (the native CustomScan,
//! the router) calls through this surface.
//!
//! Vortex 0.73 has **no** sync `into_record_batch_reader`: the scan terminators
//! are async (`into_array_stream` yields Vortex `ArrayRef`s, which canonicalize to
//! Arrow). We drive that on rvbbit's shared tokio runtime (`crate::df::with_lance_runtime`)
//! — the same pattern `df.rs`/`compact.rs` already use for Vortex/Lance. The
//! conversion mirrors what `vortex-datafusion` does internally.
//!
//! Phase 0 reads a whole row-group into one canonical Arrow batch (projection +
//! filter pushdown and per-chunk streaming are later phases) and proves the
//! embedded read path compiles AND runs against a real `.vortex` file via the
//! `rvbbit.vortex_native_probe(path)` smoke function.

use std::path::Path;

use arrow::array::{Array, StructArray};
use arrow::record_batch::RecordBatch;
use pgrx::prelude::*;

// Phase 3 switches this to the non-deprecated `session.arrow().execute_arrow(None, ctx)`
// (alongside the Utf8View->Utf8 + Int64->Timestamp canonicalization); for the Phase 0
// read proof the self-contained `into_arrow_preferred()` is enough.
#[allow(deprecated)]
use vortex::array::arrow::IntoArrowArray;
use vortex::array::stream::ArrayStreamExt;
use vortex::array::ArrayRef;
use vortex::file::OpenOptionsSessionExt;
use vortex::io::session::RuntimeSessionExt;
use vortex::session::VortexSession;
use vortex::VortexSessionDefault;

use vortex::expr::{self, Expression};

use crate::scan_types::{CmpOp, FilterRepr, LitRepr, QualRepr};

/// A single `.vortex` row-group reader. Phase 0 materializes the whole row group
/// into one batch up front; `next_batch` yields it once, then `None`.
pub(crate) struct VortexRgReader {
    batch: Option<RecordBatch>,
}

/// Open a `.vortex` row-group file and prepare it for reading.
///
/// Phase 0 reads ALL columns (no projection/filter pushdown yet — the native scan
/// still projects via its existing needed-attnums path). Runs the async Vortex open
/// + scan on rvbbit's shared runtime.
pub(crate) fn open_vortex_projected(path: &Path) -> Result<VortexRgReader, String> {
    crate::df::with_lance_runtime(|rt| {
        rt.block_on(async {
            let session = VortexSession::default().with_tokio();
            let file = session
                .open_options()
                .open_path(path)
                .await
                .map_err(|e| format!("vortex open {}: {e}", path.display()))?;
            let array: ArrayRef = file
                .scan()
                .map_err(|e| format!("vortex scan {}: {e}", path.display()))?
                .into_array_stream()
                .map_err(|e| format!("vortex stream {}: {e}", path.display()))?
                .read_all()
                .await
                .map_err(|e| format!("vortex read_all {}: {e}", path.display()))?;
            let batch = vortex_array_to_record_batch(array)?;
            Ok::<_, String>(VortexRgReader { batch: Some(batch) })
        })
    })
}

/// Pull the next Arrow batch. Phase 0: one whole-row-group batch, then `None`.
pub(crate) fn next_batch(reader: &mut VortexRgReader) -> Option<Result<RecordBatch, String>> {
    reader.batch.take().map(Ok)
}

/// Canonicalize a Vortex (struct) array into an Arrow `RecordBatch`.
///
/// TODO(Phase 3): handle `Utf8View -> Utf8` and the `Int64 -> Timestamp` re-cast
/// here so the native tuple-fill (`ColumnReader`) sees the types it expects.
fn vortex_array_to_record_batch(array: ArrayRef) -> Result<RecordBatch, String> {
    #[allow(deprecated)]
    let arrow = array
        .into_arrow_preferred()
        .map_err(|e| format!("vortex->arrow: {e}"))?;
    let sa = arrow
        .as_any()
        .downcast_ref::<StructArray>()
        .ok_or_else(|| "vortex root array is not a struct".to_string())?
        .clone();
    Ok(RecordBatch::from(sa))
}

/// Phase 0 smoke probe (exit criterion): open a `.vortex` file via the embedded
/// reader and report row/batch/column counts. Lets us prove on bench that the
/// in-process read path works against a real compacted `.vortex` row group.
#[pg_extern]
fn vortex_native_probe(path: &str) -> pgrx::JsonB {
    let mut reader = match open_vortex_projected(Path::new(path)) {
        Ok(r) => r,
        Err(e) => return pgrx::JsonB(serde_json::json!({ "ok": false, "error": e })),
    };
    let mut rows: i64 = 0;
    let mut batches: i64 = 0;
    let mut cols: i64 = 0;
    while let Some(res) = next_batch(&mut reader) {
        match res {
            Ok(b) => {
                rows += b.num_rows() as i64;
                cols = b.num_columns() as i64;
                batches += 1;
            }
            Err(e) => return pgrx::JsonB(serde_json::json!({ "ok": false, "error": e })),
        }
    }
    pgrx::JsonB(serde_json::json!({ "ok": true, "rows": rows, "batches": batches, "cols": cols }))
}

// ── Phase 2: PG-qual → Vortex expression translation ───────────────────────
// custom_scan (Phase 3) lowers its pushable quals into a PG-free `FilterRepr`; we
// map that to a `vortex::expr::Expression` to push into `ScanBuilder::with_filter`.
// Total + Option-safe: any combo we can't faithfully express returns None, so the
// scan falls back to no pushdown (the residual Postgres quals stay the correctness
// gate). Mirrors what `vortex-datafusion`'s convert/exprs.rs does.

/// A pushable filter ready to hand to Vortex's `ScanBuilder::with_filter`.
#[allow(dead_code)] // consumed by the native+vortex reader in Phase 3
pub(crate) struct VortexPushedFilter {
    pub(crate) expr: Expression,
}

/// Translate a lowered `FilterRepr` into a Vortex filter expression, or `None` if any
/// node can't be faithfully expressed (→ no pushdown; PG residual quals remain).
#[allow(dead_code)] // consumed by the native+vortex reader in Phase 3
pub(crate) fn translate(filter: &FilterRepr) -> Option<VortexPushedFilter> {
    filter_to_expr(filter).map(|expr| VortexPushedFilter { expr })
}

fn filter_to_expr(f: &FilterRepr) -> Option<Expression> {
    match f {
        FilterRepr::Qual(q) => qual_to_expr(q),
        FilterRepr::And(xs) => fold(xs, expr::and),
        FilterRepr::Or(xs) => fold(xs, expr::or),
    }
}

fn fold(xs: &[FilterRepr], combine: fn(Expression, Expression) -> Expression) -> Option<Expression> {
    let mut acc: Option<Expression> = None;
    for x in xs {
        let e = filter_to_expr(x)?;
        acc = Some(match acc {
            None => e,
            Some(prev) => combine(prev, e),
        });
    }
    acc // None for an empty And/Or (degenerate — don't push)
}

fn qual_to_expr(q: &QualRepr) -> Option<Expression> {
    match q.op {
        CmpOp::In => set_membership(q),
        CmpOp::Like => match &q.val {
            LitRepr::Text(p) => Some(expr::like(expr::col(q.col.as_str()), expr::lit(p.as_str()))),
            _ => None,
        },
        CmpOp::Eq => Some(expr::eq(expr::col(q.col.as_str()), scalar_lit(&q.val)?)),
        CmpOp::Lt => Some(expr::lt(expr::col(q.col.as_str()), scalar_lit(&q.val)?)),
        CmpOp::Le => Some(expr::lt_eq(expr::col(q.col.as_str()), scalar_lit(&q.val)?)),
        CmpOp::Gt => Some(expr::gt(expr::col(q.col.as_str()), scalar_lit(&q.val)?)),
        CmpOp::Ge => Some(expr::gt_eq(expr::col(q.col.as_str()), scalar_lit(&q.val)?)),
    }
}

fn scalar_lit(v: &LitRepr) -> Option<Expression> {
    match v {
        LitRepr::I64(x) => Some(expr::lit(*x)),
        LitRepr::F64(x) => Some(expr::lit(*x)),
        LitRepr::Bool(x) => Some(expr::lit(*x)),
        LitRepr::Text(s) => Some(expr::lit(s.as_str())),
        LitRepr::I64Set(_) | LitRepr::F64Set(_) | LitRepr::TextSet(_) => None,
    }
}

/// `col IN (a,b,...)` → `eq OR eq OR ...` (Vortex has no native IN). Empty set → None.
fn set_membership(q: &QualRepr) -> Option<Expression> {
    let eqs: Vec<Expression> = match &q.val {
        LitRepr::I64Set(xs) => xs.iter().map(|x| expr::eq(expr::col(q.col.as_str()), expr::lit(*x))).collect(),
        LitRepr::F64Set(xs) => xs.iter().map(|x| expr::eq(expr::col(q.col.as_str()), expr::lit(*x))).collect(),
        LitRepr::TextSet(xs) => xs.iter().map(|x| expr::eq(expr::col(q.col.as_str()), expr::lit(x.as_str()))).collect(),
        _ => return None,
    };
    eqs.into_iter().reduce(expr::or)
}

#[cfg(test)]
mod tests {
    use super::translate;
    use crate::scan_types::{CmpOp, FilterRepr, LitRepr, QualRepr};

    fn qual(col: &str, op: CmpOp, val: LitRepr) -> FilterRepr {
        FilterRepr::Qual(QualRepr { col: col.into(), op, val })
    }

    #[test]
    fn eq_i64_pushes() {
        assert!(translate(&qual("a", CmpOp::Eq, LitRepr::I64(5))).is_some());
    }

    #[test]
    fn range_between_via_and_pushes() {
        let f = FilterRepr::And(vec![
            qual("a", CmpOp::Ge, LitRepr::I64(1)),
            qual("a", CmpOp::Lt, LitRepr::I64(9)),
        ]);
        assert!(translate(&f).is_some());
    }

    #[test]
    fn in_set_and_like_push() {
        assert!(translate(&qual("a", CmpOp::In, LitRepr::I64Set(vec![1, 2, 3]))).is_some());
        assert!(translate(&qual("s", CmpOp::Like, LitRepr::Text("foo%".into()))).is_some());
    }

    #[test]
    fn empty_in_does_not_push() {
        assert!(translate(&qual("a", CmpOp::In, LitRepr::I64Set(vec![]))).is_none());
    }

    #[test]
    fn mismatched_op_value_does_not_push() {
        // Eq with a set value is not expressible → None (and an empty And → None).
        assert!(translate(&qual("a", CmpOp::Eq, LitRepr::I64Set(vec![1]))).is_none());
        assert!(translate(&FilterRepr::And(vec![])).is_none());
    }
}

