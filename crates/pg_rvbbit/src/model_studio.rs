//! Model Studio: SQL-native model evaluation (predictions-vs-actuals).
//!
//! Adds `rvbbit.ml_evaluations` + `rvbbit.evaluate_model(model, eval_sql, ...)`
//! on top of the existing model lifecycle (ml_models / ml_training_runs / the
//! auto-generated predict_<model> operator). Pure SQL/PLpgSQL kept in
//! `sql/model_studio.sql` so it is psql-loadable and compiled into the
//! extension here. See docs/MODEL_STUDIO_PLAN.md.

use pgrx::extension_sql_file;

extension_sql_file!(
    "../sql/model_studio.sql",
    name = "model_studio",
    requires = ["rvbbit_bootstrap"]
);
