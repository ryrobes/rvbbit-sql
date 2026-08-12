"""Source-independent packet helpers and the first PostgreSQL adapter.

The PostgreSQL adapter intentionally reconstructs the exact outbound field and
source-motif packet shapes emitted by migration 0279.  Evaluation can therefore
run in shadow mode from ``business_topology_profile_packet`` without inserting
topology rows or changing the eventual model contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .contracts import CORRESPONDENCE_VERDICTS, ContractError, validate_outbound_packet


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def postgres_source_key(source: Mapping[str, Any]) -> str:
    identity = "\x1f".join(
        str(source.get(part) or "") for part in ("database", "schema", "relation")
    )
    return "postgres_relation:" + hashlib.md5(identity.encode()).hexdigest()  # noqa: S324


def packet_population_id(packet: Mapping[str, Any]) -> str:
    population = packet.get("population")
    if not isinstance(population, Mapping):
        raise ContractError("packet.population must be an object")
    population_key = population.get("population_key")
    if not isinstance(population_key, str) or not population_key:
        raise ContractError("packet.population.population_key must be a non-empty string")
    return population_key


def make_population_packet(
    *,
    population_key: str,
    population_kind: str,
    source: Mapping[str, Any],
    sample: Mapping[str, Any] | None = None,
    field: Mapping[str, Any] | None = None,
    neighbor_fields: list[Mapping[str, Any]] | None = None,
    field_pair_features: list[Mapping[str, Any]] | None = None,
    relation_context: Mapping[str, Any] | None = None,
    selector: Mapping[str, Any] | None = None,
    context_only: bool = False,
) -> dict[str, Any]:
    """Build one privacy-safe population packet for any source adapter."""

    population: dict[str, Any] = {
        "population_key": population_key,
        "kind": population_kind,
    }
    if selector is not None:
        population["selector"] = deepcopy(dict(selector))
    if context_only:
        population["context_only"] = True

    packet: dict[str, Any] = {
        "schema_version": "rvbbit.business-topology.population.v1",
        "privacy": {
            "raw_values": False,
            "value_hashes": False,
            "bounded_sample": True,
        },
        "population": population,
        "source": deepcopy(dict(source)),
        "sample": deepcopy(dict(sample or {})),
    }
    if field is not None:
        packet["field"] = deepcopy(dict(field))
    if neighbor_fields is not None:
        packet["neighbor_fields"] = deepcopy(neighbor_fields)
    if field_pair_features is not None:
        packet["field_pair_features"] = deepcopy(field_pair_features)
    if relation_context is not None:
        packet["relation_context"] = deepcopy(dict(relation_context))
    validate_outbound_packet(packet)
    return packet


def make_source_motif_packet(relation_profile: Mapping[str, Any]) -> dict[str, Any]:
    packet = {
        "schema_version": "rvbbit.business-topology.source-motifs.v1",
        "privacy": deepcopy(relation_profile["privacy"]),
        "source": deepcopy(relation_profile["source"]),
        "sample": deepcopy(relation_profile.get("sample", {})),
        "relation_context": deepcopy(relation_profile["relation_context"]),
        "output_contract": {
            "populations": "declarative field bundles or slices",
            "allow_sql": False,
            "allow_multiple_objects_per_source": True,
            "allow_abstain": True,
        },
    }
    validate_outbound_packet(packet)
    return packet


def expand_postgres_relation_profile(
    relation_profile: Mapping[str, Any],
    *,
    split_group: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Expand a bounded relation profile into model-bound population packets.

    No value fingerprints are needed.  The returned packets match the shapes
    queued by ``business_topology_excavate_relation`` for fields, relation
    context, and source motifs.
    """

    validate_outbound_packet(relation_profile)
    if relation_profile.get("schema_version") != "rvbbit.business-topology.profile-packet.v1":
        raise ContractError("PostgreSQL expansion requires a profile-packet.v1 packet")

    source = relation_profile.get("source")
    context = relation_profile.get("relation_context")
    if not isinstance(source, Mapping) or not isinstance(context, Mapping):
        raise ContractError("relation profile requires source and relation_context objects")
    fields = context.get("fields")
    if not isinstance(fields, list):
        raise ContractError("relation profile relation_context.fields must be an array")
    field_pairs = context.get("field_pairs", [])
    if not isinstance(field_pairs, list):
        raise ContractError("relation profile relation_context.field_pairs must be an array")

    source_key = postgres_source_key(source)
    group = split_group or source_key
    neighbors: list[Mapping[str, Any]] = [
        {
            key: deepcopy(field[key])
            for key in (
                "name",
                "data_type",
                "type_family",
                "role_hints",
                "sensitivity_hint",
            )
            if key in field
        }
        for field in fields[:96]
        if isinstance(field, Mapping)
    ]

    populations: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, Mapping):
            raise ContractError("relation_context.fields entries must be objects")
        field_name = field.get("name")
        if not isinstance(field_name, str) or not field_name:
            raise ContractError("every profiled field requires a name")
        population_key = f"{source_key}#field:{field_name}"
        packet = make_population_packet(
            population_key=population_key,
            population_kind="field",
            selector={"columns": [field_name]},
            source=source,
            sample=relation_profile.get("sample", {}),
            field=field,
            neighbor_fields=neighbors,
            field_pair_features=[
                deepcopy(pair)
                for pair in field_pairs
                if isinstance(pair, Mapping)
                and (pair.get("left") == field_name or pair.get("right") == field_name)
            ],
        )
        populations.append(
            {
                "population_id": population_key,
                "split_group": group,
                "packet": packet,
            }
        )

    context_key = f"{source_key}#context"
    context_packet = make_population_packet(
        population_key=context_key,
        population_kind="record_context",
        context_only=True,
        source=source,
        sample=relation_profile.get("sample", {}),
        relation_context=context,
    )
    populations.append(
        {
            "population_id": context_key,
            "split_group": group,
            "packet": context_packet,
        }
    )

    motif_packet = make_source_motif_packet(relation_profile)
    return {
        "populations": populations,
        "motifs": [
            {
                "motif_id": f"{source_key}#motifs",
                "source_population_id": context_key,
                "split_group": group,
                "packet": motif_packet,
            }
        ],
    }


