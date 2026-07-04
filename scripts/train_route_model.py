#!/usr/bin/env python3
"""Train the ML routing layer's per-engine latency models from benchmark history.

NOTE: the primary, product-facing trainer is the in-database SQL function
`SELECT rvbbit.train_route_model();` (pure Rust, no Python/sklearn needed) — use
that from a SQL client or the UI. This script is an offline equivalent for
experimentation (e.g. trying different sklearn hyperparameters); it writes the
same rvbbit.route_model rows.

Reads forced-engine runs from bench_history.query_results (each rvbbit_<engine>_forced
system ran the query set on ONE engine, giving per-engine timings + the query
features), trains a small gradient-boosted regressor per engine predicting
log(median_ms), and writes the tree ensembles into rvbbit.route_model for the
router's ml_route_decision hook.

The feature vocabulary here MUST match crates/pg_rvbbit/src/router.rs::feature_value.

Usage:
  python3 scripts/train_route_model.py [--dsn DSN] [--min-samples N] [--dry-run]
"""
import argparse
import json
import math
import sys

import psycopg
from sklearn.ensemble import GradientBoostingRegressor

DEFAULT_DSN = "postgresql://postgres:rvbbit@localhost:55433/bench"

SYSTEM_ENGINE = {
    "rvbbit_native_forced": "native",
    "rvbbit_native_vortex_forced": "native_vortex",
    "rvbbit_duck_forced": "duck",
    "rvbbit_duck_hive_forced": "duck_hive",
    "rvbbit_duck_vortex_forced": "duck_vortex",
    "rvbbit_datafusion_forced": "datafusion",
    "rvbbit_datafusion_hive_forced": "datafusion_hive",
    "rvbbit_datafusion_vortex_forced": "datafusion_vortex",
    "rvbbit_gpu_gqe_forced": "gpu_gqe",
    "rvbbit_pg_heap_forced": "pg",
}

# Auto-run route strings (Candidate::route()) -> model engine name. Used when
# ingesting auto 'rvbbit' runs (system not *_forced): each query recorded the ONE
# engine the router chose + its latency. Weaker signal than forced runs (biased
# to the router's own picks, no counterfactual) but adds real samples — especially
# at scales where no forced sweep was run. Note: native_vortex routes as "native",
# so it folds into native here.
ROUTE_ENGINE = {
    "native": "native",
    "duck": "duck", "duck_vector": "duck",
    "duck_hive": "duck_hive",
    "duck_vortex": "duck_vortex",
    "datafusion": "datafusion", "datafusion_vector": "datafusion",
    "datafusion_hive": "datafusion_hive",
    "datafusion_vortex": "datafusion_vortex",
    "gpu_gqe": "gpu_gqe",
    "pg": "pg", "postgres_rowstore": "pg", "pg_rowstore": "pg",
}

# Must match router.rs::feature_value name -> value resolution.
BOOL_FEATURES = {
    "group_by", "order_by", "having", "distinct", "where", "select_star",
    "offset_present", "starts_with_with", "has_native_function",
    "plan_has_group", "plan_has_hash", "plan_has_join", "plan_has_sort",
    "plan_has_subplan",
}

FEATURE_NAMES = [
    "ln_table_rows", "ln_table_bytes", "ln_row_group_count",
    "aggregate_count", "count_count", "count_distinct_count",
    "sum_count", "avg_count", "min_count", "max_count",
    "join_count", "from_count", "in_count", "between_count", "or_count",
    "and_count", "comparison_count", "like_count", "not_like_count",
    "regex_count", "exists_count",
    "referenced_text_col_count", "group_text_col_count",
    "order_text_col_count", "count_distinct_text_count",
    "group_by", "order_by", "having", "distinct", "where", "select_star",
    "offset_present", "plan_has_join", "plan_has_sort", "plan_has_group",
    "plan_has_subplan", "plan_has_hash",
]


def feat(features: dict, name: str) -> float:
    if name == "ln_table_rows":
        return math.log(max(0, features.get("table_rows", 0) or 0) + 1)
    if name == "ln_table_bytes":
        return math.log(max(0, features.get("table_bytes", 0) or 0) + 1)
    if name == "ln_row_group_count":
        return math.log(max(0, features.get("row_group_count", 0) or 0) + 1)
    if name in BOOL_FEATURES:
        return 1.0 if features.get(name) else 0.0
    v = features.get(name, 0)
    return float(v) if isinstance(v, (int, float)) else 0.0


def vectorize(features: dict) -> list:
    return [feat(features, n) for n in FEATURE_NAMES]


def export_tree(dtree, lr: float) -> dict:
    t = dtree.tree_
    nodes = []
    for i in range(t.node_count):
        if t.children_left[i] == -1:  # leaf
            nodes.append({"leaf": float(t.value[i][0][0]) * lr})
        else:
            nodes.append({
                "feature": int(t.feature[i]),
                "threshold": float(t.threshold[i]),
                "left": int(t.children_left[i]),
                "right": int(t.children_right[i]),
            })
    return {"nodes": nodes}


