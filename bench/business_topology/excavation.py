"""Neighborhood-scoped work planning for Business Topology excavation.

Source neighborhoods bound the search space; they do not themselves assert a
business domain or object.  This module turns those neighborhoods into a stable
model-work DAG:

1. inspect every source for multiple motifs;
2. score bounded cross-source population correspondences inside each unit;
3. synthesize an internal object skeleton from those receipts; and
4. inspect only nominated unit boundaries for cross-neighborhood relationships.

The planner never materializes nodes, bindings, edges, or executable SQL.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .candidates import build_candidate_queue
from .contracts import ContractError, validate_corpus, validate_outbound_packet
from .domains import make_source_key, validate_source_neighborhoods
from .packets import stable_hash


EXCAVATION_PLAN_SCHEMA_VERSION = "rvbbit.business-topology.excavation-plan.v1"

_WORK_STAGE = {
    "source_motifs": 1,
    "correspondence": 2,
    "neighborhood_synthesis": 3,
    "bridge_synthesis": 4,
}


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}:{stable_hash(value)[:24]}"


def _packet_source_key(packet: Mapping[str, Any], *, path: str) -> str:
    source = packet.get("source")
    if not isinstance(source, Mapping) or not source:
        raise ContractError(f"{path} requires source metadata")
    return make_source_key(source)


def _partition_members(
    members: Sequence[str],
    affinities: Sequence[Mapping[str, Any]],
    *,
    maximum_sources: int,
    population_weights: Mapping[str, int] | None = None,
    maximum_populations: int | None = None,
) -> list[list[str]]:
    """Split a component along weak boundaries and bounded model workload."""

    ordered_members = sorted(set(members))
    weights = {
        member: max(int((population_weights or {}).get(member, 0)), 0) for member in ordered_members
    }
    population_count = sum(weights.values())
    if len(ordered_members) <= maximum_sources and (
        maximum_populations is None or population_count <= maximum_populations
    ):
        return [ordered_members]
    member_set = set(ordered_members)
    adjacency: dict[str, dict[str, float]] = defaultdict(dict)
    for edge in affinities:
        left = str(edge["left_source_key"])
        right = str(edge["right_source_key"])
        if left in member_set and right in member_set:
            score = float(edge["similarity"])
            adjacency[left][right] = score
            adjacency[right][left] = score

    remaining = set(ordered_members)
    partitions: list[list[str]] = []
    while remaining:
        seed = min(
            remaining,
            key=lambda candidate: (
                -sum(adjacency[candidate].get(peer, 0.0) for peer in remaining),
                -sum(peer in remaining for peer in adjacency[candidate]),
                candidate,
            ),
        )
        selected = [seed]
        selected_population_count = weights[seed]
        remaining.remove(seed)
        while remaining and len(selected) < maximum_sources:
            connected = [
                candidate
                for candidate in remaining
                if any(peer in adjacency[candidate] for peer in selected)
                and (
                    maximum_populations is None
                    or selected_population_count + weights[candidate] <= maximum_populations
                )
            ]
            if not connected:
                break
            candidate = min(
                connected,
                key=lambda option: (
                    -sum(adjacency[option].get(peer, 0.0) for peer in selected),
                    -max(adjacency[option].get(peer, 0.0) for peer in selected),
                    option,
                ),
            )
            selected.append(candidate)
            selected_population_count += weights[candidate]
            remaining.remove(candidate)
        partitions.append(sorted(selected))
    return partitions


def _scoped_candidates(
    corpus: Mapping[str, Any],
    allowed_sources: set[str],
    *,
    evidence_by_pair: Mapping[str, Mapping[str, Any]],
    max_pairs: int,
    max_fanout: int,
    minimum_priority: float,
) -> list[dict[str, Any]]:
    if len(allowed_sources) < 2 or max_pairs == 0:
        return []
    populations = [
        deepcopy(item)
        for item in corpus.get("populations", [])
        if _packet_source_key(item["packet"], path=str(item["population_id"])) in allowed_sources
    ]
    population_ids = {str(item["population_id"]) for item in populations}
    if len(population_ids) < 2:
        return []
    scoped_evidence = {
        key: dict(values)
        for key, values in evidence_by_pair.items()
        if set(key.split("\x1f")) <= population_ids and len(key.split("\x1f")) == 2
    }
    scoped_corpus = {
        "schema_version": corpus["schema_version"],
        "corpus_id": corpus["corpus_id"],
        "populations": populations,
        "motifs": [],
        "correspondences": [],
    }
    queued = build_candidate_queue(
        scoped_corpus,
        max_pairs=max_pairs,
        max_fanout=max_fanout,
        minimum_priority=minimum_priority,
        evidence_by_pair=scoped_evidence,
        allowed_strata=("local_overlap", "semantic_neighbor", "usage_evidence"),
    )
    return list(queued["correspondences"])


def _correspondence_work(
    candidate: Mapping[str, Any],
    population_sources: Mapping[str, str],
    *,
    scope_kind: str,
    scope_id: str,
) -> dict[str, Any]:
    left = str(candidate["left_population_id"])
    right = str(candidate["right_population_id"])
    packet = deepcopy(candidate["packet"])
    validate_outbound_packet(packet)
    return {
        "work_id": _stable_id("work", ["correspondence", scope_id, left, right]),
        "work_kind": "correspondence",
        "stage": _WORK_STAGE["correspondence"],
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "source_keys": sorted({population_sources[left], population_sources[right]}),
        "population_ids": sorted((left, right)),
        "depends_on": [],
        "priority": candidate.get("review", {}).get("priority"),
        "evidence_stratum": candidate.get("review", {}).get("stratum"),
        "input_packet": packet,
    }


def _neighborhood_synthesis_packet(
    unit: Mapping[str, Any],
    source_inputs: Sequence[Mapping[str, Any]],
    evidence_work_ids: Sequence[str],
) -> dict[str, Any]:
    packet = {
        "schema_version": "rvbbit.business-topology.neighborhood-synthesis.v1",
        "privacy": {
            "raw_values": False,
            "value_hashes": False,
            "bounded_inputs": True,
        },
        "scope": {
            "excavation_unit_id": unit["excavation_unit_id"],
            "parent_neighborhood_id": unit["parent_neighborhood_id"],
            "source_keys": list(unit["source_keys"]),
        },
        "source_inputs": list(source_inputs),
        "evidence_work_ids": sorted(evidence_work_ids),
        "output_contract": {
            "schema_version": "rvbbit.business-topology.neighborhood-skeleton-result.v1",
            "requires_canonical_name": True,
            "allow_multiple_objects_per_source": True,
            "allow_cross_source_objects": True,
            "allow_population_reuse": True,
            "allow_unbound_populations": True,
            "allow_abstain": True,
            "allow_executable_sql": False,
            "max_nodes": 16,
            "max_bindings": 48,
            "max_edges": 24,
            "node_kinds": [
                "object",
                "facet",
                "lifecycle",
                "event",
                "measure",
                "category",
            ],
            "binding_roles": [
                "identity",
                "attribute",
                "event",
                "measure",
                "category",
                "status",
                "time",
                "geography",
                "evidence",
                "context",
            ],
            "result_is_proposal_only": True,
        },
    }
    validate_outbound_packet(packet)
    return packet


def _bridge_synthesis_packet(
    link: Mapping[str, Any],
    synthesis_work_ids: Sequence[str],
    evidence_work_ids: Sequence[str],
) -> dict[str, Any]:
    packet = {
        "schema_version": "rvbbit.business-topology.bridge-synthesis.v1",
        "privacy": {
            "raw_values": False,
            "value_hashes": False,
            "bounded_inputs": True,
        },
        "scope": {
            "link_id": link["link_id"],
            "link_kind": link["link_kind"],
            "left_excavation_unit_id": link["left_excavation_unit_id"],
            "right_excavation_unit_id": link["right_excavation_unit_id"],
            "anchor_source_keys": [
                link["left_source_key"],
                link["right_source_key"],
            ],
            "source_affinity": link["similarity"],
        },
        "neighborhood_synthesis_work_ids": sorted(synthesis_work_ids),
        "evidence_work_ids": sorted(evidence_work_ids),
        "output_contract": {
            "schema_version": "rvbbit.business-topology.bridge-result.v1",
            "allowed_outcomes": [
                "shared_object",
                "related_objects",
                "joinable_populations",
                "correlated",
                "unrelated",
                "abstain",
            ],
            "merge_excavation_units": False,
            "allow_executable_sql": False,
            "result_is_proposal_only": True,
        },
    }
    validate_outbound_packet(packet)
    return packet


def build_excavation_plan(
    corpus: Mapping[str, Any],
    neighborhoods: Mapping[str, Any],
    *,
    evidence_by_pair: Mapping[str, Mapping[str, Any]] | None = None,
    maximum_sources_per_unit: int = 24,
    maximum_populations_per_unit: int = 48,
    max_pairs_per_unit: int = 64,
    max_pairs_per_link: int = 8,
    max_population_fanout: int = 8,
    minimum_pair_priority: float = 0.32,
    max_cross_neighborhood_links: int = 100,
) -> dict[str, Any]:
    """Build a bounded, non-persisting work DAG from source neighborhoods."""

    validate_corpus(corpus)
    validate_source_neighborhoods(corpus, neighborhoods)
    for name, value in (
        ("maximum_sources_per_unit", maximum_sources_per_unit),
        ("maximum_populations_per_unit", maximum_populations_per_unit),
        ("max_pairs_per_unit", max_pairs_per_unit),
        ("max_pairs_per_link", max_pairs_per_link),
        ("max_population_fanout", max_population_fanout),
        ("max_cross_neighborhood_links", max_cross_neighborhood_links),
    ):
        if value < 1:
            raise ValueError(f"{name} must be positive")
    if not 0.0 <= minimum_pair_priority <= 1.0:
        raise ValueError("minimum_pair_priority must be between zero and one")
    evidence_by_pair = evidence_by_pair or {}

    population_sources: dict[str, str] = {}
    populations_by_source: dict[str, list[str]] = defaultdict(list)
    contexts_by_source: dict[str, list[str]] = defaultdict(list)
    for item in corpus.get("populations", []):
        population_id = str(item["population_id"])
        key = _packet_source_key(item["packet"], path=population_id)
        population_sources[population_id] = key
        kind = item["packet"].get("population", {}).get("kind")
        target = contexts_by_source if kind == "record_context" else populations_by_source
        target[key].append(population_id)

    tight_affinities = list(neighborhoods.get("tight_affinities", []))
    excavation_units: list[dict[str, Any]] = []
    unit_by_source: dict[str, str] = {}
    for neighborhood in sorted(
        neighborhoods["neighborhoods"],
        key=lambda item: str(item["neighborhood_id"]),
    ):
        partitions = _partition_members(
            neighborhood["member_source_keys"],
            tight_affinities,
            maximum_sources=maximum_sources_per_unit,
            population_weights={
                key: len(populations_by_source.get(key, []))
                for key in neighborhood["member_source_keys"]
            },
            maximum_populations=maximum_populations_per_unit,
        )
        for shard_index, members in enumerate(partitions, 1):
            population_count = sum(len(populations_by_source.get(key, [])) for key in members)
            unit_id = _stable_id(
                "excavation",
                [neighborhood["neighborhood_id"], members],
            )
            unit = {
                "excavation_unit_id": unit_id,
                "parent_neighborhood_id": neighborhood["neighborhood_id"],
                "source_keys": members,
                "size": len(members),
                "population_count": population_count,
                "oversized_source": (
                    len(members) == 1 and population_count > maximum_populations_per_unit
                ),
                "unit_kind": (
                    "singleton"
                    if len(members) == 1 and len(partitions) == 1
                    else "neighborhood_shard"
                    if len(partitions) > 1
                    else "neighborhood"
                ),
                "shard_index": shard_index,
                "shard_count": len(partitions),
            }
            excavation_units.append(unit)
            for key in members:
                unit_by_source[key] = unit_id

    links_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    def nominate_link(edge: Mapping[str, Any], *, link_kind: str) -> None:
        left_source = str(edge["left_source_key"])
        right_source = str(edge["right_source_key"])
        left_unit = unit_by_source[left_source]
        right_unit = unit_by_source[right_source]
        if left_unit == right_unit:
            return
        first_unit, second_unit = sorted((left_unit, right_unit))
        key = (link_kind, first_unit, second_unit)
        candidate = {
            "link_id": _stable_id("link", key),
            "link_kind": link_kind,
            "left_excavation_unit_id": first_unit,
            "right_excavation_unit_id": second_unit,
            "left_source_key": left_source,
            "right_source_key": right_source,
            "similarity": float(edge["similarity"]),
            "source_affinity": {
                name: edge[name]
                for name in (
                    "left_rank",
                    "right_rank",
                    "left_local_lift",
                    "right_local_lift",
                )
                if name in edge
            },
        }
        current = links_by_key.get(key)
        if current is None or (
            candidate["similarity"],
            tuple(sorted((candidate["left_source_key"], candidate["right_source_key"]))),
        ) > (
            current["similarity"],
            tuple(sorted((current["left_source_key"], current["right_source_key"]))),
        ):
            links_by_key[key] = candidate

    # When a large connected neighborhood is sharded for bounded execution,
    # preserve its strongest cut affinities as explicit continuation work.
    for affinity in tight_affinities:
        nominate_link(affinity, link_kind="neighborhood_boundary")
    strongest_bridges = sorted(
        neighborhoods.get("bridges", []),
        key=lambda bridge: (
            -float(bridge["similarity"]),
            str(bridge["left_neighborhood_id"]),
            str(bridge["right_neighborhood_id"]),
        ),
    )[:max_cross_neighborhood_links]
    for bridge in strongest_bridges:
        nominate_link(bridge, link_kind="cross_neighborhood")
    links = sorted(
        links_by_key.values(),
        key=lambda link: (
            link["link_kind"],
            -link["similarity"],
            link["link_id"],
        ),
    )

    motif_work_by_source: dict[str, list[str]] = defaultdict(list)
    work_items: list[dict[str, Any]] = []
    for motif in corpus.get("motifs", []):
        packet = deepcopy(motif["packet"])
        key = _packet_source_key(packet, path=str(motif["motif_id"]))
        if key not in unit_by_source:
            continue
        validate_outbound_packet(packet)
        work_id = _stable_id("work", ["source_motifs", motif["motif_id"]])
        population_id = motif.get("source_population_id")
        work_items.append(
            {
                "work_id": work_id,
                "work_kind": "source_motifs",
                "stage": _WORK_STAGE["source_motifs"],
                "scope_kind": "source",
                "scope_id": key,
                "source_keys": [key],
                "population_ids": [population_id] if isinstance(population_id, str) else [],
                "depends_on": [],
                "input_packet": packet,
            }
        )
        motif_work_by_source[key].append(work_id)

    synthesis_work_by_unit: dict[str, str] = {}
    for unit in excavation_units:
        unit_id = str(unit["excavation_unit_id"])
        member_sources = set(unit["source_keys"])
        candidates = _scoped_candidates(
            corpus,
            member_sources,
            evidence_by_pair=evidence_by_pair,
            max_pairs=max_pairs_per_unit,
            max_fanout=max_population_fanout,
            minimum_priority=minimum_pair_priority,
        )
        correspondence_work = [
            _correspondence_work(
                candidate,
                population_sources,
                scope_kind="within_excavation_unit",
                scope_id=unit_id,
            )
            for candidate in candidates
        ]
        work_items.extend(correspondence_work)
        evidence_work_ids = [item["work_id"] for item in correspondence_work]
        source_inputs = [
            {
                "source_key": key,
                "population_ids": sorted(populations_by_source.get(key, [])),
                "context_population_ids": sorted(contexts_by_source.get(key, [])),
                "source_motif_work_ids": sorted(motif_work_by_source.get(key, [])),
            }
            for key in sorted(member_sources)
        ]
        dependencies = sorted(
            evidence_work_ids
            + [work_id for key in member_sources for work_id in motif_work_by_source.get(key, [])]
        )
        packet = _neighborhood_synthesis_packet(unit, source_inputs, evidence_work_ids)
        synthesis_id = _stable_id("work", ["neighborhood_synthesis", unit_id])
        synthesis_work_by_unit[unit_id] = synthesis_id
        work_items.append(
            {
                "work_id": synthesis_id,
                "work_kind": "neighborhood_synthesis",
                "stage": _WORK_STAGE["neighborhood_synthesis"],
                "scope_kind": "excavation_unit",
                "scope_id": unit_id,
                "source_keys": sorted(member_sources),
                "population_ids": sorted(
                    population_id
                    for key in member_sources
                    for population_id in populations_by_source.get(key, [])
                ),
                "depends_on": dependencies,
                "input_packet": packet,
            }
        )

    for link in links:
        link_id = str(link["link_id"])
        anchor_sources = {str(link["left_source_key"]), str(link["right_source_key"])}
        candidates = _scoped_candidates(
            corpus,
            anchor_sources,
            evidence_by_pair=evidence_by_pair,
            max_pairs=max_pairs_per_link,
            max_fanout=max_population_fanout,
            minimum_priority=minimum_pair_priority,
        )
        correspondence_work = [
            _correspondence_work(
                candidate,
                population_sources,
                scope_kind="boundary_probe",
                scope_id=link_id,
            )
            for candidate in candidates
        ]
        work_items.extend(correspondence_work)
        synthesis_dependencies = [
            synthesis_work_by_unit[str(link["left_excavation_unit_id"])],
            synthesis_work_by_unit[str(link["right_excavation_unit_id"])],
        ]
        evidence_work_ids = [item["work_id"] for item in correspondence_work]
        packet = _bridge_synthesis_packet(
            link,
            synthesis_dependencies,
            evidence_work_ids,
        )
        work_items.append(
            {
                "work_id": _stable_id("work", ["bridge_synthesis", link_id]),
                "work_kind": "bridge_synthesis",
                "stage": _WORK_STAGE["bridge_synthesis"],
                "scope_kind": str(link["link_kind"]),
                "scope_id": link_id,
                "source_keys": sorted(anchor_sources),
                "population_ids": sorted(
                    {
                        population_id
                        for item in correspondence_work
                        for population_id in item["population_ids"]
                    }
                ),
                "depends_on": sorted(synthesis_dependencies + evidence_work_ids),
                "input_packet": packet,
            }
        )

    work_items.sort(key=lambda item: (item["stage"], item["work_id"]))
    plan = {
        "schema_version": EXCAVATION_PLAN_SCHEMA_VERSION,
        "corpus_id": corpus["corpus_id"],
        "source_neighborhood_schema_version": neighborhoods["schema_version"],
        "inference_contract": {
            "uses_split_groups": False,
            "relation_is_business_object": False,
            "allows_multiple_objects_per_source": True,
            "materializes_topology": False,
            "requires_proposal_review": True,
        },
        "policy": {
            "maximum_sources_per_unit": maximum_sources_per_unit,
            "maximum_populations_per_unit": maximum_populations_per_unit,
            "max_pairs_per_unit": max_pairs_per_unit,
            "max_pairs_per_link": max_pairs_per_link,
            "max_population_fanout": max_population_fanout,
            "minimum_pair_priority": minimum_pair_priority,
            "pair_strata": ["local_overlap", "semantic_neighbor", "usage_evidence"],
            "max_cross_neighborhood_links": max_cross_neighborhood_links,
        },
        "excavation_units": excavation_units,
        "links": links,
        "work_items": work_items,
        "summary": {
            "represented_sources": len(unit_by_source),
            "excavation_units": len(excavation_units),
            "sharded_neighborhoods": len(
                {
                    unit["parent_neighborhood_id"]
                    for unit in excavation_units
                    if unit["shard_count"] > 1
                }
            ),
            "boundary_links": len(links),
            "work_items": len(work_items),
            "work_by_kind": {
                kind: sum(item["work_kind"] == kind for item in work_items) for kind in _WORK_STAGE
            },
        },
    }
    validate_excavation_plan(corpus, neighborhoods, plan)
    return plan


def _contains_key(value: Any, forbidden: str) -> bool:
    if isinstance(value, Mapping):
        return forbidden in value or any(
            _contains_key(child, forbidden) for child in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_key(child, forbidden) for child in value)
    return False


def validate_excavation_plan(
    corpus: Mapping[str, Any],
    neighborhoods: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    """Validate privacy, partition, bounds, and DAG integrity."""

    validate_corpus(corpus)
    validate_source_neighborhoods(corpus, neighborhoods)
    if plan.get("schema_version") != EXCAVATION_PLAN_SCHEMA_VERSION:
        raise ContractError("unsupported excavation-plan schema")
    if plan.get("corpus_id") != corpus.get("corpus_id"):
        raise ContractError("excavation-plan corpus_id does not match the corpus")
    if _contains_key(plan, "split_group"):
        raise ContractError("excavation plans may not contain private split_group controls")

    policy = plan.get("policy")
    if not isinstance(policy, Mapping):
        raise ContractError("excavation plan requires a policy object")
    maximum_sources = policy.get("maximum_sources_per_unit")
    if not isinstance(maximum_sources, int) or maximum_sources < 1:
        raise ContractError("excavation policy maximum_sources_per_unit must be positive")
    maximum_populations = policy.get("maximum_populations_per_unit")
    if maximum_populations is not None and (
        not isinstance(maximum_populations, int) or maximum_populations < 1
    ):
        raise ContractError("excavation policy maximum_populations_per_unit must be positive")

    population_counts_by_source: dict[str, int] = defaultdict(int)
    for population in corpus.get("populations", []):
        packet = population.get("packet")
        if not isinstance(packet, Mapping):
            continue
        if packet.get("population", {}).get("kind") == "record_context":
            continue
        population_counts_by_source[
            _packet_source_key(packet, path=str(population.get("population_id")))
        ] += 1

    expected_sources = {str(source["source_key"]) for source in neighborhoods.get("sources", [])}
    units = plan.get("excavation_units")
    if not isinstance(units, list) or not units:
        raise ContractError("excavation plan requires excavation_units")
    unit_ids: set[str] = set()
    partition: list[str] = []
    sources_by_unit: dict[str, set[str]] = {}
    for unit in units:
        if not isinstance(unit, Mapping):
            raise ContractError("excavation unit entries must be objects")
        unit_id = unit.get("excavation_unit_id")
        sources = unit.get("source_keys")
        if not isinstance(unit_id, str) or not unit_id or unit_id in unit_ids:
            raise ContractError("excavation unit ids must be unique non-empty strings")
        if not isinstance(sources, list) or not sources or len(sources) > maximum_sources:
            raise ContractError("excavation unit source bounds are invalid")
        if not all(isinstance(source, str) and source in expected_sources for source in sources):
            raise ContractError(f"excavation unit {unit_id} has an unknown source")
        expected_population_count = sum(
            population_counts_by_source[str(source)] for source in sources
        )
        if unit.get("population_count") is not None and (
            not isinstance(unit.get("population_count"), int)
            or unit.get("population_count") != expected_population_count
        ):
            raise ContractError(f"excavation unit {unit_id} has an invalid population_count")
        if (
            isinstance(maximum_populations, int)
            and expected_population_count > maximum_populations
            and len(sources) > 1
        ):
            raise ContractError(f"excavation unit {unit_id} exceeds its population bound")
        expected_oversized = (
            isinstance(maximum_populations, int)
            and len(sources) == 1
            and expected_population_count > maximum_populations
        )
        if unit.get("oversized_source") is not None and (
            unit.get("oversized_source") is not expected_oversized
        ):
            raise ContractError(f"excavation unit {unit_id} has an invalid oversized_source flag")
        unit_ids.add(unit_id)
        partition.extend(sources)
        sources_by_unit[unit_id] = set(sources)
    if len(partition) != len(set(partition)) or set(partition) != expected_sources:
        raise ContractError("excavation units must partition represented sources exactly once")

    link_ids: set[str] = set()
    for link in plan.get("links", []):
        if not isinstance(link, Mapping):
            raise ContractError("excavation link entries must be objects")
        link_id = link.get("link_id")
        left_unit = link.get("left_excavation_unit_id")
        right_unit = link.get("right_excavation_unit_id")
        left_source = link.get("left_source_key")
        right_source = link.get("right_source_key")
        if not isinstance(link_id, str) or not link_id or link_id in link_ids:
            raise ContractError("excavation link ids must be unique non-empty strings")
        if left_unit not in unit_ids or right_unit not in unit_ids or left_unit == right_unit:
            raise ContractError(f"excavation link {link_id} has invalid unit endpoints")
        if not (
            (
                left_source in sources_by_unit[str(left_unit)]
                and right_source in sources_by_unit[str(right_unit)]
            )
            or (
                right_source in sources_by_unit[str(left_unit)]
                and left_source in sources_by_unit[str(right_unit)]
            )
        ):
            raise ContractError(f"excavation link {link_id} source endpoints disagree with units")
        link_ids.add(link_id)

    work_items = plan.get("work_items")
    if not isinstance(work_items, list) or not work_items:
        raise ContractError("excavation plan requires work_items")
    work_by_id: dict[str, Mapping[str, Any]] = {}
    for work in work_items:
        if not isinstance(work, Mapping):
            raise ContractError("excavation work entries must be objects")
        work_id = work.get("work_id")
        kind = work.get("work_kind")
        stage = work.get("stage")
        packet = work.get("input_packet")
        if not isinstance(work_id, str) or not work_id or work_id in work_by_id:
            raise ContractError("excavation work ids must be unique non-empty strings")
        if kind not in _WORK_STAGE or stage != _WORK_STAGE[str(kind)]:
            raise ContractError(f"excavation work {work_id} has an invalid kind or stage")
        if not isinstance(packet, Mapping):
            raise ContractError(f"excavation work {work_id} requires an input packet")
        validate_outbound_packet(packet)
        work_by_id[work_id] = work
    for work_id, work in work_by_id.items():
        dependencies = work.get("depends_on")
        if not isinstance(dependencies, list):
            raise ContractError(f"excavation work {work_id} dependencies must be an array")
        for dependency in dependencies:
            if dependency not in work_by_id:
                raise ContractError(f"excavation work {work_id} has an unknown dependency")
            if int(work_by_id[str(dependency)]["stage"]) >= int(work["stage"]):
                raise ContractError(f"excavation work {work_id} has a non-prior dependency")
