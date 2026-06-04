# The Unified Inference Plane

Status: **SQL surface + lens UI + e2e landed**; the Warren agent now executes
training (managed path is fully SQL-only, no CLI). Foundation-model GPU run is the
remaining executor. Date: 2026-06-04.

**Warren agent is the training executor (2026-06-04).** The `warren-agent` daemon
now handles the `model_training` job kind, so `SELECT rvbbit.train_model_managed(...)`
is fully hands-off: the standing agent claims the job, runs the trainer, serves,
and registers — no user terminal. New SQL `claim_model_training_job_for_node(node)`
is the agent's atomic claim (job + run + model → running) **with node placement**
(`n.labels @> j.target_selector`), so a GPU-targeted job is only picked up by a GPU
node; `claim_warren_job` keeps excluding `model_training` (deploy-only), and
`complete_warren_job` / `fail_warren_job` skip the `warren_deployments` record for
training jobs (compute, not a managed serving deployment). SQL observability:
`rvbbit.training_queue` (view: run ⨝ Warren job ⨝ model) and
`rvbbit.training_status(model)`. The agent's trainer invocation is configurable
(`--trainer-cmd` / `--trainer-dsn` / `--trainer-serve-host` / `--trainer-output-root`)
so it works whether the trainer is local or in a worker container. Verified live
(agent `--once` drains a queued bigfoot classifier → active + live predict) and in
e2e `ml/managed_training_via_node` (passes). The standalone `rvbbit-trainer watch`
remains as a bring-your-own-worker option.

**Implemented** (`sql/model_orchestration.sql`, `src/model_orchestration.rs`):
lifecycle (`cancel_model_training` / `disable_model` / `enable_model` /
`drop_model` / `reap_stale_training_runs`), versioning (`ml_model_versions`) +
monitoring (`ml_accuracy_series`); ergonomics (`validate_training_sql`,
`infer_feature_schema`); Warren-ify (`model_training` job kind,
`train_model_managed`, `claim_model_training_job`, `deploy_model_serving` — all
carrying a host/GPU `target_selector`); distillation (`distill_model`); foundation
surface (`predict_tabular` + `capabilities/packs/tabular/tabpfn-foundation`,
dry-run verified, live path gated). Lens Model Studio gained a **Monitor** tab
(accuracy-over-time + versions + diff) and lifecycle controls. The e2e harness
gained `ml/orchestration_surface` (passes).

**Loops closed (2026-06-04):** both executors are now SQL-bound binaries.
(1) `rvbbit-trainer watch` polls `claim_model_training_job` (managed) /
`claim_model_training_run`, fits, serves (`--serve-local` launches the uvicorn
sidecar, detached), and registers — so `train_model` → worker →
`predict_<model>(row)` returns live predictions (verified: batch 20/20 correct;
e2e `ml/closed_loop_watch` passes). (2) a `tabular_foundation` handler in
`hf-rvbbit-fastapi` does in-context fit-on-support → predict-queries, powering
`predict_tabular` end-to-end (verified live; CPU reference handler — swap for a
real TabPFN forward pass on a GPU host). The lens Model Studio gained a **Deploy
serving** action, a **Managed (train+deploy)** toggle, and **live run-status**.
**Race fixed (2026-06-04):** the deploy agent used to also claim `model_training`
jobs (it calls `claim_warren_job`, which filtered by node, not kind) but has no
fit handler — racing the trainer worker. Fix: `claim_warren_job` now excludes
`kind = 'model_training'`, so the deploy agent only takes deployment jobs and the
trainer worker owns training via `claim_model_training_job`. Verified: the deploy
claim returns 0 model_training / 1 deploy job, and the **managed** loop
(`train_model_managed` → `rvbbit-trainer watch` → `predict`) runs end-to-end
(batch 20/20; e2e `ml/closed_loop_watch` uses the managed path and doubles as the
race regression test). One SQL-line change; no Rust rebuild needed (the running
agent calls the patched function each poll).

A synthesis plan for pulling rvbbit's predictive-model system together with the
rest of the inference machinery (specialists, capabilities, Warren, semantic +
LLM operators), and skating toward where the puck is going. Builds on
`MODEL_STUDIO_PLAN.md` (the SQL-native model system + the evaluation/observability
window we shipped).

Everything here is **SQL-first**: the SQL surface is the contract; the rvbbit-lens
UI is ergonomic + observability sugar over it (every action is a SQL statement the
user can see, copy, and run in psql/DataGrip). UI additions are called out per
track but never own behavior.

---

## 0. North star — one thesis

> **Every prediction is an operator with a receipt. A "model" is just a backend
> behind an operator.**

rvbbit already converges five things onto that single shape:

