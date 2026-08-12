"""Deterministic, privacy-safe features for topology baselines.

These features are deliberately business-vocabulary agnostic.  They describe
shape, role hints, bounded cardinality behavior, local overlap, and optional
embedding similarity.  A trained specialist may add richer representations,
but these features remain a stable floor and an explanation surface.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .contracts import POPULATION_ROLES, validate_outbound_packet


TYPE_FAMILIES = (
    "number",
    "time",
    "boolean",
    "document",
    "binary",
    "identifier",
    "array",
    "text",
    "other",
)

VALUE_SHAPES = (
    "email",
    "uuid",
    "url",
    "integer",
    "decimal",
    "date_or_timestamp",
    "boolean",
    "structured_text",
    "long_text",
    "phrase",
    "token",
    "text",
    "empty",
    "null",
)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _bounded(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(max(_number(value), lower), upper)


def _tokens(value: Any) -> set[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw = [str(item).lower() for item in value]
    else:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        raw = re.split(r"[^a-zA-Z0-9]+", text.lower())
    return {
        token
        for token in raw
        if token and token not in {"id", "key", "code", "field", "value", "data", "table", "tbl"}
    }


def _set_overlap(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _shape_distribution(field: Mapping[str, Any]) -> dict[str, float]:
    raw = field.get("value_shapes")
    if not isinstance(raw, Mapping):
        return {}
    counts = {str(key): max(_number(value), 0.0) for key, value in raw.items()}
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {key: value / total for key, value in counts.items()}


def _shape_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_dist = _shape_distribution(left)
    right_dist = _shape_distribution(right)
    keys = set(left_dist) | set(right_dist)
    if not keys:
        return 0.0
    # Histogram intersection is robust when samples have different sizes.
    return sum(min(left_dist.get(key, 0.0), right_dist.get(key, 0.0)) for key in keys)


def _type_compatibility(left: str, right: str) -> float:
    if not left or not right:
        return 0.5
    if left == right:
        return 1.0
    stringish = {"text", "identifier"}
    if left in stringish and right in stringish:
        return 0.8
    if left == "other" or right == "other":
        return 0.4
    return 0.0


def packet_field(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    field = packet.get("field")
    return field if isinstance(field, Mapping) else {}


def packet_roles(packet: Mapping[str, Any]) -> set[str]:
    field = packet_field(packet)
    roles = field.get("role_hints", [])
    if not isinstance(roles, list):
        return set()
    return {str(role) for role in roles if role in POPULATION_ROLES}


def packet_name_tokens(packet: Mapping[str, Any]) -> set[str]:
    field = packet_field(packet)
    if field:
        return _tokens(field.get("name_tokens") or field.get("name"))
    context = packet.get("relation_context")
    if isinstance(context, Mapping):
        return _tokens(context.get("name_tokens") or context.get("name"))
    population = packet.get("population")
    if isinstance(population, Mapping):
        return _tokens(population.get("population_key"))
    return set()


def population_features(packet: Mapping[str, Any]) -> dict[str, float]:
    validate_outbound_packet(packet)
    field = packet_field(packet)
    roles = packet_roles(packet)
    type_family = str(field.get("type_family") or "other")
    features: dict[str, float] = {
        "has_field": 1.0 if field else 0.0,
        "null_fraction": _bounded(field.get("null_fraction")),
        "cardinality_ratio": _bounded(field.get("cardinality_ratio")),
        "log_sample_distinct": math.log1p(max(_number(field.get("sample_distinct")), 0.0)),
        "log_average_length": math.log1p(max(_number(field.get("average_length")), 0.0)),
        "log_maximum_length": math.log1p(max(_number(field.get("maximum_length")), 0.0)),
        "declared_pk": 1.0 if field.get("declared_pk") is True else 0.0,
        "declared_fk": 1.0 if field.get("declared_fk") is True else 0.0,
        "sensitive_identifier": 1.0
        if field.get("sensitivity_hint") in {"identifier", "direct_identifier", "restricted"}
        else 0.0,
    }
    for family in TYPE_FAMILIES:
        features[f"type:{family}"] = 1.0 if type_family == family else 0.0
    for role in POPULATION_ROLES:
        features[f"hint:{role}"] = 1.0 if role in roles else 0.0
    shapes = _shape_distribution(field)
    for shape in VALUE_SHAPES:
        features[f"shape:{shape}"] = shapes.get(shape, 0.0)

    context = packet.get("relation_context")
    if isinstance(context, Mapping):
        fields = context.get("fields", [])
        pairs = context.get("field_pairs", [])
        features["context_field_count"] = math.log1p(len(fields) if isinstance(fields, list) else 0)
        features["context_pair_count"] = math.log1p(len(pairs) if isinstance(pairs, list) else 0)
    else:
        neighbors = packet.get("neighbor_fields", [])
        features["context_field_count"] = math.log1p(
            len(neighbors) if isinstance(neighbors, list) else 0
        )
        pairs = packet.get("field_pair_features", [])
        features["context_pair_count"] = math.log1p(len(pairs) if isinstance(pairs, list) else 0)
    return features


def correspondence_features(packet: Mapping[str, Any]) -> dict[str, float]:
    validate_outbound_packet(packet)
    left = packet.get("left")
    right = packet.get("right")
    evidence = packet.get("local_evidence", {})
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise ValueError("correspondence packet requires left and right packet objects")
    if not isinstance(evidence, Mapping):
        evidence = {}

    left_field = packet_field(left)
    right_field = packet_field(right)
    left_roles = packet_roles(left)
    right_roles = packet_roles(right)
    left_type = str(left_field.get("type_family") or "other")
    right_type = str(right_field.get("type_family") or "other")
    left_count = max(_number(evidence.get("left_fingerprints")), 0.0)
    right_count = max(_number(evidence.get("right_fingerprints")), 0.0)
    shared = max(_number(evidence.get("shared_fingerprints")), 0.0)
    left_containment = shared / left_count if left_count else 0.0
    right_containment = shared / right_count if right_count else 0.0

    left_source = left.get("source", {})
    right_source = right.get("source", {})
    same_source = (
        isinstance(left_source, Mapping)
        and isinstance(right_source, Mapping)
        and (left_source == right_source)
    )

    features: dict[str, float] = {
        "name_token_overlap": _bounded(
            evidence.get(
                "name_token_overlap",
                _set_overlap(packet_name_tokens(left), packet_name_tokens(right)),
            )
        ),
        "role_overlap": _set_overlap(left_roles, right_roles),
        "shape_overlap": _shape_overlap(left_field, right_field),
        "type_compatibility": _type_compatibility(left_type, right_type),
        "jaccard": _bounded(evidence.get("jaccard")),
        "containment": _bounded(evidence.get("containment")),
        "left_containment": _bounded(left_containment),
        "right_containment": _bounded(right_containment),
        "log_shared": math.log1p(shared),
        "fingerprint_balance": min(left_count, right_count) / max(left_count, right_count)
        if max(left_count, right_count) > 0
        else 0.0,
        "cardinality_similarity": 1.0
        - min(
            abs(
                _bounded(left_field.get("cardinality_ratio"))
                - _bounded(right_field.get("cardinality_ratio"))
            ),
            1.0,
        ),
        "both_identity": 1.0 if "identity" in left_roles and "identity" in right_roles else 0.0,
        "either_identity": 1.0 if "identity" in left_roles or "identity" in right_roles else 0.0,
        "same_source": 1.0 if same_source else 0.0,
        "declared_link": 1.0
        if left_field.get("declared_fk") is True or right_field.get("declared_fk") is True
        else 0.0,
        "embedding_similarity": _bounded(evidence.get("embedding_similarity"), -1.0, 1.0),
        "log_query_join_count": math.log1p(max(_number(evidence.get("query_join_count")), 0.0)),
        "log_query_cooccurrence_count": math.log1p(
            max(_number(evidence.get("query_cooccurrence_count")), 0.0)
        ),
    }
    for role in POPULATION_ROLES:
        features[f"left_role:{role}"] = 1.0 if role in left_roles else 0.0
        features[f"right_role:{role}"] = 1.0 if role in right_roles else 0.0
    return features


def vectorize_feature_dicts(
    rows: Sequence[Mapping[str, float]],
    feature_names: Sequence[str] | None = None,
) -> tuple[np.ndarray, list[str]]:
    names = list(feature_names or sorted({key for row in rows for key in row}))
    matrix = np.asarray(
        [[_number(row.get(name)) for name in names] for row in rows],
        dtype=np.float64,
    )
    if matrix.ndim == 1:
        matrix = matrix.reshape((len(rows), len(names)))
    return matrix, names
