//! Unified Inference Plane — model orchestration / lifecycle / ergonomics.
//!
//! SQL-first surface bringing the model subsystem to parity with the rest of
//! rvbbit: lifecycle (cancel/disable/drop/reap), versioning + monitoring,
//! declarative ergonomics (validate/infer), Warren-ified train+serve, and LLM
//! distillation. Pure SQL/PLpgSQL in `sql/model_orchestration.sql`.
//! See docs/MODELS_UNIFIED_PLAN.md.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/model_orchestration.sql",
    name = "model_orchestration",
    requires = ["rvbbit_bootstrap", "model_studio"]
);