| inference kind | created by | served by | invoked as | observed via |
|---|---|---|---|---|
| trained tabular model | `train_model` → trainer | sklearn sidecar (CPU) | `predict_<m>(row)` operator | receipts + `ml_model_status` |
| tabular **foundation** model | (none — training-free) | GPU specialist sidecar | `predict_tabular(...)` operator | receipts |
| GPU specialist (rerank/NER/embed) | capability pack | GPU sidecar | operator step → backend | receipts |
| embedding / vector | `embed` | in-proc / sidecar | `knn_text`, `cosine` … | receipts + embedding_cache |
| semantic / LLM op | `create_operator` | provider | `classify/extract/triples` | receipts + judgment_stats |

The model subsystem is the **least-orchestrated** member of that family (clean
data model, detached execution). This plan brings it to parity by **reusing the
existing machinery** — Warren, capabilities, the operator/receipts spine — rather
than inventing new infrastructure, and then extends the family in the two
forward-looking directions (training-free foundation prediction, LLM
distillation).

---

## Track A — Warren-ify the model lifecycle (the core unification)

Today `train_model()` enqueues a run that **nothing claims**, and the serving
sidecar must be launched by hand. Warren already owns claim → deploy → probe →
register → progress/heartbeat/metrics for sidecars (`warren_jobs`,
`enqueue_warren_job`, `claim_warren_job`, `try_update_job_progress`,
`record_warren_metrics`, `deploy_catalog_capability`). Crucially, `warren_jobs`
already carries a **`target_selector jsonb`** — host/GPU targeting is built in.
This is exactly the "micro-warrens on a GPU server" substrate.

**A1 — training as a Warren job.** Add a `model_training` job kind (extend the
`warren_jobs_kind_check`). `train_model(..., deploy => bool, target => jsonb)`
optionally `enqueue_warren_job('model_training', model_name, manifest =>
{run_id, source_sql, training_opts, ...}, target_selector => target)`. A
Warren-side **trainer handler** claims it, runs `rvbbit-trainer`, and streams
`try_update_job_progress` stages (`querying → fitting → evaluating → writing →
registering`), then calls `complete_model_training`. Net: a real hands-off
"Train" with a live progress bar — the missing muscle. `target_selector =>
'{"gpu": true}'` lands training on a GPU host when the estimator warrants it.

**A2 — serving as an auto-deployed micro-warren.** On `register_trained_model`,
optionally `enqueue_warren_job('trained_model', ...)` (this path already exists →
`deploy_capability`) to stand up the serving sidecar on a `target_selector` host.
A user model and a pre-trained pack become *the same deployment object*. Net: the
`predict_<model>` operator is reachable with zero manual `uvicorn`.

**A3 — artifacts to object store.** Write `model.joblib` to the **same s3:// /
gs:// object store** rvbbit already uses for cold row groups (the `cold_url` /
ObjectStore plumbing) instead of a local path, so artifacts are portable across
trainer and serving hosts. `artifact_uri` becomes an object-store URL.

**SQL surface:** `train_model` gains `deploy boolean`, `target jsonb`; new
`model_training` job kind + a `claim_model_training_job()` convenience; trainer
handler uses existing progress/complete functions.

