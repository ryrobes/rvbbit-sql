"""Adaptive source neighborhoods for heterogeneous business-data corpora.

This module deliberately proposes unnamed source neighborhoods rather than a
flat customer ontology.  It uses source-level semantic representations to find
reciprocal local affinity, keeps singleton sources when the evidence is weak,
and records broader links as bridges instead of using them to merge every
connected source into one sprawling domain.

The implementation is source-adapter neutral.  A ``record_context`` population
is the preferred representation when an adapter supplies one; otherwise the
available population vectors for that source are pooled into a centroid.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from .contracts import ContractError, validate_corpus
from .embedding import resolve_channel_weights
from .packets import stable_hash


SOURCE_NEIGHBORHOOD_SCHEMA_VERSION = "rvbbit.business-topology.source-neighborhoods.v1"


def make_source_key(source: Mapping[str, Any]) -> str:
    return "source:" + stable_hash(source)[:24]


def _normalized(vector: np.ndarray, *, path: str) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm == 0:
        raise ContractError(f"{path} must have a finite, non-zero norm")
    return vector / norm


def _source_representations(
    corpus: Mapping[str, Any],
    vector_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    """Validate population vectors and pool them into source representations."""

    validate_corpus(corpus)
    populations = {str(item["population_id"]): item for item in corpus.get("populations", [])}
    vectors: dict[tuple[str, str], np.ndarray] = {}
    dimensions: dict[str, int] = {}
    for row_number, row in enumerate(vector_rows, 1):
        if not isinstance(row, Mapping):
            raise ContractError(f"embedding row {row_number} must be an object")
        population_id = row.get("population_id")
        channel = row.get("channel", "combined")
        vector = row.get("embedding", row.get("vector"))
        if not isinstance(population_id, str) or population_id not in populations:
            raise ContractError(
                f"embedding row {row_number} references a population outside the corpus"
            )
        if not isinstance(channel, str) or not channel:
            raise ContractError(f"embedding row {row_number} channel must be non-empty")
        if not isinstance(vector, list) or not vector:
            raise ContractError(f"embedding row {row_number} vector must be a non-empty array")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector):
            raise ContractError(f"embedding row {row_number} vector must contain finite numbers")
        key = (population_id, channel)
        if key in vectors:
            raise ContractError(f"duplicate {channel} embedding row for {population_id}")
        expected_dimension = dimensions.setdefault(channel, len(vector))
        if len(vector) != expected_dimension:
            raise ContractError(f"all {channel} embedding vectors must have the same dimension")
        vectors[key] = _normalized(
            np.asarray(vector, dtype=np.float32),
            path=f"embedding row {row_number}",
        )

    if not vectors:
        raise ContractError("source neighborhoods require population embeddings")

    population_channels: dict[str, set[str]] = defaultdict(set)
    for population_id, channel in vectors:
        population_channels[population_id].add(channel)

    source_populations: dict[str, list[str]] = defaultdict(list)
    source_packets: dict[str, dict[str, Any]] = {}
    for population_id in population_channels:
        item = populations[population_id]
        source = item["packet"].get("source")
        if not isinstance(source, Mapping) or not source:
            raise ContractError(f"population {population_id} requires source metadata")
        key = make_source_key(source)
        source_populations[key].append(population_id)
        source_packets.setdefault(key, dict(source))

    sources: list[dict[str, Any]] = []
    pooled: dict[str, list[np.ndarray]] = defaultdict(list)
    expected_channels: set[str] | None = None
    for source_key in sorted(source_populations):
        available = sorted(source_populations[source_key])
        contexts = [
            population_id
            for population_id in available
            if populations[population_id]["packet"].get("population", {}).get("kind")
            == "record_context"
        ]
        selected = contexts or available
        selected_channels = set.intersection(
            *(population_channels[population_id] for population_id in selected)
        )
        if not selected_channels:
            raise ContractError(
                f"source {source_key} has no vector channel shared by its populations"
            )
        if expected_channels is None:
            expected_channels = selected_channels
        elif selected_channels != expected_channels:
            raise ContractError("every source representation must expose the same vector channels")

        for channel in sorted(selected_channels):
            centroid = np.mean(
                [vectors[(population_id, channel)] for population_id in selected],
                axis=0,
            )
            pooled[channel].append(_normalized(centroid, path=f"{source_key} {channel} centroid"))
        sources.append(
            {
                "source_key": source_key,
                "source": source_packets[source_key],
                "population_ids": selected,
                "representation": "record_context" if contexts else "population_centroid",
            }
        )

    if not sources:
        raise ContractError("source neighborhoods require at least one represented source")
    return sources, {
        channel: np.asarray(channel_vectors, dtype=np.float32)
        for channel, channel_vectors in pooled.items()
    }


def _mutual_edges(
    similarities: np.ndarray,
    source_keys: list[str],
    *,
    top_k: int,
    threshold: float,
    local_baselines: np.ndarray,
    minimum_local_lift: float,
) -> list[dict[str, Any]]:
    count = similarities.shape[0]
    neighbor_count = min(top_k, count - 1)
    rankings: list[list[int]] = []
    rank_maps: list[dict[int, int]] = []
    for left_index in range(count):
        ranked = sorted(
            (right_index for right_index in range(count) if right_index != left_index),
            key=lambda right_index: (
                -float(similarities[left_index, right_index]),
                source_keys[right_index],
            ),
        )[:neighbor_count]
        rankings.append(ranked)
        rank_maps.append({right_index: rank + 1 for rank, right_index in enumerate(ranked)})

    edges: list[dict[str, Any]] = []
    for left_index, neighbors in enumerate(rankings):
        for right_index in neighbors:
            if right_index <= left_index or left_index not in rank_maps[right_index]:
                continue
            similarity = float(similarities[left_index, right_index])
            left_lift = similarity - float(local_baselines[left_index])
            right_lift = similarity - float(local_baselines[right_index])
            if similarity < threshold or min(left_lift, right_lift) < minimum_local_lift:
                continue
            edges.append(
                {
                    "left_source_key": source_keys[left_index],
                    "right_source_key": source_keys[right_index],
                    "similarity": round(similarity, 8),
                    "left_rank": rank_maps[left_index][right_index],
                    "right_rank": rank_maps[right_index][left_index],
                    "left_local_lift": round(left_lift, 8),
                    "right_local_lift": round(right_lift, 8),
                }
            )
    return sorted(
        edges,
        key=lambda edge: (
            -edge["similarity"],
            edge["left_source_key"],
            edge["right_source_key"],
        ),
    )


def _components(source_keys: list[str], edges: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    positions = {source_key: index for index, source_key in enumerate(source_keys)}
    parents = list(range(len(source_keys)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for edge in edges:
        left = find(positions[str(edge["left_source_key"])])
        right = find(positions[str(edge["right_source_key"])])
        if left != right:
            parents[right] = left

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(source_keys)):
        grouped[find(index)].append(index)
    return sorted(
        (sorted(indices, key=lambda index: source_keys[index]) for indices in grouped.values()),
        key=lambda indices: (-len(indices), [source_keys[index] for index in indices]),
    )


def propose_source_neighborhoods(
    corpus: Mapping[str, Any],
    vector_rows: Iterable[Mapping[str, Any]],
    *,
    tight_top_k: int = 4,
    bridge_top_k: int = 6,
    tight_quantile: float = 0.97,
    bridge_quantile: float = 0.90,
    minimum_tight_similarity: float = 0.80,
    minimum_bridge_similarity: float = 0.70,
    minimum_tight_local_lift: float = 0.05,
    minimum_bridge_local_lift: float = 0.03,
    channel_weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Propose tight source neighborhoods and non-merging semantic bridges.

    The quantile gates adapt to each installation's similarity distribution;
    absolute similarity and local-lift floors prevent the algorithm from being
    forced to connect an entirely incoherent corpus merely because some pair is
    relatively least bad.  Reciprocal nearest-neighbor gating suppresses hubs.
    """

    for name, value in (("tight_top_k", tight_top_k), ("bridge_top_k", bridge_top_k)):
        if value < 1:
            raise ValueError(f"{name} must be positive")
    if bridge_top_k < tight_top_k:
        raise ValueError("bridge_top_k must be at least tight_top_k")
    for name, value in (
        ("tight_quantile", tight_quantile),
        ("bridge_quantile", bridge_quantile),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between zero and one")
    if bridge_quantile > tight_quantile:
        raise ValueError("bridge_quantile cannot exceed tight_quantile")
    for name, value in (
        ("minimum_tight_similarity", minimum_tight_similarity),
        ("minimum_bridge_similarity", minimum_bridge_similarity),
    ):
        if not -1.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between -1 and 1")
    if minimum_bridge_similarity > minimum_tight_similarity:
        raise ValueError("minimum_bridge_similarity cannot exceed the tight floor")
    if minimum_tight_local_lift < 0 or minimum_bridge_local_lift < 0:
        raise ValueError("minimum local lifts must be non-negative")

    sources, matrices = _source_representations(corpus, vector_rows)
    weights = resolve_channel_weights(sorted(matrices), channel_weights)
    if len(sources) == 1:
        source_key = str(sources[0]["source_key"])
        neighborhood_id = "neighborhood:" + stable_hash([source_key])[:24]
        proposal = {
            "schema_version": SOURCE_NEIGHBORHOOD_SCHEMA_VERSION,
            "corpus_id": corpus["corpus_id"],
            "inference_contract": {
                "asserts_business_domain": False,
                "uses_split_groups": False,
                "allows_singletons": True,
                "broad_links_merge_neighborhoods": False,
            },
            "algorithm": {
                "name": "adaptive_mutual_source_affinity_v1",
                "channels": weights,
                "tight_top_k": tight_top_k,
                "bridge_top_k": bridge_top_k,
                "tight_quantile": tight_quantile,
                "bridge_quantile": bridge_quantile,
                "minimum_tight_similarity": minimum_tight_similarity,
                "minimum_bridge_similarity": minimum_bridge_similarity,
                "minimum_tight_local_lift": minimum_tight_local_lift,
                "minimum_bridge_local_lift": minimum_bridge_local_lift,
                "observed_tight_threshold": None,
                "observed_bridge_threshold": None,
            },
            "sources": sources,
            "nearest_neighbors": [],
            "tight_affinities": [],
            "neighborhoods": [{
                "neighborhood_id": neighborhood_id,
                "member_source_keys": [source_key],
                "size": 1,
                "disposition": "unassigned_singleton",
                "tight_edge_count": 0,
                "tight_edge_density": None,
                "mean_internal_similarity": None,
                "minimum_internal_similarity": None,
            }],
            "bridges": [],
            "summary": {
                "sources": 1,
                "candidate_neighborhoods": 0,
                "singleton_sources": 1,
                "tight_affinities": 0,
                "bridges": 0,
            },
        }
        validate_source_neighborhoods(corpus, proposal)
        return proposal
    similarities = np.zeros((len(sources), len(sources)), dtype=np.float32)
    for channel, matrix in matrices.items():
        similarities += weights[channel] * (matrix @ matrix.T)
    similarities = np.clip(similarities, -1.0, 1.0)
    np.fill_diagonal(similarities, np.nan)
    pair_values = similarities[np.triu_indices(len(sources), k=1)]
    if not np.all(np.isfinite(pair_values)):
        raise ContractError("source similarity matrix contains non-finite values")

    tight_threshold = max(
        minimum_tight_similarity,
        float(np.quantile(pair_values, tight_quantile)),
    )
    bridge_threshold = max(
        minimum_bridge_similarity,
        float(np.quantile(pair_values, bridge_quantile)),
    )
    local_baselines = np.nanmedian(similarities, axis=1)
    source_keys = [str(source["source_key"]) for source in sources]
    tight_edges = _mutual_edges(
        similarities,
        source_keys,
        top_k=tight_top_k,
        threshold=tight_threshold,
        local_baselines=local_baselines,
        minimum_local_lift=minimum_tight_local_lift,
    )
    broad_edges = _mutual_edges(
        similarities,
        source_keys,
        top_k=bridge_top_k,
        threshold=bridge_threshold,
        local_baselines=local_baselines,
        minimum_local_lift=minimum_bridge_local_lift,
    )

    components = _components(source_keys, tight_edges)
    neighborhood_by_source: dict[str, str] = {}
    neighborhoods: list[dict[str, Any]] = []
    tight_edge_pairs = {
        frozenset((str(edge["left_source_key"]), str(edge["right_source_key"])))
        for edge in tight_edges
    }
    for member_indices in components:
        member_keys = [source_keys[index] for index in member_indices]
        neighborhood_id = "neighborhood:" + stable_hash(member_keys)[:24]
        for source_key in member_keys:
            neighborhood_by_source[source_key] = neighborhood_id
        possible_edges = len(member_keys) * (len(member_keys) - 1) // 2
        observed_edges = sum(
            frozenset((left, right)) in tight_edge_pairs
            for offset, left in enumerate(member_keys)
            for right in member_keys[offset + 1 :]
        )
        internal_values = [
            float(similarities[left, right])
            for offset, left in enumerate(member_indices)
            for right in member_indices[offset + 1 :]
        ]
        neighborhoods.append(
            {
                "neighborhood_id": neighborhood_id,
                "member_source_keys": member_keys,
                "size": len(member_keys),
                "disposition": (
                    "candidate_neighborhood" if len(member_keys) > 1 else "unassigned_singleton"
                ),
                "tight_edge_count": observed_edges,
                "tight_edge_density": (
                    round(observed_edges / possible_edges, 8) if possible_edges else None
                ),
                "mean_internal_similarity": (
                    round(float(np.mean(internal_values)), 8) if internal_values else None
                ),
                "minimum_internal_similarity": (
                    round(min(internal_values), 8) if internal_values else None
                ),
            }
        )

    bridge_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in broad_edges:
        left_neighborhood = neighborhood_by_source[str(edge["left_source_key"])]
        right_neighborhood = neighborhood_by_source[str(edge["right_source_key"])]
        if left_neighborhood == right_neighborhood:
            continue
        pair = (
            (left_neighborhood, right_neighborhood)
            if left_neighborhood < right_neighborhood
            else (right_neighborhood, left_neighborhood)
        )
        current = bridge_pairs.get(pair)
        if current is None or edge["similarity"] > current["similarity"]:
            bridge_pairs[pair] = {
                "left_neighborhood_id": pair[0],
                "right_neighborhood_id": pair[1],
                **edge,
            }

    nearest_neighbors = []
    for left_index, source_key in enumerate(source_keys):
        right_index = min(
            (index for index in range(len(source_keys)) if index != left_index),
            key=lambda index: (-float(similarities[left_index, index]), source_keys[index]),
        )
        nearest_neighbors.append(
            {
                "source_key": source_key,
                "neighbor_source_key": source_keys[right_index],
                "similarity": round(float(similarities[left_index, right_index]), 8),
            }
        )

    proposal = {
        "schema_version": SOURCE_NEIGHBORHOOD_SCHEMA_VERSION,
        "corpus_id": corpus["corpus_id"],
        "inference_contract": {
            "asserts_business_domain": False,
            "uses_split_groups": False,
            "allows_singletons": True,
            "broad_links_merge_neighborhoods": False,
        },
        "algorithm": {
            "name": "adaptive_mutual_source_affinity_v1",
            "channels": weights,
            "tight_top_k": tight_top_k,
            "bridge_top_k": bridge_top_k,
            "tight_quantile": tight_quantile,
            "bridge_quantile": bridge_quantile,
            "minimum_tight_similarity": minimum_tight_similarity,
            "minimum_bridge_similarity": minimum_bridge_similarity,
            "minimum_tight_local_lift": minimum_tight_local_lift,
            "minimum_bridge_local_lift": minimum_bridge_local_lift,
            "observed_tight_threshold": round(tight_threshold, 8),
            "observed_bridge_threshold": round(bridge_threshold, 8),
        },
        "sources": sources,
        "nearest_neighbors": nearest_neighbors,
        "tight_affinities": tight_edges,
        "neighborhoods": neighborhoods,
        "bridges": sorted(
            bridge_pairs.values(),
            key=lambda bridge: (
                -bridge["similarity"],
                bridge["left_neighborhood_id"],
                bridge["right_neighborhood_id"],
            ),
        ),
        "summary": {
            "sources": len(sources),
            "candidate_neighborhoods": sum(
                neighborhood["size"] > 1 for neighborhood in neighborhoods
            ),
            "singleton_sources": sum(neighborhood["size"] == 1 for neighborhood in neighborhoods),
            "tight_affinities": len(tight_edges),
            "bridges": len(bridge_pairs),
        },
    }
    validate_source_neighborhoods(corpus, proposal)
    return proposal


def validate_source_neighborhoods(
    corpus: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> None:
    """Validate that a proposal is a complete, source-safe partition."""

    validate_corpus(corpus)
    if proposal.get("schema_version") != SOURCE_NEIGHBORHOOD_SCHEMA_VERSION:
        raise ContractError("unsupported source-neighborhood proposal schema")
    if proposal.get("corpus_id") != corpus.get("corpus_id"):
        raise ContractError("source-neighborhood proposal corpus_id does not match the corpus")

    available_sources = {
        make_source_key(source)
        for item in corpus.get("populations", [])
        if isinstance((source := item["packet"].get("source")), Mapping) and source
    }
    sources = proposal.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ContractError("source-neighborhood proposal requires at least one source")
    proposed_source_keys: set[str] = set()
    for index, item in enumerate(sources):
        if not isinstance(item, Mapping):
            raise ContractError(f"source-neighborhood source {index} must be an object")
        key = item.get("source_key")
        source = item.get("source")
        if not isinstance(key, str) or not key:
            raise ContractError(f"source-neighborhood source {index} requires source_key")
        if key in proposed_source_keys:
            raise ContractError(f"duplicate source-neighborhood source_key: {key}")
        if not isinstance(source, Mapping) or make_source_key(source) != key:
            raise ContractError(f"source-neighborhood source {key} has inconsistent metadata")
        if key not in available_sources:
            raise ContractError(f"source-neighborhood source is outside the corpus: {key}")
        proposed_source_keys.add(key)

    neighborhoods = proposal.get("neighborhoods")
    if not isinstance(neighborhoods, list) or not neighborhoods:
        raise ContractError("source-neighborhood proposal requires neighborhoods")
    neighborhood_ids: set[str] = set()
    partition_members: list[str] = []
    neighborhood_by_source: dict[str, str] = {}
    for index, neighborhood in enumerate(neighborhoods):
        if not isinstance(neighborhood, Mapping):
            raise ContractError(f"source neighborhood {index} must be an object")
        neighborhood_id = neighborhood.get("neighborhood_id")
        members = neighborhood.get("member_source_keys")
        if not isinstance(neighborhood_id, str) or not neighborhood_id:
            raise ContractError(f"source neighborhood {index} requires neighborhood_id")
        if neighborhood_id in neighborhood_ids:
            raise ContractError(f"duplicate source neighborhood id: {neighborhood_id}")
        if not isinstance(members, list) or not members:
            raise ContractError(f"source neighborhood {neighborhood_id} requires members")
        if not all(
            isinstance(member, str) and member in proposed_source_keys for member in members
        ):
            raise ContractError(f"source neighborhood {neighborhood_id} has an unknown member")
        neighborhood_ids.add(neighborhood_id)
        partition_members.extend(members)
        for member in members:
            neighborhood_by_source[member] = neighborhood_id
    if len(partition_members) != len(set(partition_members)):
        raise ContractError("source neighborhoods must not overlap")
    if set(partition_members) != proposed_source_keys:
        raise ContractError("source neighborhoods must partition every proposed source")

    for family_name in ("tight_affinities", "nearest_neighbors"):
        items = proposal.get(family_name)
        if not isinstance(items, list):
            raise ContractError(f"source-neighborhood {family_name} must be an array")
        for item in items:
            if not isinstance(item, Mapping):
                raise ContractError(f"source-neighborhood {family_name} entries must be objects")
            endpoints = (
                (item.get("left_source_key"), item.get("right_source_key"))
                if family_name == "tight_affinities"
                else (item.get("source_key"), item.get("neighbor_source_key"))
            )
            if any(endpoint not in proposed_source_keys for endpoint in endpoints):
                raise ContractError(f"source-neighborhood {family_name} has an unknown endpoint")

    bridges = proposal.get("bridges")
    if not isinstance(bridges, list):
        raise ContractError("source-neighborhood bridges must be an array")
    for bridge in bridges:
        if not isinstance(bridge, Mapping):
            raise ContractError("source-neighborhood bridge entries must be objects")
        left_source = bridge.get("left_source_key")
        right_source = bridge.get("right_source_key")
        left_neighborhood = bridge.get("left_neighborhood_id")
        right_neighborhood = bridge.get("right_neighborhood_id")
        if left_source not in proposed_source_keys or right_source not in proposed_source_keys:
            raise ContractError("source-neighborhood bridge has an unknown source endpoint")
        if (
            left_neighborhood not in neighborhood_ids
            or right_neighborhood not in neighborhood_ids
            or left_neighborhood == right_neighborhood
        ):
            raise ContractError("source-neighborhood bridge has invalid neighborhood endpoints")
        source_neighborhoods = {
            neighborhood_by_source[str(left_source)],
            neighborhood_by_source[str(right_source)],
        }
        if source_neighborhoods != {left_neighborhood, right_neighborhood}:
            raise ContractError("source-neighborhood bridge source and neighborhood disagree")


def evaluate_source_neighborhood_controls(
    corpus: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Audit proposals against private opaque source-family controls.

    ``split_group`` is never an inference input.  This diagnostic deliberately
    treats it as a coarse control rather than truth: a cross-control affinity
    may expose a replica, an adjacent business domain, or a bad private label.
    """

    validate_source_neighborhoods(corpus, proposal)
    groups_by_source: dict[str, set[str]] = defaultdict(set)
    for item in corpus.get("populations", []):
        source = item["packet"].get("source")
        if isinstance(source, Mapping) and source:
            groups_by_source[make_source_key(source)].add(str(item["split_group"]))

    proposal_sources = [str(item["source_key"]) for item in proposal.get("sources", [])]
    ambiguous = sorted(
        source_key
        for source_key in proposal_sources
        if len(groups_by_source.get(source_key, set())) != 1
    )
    if ambiguous:
        raise ContractError(
            "control evaluation requires exactly one split_group per represented source"
        )
    controls = {
        source_key: next(iter(groups_by_source[source_key])) for source_key in proposal_sources
    }
    neighborhood_for_source = {
        str(source_key): str(neighborhood["neighborhood_id"])
        for neighborhood in proposal.get("neighborhoods", [])
        for source_key in neighborhood.get("member_source_keys", [])
    }

    actual_pairs: set[frozenset[str]] = set()
    predicted_pairs: set[frozenset[str]] = set()
    for offset, left in enumerate(proposal_sources):
        for right in proposal_sources[offset + 1 :]:
            pair = frozenset((left, right))
            if controls[left] == controls[right]:
                actual_pairs.add(pair)
            if neighborhood_for_source[left] == neighborhood_for_source[right]:
                predicted_pairs.add(pair)
    true_pairs = predicted_pairs & actual_pairs
    precision = len(true_pairs) / len(predicted_pairs) if predicted_pairs else 1.0
    recall = len(true_pairs) / len(actual_pairs) if actual_pairs else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    tight_edges = list(proposal.get("tight_affinities", []))
    cross_control_edges = [
        {
            "left_source_key": edge["left_source_key"],
            "right_source_key": edge["right_source_key"],
            "similarity": edge["similarity"],
            "left_control": controls[str(edge["left_source_key"])],
            "right_control": controls[str(edge["right_source_key"])],
        }
        for edge in tight_edges
        if controls[str(edge["left_source_key"])] != controls[str(edge["right_source_key"])]
    ]
    nearest = list(proposal.get("nearest_neighbors", []))
    same_nearest = sum(
        controls[str(item["source_key"])] == controls[str(item["neighbor_source_key"])]
        for item in nearest
    )
    control_components: dict[str, set[str]] = defaultdict(set)
    for source_key, control in controls.items():
        control_components[control].add(neighborhood_for_source[source_key])

    return {
        "schema_version": "rvbbit.business-topology.source-neighborhood-controls.v1",
        "corpus_id": corpus["corpus_id"],
        "warning": (
            "split_group is a coarse private control, not an inference input or asserted truth"
        ),
        "sources_with_controls": len(controls),
        "control_groups": len(set(controls.values())),
        "nearest_neighbor_same_control_rate": (
            round(same_nearest / len(nearest), 8) if nearest else None
        ),
        "tight_edge_same_control_rate": (
            round((len(tight_edges) - len(cross_control_edges)) / len(tight_edges), 8)
            if tight_edges
            else None
        ),
        "pairwise_neighborhood": {
            "true_positive_pairs": len(true_pairs),
            "predicted_pairs": len(predicted_pairs),
            "control_pairs": len(actual_pairs),
            "precision": round(precision, 8),
            "recall": round(recall, 8),
            "f1": round(f1, 8),
        },
        "control_fragmentation": {
            "mean_neighborhoods_per_control": round(
                sum(map(len, control_components.values())) / len(control_components), 8
            ),
            "maximum_neighborhoods_per_control": max(map(len, control_components.values())),
        },
        "cross_control_tight_edges": cross_control_edges,
    }
