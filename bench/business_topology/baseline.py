"""Explainable deterministic baselines for topology model development.

These heuristics are not intended to become corporate truth.  They provide a
portable minimum bar: a trained specialist must beat them on held-out object
families and must preserve calibrated abstention on ambiguous pairs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .contracts import POPULATION_ROLES
from .features import correspondence_features, packet_field, packet_roles, population_features


def _clamp(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def population_role_scores(packet: Mapping[str, Any]) -> dict[str, float]:
    field = packet_field(packet)
    hints = packet_roles(packet)
    features = population_features(packet)
    scores = {role: 0.04 for role in POPULATION_ROLES}
    for role in hints:
        scores[role] = 0.76

    type_family = str(field.get("type_family") or "other")
    sensitivity = str(field.get("sensitivity_hint") or "unknown")
    if sensitivity in {"identifier", "direct_identifier", "restricted"}:
        scores["identity"] = max(scores["identity"], 0.68)
    if features["cardinality_ratio"] >= 0.92 and type_family in {"identifier", "text", "number"}:
        scores["identity"] = max(scores["identity"], 0.62)
    if features["declared_pk"] or features["declared_fk"]:
        scores["identity"] = max(scores["identity"], 0.91)
    if type_family == "time":
        scores["time"] = max(scores["time"], 0.84)
    if type_family == "document":
        scores["evidence"] = max(scores["evidence"], 0.82)
    if type_family == "text" and features["shape:long_text"] + features["shape:phrase"] >= 0.5:
        scores["evidence"] = max(scores["evidence"], 0.66)
    if features["shape:email"] >= 0.25 or features["shape:uuid"] >= 0.5:
        scores["identity"] = max(scores["identity"], 0.87)
    if features["shape:date_or_timestamp"] >= 0.5:
        scores["time"] = max(scores["time"], 0.82)
    return {role: _clamp(score) for role, score in scores.items()}


def predict_population_roles(
    packet: Mapping[str, Any],
    *,
    threshold: float = 0.6,
) -> dict[str, Any]:
    scores = population_role_scores(packet)
    roles = [role for role in POPULATION_ROLES if scores[role] >= threshold]
    return {
        "roles": roles,
        "scores": scores,
        "uncertainty": 1.0 - max(scores.values(), default=0.0),
        "model": "deterministic-population-baseline-v1",
    }


def correspondence_scores(packet: Mapping[str, Any]) -> dict[str, float]:
    features = correspondence_features(packet)
    left_roles = packet_roles(packet["left"])
    right_roles = packet_roles(packet["right"])

    overlap_strength = _clamp(
        0.42 * features["containment"]
        + 0.24 * features["jaccard"]
        + 0.12 * features["fingerprint_balance"]
        + 0.12 * min(features["log_shared"] / math.log1p(128), 1.0)
        + 0.10 * min(features["log_query_join_count"] / math.log1p(10), 1.0)
    )
    semantic_strength = _clamp(
        0.26 * features["name_token_overlap"]
        + 0.18 * features["role_overlap"]
        + 0.18 * features["shape_overlap"]
        + 0.16 * features["type_compatibility"]
        + 0.22 * max(features["embedding_similarity"], 0.0)
    )
    usage_strength = _clamp(
        0.65 * min(features["log_query_join_count"] / math.log1p(10), 1.0)
        + 0.35 * min(features["log_query_cooccurrence_count"] / math.log1p(10), 1.0)
    )

    same_instance = _clamp(
        features["both_identity"]
        * (
            0.50 * features["containment"]
            + 0.20 * overlap_strength
            + 0.10 * features["cardinality_similarity"]
            + 0.10 * features["type_compatibility"]
            + 0.10 * usage_strength
        )
    )
    joinable = _clamp(
        0.58 * overlap_strength
        + 0.20 * max(features["left_containment"], features["right_containment"])
        + 0.17 * usage_strength
        + 0.05 * features["declared_link"]
    )
    same_facet = _clamp(
        0.42 * semantic_strength
        + 0.28 * overlap_strength
        + 0.20 * max(features["embedding_similarity"], 0.0)
        + 0.10 * features["cardinality_similarity"]
    )
    same_concept = _clamp(
        0.43 * semantic_strength
        + 0.20 * overlap_strength
        + 0.27 * max(features["embedding_similarity"], 0.0)
        + 0.10 * usage_strength
    )

    left_identity = "identity" in left_roles
    right_identity = "identity" in right_roles
    direction_support = _clamp(0.55 * semantic_strength + 0.30 * usage_strength + 0.15 * joinable)
    attribute_of = direction_support if left_identity != right_identity else 0.0

    def directional(role: str) -> float:
        role_to_identity = (role in left_roles and right_identity) or (
            role in right_roles and left_identity
        )
        return direction_support if role_to_identity else 0.0

    measurement_of = directional("measure")
    category_of = max(directional("category"), directional("status"))
    time_of = directional("time")
    geography_of = directional("geography")

    left_kind = str(packet["left"].get("population", {}).get("kind", ""))
    right_kind = str(packet["right"].get("population", {}).get("kind", ""))
    event_about = (
        direction_support
        if (
            (left_kind == "event_stream" and right_identity)
            or (right_kind == "event_stream" and left_identity)
        )
        else 0.0
    )
    correlated = _clamp(
        0.48 * max(features["embedding_similarity"], 0.0)
        + 0.34 * usage_strength
        + 0.18 * semantic_strength
    )

    semantic_max = max(
        same_concept,
        same_facet,
        same_instance,
        joinable,
        attribute_of,
        event_about,
        measurement_of,
        category_of,
        time_of,
        geography_of,
        correlated,
    )
    unrelated = _clamp(1.0 - semantic_max)
    ranked = sorted(
        (
            same_concept,
            same_facet,
            same_instance,
            joinable,
            attribute_of,
            event_about,
            measurement_of,
            category_of,
            time_of,
            geography_of,
            correlated,
        ),
        reverse=True,
    )
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
    evidence_mass = max(
        overlap_strength,
        usage_strength,
        max(features["embedding_similarity"], 0.0),
    )
    abstain = _clamp(
        (0.58 if evidence_mass < 0.28 else 0.0)
        + (
            0.60
            if overlap_strength < 0.08
            and usage_strength == 0
            and 0.20 <= features["embedding_similarity"] < 0.62
            else 0.0
        )
        + (0.30 if margin < 0.07 and ranked[0] < 0.82 else 0.0)
        + (0.18 if features["type_compatibility"] < 0.4 and overlap_strength < 0.2 else 0.0)
    )
    return {
        "same_concept": same_concept,
        "same_facet": same_facet,
        "same_instance_key": same_instance,
        "joinable": joinable,
        "attribute_of": attribute_of,
        "event_about": event_about,
        "measurement_of": measurement_of,
        "category_of": category_of,
        "time_of": time_of,
        "geography_of": geography_of,
        "correlated": correlated,
        "unrelated": unrelated,
        "abstain": abstain,
    }


def predict_correspondence(
    packet: Mapping[str, Any],
    *,
    threshold: float = 0.62,
    abstain_threshold: float = 0.55,
) -> dict[str, Any]:
    scores = correspondence_scores(packet)
    semantic_labels = [
        label
        for label in (
            "same_concept",
            "same_facet",
            "same_instance_key",
            "joinable",
            "attribute_of",
            "event_about",
            "measurement_of",
            "category_of",
            "time_of",
            "geography_of",
            "correlated",
        )
        if scores[label] >= threshold
    ]
    if scores["abstain"] >= abstain_threshold:
        verdicts = ["abstain"]
    elif semantic_labels:
        # ``correlated`` is a useful fallback verdict, not an extra label on
        # top of a more specific same-facet/key/relationship conclusion.
        specific = [label for label in semantic_labels if label != "correlated"]
        verdicts = specific or semantic_labels
    elif scores["unrelated"] >= threshold:
        verdicts = ["unrelated"]
    else:
        verdicts = ["abstain"]
    confidence = max(scores[label] for label in verdicts)
    return {
        "verdicts": verdicts,
        "scores": scores,
        "confidence": confidence,
        "uncertainty": 1.0 - confidence,
        "model": "deterministic-correspondence-baseline-v1",
    }