**UI (Model Studio + Warren window):** the Train tab shows the live Warren job
progress (reuse the existing Warren progress/log rendering); a model gets a
**serving chip** (backend health + host from `target_selector`); training/serving
jobs show up in the existing Warren window automatically (they're `warren_jobs`).

---

## Track B — Tabular foundation model as a GPU specialist (training-free)

The forward bet. Package a **tabular foundation model** (TabPFN-v2-class: predicts
in-context from support rows, no per-dataset `fit`) as a capability pack — same
shape as wine/housing (`kind: hf_backend`, `runtime.template: hf-rvbbit-fastapi`,
`handler: tabular_foundation`, `device: gpu`), deployed as a GPU micro-warren via
Warren.

**SQL surface:** a single operator that needs no training run —
`rvbbit.predict_tabular(support_sql, predict_sql, target_column, task)` ships the
labeled support set + the rows-to-score to the foundation specialist and returns
predictions. Optionally register a thin `ml_models` row (`status = 'foundation'`,
no artifact) so it appears uniformly in Model Studio and reuses `evaluate_model`.

This collapses "train a model" into "call a specialist" — the purest expression
of the thesis, and the genuine "where the puck is going" move: zero training step,
GPU-served, SQL-invoked.

**UI:** Model Studio "New → Foundation (no training)" path; Predict / Evaluate
tabs work unchanged (still a predict operator).

---

## Track C — LLM → distill → cheap model → serve (the moat pattern)

Make the distillation loop first-class and, above all, **observable** — this is the
pattern only rvbbit can do in one substrate.

**Pattern:** label a sample with an LLM/semantic operator → train a cheap fast
model on those labels → serve at scale. (Label 5k rows with `classify`/`extract`
once; predict the next 50M for ≈0.)

**SQL surface:** `rvbbit.distill_model(model_name, unlabeled_sql,
labeler_operator, n_label, task, ...)` → runs the labeler over a sample (writing
receipts), materializes a labeled training set, calls `train_model`. Because both
the LLM labeling and the model predictions are operators, the **cost delta is
already in `receipts`** — the "cost-to-build vs cost-to-serve" story is a query,
not a slide.

**UI:** Model Studio "Distill from operator" path; a **cost-savings panel** (LLM
labeling $ vs model-serve $≈0) reusing the existing Costs/receipts window.

---

## Track D — Declarative / AutoML ergonomics

Lean into one-statement training (the trainer already half-does this).

- **Infer `feature_schema`** from `source_sql` output columns (minus
  `target_column`) → feature_schema becomes optional.
- **Auto estimator/holdout**: `training_opts => '{"auto": true}'` picks an
  estimator family by task + cardinality and a sensible holdout.
- **Validate before train**: `rvbbit.validate_training_sql(source_sql, target)` —
  EXPLAIN/dry-run, confirm the target column is produced, check feature types.
  (Closes a recon-flagged gap where bad specs only fail later in the worker.)

**UI:** Train tab "infer features from query" + inline validation chip.

---

## Cross-cutting — the observe / evaluate / version spine

We shipped `evaluate_model` (predictions-vs-actuals, confusion/residuals). Extend
toward a real monitoring loop, reusing patterns we already built:

- **Accuracy-over-time / live monitoring**: scheduled `evaluate_model` over a
  fresh labeled query → an accuracy series (reuse the **Drift** snapshot/series
  idea: `ml_evaluations` over time → sparkline). When ground truth arrives later,
  join `receipts` predictions → actuals by key for true production accuracy.
- **Model versioning**: `ml_training_runs` already preserves history; add an
  `ml_model_versions` view + a pinned/active version, and a metric **diff between
  two runs** (literally the Drift window pattern applied to model metrics).
- **Lifecycle helpers** (recon gaps): `cancel_model_training`, `disable_model`,
  `drop_model`, and a stuck-run reaper (heartbeat/lease on `ml_training_runs`).

**UI:** Model Studio **Monitor** tab (accuracy sparkline across evals + drift),
version-diff view, and cancel/disable/drop buttons.

---

## SQL-first contract (what the UI consumes)

New / extended surface — the UI is a thin client over exactly this:

```
train_model(..., deploy bool, target jsonb)            -- A1/A2: optional Warren train+serve
validate_training_sql(source_sql, target) -> jsonb     -- D:   pre-flight
predict_tabular(support_sql, predict_sql, target, task)-- B:   training-free foundation predict
distill_model(model, unlabeled_sql, labeler_op, n, …)  -- C:   LLM → labeled set → train_model
evaluate_model(model, eval_sql, label_col)             -- (shipped) predictions-vs-actuals
catalog/ml_model_status / ml_training_runs / ml_evaluations  -- (exist) registry/runs/evals
warren_jobs (kind in …, model_training)                -- A:   training+serving jobs + progress
cancel_model_training / disable_model / drop_model     -- lifecycle
ml_model_versions (view)                               -- versioning/diff
```

Design rule: **if the UI does it, it's a SQL statement it can show.** No
UI-only computation, no UI-only state that the DB can't reproduce.

---

## UI additions, summarized (all sugar, all `hasRvbbit`-gated)

- **Model Studio**: Train tab → live Warren progress + "infer features" +
  validation; **Monitor** tab (accuracy-over-time, version diff); **Distill**
  and **Foundation (no-train)** entry points; serving/host chip; cost-to-build vs
  cost-to-serve panel; lifecycle buttons.
- **Warren window**: training/serving model jobs appear automatically (they're
  `warren_jobs`); a "deploy on host/GPU" target selector surfaces `target_selector`.
- **Costs window**: the distillation cost-delta view (LLM labeling vs model serve).
- Everything reuses existing components (Warren progress, receipts timeline,
  sparkline, Costs) — minimal net-new UI.

---

## Sequencing (by leverage)

1. **Track A1 — Warren-ify training.** Highest leverage: turns the whole
   queue-and-claim spine into a hands-off loop, reusing Warren wholesale. Unblocks
   the "Train" button actually working.
2. **Cross-cutting eval/monitor** (small; builds directly on `evaluate_model` +
   the Drift series pattern).
3. **Track A2/A3** — serving auto-deploy + object-store artifacts (alongside A1).
4. **Track C — distillation** (high story value, mostly composition over existing
   operators + the cost spine).
5. **Track B — tabular foundation model** (the biggest "puck" bet; needs a GPU
   capability pack + a `tabular_foundation` handler).
6. **Track D — ergonomics** (polish: inference, validation).

The unifying win across all six: a user never leaves SQL, every prediction —
classical, foundation, distilled, embedding, or LLM — is **one object (operator +
backend + receipt)**, and the lens makes that one object trainable, testable,
deployable, and observable.
