"""Small portable one-vs-rest baseline with calibrated proposal thresholds.

This is an explainable benchmark artifact, not the final Clover architecture.
It proves that reviewed corpora can train and receipt a model without depending
on one customer's vocabulary or importing an ML runtime into PostgreSQL.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np


CHECKPOINT_SCHEMA_VERSION = "rvbbit.business-topology.linear-checkpoint.v1"


def _sigmoid(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _threshold_for_precision(
    probabilities: np.ndarray,
    labels: np.ndarray,
    *,
    target_precision: float,
) -> float:
    best: tuple[float, float] | None = None
    for threshold in np.linspace(0.50, 0.99, 100):
        predicted = probabilities >= threshold
        tp = int(np.sum(predicted & (labels == 1)))
        fp = int(np.sum(predicted & (labels == 0)))
        fn = int(np.sum(~predicted & (labels == 1)))
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        if tp and precision >= target_precision:
            candidate = (recall, -float(threshold))
            if best is None or candidate > best:
                best = candidate
    return -best[1] if best is not None else 0.99


@dataclass
class PortableOVR:
    task: str
    feature_names: list[str]
    labels: list[str]
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray
    bias: np.ndarray
    thresholds: np.ndarray
    receipt: dict[str, Any]

    def predict_proba(self, matrix: np.ndarray) -> np.ndarray:
        values = np.asarray(matrix, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != len(self.feature_names):
            raise ValueError("feature matrix does not match checkpoint feature_names")
        normalized = (values - self.mean) / self.scale
        return _sigmoid(normalized @ self.weights.T + self.bias)

    def predict(self, matrix: np.ndarray) -> list[list[str]]:
        probabilities = self.predict_proba(matrix)
        rows: list[list[str]] = []
        for probability_row in probabilities:
            selected = [
                label
                for label, probability, threshold in zip(
                    self.labels,
                    probability_row,
                    self.thresholds,
                )
                if probability >= threshold
            ]
            if self.task == "correspondence":
                if "abstain" in selected:
                    selected = ["abstain"]
                elif not selected:
                    selected = ["abstain"]
                elif "correlated" in selected and len(selected) > 1:
                    selected.remove("correlated")
            rows.append(selected)
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "task": self.task,
            "feature_names": self.feature_names,
            "labels": self.labels,
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "weights": self.weights.tolist(),
            "bias": self.bias.tolist(),
            "thresholds": self.thresholds.tolist(),
            "receipt": self.receipt,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PortableOVR":
        if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported linear checkpoint schema_version")
        return cls(
            task=str(value["task"]),
            feature_names=[str(item) for item in value["feature_names"]],
            labels=[str(item) for item in value["labels"]],
            mean=np.asarray(value["mean"], dtype=np.float64),
            scale=np.asarray(value["scale"], dtype=np.float64),
            weights=np.asarray(value["weights"], dtype=np.float64),
            bias=np.asarray(value["bias"], dtype=np.float64),
            thresholds=np.asarray(value["thresholds"], dtype=np.float64),
            receipt=dict(value.get("receipt", {})),
        )

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def fit_portable_ovr(
    matrix: np.ndarray,
    gold_rows: Sequence[set[str]],
    *,
    task: str,
    feature_names: Sequence[str],
    labels: Sequence[str],
    target_precision: float = 0.98,
    steps: int = 1200,
    learning_rate: float = 0.08,
    l2: float = 0.002,
    receipt: Mapping[str, Any] | None = None,
) -> PortableOVR:
    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] != len(gold_rows):
        raise ValueError("training matrix and gold row lengths differ")
    if values.shape[0] < 2:
        raise ValueError("at least two training rows are required")
    names = list(feature_names)
    label_names = list(labels)
    if values.shape[1] != len(names):
        raise ValueError("training matrix width does not match feature_names")

    mean = np.mean(values, axis=0)
    scale = np.std(values, axis=0)
    scale[scale < 1e-9] = 1.0
    normalized = (values - mean) / scale
    targets = np.asarray(
        [[1.0 if label in gold else 0.0 for label in label_names] for gold in gold_rows],
        dtype=np.float64,
    )
    weights = np.zeros((len(label_names), len(names)), dtype=np.float64)
    bias = np.zeros(len(label_names), dtype=np.float64)

    for label_index in range(len(label_names)):
        target = targets[:, label_index]
        positives = float(np.sum(target))
        negatives = float(len(target) - positives)
        if positives == 0 or negatives == 0:
            prevalence = (positives + 0.5) / (len(target) + 1.0)
            bias[label_index] = math.log(prevalence / (1.0 - prevalence))
            continue
        positive_weight = len(target) / (2.0 * positives)
        negative_weight = len(target) / (2.0 * negatives)
        sample_weights = np.where(target == 1.0, positive_weight, negative_weight)
        weight = weights[label_index]
        intercept = 0.0
        for step in range(steps):
            probability = _sigmoid(normalized @ weight + intercept)
            error = (probability - target) * sample_weights
            rate = learning_rate / math.sqrt(1.0 + step / 180.0)
            weight -= rate * ((normalized.T @ error) / len(target) + l2 * weight)
            intercept -= rate * float(np.mean(error))
        weights[label_index] = weight
        bias[label_index] = intercept

    probabilities = _sigmoid(normalized @ weights.T + bias)
    thresholds = np.asarray(
        [
            _threshold_for_precision(
                probabilities[:, label_index],
                targets[:, label_index],
                target_precision=target_precision,
            )
            for label_index in range(len(label_names))
        ],
        dtype=np.float64,
    )
    return PortableOVR(
        task=task,
        feature_names=names,
        labels=label_names,
        mean=mean,
        scale=scale,
        weights=weights,
        bias=bias,
        thresholds=thresholds,
        receipt=dict(receipt or {}),
    )
