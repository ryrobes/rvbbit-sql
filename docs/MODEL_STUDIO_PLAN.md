# Model Studio — SQL-native train / test / observe

Status: **design**. Date: 2026-06-03.

A rvbbit-lens "Model Studio" window for the SQL-native predictive-model system:
create a model from a `SELECT`, **evaluate it against a labeled query** (live
confusion / residuals), run ad-hoc predictions, and observe training runs +
per-prediction receipts — all as a thin, explorable layer over SQL that runs
identically in psql / DataGrip.

---

## 1. The capability is real (verified)

rvbbit already has a complete SQL-native model lifecycle (`catalog.rs`
~1922-2405), confirmed live + in e2e:

- **Create from SQL** — `rvbbit.train_model(model_name, source_sql,
  target_column, task, feature_schema, training_opts)` enqueues into
  `rvbbit.ml_models` + `rvbbit.ml_training_runs`. The training set *is* SQL:
  `source_sql` is the `SELECT`, `target_column` the label, `feature_schema` the
  `[{name,type}]` features, `training_opts` the hyperparameters.
- **Train → register** — the external `capabilities/tools/rvbbit-trainer`
  (sklearn; ColumnTransformer + RF/ExtraTrees/Logistic/Ridge; real holdout
  split) claims a run (`claim_model_training_run`, `FOR UPDATE SKIP LOCKED`),
  fits, then `complete_model_training` → `register_trained_model` registers a
  serving backend **and auto-generates a `predict_<model>(row jsonb)` SQL
  operator**.
- **Predict is plain SQL** — `SELECT rvbbit.predict_<model>(to_jsonb(t)) FROM t`
  (verified live: `rvbbit.wine_quality('{…}'::jsonb)` → `5`). Because the
  predictor is an ordinary operator, every call writes a `rvbbit.receipts` row.
- **Tested** — e2e step `ml / sql_trained_bigfoot_classifier`
  (`bench/e2e_realworld.py:1077-1317`) trains a RF classifier from a `SELECT`
  over the bigfoot table and asserts the model goes `active` + predictions
  return. (Optional; skips without the trainer + host CSV. No pytest yet.)

Tasks (CHECK enum): `classification, regression, tabular_classification,
tabular_regression, forecasting, anomaly, survival, causal, embedding, rerank,
custom` — only tabular cls/reg has a worker today.

## 2. Observability spine (reuse, no new code)

| Read | What it gives |
|---|---|
| `rvbbit.ml_model_status` (view) | model row + latest run joined: status, latest_run_status, worker, error, queue/start/finish timestamps |
| `rvbbit.ml_training_runs` | per-run queue/history: status, worker_id, error, timings, source_sql, metrics |
| `rvbbit.ml_models.metrics` jsonb | holdout scorecard (accuracy/f1/balanced_acc or mae/rmse/r2 + estimator, train/test rows, class_labels) |
| `rvbbit.ml_models.feature_schema` | `[{name,type}]` → auto-generated "try a row" form |
| `rvbbit.receipts` (operator = predict op) | per-prediction log: inputs, parsed output, latency_ms, cost_usd, query_id |
| `rvbbit.judgment_stats('predict_<model>')` | prediction volume / latency / cost rollup |
| `rvbbit.backend_health` | serving sidecar reachability / latency |

**status enums:** model `{queued,running,active,failed,disabled,dropped,registered}`;
run `{queued,running,completed,failed,cancelled}`.

## 3. The one gap worth filling: model evaluation

Today the only evaluation is a one-shot holdout *inside the trainer at fit time*.
There is no SQL primitive to run a model over a labeled query and compare
predictions to actuals — no confusion / residuals / accuracy-over-time. That
predictions-vs-actuals loop is the "test it / iterate" core of the studio.

### New: `rvbbit.evaluate_model` + `rvbbit.ml_evaluations`

A first-class, recorded, re-runnable evaluation object (mirrors how `train_model`
records training), built in plpgsql over the existing predict operator — so it
runs in psql too:

```sql
rvbbit.evaluate_model(
  model_name text,
  eval_sql   text,                 -- SELECT yielding features + the true label
  label_column text DEFAULT NULL,  -- defaults to the model's target_column
  eval_name  text DEFAULT NULL,
  opts       jsonb DEFAULT '{}'
) RETURNS uuid                      -- eval_id
```

