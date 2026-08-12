"""Train and evaluate portable population/correspondence baseline checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .candidates import correspondence_packet_for_item
from .contracts import CORRESPONDENCE_VERDICTS, POPULATION_ROLES, validate_corpus
from .features import correspondence_features, population_features, vectorize_feature_dicts
from .linear import PortableOVR, fit_portable_ovr
from .metrics import grouped_split, multilabel_metrics


def _rows_for_task(
    corpus: Mapping[str, Any],
    task: str,
) -> tuple[list[dict[str, float]], list[set[str]], list[str], list[str]]:
    feature_rows: list[dict[str, float]] = []
    gold_rows: list[set[str]] = []
    groups: list[str] = []
    ids: list[str] = []
    if task == "population":
        for item in corpus.get("populations", []):
            gold = item.get("gold")
            if not isinstance(gold, Mapping):
                continue
            feature_rows.append(population_features(item["packet"]))
            gold_rows.append(set(gold.get("roles", [])))
            groups.append(str(item["split_group"]))
            ids.append(str(item["population_id"]))
    elif task == "correspondence":
        for item in corpus.get("correspondences", []):
            gold = item.get("gold")
            if not isinstance(gold, Mapping):
                continue
            packet = correspondence_packet_for_item(corpus, item)
            feature_rows.append(correspondence_features(packet))
            gold_rows.append(set(gold.get("verdicts", [])))
            groups.append(str(item["split_group"]))
            ids.append(str(item["pair_id"]))
    else:
        raise ValueError("task must be population or correspondence")
    return feature_rows, gold_rows, groups, ids


def train_corpus_baseline(
    corpus: Mapping[str, Any],
    *,
    task: str,
    test_fraction: float = 0.25,
    seed: str = "rvbbit-topology-v1",
    target_precision: float = 0.98,
) -> tuple[PortableOVR, dict[str, Any]]:
    validate_corpus(corpus, require_reviewed=True)
    feature_rows, gold_rows, groups, row_ids = _rows_for_task(corpus, task)
    assignments = grouped_split(groups, test_fraction=test_fraction, seed=seed)
    matrix, feature_names = vectorize_feature_dicts(feature_rows)
    train_indices = [index for index, group in enumerate(groups) if assignments[group] == "train"]
    test_indices = [index for index, group in enumerate(groups) if assignments[group] == "test"]
    if not train_indices or not test_indices:
        raise ValueError("grouped split requires at least one train and one test row")
    labels = list(POPULATION_ROLES if task == "population" else CORRESPONDENCE_VERDICTS)
    checkpoint = fit_portable_ovr(
        matrix[train_indices],
        [gold_rows[index] for index in train_indices],
        task=task,
        feature_names=feature_names,
        labels=labels,
        target_precision=target_precision,
        receipt={
            "corpus_id": corpus["corpus_id"],
            "seed": seed,
            "target_precision": target_precision,
            "train_groups": sorted(
                group for group, split in assignments.items() if split == "train"
            ),
            "test_groups": sorted(group for group, split in assignments.items() if split == "test"),
            "train_rows": len(train_indices),
            "test_rows": len(test_indices),
        },
    )
    predictions = [set(row) for row in checkpoint.predict(matrix[test_indices])]
    metrics = multilabel_metrics(
        [gold_rows[index] for index in test_indices],
        predictions,
        labels,
    )
    report = {
        "schema_version": "rvbbit.business-topology.train-report.v1",
        "task": task,
        "corpus_id": corpus["corpus_id"],
        "assignments": assignments,
        "metrics": metrics,
        "test_predictions": [
            {
                "id": row_ids[index],
                "gold": sorted(gold_rows[index]),
                "predicted": sorted(predicted),
            }
            for index, predicted in zip(test_indices, predictions)
        ],
    }
    return checkpoint, report