def make_correspondence_packet(
    left_packet: Mapping[str, Any],
    right_packet: Mapping[str, Any],
    *,
    local_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the common pair-scoring packet without source-specific rules."""

    validate_outbound_packet(left_packet)
    validate_outbound_packet(right_packet)
    if left_packet.get("schema_version") != "rvbbit.business-topology.population.v1":
        raise ContractError("left correspondence input must be a population.v1 packet")
    if right_packet.get("schema_version") != "rvbbit.business-topology.population.v1":
        raise ContractError("right correspondence input must be a population.v1 packet")
    if packet_population_id(left_packet) == packet_population_id(right_packet):
        raise ContractError("correspondence inputs must be different populations")

    evidence = dict(local_evidence or {})
    shared = max(int(evidence.get("shared_fingerprints", 0) or 0), 0)
    left_count = max(int(evidence.get("left_fingerprints", 0) or 0), 0)
    right_count = max(int(evidence.get("right_fingerprints", 0) or 0), 0)
    union = left_count + right_count - shared
    jaccard = float(evidence.get("jaccard", shared / union if union > 0 else 0.0) or 0.0)
    containment = float(
        evidence.get(
            "containment",
            max(
                shared / left_count if left_count else 0.0,
                shared / right_count if right_count else 0.0,
            ),
        )
        or 0.0
    )
    safe_evidence: dict[str, Any] = {
        "shared_fingerprints": shared,
        "left_fingerprints": left_count,
        "right_fingerprints": right_count,
        "jaccard": min(max(jaccard, 0.0), 1.0),
        "containment": min(max(containment, 0.0), 1.0),
    }
    for optional_key in (
        "name_token_overlap",
        "embedding_similarity",
        "query_join_count",
        "query_cooccurrence_count",
    ):
        if optional_key in evidence:
            safe_evidence[optional_key] = evidence[optional_key]

    packet = {
        "schema_version": "rvbbit.business-topology.correspondence.v1",
        "privacy": {
            "raw_values": False,
            "value_hashes": False,
            "local_overlap_only": True,
        },
        "verdict_contract": list(CORRESPONDENCE_VERDICTS),
        "left": deepcopy(dict(left_packet)),
        "right": deepcopy(dict(right_packet)),
        "local_evidence": safe_evidence,
        "profile_receipts": {
            "left_profile_hash": stable_hash(left_packet),
            "right_profile_hash": stable_hash(right_packet),
        },
    }
    validate_outbound_packet(packet)
    return packet


def packet_text(packet: Mapping[str, Any], *, channel: str = "combined") -> str:
    """Render stable, privacy-safe text suitable for an existing embedder.

    This is intentionally generic.  It names the source and summarized field
    behavior, but does not introduce customer-specific prompt templates.
    """

    validate_outbound_packet(packet)
    if channel not in {"combined", "focus", "context"}:
        raise ValueError("embedding text channel must be combined, focus, or context")
    parts: list[str] = []
    source = packet.get("source")
    if channel != "focus" and isinstance(source, Mapping):
        source_name = ".".join(
            str(source[key])
            for key in ("database", "schema", "relation", "name")
            if source.get(key) not in (None, "")
        )
        if source_name:
            parts.append(f"Source: {source_name}.")
        if source.get("kind") not in (None, ""):
            parts.append(f"Source kind: {source['kind']}.")
        if source.get("comment") not in (None, ""):
            parts.append(f"Source description: {source['comment']}")
    population = packet.get("population")
    if isinstance(population, Mapping):
        parts.append(f"Population kind: {population.get('kind', 'unknown')}.")
        selector = population.get("selector")
        if channel != "context" and isinstance(selector, Mapping) and selector:
            parts.append("Population selector metadata: " + json.dumps(selector, sort_keys=True))
    field = packet.get("field")
    if channel != "context" and isinstance(field, Mapping):
        field_name = str(field.get("name") or "unnamed")
        parts.append(f"Focus field: {field_name}.")
        for label, key in (
            ("Field description", "comment"),
            ("Database type", "data_type"),
            ("Type family", "type_family"),
            ("Sensitivity hint", "sensitivity_hint"),
            ("Structural roles", "role_hints"),
            ("Name tokens", "name_tokens"),
            ("Observed value shapes", "value_shapes"),
        ):
            value = field.get(key)
            if value not in (None, "", [], {}):
                parts.append(f"{label}: {json.dumps(value, sort_keys=True)}.")
    neighbors = packet.get("neighbor_fields")
    if channel == "context" and isinstance(neighbors, list):
        field_names = sorted(
            {
                str(item["name"])
                for item in neighbors
                if isinstance(item, Mapping) and item.get("name") not in (None, "")
            }
        )[:64]
        if field_names:
            parts.append("Fields observed in this record context: " + ", ".join(field_names) + ".")
    elif channel == "combined" and isinstance(neighbors, list):
        focus_name = str(field.get("name") or "") if isinstance(field, Mapping) else ""
        focus_tokens = (
            set(str(token) for token in field.get("name_tokens", []))
            if isinstance(field, Mapping)
            else set()
        )
        focus_roles = (
            set(str(role) for role in field.get("role_hints", []))
            if isinstance(field, Mapping)
            else set()
        )

        def neighbor_priority(item: Mapping[str, Any]) -> tuple[int, int, str]:
            name = str(item.get("name") or "")
            tokens = {token for token in name.lower().replace("-", "_").split("_") if token}
            roles = {str(role) for role in item.get("role_hints", [])}
            return (
                -len(focus_roles & roles),
                -len(focus_tokens & tokens),
                name,
            )

        ranked_neighbors = sorted(
            (
                item
                for item in neighbors
                if isinstance(item, Mapping) and item.get("name") != focus_name
            ),
            key=neighbor_priority,
        )[:24]
        summaries = [
            {
                key: item.get(key)
                for key in ("name", "type_family", "role_hints")
                if item.get(key) not in (None, [], "")
            }
            for item in ranked_neighbors
        ]
        if summaries:
            parts.append("Relevant neighboring fields: " + json.dumps(summaries, sort_keys=True))

    field_pairs = packet.get("field_pair_features")
    if channel == "combined" and isinstance(field_pairs, list) and isinstance(field, Mapping):
        focus_name = str(field.get("name") or "")

        def numeric_strength(value: Any) -> float:
            try:
                parsed = float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return parsed if parsed == parsed else 0.0

        def pair_priority(item: Mapping[str, Any]) -> tuple[float, str]:
            strength = max(
                numeric_strength(item.get("left_to_right_strength")),
                numeric_strength(item.get("right_to_left_strength")),
                numeric_strength(item.get("same_presence_fraction")),
            )
            other = str(item.get("right") if item.get("left") == focus_name else item.get("left"))
            return (-strength, other)

        associations = []
        for item in sorted(
            (item for item in field_pairs if isinstance(item, Mapping)),
            key=pair_priority,
        )[:12]:
            other = item.get("right") if item.get("left") == focus_name else item.get("left")
            associations.append(
                {
                    "other_field": other,
                    "co_presence": item.get("both_present_fraction"),
                    "same_presence": item.get("same_presence_fraction"),
                    "focus_determines_other": (
                        item.get("left_to_right_strength")
                        if item.get("left") == focus_name
                        else item.get("right_to_left_strength")
                    ),
                    "other_determines_focus": (
                        item.get("right_to_left_strength")
                        if item.get("left") == focus_name
                        else item.get("left_to_right_strength")
                    ),
                }
            )
        if associations:
            parts.append(
                "Bounded within-record associations: " + json.dumps(associations, sort_keys=True)
            )
    relation_context = packet.get("relation_context")
    if channel != "focus" and isinstance(relation_context, Mapping):
        parts.append(f"Relation context: {relation_context.get('name', '')}.")
        fields = relation_context.get("fields", [])
        if isinstance(fields, list):
            if channel == "context":
                field_names = sorted(
                    {
                        str(item["name"])
                        for item in fields
                        if isinstance(item, Mapping) and item.get("name") not in (None, "")
                    }
                )[:64]
                if field_names:
                    parts.append("Fields observed: " + ", ".join(field_names) + ".")
            else:
                summaries = []
                for item in fields[:96]:
                    if isinstance(item, Mapping):
                        summaries.append(
                            {
                                key: item.get(key)
                                for key in ("name", "type_family", "role_hints")
                                if item.get(key) not in (None, [], "")
                            }
                        )
                parts.append("Neighbor fields: " + json.dumps(summaries, sort_keys=True))
    return "\n".join(parts)
