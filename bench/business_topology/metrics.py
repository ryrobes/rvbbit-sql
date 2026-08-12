"""Leakage-resistant splits and selective metrics for topology evaluation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .baseline import predict_correspondence, predict_population_roles
from .candidates import correspondence_packet_for_item, pair_key
from .contracts import CORRESPONDENCE_VERDICTS, POPULATION_ROLES, validate_corpus


def grouped_split(
    groups: Iterable[str],
    *,
    test_fraction: float = 0.25,
    seed: str = "rvbbit-topology-v1",
) -> dict[str, str]:
    """Assign complete object/source families to train or test deterministically."""

    unique = sorted(set(groups))
    if not unique:
        return {}
    ranked = sorted(
        unique,
        key=lambda group: hashlib.sha256(f"{seed}\x1f{group}".encode()).hexdigest(),
    )
    if len(ranked) == 1:
        return {ranked[0]: "train"}
    n_test = min(max(round(len(ranked) * test_fraction), 1), len(ranked) - 1)
    test = set(ranked[:n_test])
    return {group: "test" if group in test else "train" for group in unique}


def multilabel_metrics(
    gold_rows: Sequence[set[str]],
    predicted_rows: Sequence[set[str]],
    labels: Sequence[str],
) -> dict[str, Any]:
    if len(gold_rows) != len(predicted_rows):
        raise ValueError("gold and prediction lengths differ")
    by_label: dict[str, dict[str, float | int]] = {}
    total_tp = total_fp = total_fn = 0
    for label in labels:
        tp = sum(
            label in gold and label in predicted
            for gold, predicted in zip(gold_rows, predicted_rows)
        )
        fp = sum(
            label not in gold and label in predicted
            for gold, predicted in zip(gold_rows, predicted_rows)
        )
        fn = sum(
            label in gold and label not in predicted
            for gold, predicted in zip(gold_rows, predicted_rows)
        )
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        by_label[label] = {
            "support": tp + fn,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
        total_tp += tp
        total_fp += fp
        total_fn += fn
    micro_precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    micro_recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if micro_precision + micro_recall
        else 0.0
    )
    supported = [row for row in by_label.values() if row["support"]]
    return {
        "rows": len(gold_rows),
        "exact_match": sum(gold == predicted for gold, predicted in zip(gold_rows, predicted_rows))
        / len(gold_rows)
        if gold_rows
        else 0.0,
        "micro_precision": micro_precision,
        "micro_recall": micro_recall,
        "micro_f1": micro_f1,
        "macro_f1": sum(float(row["f1"]) for row in supported) / len(supported)
        if supported
        else 0.0,
        "by_label": by_label,
    }


def evaluate_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    validate_corpus(corpus, require_reviewed=True)

    role_gold: list[set[str]] = []
    role_predicted: list[set[str]] = []
    for item in corpus.get("populations", []):
        gold = item.get("gold")
        if not isinstance(gold, Mapping):
            continue
        role_gold.append(set(gold.get("roles", [])))
        role_predicted.append(set(predict_population_roles(item["packet"])["roles"]))

    pair_gold: list[set[str]] = []
    pair_predicted: list[set[str]] = []
    pair_predictions: list[dict[str, Any]] = []
    hard_negative_count = hard_negative_false_edges = 0
    covered = 0
    for item in corpus.get("correspondences", []):
        gold = item.get("gold")
        if not isinstance(gold, Mapping):
            continue
        packet = correspondence_packet_for_item(corpus, item)
        prediction = predict_correspondence(packet)
        gold_set = set(gold.get("verdicts", []))
        predicted_set = set(prediction["verdicts"])
        pair_gold.append(gold_set)
        pair_predicted.append(predicted_set)
        if predicted_set != {"abstain"}:
            covered += 1
        if gold_set == {"unrelated"}:
            hard_negative_count += 1
            if predicted_set not in ({"unrelated"}, {"abstain"}):
                hard_negative_false_edges += 1
        pair_predictions.append(
            {
                "pair_id": item["pair_id"],
                "gold": sorted(gold_set),
                "predicted": prediction["verdicts"],
                "confidence": prediction["confidence"],
                "uncertainty": prediction["uncertainty"],
            }
        )

    pair_metrics = multilabel_metrics(pair_gold, pair_predicted, CORRESPONDENCE_VERDICTS)
    pair_metrics["coverage"] = covered / len(pair_gold) if pair_gold else 0.0
    pair_metrics["hard_negative_false_edge_rate"] = (
        hard_negative_false_edges / hard_negative_count if hard_negative_count else 0.0
    )
    pair_metrics["hard_negative_count"] = hard_negative_count

    groups = [
        str(item["split_group"])
        for family in ("populations", "motifs", "correspondences")
        for item in corpus.get(family, [])
    ]
    return {
        "schema_version": "rvbbit.business-topology.eval-report.v1",
        "corpus_id": corpus["corpus_id"],
        "group_split": grouped_split(groups),
        "population_roles": multilabel_metrics(role_gold, role_predicted, POPULATION_ROLES),
        "correspondences": pair_metrics,
        "pair_predictions": pair_predictions,
        "motifs": {
            "reviewed": sum(1 for item in corpus.get("motifs", []) if item.get("gold") is not None),
            "baseline": "not_scored",
            "note": "motif scoring starts with the learned population specialist; the deterministic floor only validates its packet and label contract",
        },
    }


def evaluate_candidate_recall(
    corpus: Mapping[str, Any],
    evidence_by_pair: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Measure recall paths before bounded human-review queue sampling."""

    validate_corpus(corpus)
    positives = 0
    recalled = 0
    negative_controls = 0
    nominated_negatives = 0
    by_verdict: dict[str, dict[str, int | float]] = {}
    reviewed_controls = 0
    for item in corpus.get("correspondences", []):
        gold = item.get("gold")
        if not isinstance(gold, Mapping) or gold.get("reviewed") is not True:
            continue
        reviewed_controls += 1
        verdicts = set(gold.get("verdicts", []))
        key = pair_key(str(item["left_population_id"]), str(item["right_population_id"]))
        nominated = key in evidence_by_pair
        if verdicts in ({"unrelated"}, {"abstain"}):
            negative_controls += 1
            nominated_negatives += int(nominated)
            continue
        positives += 1
        recalled += int(nominated)
        for verdict in verdicts:
            row = by_verdict.setdefault(verdict, {"support": 0, "recalled": 0})
            row["support"] = int(row["support"]) + 1
            row["recalled"] = int(row["recalled"]) + int(nominated)
    for row in by_verdict.values():
        support = int(row["support"])
        row["recall"] = int(row["recalled"]) / support if support else 0.0
    return {
        "schema_version": "rvbbit.business-topology.candidate-recall.v1",
        "corpus_id": corpus["corpus_id"],
        "reviewed_controls": reviewed_controls,
        "positive_controls": positives,
        "recalled_positive_controls": recalled,
        "positive_recall": recalled / positives if positives else 0.0,
        "negative_controls": negative_controls,
        "negative_nomination_rate": (
            nominated_negatives / negative_controls if negative_controls else 0.0
        ),
        "by_verdict": dict(sorted(by_verdict.items())),
    }
