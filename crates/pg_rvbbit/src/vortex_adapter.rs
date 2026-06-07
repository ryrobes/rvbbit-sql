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

use std::collections::HashSet;
use std::path::Path;
use std::sync::Arc;

use arrow::array::{Array, StructArray};
use arrow::compute::cast;
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
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

use crate::scan_types::{CmpOp, FilterRepr, IntWidth, LitRepr, QualRepr};

/// A single `.vortex` row-group reader. Phase 0 materializes the whole row group
/// into one batch up front; `next_batch` yields it once, then `None`.
pub(crate) struct VortexRgReader {
    batch: Option<RecordBatch>,
}

/// Open a `.vortex` row-group file and prepare it for reading.
///
/// `projection` lists the top-level column names to read (output columns + WHERE/qual
/// columns — the native scan's `needed_attnums` set). When non-empty it is pushed into
/// Vortex's `ScanBuilder::with_projection` via `select(names, root())`, so only those
/// columns are decoded — matching parquet's `open_projected`. Empty = read all columns.
/// The native tuple-fill matches batch columns by NAME, so projection order is irrelevant.
///
/// `timestamp_cols` lists the columns the native scan expects as PG timestamp/timestamptz
/// (typoid 1114/1184). Vortex stores those as Int64 unix-epoch micros; the conversion
/// re-casts them to Arrow `Timestamp(Microsecond, UTC)` so the native tuple-fill
/// (`ColumnReader::Int64`-vs-Timestamp dispatch) sees the type it expects.
///
/// `filter`, when present, is pushed into `ScanBuilder::with_filter` so Vortex evaluates
/// the predicate at the source (zone-map pruning + compute-over-compressed). It is always
/// safe because the native scan re-applies the full `pushed_quals` to the returned batch —
/// the pushed filter only needs to be *implied by* those quals (it returns a row superset).
///
/// Runs the async Vortex open + scan on rvbbit's shared runtime.
pub(crate) fn open_vortex_for_scan(
    path: &Path,
    projection: &[String],
    timestamp_cols: &[String],
    filter: Option<VortexPushedFilter>,
) -> Result<VortexRgReader, String> {
    let ts: HashSet<&str> = timestamp_cols.iter().map(String::as_str).collect();
    crate::df::with_lance_runtime(move |rt| {
        rt.block_on(async move {
            let session = VortexSession::default().with_tokio();
            let file = session
                .open_options()
                .open_path(path)
                .await
                .map_err(|e| format!("vortex open {}: {e}", path.display()))?;
            let mut scan = file
                .scan()
                .map_err(|e| format!("vortex scan {}: {e}", path.display()))?;
            if !projection.is_empty() {
                // Projection pushdown: decode only the requested top-level fields,
                // expressed as `select([names], root())` over the file's struct root.
                let names: Vec<&str> = projection.iter().map(String::as_str).collect();
                scan = scan.with_projection(expr::select(names, expr::root()));
            }
            if let Some(f) = filter {
                // Filter pushdown: Vortex evaluates the predicate at the source
                // (zone-map prune + compute-over-compressed). The native scan still
                // re-applies pushed_quals, so this only needs to be a row superset.
                scan = scan.with_filter(f.expr);
            }
            let array: ArrayRef = scan
                .into_array_stream()
                .map_err(|e| format!("vortex stream {}: {e}", path.display()))?
                .read_all()
                .await
                .map_err(|e| format!("vortex read_all {}: {e}", path.display()))?;
            let batch = vortex_array_to_record_batch(array, &ts)?;
            Ok::<_, String>(VortexRgReader { batch: Some(batch) })
        })
    })
}

/// Pull the next Arrow batch. Phase 0: one whole-row-group batch, then `None`.
pub(crate) fn next_batch(reader: &mut VortexRgReader) -> Option<Result<RecordBatch, String>> {
    reader.batch.take().map(Ok)
}

