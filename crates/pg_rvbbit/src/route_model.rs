//! ML routing model — a tiny gradient-boosted-tree ensemble per engine that
//! predicts log-latency from query features. Pure inference: no PG or router
//! dependencies here, so it's unit-testable in isolation. The router builds the
//! feature vector (by resolving each model's declared feature names against
//! RouteFeatures) and calls `EngineModel::predict`.
//!
//! Wire format (per engine, stored in rvbbit.route_model.params jsonb, produced
//! by scripts/train_route_model.py):
//!
//! ```json
//! {
//!   "base": 3.14,                       // additive constant (log-ms)
//!   "feature_names": ["ln_table_rows", "aggregate_count", ...],
//!   "trees": [
//!     { "nodes": [
//!         {"feature": 0, "threshold": 12.5, "left": 1, "right": 2},  // internal
//!         {"leaf": -0.08},                                            // leaf
//!         {"leaf": 0.12}
//!     ]}
//!   ]
//! }
//! ```
//!
//! Prediction = base + sum over trees of the reached leaf value. Leaf values are
//! already scaled by the learning rate at export time, so inference is a plain
//! sum. The predicted value is log-latency; smaller = faster.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub(crate) enum TreeNode {
    /// Internal split: if feature[feature] < threshold go to `left`, else `right`
    /// (both are node indices within the same tree).
    Internal {
        feature: usize,
        threshold: f64,
        left: usize,
        right: usize,
    },
    /// Terminal node contributing `leaf` to the sum.
    Leaf { leaf: f64 },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Tree {
    pub nodes: Vec<TreeNode>,
}

