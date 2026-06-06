//! PG-free, plain-Rust mirror of the *pushable* subset of a scan filter — Phase 2
//! of `docs/NATIVE_VORTEX_PLAN.md`.
//!
//! `custom_scan.rs` owns the real `PushedQual`/`PushExpr` tree (which carries PG
//! attnums + `pg_sys` types). To translate it into a Vortex expression we don't
//! want `vortex_adapter` to depend on those PG types, nor `custom_scan` to depend
//! on `vortex::*`. So `custom_scan` LOWERS its private tree into this neutral
//! `FilterRepr` (resolving attnum → column name, keeping only pushable ops/values),
//! and `vortex_adapter::translate` maps `FilterRepr` → `vortex::expr`. This keeps
//! Vortex isolated to one module AND makes the translator unit-testable with no PG.
//!
//! Lowering is conservative (V1): if ANY node isn't pushable, `custom_scan` produces
//! no `FilterRepr` at all and the whole predicate stays a Postgres residual qual.
//! So everything that reaches here is already known-pushable; the residual quals are
//! still re-checked by PG, which is the correctness backstop.

/// A pushable comparison operator. PG `ILIKE`/`NOT LIKE`/`NOT ILIKE`, regex, modulo,
/// `IS [NOT] NULL`, and column-vs-column are intentionally absent — they are not
/// pushed (kept as residual quals).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum CmpOp {
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
    In,
    /// Case-sensitive SQL LIKE only (FSST `like` is case-sensitive; ILIKE not pushed).
    Like,
}

/// A pushable literal. Sets back `IN`; scalars back the comparisons / `LIKE`.
/// (PG NULL, bool-set, and column refs are not pushable → omitted.)
#[derive(Clone, PartialEq, Debug)]
pub(crate) enum LitRepr {
    I64(i64),
    F64(f64),
    Bool(bool),
    Text(String),
    I64Set(Vec<i64>),
    F64Set(Vec<f64>),
    TextSet(Vec<String>),
}

/// One `column <op> literal` predicate, with the column already resolved to a name.
#[derive(Clone, PartialEq, Debug)]
pub(crate) struct QualRepr {
    pub col: String,
    pub op: CmpOp,
    pub val: LitRepr,
}

/// A boolean tree of pushable quals — mirrors `PushExpr` minus the PG indirection.
#[derive(Clone, PartialEq, Debug)]
pub(crate) enum FilterRepr {
    Qual(QualRepr),
    And(Vec<FilterRepr>),
    Or(Vec<FilterRepr>),
}