/// Map a Vortex-preferred Arrow encoding to the plain type the native reader
/// (`make_reader_for`) can downcast, or `None` if it's already plain. View strings
/// and `LargeUtf8` decode to `Utf8`; view/large binary to `Binary`; a dictionary
/// decodes to (the canonical form of) its value type. Returns `None` for everything
/// `make_reader_for` already handles, so the column passes through untouched.
fn canonical_native_type(dt: &DataType) -> Option<DataType> {
    match dt {
        DataType::Utf8View | DataType::LargeUtf8 => Some(DataType::Utf8),
        DataType::BinaryView | DataType::LargeBinary => Some(DataType::Binary),
        DataType::Dictionary(_, value) => {
            Some(canonical_native_type(value).unwrap_or_else(|| value.as_ref().clone()))
        }
        _ => None,
    }
}

/// Canonicalize a Vortex (struct) array into an Arrow `RecordBatch` whose column
/// types match what the native tuple-fill (`ColumnReader`) expects.
///
/// Two re-casts (the Phase 3 gotchas):
///   * Vortex's preferred string encoding is `Utf8View`/`LargeUtf8`; the native
///     reader only handles `StringArray` (`Utf8`), so view/large strings are cast
///     down to `Utf8`.
///   * Timestamp columns come back as `Int64` (unix-epoch micros). For each column
///     named in `timestamp_cols` we cast `Int64 -> Timestamp(Microsecond, UTC)` so
///     the native reader hits its Timestamp arm (the epoch handling lives there,
///     identical to the parquet path — no offset math here).
///
/// All other columns pass through untouched. Casts are zero-copy where Arrow allows
/// it and only run on the columns that need them.
fn vortex_array_to_record_batch(
    array: ArrayRef,
    timestamp_cols: &HashSet<&str>,
) -> Result<RecordBatch, String> {
    #[allow(deprecated)]
    let arrow = array
        .into_arrow_preferred()
        .map_err(|e| format!("vortex->arrow: {e}"))?;
    let sa = arrow
        .as_any()
        .downcast_ref::<StructArray>()
        .ok_or_else(|| "vortex root array is not a struct".to_string())?
        .clone();
    let batch = RecordBatch::from(sa);
    let schema = batch.schema();

    let mut fields: Vec<Field> = Vec::with_capacity(batch.num_columns());
    let mut columns: Vec<arrow::array::ArrayRef> = Vec::with_capacity(batch.num_columns());
    let mut changed = false;
    for (idx, field) in schema.fields().iter().enumerate() {
        let col = batch.column(idx);
        let target: Option<DataType> = if let Some(t) = canonical_native_type(field.data_type()) {
            // Vortex's preferred encodings (view strings/binary, dictionary) that
            // the native reader doesn't downcast — decode them to the plain type.
            Some(t)
        } else if matches!(field.data_type(), DataType::Int64)
            && timestamp_cols.contains(field.name().as_str())
        {
            // Vortex stores timestamps as Int64 unix-epoch micros; native expects
            // a TimestampMicrosecondArray (epoch handling lives in its Timestamp arm).
            Some(DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into())))
        } else {
            None
        };
        match target {
            Some(dt) => {
                let casted = cast(col, &dt)
                    .map_err(|e| format!("vortex canonicalize column {}: {e}", field.name()))?;
                fields.push(Field::new(field.name(), dt, field.is_nullable()));
                columns.push(casted);
                changed = true;
            }
            None => {
                fields.push(field.as_ref().clone());
                columns.push(col.clone());
            }
        }
    }
    if !changed {
        return Ok(batch);
    }
    RecordBatch::try_new(Arc::new(Schema::new(fields)), columns)
        .map_err(|e| format!("vortex canonicalized batch: {e}"))
}

