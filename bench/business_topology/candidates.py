"""Bounded, explainable candidate generation for human label queues."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .contracts import validate_corpus
from .features import packet_field, packet_name_tokens, packet_roles
from .packets import make_correspondence_packet, stable_hash


CANDIDATE_STRATA = frozenset(
    {
        "local_overlap",
        "semantic_neighbor",
        "usage_evidence",
        "hard_negative_probe",
        "diversity_probe",
    }
)


def pair_key(left_population_id: str, right_population_id: str) -> str:
    left, right = sorted((left_population_id, right_population_id))
    return f"{left}\x1f{right}"


def pair_id(left_population_id: str, right_population_id: str) -> str:
    digest = hashlib.sha256(pair_key(left_population_id, right_population_id).encode()).hexdigest()
    return f"pair:{digest[:24]}"


def population_index(corpus: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["population_id"]): item for item in corpus.get("populations", [])}


def correspondence_packet_for_item(
    corpus: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(item.get("packet"), Mapping):
        return deepcopy(dict(item["packet"]))
    populations = population_index(corpus)
    left = populations[str(item["left_population_id"])]["packet"]
    right = populations[str(item["right_population_id"])]["packet"]
    evidence = item.get("local_evidence")
    return make_correspondence_packet(
        left,
        right,
        local_evidence=evidence if isinstance(evidence, Mapping) else None,
    )


def candidate_priority(features: Mapping[str, float]) -> tuple[float, str]:
    semantic = (
        0.24 * features["name_token_overlap"]
        + 0.20 * features["role_overlap"]
        + 0.18 * features["shape_overlap"]
        + 0.18 * features["type_compatibility"]
        + 0.20 * max(features["embedding_similarity"], 0.0)
    )
    overlap = (
        0.45 * features["containment"]
        + 0.30 * features["jaccard"]
        + 0.15 * min(features["log_shared"] / 4.0, 1.0)
        + 0.10 * features["fingerprint_balance"]
    )
    usage = min(
        (features["log_query_join_count"] + features["log_query_cooccurrence_count"]) / 4.0,
        1.0,
    )
    priority = min(1.0, 0.42 * semantic + 0.43 * overlap + 0.15 * usage)

    if overlap >= 0.6:
        stratum = "local_overlap"
    elif semantic >= 0.62:
        stratum = "semantic_neighbor"
    elif (
        features["type_compatibility"] >= 0.8
        and features["shape_overlap"] >= 0.65
        and features["name_token_overlap"] <= 0.15
    ):
        # These deceptive pairs are intentionally retained to measure whether
        # a model invents edges from format similarity alone.
        stratum = "hard_negative_probe"
        priority = max(priority, 0.34)
    elif usage > 0:
        stratum = "usage_evidence"
    else:
        stratum = "diversity_probe"
    return priority, stratum


def _overlap(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _type_compatibility(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left in {"text", "identifier"} and right in {"text", "identifier"}:
        return 0.8
    if left == "other" or right == "other":
        return 0.4
    return 0.0


def _signature(item: Mapping[str, Any]) -> dict[str, Any]:
    packet = item["packet"]
    field = packet_field(packet)
    raw_shapes = field.get("value_shapes", {})
    shape_counts = (
        {str(key): max(float(value or 0), 0.0) for key, value in raw_shapes.items()}
        if isinstance(raw_shapes, Mapping)
        else {}
    )
    shape_total = sum(shape_counts.values())
    shapes = (
        {key: value / shape_total for key, value in shape_counts.items()} if shape_total else {}
    )
    source = packet.get("source", {})
    source_key = stable_hash(source)[:20]
    return {
        "item": item,
        "population_id": str(item["population_id"]),
        "source_key": source_key,
        "tokens": packet_name_tokens(packet),
        "roles": packet_roles(packet),
        "type": str(field.get("type_family") or "other"),
        "shapes": shapes,
        "dominant_shape": max(shapes, key=lambda shape: shapes[shape]) if shapes else "unknown",
        "cardinality": min(max(float(field.get("cardinality_ratio") or 0.0), 0.0), 1.0),
    }


def _light_features(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
) -> dict[str, float]:
    evidence = evidence or {}
    shared = max(float(evidence.get("shared_fingerprints", 0) or 0), 0.0)
    left_count = max(float(evidence.get("left_fingerprints", 0) or 0), 0.0)
    right_count = max(float(evidence.get("right_fingerprints", 0) or 0), 0.0)
    shape_keys = set(left["shapes"]) | set(right["shapes"])
    shape_overlap = sum(
        min(left["shapes"].get(key, 0.0), right["shapes"].get(key, 0.0)) for key in shape_keys
    )
    return {
        "name_token_overlap": min(
            max(
                float(
                    evidence.get("name_token_overlap", _overlap(left["tokens"], right["tokens"]))
                ),
                0.0,
            ),
            1.0,
        ),
        "role_overlap": _overlap(left["roles"], right["roles"]),
        "shape_overlap": shape_overlap,
        "type_compatibility": _type_compatibility(left["type"], right["type"]),
        "jaccard": min(max(float(evidence.get("jaccard", 0) or 0), 0.0), 1.0),
        "containment": min(max(float(evidence.get("containment", 0) or 0), 0.0), 1.0),
        "log_shared": math.log1p(shared),
        "fingerprint_balance": min(left_count, right_count) / max(left_count, right_count)
        if max(left_count, right_count)
        else 0.0,
        "embedding_similarity": min(
            max(float(evidence.get("embedding_similarity", 0) or 0), -1.0),
            1.0,
        ),
        "log_query_join_count": math.log1p(
            max(float(evidence.get("query_join_count", 0) or 0), 0.0)
        ),
        "log_query_cooccurrence_count": math.log1p(
            max(float(evidence.get("query_cooccurrence_count", 0) or 0), 0.0)
        ),
    }


def _blocked_pairs(
    signatures: list[dict[str, Any]],
    *,
    block_fanout: int,
    max_cross_sources: int = 8,
) -> set[tuple[int, int]]:
    """Generate sub-quadratic semantic and diversity blocks."""

    buckets: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    source_buckets: defaultdict[str, list[int]] = defaultdict(list)
    for index, signature in enumerate(signatures):
        source_buckets[signature["source_key"]].append(index)
        for token in signature["tokens"]:
            buckets[("token", token)].append(index)
        for role in signature["roles"]:
            buckets[("role-type", f"{role}:{signature['type']}")].append(index)
        buckets[("shape-type", f"{signature['dominant_shape']}:{signature['type']}")].append(index)

    pairs: set[tuple[int, int]] = set()
    for bucket in buckets.values():
        ordered = sorted(
            set(bucket),
            key=lambda index: hashlib.sha256(
                signatures[index]["population_id"].encode()
            ).hexdigest(),
        )
        if len(ordered) < 2:
            continue
        neighbor_count = min(block_fanout, len(ordered) - 1)
        for position, left_index in enumerate(ordered):
            for offset in range(1, neighbor_count + 1):
                right_index = ordered[(position + offset) % len(ordered)]
                if signatures[left_index]["source_key"] == signatures[right_index]["source_key"]:
                    continue
                pairs.add((min(left_index, right_index), max(left_index, right_index)))

    # Add deterministic cross-source probes so absent tokens do not make a
    # source invisible and every run retains some negative/control coverage.
    sources = sorted(source_buckets)
    for left_index, signature in enumerate(signatures):
        other_sources = [source for source in sources if source != signature["source_key"]]
        other_sources.sort(
            key=lambda source: hashlib.sha256(
                f"{signature['population_id']}\x1f{source}".encode()
            ).hexdigest()
        )
        for source in other_sources[:max_cross_sources]:
            candidates = source_buckets[source]
            right_index = min(
                candidates,
                key=lambda index: hashlib.sha256(
                    f"{signature['population_id']}\x1f{signatures[index]['population_id']}".encode()
                ).hexdigest(),
            )
            pairs.add((min(left_index, right_index), max(left_index, right_index)))
    return pairs


def build_candidate_queue(
    corpus: Mapping[str, Any],
    *,
    max_pairs: int = 500,
    max_fanout: int = 12,
    minimum_priority: float = 0.12,
    evidence_by_pair: Mapping[str, Mapping[str, Any]] | None = None,
    allowed_strata: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a bounded review queue without assuming source-specific nouns."""

    validate_corpus(corpus)
    evidence_by_pair = evidence_by_pair or {}
    requested_strata = set(CANDIDATE_STRATA if allowed_strata is None else allowed_strata)
    unknown_strata = requested_strata - CANDIDATE_STRATA
    if unknown_strata:
        raise ValueError(f"unknown candidate strata: {sorted(unknown_strata)}")
    if not requested_strata:
        raise ValueError("allowed_strata must retain at least one candidate stratum")
    populations = [
        item
        for item in corpus.get("populations", [])
        if item.get("packet", {}).get("population", {}).get("kind") != "record_context"
    ]
    existing = {
        pair_key(str(item["left_population_id"]), str(item["right_population_id"]))
        for item in corpus.get("correspondences", [])
    }
    signatures = [_signature(item) for item in populations]
    blocked = _blocked_pairs(signatures, block_fanout=max(8, min(max_fanout * 4, 32)))
    index_by_id = {signature["population_id"]: index for index, signature in enumerate(signatures)}
    for key in evidence_by_pair:
        parts = key.split("\x1f")
        if len(parts) != 2 or parts[0] not in index_by_id or parts[1] not in index_by_id:
            raise ValueError("pair evidence references a population outside the corpus")
        left_index = index_by_id[parts[0]]
        right_index = index_by_id[parts[1]]
        blocked.add((min(left_index, right_index), max(left_index, right_index)))
    candidates: list[dict[str, Any]] = []
    for left_index, right_index in blocked:
        left = populations[left_index]
        right = populations[right_index]
        if signatures[left_index]["source_key"] == signatures[right_index]["source_key"]:
            # External evidence adapters may legitimately emit within-source
            # pairs, but this queue is the cross-source correspondence path.
            # Source-internal structure belongs to motif discovery.
            continue
        left_id = str(left["population_id"])
        right_id = str(right["population_id"])
        key = pair_key(left_id, right_id)
        if key in existing:
            continue
        evidence = evidence_by_pair.get(key)
        features = _light_features(signatures[left_index], signatures[right_index], evidence)
        priority, stratum = candidate_priority(features)
        if stratum not in requested_strata:
            continue
        if priority < minimum_priority and stratum != "hard_negative_probe":
            continue
        pair_groups = sorted({str(left["split_group"]), str(right["split_group"])})
        candidates.append(
            {
                "pair_id": pair_id(left_id, right_id),
                "split_group": "pair-group:"
                + hashlib.sha256("\x1f".join(pair_groups).encode()).hexdigest()[:16],
                "left_population_id": left_id,
                "right_population_id": right_id,
                "local_evidence": dict(evidence or {}),
                "review": {
                    "status": "unreviewed",
                    "stratum": stratum,
                    "priority": round(priority, 6),
                    "feature_summary": {
                        feature_name: round(features[feature_name], 6)
                        for feature_name in (
                            "name_token_overlap",
                            "role_overlap",
                            "shape_overlap",
                            "type_compatibility",
                            "jaccard",
                            "containment",
                            "embedding_similarity",
                        )
                    },
                },
            }
        )

    candidates.sort(
        key=lambda item: (
            -float(item["review"]["priority"]),
            str(item["review"]["stratum"]),
            str(item["pair_id"]),
        )
    )
    pool_strata = {
        stratum: sum(1 for item in candidates if item["review"]["stratum"] == stratum)
        for stratum in sorted({item["review"]["stratum"] for item in candidates})
    }
    fanout: defaultdict[str, int] = defaultdict(int)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def select(candidate: dict[str, Any]) -> bool:
        left_id = str(candidate["left_population_id"])
        right_id = str(candidate["right_population_id"])
        if fanout[left_id] >= max_fanout or fanout[right_id] >= max_fanout:
            return False
        left = populations[index_by_id[candidate["left_population_id"]]]
        right = populations[index_by_id[candidate["right_population_id"]]]
        materialized = deepcopy(candidate)
        materialized["packet"] = make_correspondence_packet(
            left["packet"],
            right["packet"],
            local_evidence=materialized.pop("local_evidence", None),
        )
        selected.append(materialized)
        selected_ids.add(str(candidate["pair_id"]))
        fanout[left_id] += 1
        fanout[right_id] += 1
        return True

    # Preserve useful review diversity. Without quotas, plentiful same-format
    # negatives can crowd every semantic or overlap candidate out of the top N.
    stratum_weights = {
        "local_overlap": 0.30,
        "semantic_neighbor": 0.30,
        "usage_evidence": 0.15,
        "hard_negative_probe": 0.20,
        "diversity_probe": 0.05,
    }
    for stratum, weight in stratum_weights.items():
        target = max(round(max_pairs * weight), 1)
        taken = 0
        for candidate in candidates:
            if candidate["review"]["stratum"] != stratum:
                continue
            if select(candidate):
                taken += 1
            if taken >= target or len(selected) >= max_pairs:
                break
        if len(selected) >= max_pairs:
            break

    # Redistribute unfilled quotas to the best remaining evidence while still
    # respecting the same per-population fanout bound.
    if len(selected) < max_pairs:
        for candidate in candidates:
            if candidate["pair_id"] in selected_ids:
                continue
            if candidate["review"]["stratum"] == "hard_negative_probe":
                continue
            select(candidate)
            if len(selected) >= max_pairs:
                break

    result = deepcopy(dict(corpus))
    result["correspondences"] = list(result.get("correspondences", [])) + selected
    result.setdefault("generation", {})["candidate_queue"] = {
        "max_pairs": max_pairs,
        "max_fanout": max_fanout,
        "minimum_priority": minimum_priority,
        "allowed_strata": sorted(requested_strata),
        "selected": len(selected),
        "pool_strata": pool_strata,
        "strata": {
            stratum: sum(1 for item in selected if item["review"]["stratum"] == stratum)
            for stratum in sorted({item["review"]["stratum"] for item in selected})
        },
    }
    validate_corpus(result)
    return result