impl Tree {
    /// Walk from the root (node 0) to a leaf. Malformed trees (out-of-range child
    /// indices, cycles beyond the node count) contribute 0.0 rather than panic —
    /// a corrupt model must never crash routing.
    fn eval(&self, x: &[f64]) -> f64 {
        let mut idx = 0usize;
        for _ in 0..self.nodes.len() {
            match self.nodes.get(idx) {
                Some(TreeNode::Leaf { leaf }) => return *leaf,
                Some(TreeNode::Internal {
                    feature,
                    threshold,
                    left,
                    right,
                }) => {
                    // `<=` for the left branch matches scikit-learn's split
                    // convention (X[feature] <= threshold -> left), so exported
                    // trees evaluate identically to how they were trained.
                    let v = x.get(*feature).copied().unwrap_or(0.0);
                    idx = if v <= *threshold { *left } else { *right };
                }
                None => return 0.0,
            }
        }
        0.0
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct EngineModel {
    #[serde(default)]
    pub base: f64,
    pub feature_names: Vec<String>,
    #[serde(default)]
    pub trees: Vec<Tree>,
}

impl EngineModel {
    /// Predicted log-latency for a pre-resolved feature vector `x` (same order as
    /// `feature_names`). Returns None if the model has no trees (untrained).
    pub(crate) fn predict(&self, x: &[f64]) -> Option<f64> {
        if self.trees.is_empty() {
            return None;
        }
        let mut acc = self.base;
        for t in &self.trees {
            acc += t.eval(x);
        }
        Some(acc)
    }
}

// ── Training (in-database gradient boosting) ────────────────────────────────
// Fits the same tree-ensemble the evaluator reads, so rvbbit.train_route_model()
// is a pure-Rust, SQL-invocable equivalent of scripts/train_route_model.py — no
// Python/sklearn dependency. Squared-error gradient boosting of shallow
// regression trees; leaf values are pre-scaled by the learning rate so the
// evaluator's plain sum reproduces predictions exactly.

/// One training row: feature vector `x` and target `y` (log-latency).
pub(crate) struct Sample {
    pub x: Vec<f64>,
    pub y: f64,
}

pub(crate) struct GbmConfig {
    pub n_trees: usize,
    pub max_depth: usize,
    pub learning_rate: f64,
    pub min_leaf: usize,
}

impl Default for GbmConfig {
    fn default() -> Self {
        GbmConfig {
            n_trees: 80,
            max_depth: 3,
            learning_rate: 0.1,
            min_leaf: 5,
        }
    }
}

fn mean_of(values: &[f64], idx: &[usize]) -> f64 {
    if idx.is_empty() {
        return 0.0;
    }
    idx.iter().map(|&i| values[i]).sum::<f64>() / idx.len() as f64
}

/// Sum of squared error of `values[idx]` around their mean: Σr² − (Σr)²/n.
fn sse(values: &[f64], idx: &[usize]) -> f64 {
    let n = idx.len() as f64;
    if n == 0.0 {
        return 0.0;
    }
    let mut s = 0.0;
    let mut sq = 0.0;
    for &i in idx {
        s += values[i];
        sq += values[i] * values[i];
    }
    sq - s * s / n
}

/// Best (feature, threshold) split of `idx` minimizing child SSE, honoring
/// min_leaf on both sides. Returns None if no split beats the parent.
fn best_split(
    samples: &[Sample],
    resid: &[f64],
    idx: &[usize],
    n_features: usize,
    cfg: &GbmConfig,
) -> Option<(usize, f64, Vec<usize>, Vec<usize>)> {
    let parent_sse = sse(resid, idx);
    let mut best: Option<(f64, usize, f64)> = None; // (gain, feature, threshold)
    for f in 0..n_features {
        // sort this node's rows by feature f
        let mut order: Vec<usize> = idx.to_vec();
        order.sort_by(|&a, &b| {
            samples[a].x[f]
                .partial_cmp(&samples[b].x[f])
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let total_sum: f64 = order.iter().map(|&i| resid[i]).sum();
        let total_sq_f: f64 = order.iter().map(|&i| resid[i] * resid[i]).sum();
        let total_n = order.len();
        let mut left_sum = 0.0;
        let mut left_sq = 0.0;
        for k in 0..total_n - 1 {
            let i = order[k];
            left_sum += resid[i];
            left_sq += resid[i] * resid[i];
            let left_n = k + 1;
            let right_n = total_n - left_n;
            if left_n < cfg.min_leaf || right_n < cfg.min_leaf {
                continue;
            }
            let vk = samples[i].x[f];
            let vnext = samples[order[k + 1]].x[f];
            if vk == vnext {
                continue; // can't split between equal values
            }
            let sse_left = left_sq - left_sum * left_sum / left_n as f64;
            let right_sum = total_sum - left_sum;
            let right_sq = total_sq_f - left_sq; // Σr²_right = Σr²_total − Σr²_left
            let sse_right = right_sq - right_sum * right_sum / right_n as f64;
            let gain = parent_sse - (sse_left + sse_right);
            let threshold = (vk + vnext) / 2.0;
            if gain > 0.0 && best.map(|(g, _, _)| gain > g).unwrap_or(true) {
                best = Some((gain, f, threshold));
            }
        }
    }
    let (_, f, threshold) = best?;
    let mut left = Vec::new();
    let mut right = Vec::new();
    for &i in idx {
        if samples[i].x[f] <= threshold {
            left.push(i);
        } else {
            right.push(i);
        }
    }
    if left.len() < cfg.min_leaf || right.len() < cfg.min_leaf {
        return None;
    }
    Some((f, threshold, left, right))
}

fn build_node(
    nodes: &mut Vec<TreeNode>,
    samples: &[Sample],
    resid: &[f64],
    idx: &[usize],
    depth: usize,
    n_features: usize,
    cfg: &GbmConfig,
) -> usize {
    let leaf_val = cfg.learning_rate * mean_of(resid, idx);
    if depth >= cfg.max_depth || idx.len() < 2 * cfg.min_leaf {
        nodes.push(TreeNode::Leaf { leaf: leaf_val });
        return nodes.len() - 1;
    }
    match best_split(samples, resid, idx, n_features, cfg) {
        None => {
            nodes.push(TreeNode::Leaf { leaf: leaf_val });
            nodes.len() - 1
        }
        Some((feature, threshold, left_idx, right_idx)) => {
            let my = nodes.len();
            nodes.push(TreeNode::Leaf { leaf: 0.0 }); // placeholder, overwritten below
            let left = build_node(nodes, samples, resid, &left_idx, depth + 1, n_features, cfg);
            let right = build_node(nodes, samples, resid, &right_idx, depth + 1, n_features, cfg);
            nodes[my] = TreeNode::Internal {
                feature,
                threshold,
                left,
                right,
            };
            my
        }
    }
}

/// Train a gradient-boosted ensemble on `samples` and return the serializable
/// model. `feature_names` labels the vector positions (stored on the model).
pub(crate) fn train_gbm(
    samples: &[Sample],
    feature_names: Vec<String>,
    cfg: &GbmConfig,
) -> EngineModel {
    let n = samples.len();
    let n_features = feature_names.len();
    let base = if n == 0 {
        0.0
    } else {
        samples.iter().map(|s| s.y).sum::<f64>() / n as f64
    };
    let mut pred = vec![base; n];
    let all: Vec<usize> = (0..n).collect();
    let mut trees = Vec::with_capacity(cfg.n_trees);
    for _ in 0..cfg.n_trees {
        let resid: Vec<f64> = (0..n).map(|i| samples[i].y - pred[i]).collect();
        let mut nodes = Vec::new();
        build_node(&mut nodes, samples, &resid, &all, 0, n_features, cfg);
        let tree = Tree { nodes };
        for (i, p) in pred.iter_mut().enumerate() {
            *p += tree.eval(&samples[i].x);
        }
        trees.push(tree);
    }
    EngineModel {
        base,
        feature_names,
        trees,
    }
}

/// Coefficient of determination (R²) of the model over `samples` — reported by
/// the trainer as a fit-quality signal.
pub(crate) fn r2(model: &EngineModel, samples: &[Sample]) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let mean_y = samples.iter().map(|s| s.y).sum::<f64>() / samples.len() as f64;
    let mut ss_res = 0.0;
    let mut ss_tot = 0.0;
    for s in samples {
        let p = model.predict(&s.x).unwrap_or(model.base);
        ss_res += (s.y - p) * (s.y - p);
        ss_tot += (s.y - mean_y) * (s.y - mean_y);
    }
    if ss_tot == 0.0 {
        return 0.0;
    }
    1.0 - ss_res / ss_tot
}

#[cfg(test)]
mod tests {
    use super::*;

    fn model_from(json: &str) -> EngineModel {
        serde_json::from_str(json).expect("valid model json")
    }

    #[test]
    fn single_stump_splits_on_threshold() {
        // base 1.0; one stump: feature0 < 10 -> -0.5 else +0.5
        let m = model_from(
            r#"{"base":1.0,"feature_names":["f0"],
                "trees":[{"nodes":[{"feature":0,"threshold":10.0,"left":1,"right":2},
                                   {"leaf":-0.5},{"leaf":0.5}]}]}"#,
        );
        assert_eq!(m.predict(&[5.0]), Some(0.5)); // 1.0 - 0.5
        assert_eq!(m.predict(&[15.0]), Some(1.5)); // 1.0 + 0.5
    }

    #[test]
    fn ensemble_sums_trees() {
        let m = model_from(
            r#"{"base":0.0,"feature_names":["f0","f1"],
                "trees":[
                  {"nodes":[{"feature":0,"threshold":1.0,"left":1,"right":2},{"leaf":1.0},{"leaf":2.0}]},
                  {"nodes":[{"feature":1,"threshold":1.0,"left":1,"right":2},{"leaf":10.0},{"leaf":20.0}]}
                ]}"#,
        );
        // f0>=1 -> 2.0 ; f1<1 -> 10.0 ; sum 12.0
        assert_eq!(m.predict(&[5.0, 0.0]), Some(12.0));
    }

    #[test]
    fn untrained_model_returns_none() {
        let m = model_from(r#"{"base":1.0,"feature_names":["f0"],"trees":[]}"#);
        assert_eq!(m.predict(&[1.0]), None);
    }

    #[test]
    fn malformed_tree_does_not_panic() {
        // child index out of range -> contributes 0, no panic.
        let m = model_from(
            r#"{"base":2.0,"feature_names":["f0"],
                "trees":[{"nodes":[{"feature":0,"threshold":1.0,"left":9,"right":9}]}]}"#,
        );
        assert_eq!(m.predict(&[0.0]), Some(2.0));
    }

    #[test]
    fn missing_feature_index_defaults_zero() {
        // feature index 5 not present in x -> treated as 0.0 -> takes left (<1).
        let m = model_from(
            r#"{"base":0.0,"feature_names":["f0"],
                "trees":[{"nodes":[{"feature":5,"threshold":1.0,"left":1,"right":2},{"leaf":7.0},{"leaf":9.0}]}]}"#,
        );
        assert_eq!(m.predict(&[3.0]), Some(7.0));
    }