/// Phase 0 smoke probe (exit criterion): open a `.vortex` file via the embedded
/// reader and report row/batch/column counts. Lets us prove on bench that the
/// in-process read path works against a real compacted `.vortex` row group.
#[pg_extern]
fn vortex_native_probe(path: &str) -> pgrx::JsonB {
    let mut reader = match open_vortex_for_scan(Path::new(path), &[], &[], None) {
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
pub(crate) struct VortexPushedFilter {
    pub(crate) expr: Expression,
}

/// Translate a lowered `FilterRepr` into a Vortex filter expression, or `None` if any
/// node can't be faithfully expressed (→ no pushdown; PG residual quals remain).
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
        LitRepr::Int(x, w) => Some(int_lit(*x, *w)),
        LitRepr::F64(x) => Some(expr::lit(*x)),
        LitRepr::Bool(x) => Some(expr::lit(*x)),
        LitRepr::Text(s) => Some(expr::lit(s.as_str())),
        LitRepr::IntSet(..) | LitRepr::F64Set(_) | LitRepr::TextSet(_) => None,
    }
}

/// Build a width-matched Vortex integer literal. The lowering guarantees the value
/// fits the width, so the `as` narrowing is lossless and avoids the i32/i64 DType clash.
fn int_lit(x: i64, w: IntWidth) -> Expression {
    match w {
        IntWidth::I16 => expr::lit(x as i16),
        IntWidth::I32 => expr::lit(x as i32),
        IntWidth::I64 => expr::lit(x),
    }
}

/// `col IN (a,b,...)` → `eq OR eq OR ...` (Vortex has no native IN). Empty set → None.
fn set_membership(q: &QualRepr) -> Option<Expression> {
    let eqs: Vec<Expression> = match &q.val {
        LitRepr::IntSet(xs, w) => xs
            .iter()
            .map(|x| expr::eq(expr::col(q.col.as_str()), int_lit(*x, *w)))
            .collect(),
        LitRepr::F64Set(xs) => xs.iter().map(|x| expr::eq(expr::col(q.col.as_str()), expr::lit(*x))).collect(),
        LitRepr::TextSet(xs) => xs.iter().map(|x| expr::eq(expr::col(q.col.as_str()), expr::lit(x.as_str()))).collect(),
        _ => return None,
    };
    eqs.into_iter().reduce(expr::or)
}

#[cfg(test)]
mod tests {
    use super::translate;
    use crate::scan_types::{CmpOp, FilterRepr, IntWidth, LitRepr, QualRepr};

    fn qual(col: &str, op: CmpOp, val: LitRepr) -> FilterRepr {
        FilterRepr::Qual(QualRepr { col: col.into(), op, val })
    }

    #[test]
    fn eq_int_pushes() {
        assert!(translate(&qual("a", CmpOp::Eq, LitRepr::Int(5, IntWidth::I32))).is_some());
        assert!(translate(&qual("a", CmpOp::Eq, LitRepr::Int(5, IntWidth::I64))).is_some());
    }

    #[test]
    fn range_between_via_and_pushes() {
        let f = FilterRepr::And(vec![
            qual("a", CmpOp::Ge, LitRepr::Int(1, IntWidth::I32)),
            qual("a", CmpOp::Lt, LitRepr::Int(9, IntWidth::I32)),
        ]);
        assert!(translate(&f).is_some());
    }

    #[test]
    fn in_set_and_like_push() {
        assert!(
            translate(&qual("a", CmpOp::In, LitRepr::IntSet(vec![1, 2, 3], IntWidth::I16)))
                .is_some()
        );
        assert!(translate(&qual("s", CmpOp::Like, LitRepr::Text("foo%".into()))).is_some());
    }

    #[test]
    fn empty_in_does_not_push() {
        assert!(
            translate(&qual("a", CmpOp::In, LitRepr::IntSet(vec![], IntWidth::I64))).is_none()
        );
    }

    #[test]
    fn mismatched_op_value_does_not_push() {
        // Eq with a set value is not expressible → None (and an empty And → None).
        assert!(translate(&qual(
            "a",
            CmpOp::Eq,
            LitRepr::IntSet(vec![1], IntWidth::I64)
        ))
        .is_none());
        assert!(translate(&FilterRepr::And(vec![])).is_none());
    }
}