It resolves the model's `operator_name` + `task`, runs
`SELECT <label>, rvbbit.predict_<model>(to_jsonb(_e)) FROM (eval_sql) _e`, and
computes:

- **classification** → `n`, `accuracy`, `labels[]`, `confusion` (`[{actual,
  predicted,n}]`).
- **regression** → `n`, `rmse`, `mae`, `r2`, a residual sample.

…recording the result into `rvbbit.ml_evaluations(eval_id, model_name, task,
eval_name, eval_sql, label_column, n_rows, metrics jsonb, status, error,
created_at)`. Because it calls the predict operator, **every evaluation also
populates receipts** — the eval is itself observable. The whole computation is
plain SQL the UI can show + copy.

## 4. Model Studio window (rvbbit-lens) — full studio

Clone the `RoutingWindow` tab-host (hasRvbbit gate, poll/refresh, active-border
tabs); reuse the `RoutingTrainTab` precedent. Tabs:

- **Models** — list from `ml_model_status` (status badge, task, metric summary,
  trained_at, predict-operator name). Select → detail.
- **Train** — `SqlEditor` for `source_sql`; target/task(11-enum)/estimator +
  `training_opts` JSON editor; **Train** → `SELECT rvbbit.train_model(…)`; live
  run history from `ml_training_runs`. *Caveat:* training only progresses if a
  `rvbbit-trainer` worker is claiming runs (see §6) — the tab surfaces queued
  state honestly.
- **Test / Evaluate** *(centerpiece)* — an `eval_sql`; **Evaluate** →
  `rvbbit.evaluate_model(…)`; render a **live confusion matrix** (classification)
  or **residual scatter + RMSE/R²** (regression) via `ChartView`/`instruments`;
  compare against the training-time holdout `metrics`; history of evaluations
  from `ml_evaluations`.
- **Predict** — auto-generate a "try a row" form from `feature_schema`; call
  `predict_<model>(to_jsonb(row))`; show parsed prediction + confidence bars
  (from `scores`); slider a numeric feature → prediction updates (sensitivity);
  "batch predict over a table" = one `SELECT`.
- **Observe** — `judgment_stats('predict_<model>')` (volume/latency/cost) +
  per-prediction `OperatorReceiptTimeline` (free, predictions are operators) +
  serving health.

### Reuse map

`RoutingWindow` (tab host), `RoutingTrainTab`/`route-training.ts` (train+observe
precedent), `SqlEditor`, `ResultGrid`, `instruments.tsx` (HBars/Histogram/
Scatter/Gauge), `ChartView` (Vega-Lite confusion/ROC), `Sparkline`,
`OperatorReceiptTimeline`/`operators.ts`. Single seam: `POST /api/db/query`.

## 5. Design principle — psql / DataGrip parity

Every panel surfaces the literal SQL it ran with a copy button, and computes
nothing the DB couldn't (aggregations are SQL). A power user gets identical
results in any client; the lens only adds the live, explorable ("Bret Victor")
layer — feature-form try-it with live confidence, confusion cells that drill to
the misclassified rows, edit-source_sql-and-retrain, two-run metric diffs.

## 6. Deferred / out of scope (first cut)

- **Trainer auto-running.** `train_model` enqueues, but nothing claims runs by
  default (Warren deploys *serving* sidecars, it doesn't train). First cut
  assumes models are trained out-of-band (`rvbbit-trainer run-once`/`train-run`).
  Future: make training a Warren-claimable job for hands-off, progress-tracked
  training.
- Feature importance / ROC / per-class P-R (trainer stops at accuracy/f1/r2/rmse)
  — needs a trainer enhancement.
- Cancel/disable/drop helpers, stuck-run reaper, progress streaming, model
  versioning (retrain overwrites `ml_models` in place; history survives in
  `ml_training_runs`).
- A formal `MODEL_STUDIO_UI_CONTRACT` (today only CAPABILITIES.md has a
  "suggested V0 UI").

## 7. Build order

1. **SQL** — `rvbbit.ml_evaluations` + `rvbbit.evaluate_model` in
   `sql/model_studio.sql`, compiled via `src/model_studio.rs`. Verify live
   (register a demo model over the running wine backend; evaluate it).
2. **Lens** — `lib/rvbbit/model-studio.ts` + `model-studio-window.tsx`
   (Models/Train/Test/Predict/Observe tabs); wire window kind + icon + menu,
   gated on `hasRvbbit`.
3. **Verify** end-to-end (Playwright); commit + push.