    #[test]
    fn gbm_learns_a_step_function() {
        // y depends on x[0] (a step) and x[1] (a second step); x[2] is noise.
        // The trainer should fit it (high R²) and rank the corners correctly.
        let mut samples = Vec::new();
        for k in 0..400 {
            let a = ((k % 20) as f64) / 20.0; // 0..1
            let b = (((k / 20) % 20) as f64) / 20.0;
            let noise = ((k * 7) % 5) as f64;
            let y = (if a < 0.5 { 1.0 } else { 3.0 }) + (if b < 0.5 { 0.0 } else { 2.0 });
            samples.push(Sample { x: vec![a, b, noise], y });
        }
        let cfg = GbmConfig::default();
        let model = train_gbm(&samples, vec!["a".into(), "b".into(), "noise".into()], &cfg);
        assert!(r2(&model, &samples) > 0.95, "R2 = {}", r2(&model, &samples));
        // low corner ~1.0, high corner ~5.0, and ordered.
        let low = model.predict(&[0.1, 0.1, 0.0]).unwrap();
        let high = model.predict(&[0.9, 0.9, 0.0]).unwrap();
        assert!((low - 1.0).abs() < 0.3, "low corner {low}");
        assert!((high - 5.0).abs() < 0.3, "high corner {high}");
        assert!(high > low);
        // A JSON round-trip evaluates identically (serialize -> deserialize).
        let json = serde_json::to_string(&model).unwrap();
        let back: EngineModel = serde_json::from_str(&json).unwrap();
        assert!((back.predict(&[0.9, 0.9, 0.0]).unwrap() - high).abs() < 1e-9);
    }
}