def train_engine(X, y):
    gbr = GradientBoostingRegressor(
        n_estimators=80, max_depth=3, learning_rate=0.1,
        loss="squared_error", subsample=1.0, random_state=0,
    )
    gbr.fit(X, y)
    import numpy as np
    const = getattr(gbr.init_, "constant_", None)
    base = float(np.ravel(const)[0]) if const is not None else float(np.mean(y))
    trees = [export_tree(gbr.estimators_[i][0], gbr.learning_rate)
             for i in range(len(gbr.estimators_))]
    model = {"base": base, "feature_names": FEATURE_NAMES, "trees": trees}

    # Self-check: our JSON evaluator must reproduce gbr.predict (guards the
    # base/learning-rate/split-convention export).
    def predict_json(x):
        acc = base
        for tr in trees:
            idx = 0
            for _ in range(len(tr["nodes"])):
                n = tr["nodes"][idx]
                if "leaf" in n:
                    acc += n["leaf"]
                    break
                idx = n["left"] if x[n["feature"]] <= n["threshold"] else n["right"]
        return acc
    import numpy as np
    sk = gbr.predict(X[:50])
    ours = np.array([predict_json(list(row)) for row in X[:50]])
    max_err = float(np.max(np.abs(sk - ours))) if len(sk) else 0.0
    return model, gbr, max_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-auto", action="store_true",
                    help="train only from forced-engine runs (skip auto 'rvbbit' timings)")
    args = ap.parse_args()

    conn = psycopg.connect(args.dsn, autocommit=True)
    cur = conn.cursor()

    # Features are a property of the query (run_id, qid), recorded only by engines
    # that go through the router explain (native/pg forced runs store just
    # first_ms). Build a features map from any run that has them, then pair it
    # with every engine's timing so native/pg get trained too.
    cur.execute(
        """
        SELECT run_id, qid, detail->'route'->'features' AS features
        FROM bench_history.query_results
        WHERE detail->'route'->'features' IS NOT NULL
        """
    )
    features_by = {}
    for run_id, qid, features in cur.fetchall():
        if isinstance(features, str):
            features = json.loads(features)
        features_by[(run_id, qid)] = features

    cur.execute(
        """
        SELECT run_id, qid, system, median_ms
        FROM bench_history.query_results
        WHERE status='ok' AND median_ms > 0 AND system = ANY(%s)
        """,
        (list(SYSTEM_ENGINE.keys()),),
    )
    per_engine = {}

    def add(engine, features, median_ms, source):
        d = per_engine.setdefault(engine, {"X": [], "y": [], "forced": 0, "auto": 0})
        d["X"].append(vectorize(features))
        d["y"].append(math.log(float(median_ms)))
        d[source] += 1

    missing = 0
    for run_id, qid, system, median_ms in cur.fetchall():
        features = features_by.get((run_id, qid))
        if features is None:
            missing += 1
            continue
        add(SYSTEM_ENGINE[system], features, median_ms, "forced")
    if missing:
        print(f"note: {missing} forced-timing rows had no matching features (skipped)")

    # Auto 'rvbbit' runs: one (engine, latency) per query, engine = the route the
    # router actually chose. Adds real samples (incl. scales with no forced sweep).
    if not args.no_auto:
        cur.execute(
            """
            SELECT detail->'route'->>'route' AS route,
                   detail->'route'->'features' AS features, median_ms
            FROM bench_history.query_results
            WHERE status='ok' AND median_ms > 0
              AND system LIKE 'rvbbit%' AND system NOT LIKE '%\\_forced'
              AND detail->'route'->'features' IS NOT NULL
            """
        )
        auto_rows = 0
        for route, features, median_ms in cur.fetchall():
            engine = ROUTE_ENGINE.get(route)
            if engine is None:
                continue
            if isinstance(features, str):
                features = json.loads(features)
            add(engine, features, median_ms, "auto")
            auto_rows += 1
        print(f"auto-run samples ingested: {auto_rows}")

    written = []
    for engine, data in sorted(per_engine.items()):
        n = len(data["y"])
        if n < args.min_samples:
            print(f"skip {engine}: only {n} samples (< {args.min_samples})")
            continue
        model, gbr, max_err = train_engine(data["X"], data["y"])
        train_r2 = gbr.score(data["X"], data["y"])
        print(f"{engine:22s} n={n:5d} (forced={data['forced']}, auto={data['auto']})  "
              f"train_R2={train_r2:.3f}  export_max_err={max_err:.2e}")
        if max_err > 1e-6:
            print(f"  !! export mismatch for {engine} ({max_err:.2e}); refusing to write")
            continue
        params = json.dumps(model)
        if not args.dry_run:
            cur.execute(
                """
                INSERT INTO rvbbit.route_model (engine, params, feature_schema, n_samples, trained_at, notes)
                VALUES (%s, %s::jsonb, 1, %s, clock_timestamp(), %s)
                ON CONFLICT (engine) DO UPDATE
                  SET params = EXCLUDED.params, n_samples = EXCLUDED.n_samples,
                      trained_at = EXCLUDED.trained_at, notes = EXCLUDED.notes
                """,
                (engine, params, n, f"train_route_model.py train_R2={train_r2:.3f}"),
            )
        written.append(engine)

    print(f"{'(dry-run) ' if args.dry_run else ''}models: {', '.join(written) or 'none'}")
    conn.close()
    sys.exit(0 if written else 1)


if __name__ == "__main__":
    main()
