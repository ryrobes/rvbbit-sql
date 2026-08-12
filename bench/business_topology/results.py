"""Validation boundary for model-produced excavation results.

Models may name and organize candidate business concepts, but they may only
refer to populations and evidence present in their bounded work item.  These
validators produce no database mutations; valid results still enter the normal
proposal and human-review lifecycle.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ContractError, POPULATION_ROLES
from .excavation import EXCAVATION_PLAN_SCHEMA_VERSION


SOURCE_MOTIFS_RESULT_SCHEMA_VERSION = "rvbbit.business-topology.source-motifs-result.v1"
CORRESPONDENCE_RESULT_SCHEMA_VERSION = "rvbbit.business-topology.correspondence-result.v1"
NEIGHBORHOOD_RESULT_SCHEMA_VERSION = "rvbbit.business-topology.neighborhood-skeleton-result.v1"
BRIDGE_RESULT_SCHEMA_VERSION = "rvbbit.business-topology.bridge-result.v1"

_FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "sql",
        "where_sql",
        "raw_value",
        "raw_values",
        "sample_value",
        "sample_values",
        "value_fingerprint",
        "value_fingerprints",
        "fingerprint",
        "fingerprints",
        "split_group",
    }
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _object(value: Any, path: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list[Any]:
    _require(isinstance(value, list), f"{path} must be an array")
    return value


def _string(value: Any, path: str, *, maximum: int = 500) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{path} must be non-empty")
    _require(len(value) <= maximum, f"{path} exceeds {maximum} characters")
    return value


def _score(value: Any, path: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        f"{path} must be a finite number",
    )
    parsed = float(value)
    _require(0.0 <= parsed <= 1.0, f"{path} must be between zero and one")
    return parsed


def _walk_result(value: Any, path: str = "result") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            _require(key_text not in _FORBIDDEN_RESULT_KEYS, f"{child_path} is forbidden")
            _walk_result(child, child_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _walk_result(child, f"{path}[{index}]")


def _work_index(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    _require(
        plan.get("schema_version") == EXCAVATION_PLAN_SCHEMA_VERSION,
        "unsupported excavation-plan schema",
    )
    work_items = _array(plan.get("work_items"), "plan.work_items")
    result: dict[str, Mapping[str, Any]] = {}
    for index, work in enumerate(work_items):
        item = _object(work, f"plan.work_items[{index}]")
        work_id = _string(item.get("work_id"), f"plan.work_items[{index}].work_id")
        _require(work_id not in result, f"duplicate plan work id: {work_id}")
        result[work_id] = item
    return result


def _evidence_ids(
    value: Any,
    path: str,
    allowed: set[str],
    *,
    required: bool = True,
) -> list[str]:
    evidence = _array(value, path)
    _require(
        all(isinstance(item, str) and item in allowed for item in evidence),
        f"{path} contains evidence outside the bounded work dependencies",
    )
    _require(len(evidence) == len(set(evidence)), f"{path} contains duplicates")
    if required:
        _require(bool(evidence), f"{path} must cite at least one evidence work item")
    return evidence


def _validate_parent_graph(nodes: Mapping[str, Mapping[str, Any]]) -> None:
    parents = {
        key: node.get("parent_node_key")
        for key, node in nodes.items()
        if node.get("parent_node_key") is not None
    }
    for key, parent in parents.items():
        _require(isinstance(parent, str) and parent in nodes, f"node {key} has an unknown parent")
        _require(parent != key, f"node {key} cannot parent itself")
    for start in nodes:
        visited: set[str] = set()
        current = start
        while current in parents:
            _require(current not in visited, "neighborhood result hierarchy contains a cycle")
            visited.add(current)
            current = str(parents[current])


def _validate_source_motifs_result(
    work: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    packet = _object(work.get("input_packet"), "work.input_packet")
    contract = _object(packet.get("output_contract"), "work.output_contract")
    context = _object(packet.get("relation_context"), "work.relation_context")
    fields = _array(context.get("fields"), "work.relation_context.fields")
    allowed_fields = {
        _string(_object(field, "work.relation_context.field").get("name"), "field.name")
        for field in fields
    }
    allowed_kinds = set(
        _array(
            contract.get(
                "population_kinds",
                ["composite", "slice", "event_stream", "query_projection"],
            ),
            "population_kinds",
        )
    )
    allowed_roles = set(
        _array(contract.get("population_roles", list(POPULATION_ROLES)), "population_roles")
    )
    status = result.get("status")
    _require(status in {"proposed", "abstained"}, "result.status is invalid")
    if result.get("source_summary") is not None:
        _string(result.get("source_summary"), "result.source_summary", maximum=4_000)

    motif_keys: set[str] = set()
    assigned_fields: set[str] = set()
    motifs = _array(result.get("motifs"), "result.motifs")
    for index, raw_motif in enumerate(motifs):
        motif = _object(raw_motif, f"result.motifs[{index}]")
        key = _string(
            motif.get("motif_key"),
            f"result.motifs[{index}].motif_key",
            maximum=180,
        )
        _require(key not in motif_keys, f"duplicate result motif_key: {key}")
        motif_keys.add(key)
        _require(
            motif.get("population_kind") in allowed_kinds,
            f"result motif {key} has an unsupported population_kind",
        )
        _string(motif.get("name"), f"result motif {key}.name", maximum=180)
        if motif.get("description") is not None:
            _string(
                motif.get("description"),
                f"result motif {key}.description",
                maximum=4_000,
            )
        _score(motif.get("confidence"), f"result motif {key}.confidence")
        motif_fields = _array(motif.get("field_names"), f"result motif {key}.field_names")
        _require(bool(motif_fields), f"result motif {key} needs at least one field")
        _require(
            all(isinstance(name, str) and name in allowed_fields for name in motif_fields),
            f"result motif {key} references a field outside its source",
        )
        _require(
            len(motif_fields) == len(set(motif_fields)),
            f"result motif {key} repeats a field",
        )
        assigned_fields.update(str(name) for name in motif_fields)
        roles = _array(motif.get("roles", []), f"result motif {key}.roles")
        _require(
            all(isinstance(role, str) and role in allowed_roles for role in roles),
            f"result motif {key} has an unsupported role",
        )
        _require(len(roles) == len(set(roles)), f"result motif {key} repeats a role")
        if motif.get("rationale") is not None:
            _string(motif.get("rationale"), f"result motif {key}.rationale", maximum=2_000)

    unassigned = _array(result.get("unassigned_field_names"), "result.unassigned_field_names")
    _require(
        all(isinstance(name, str) and name in allowed_fields for name in unassigned),
        "result.unassigned_field_names contains a field outside its source",
    )
    _require(
        len(unassigned) == len(set(unassigned)),
        "result.unassigned_field_names contains duplicates",
    )
    _require(
        not assigned_fields.intersection(unassigned),
        "a source field cannot be both assigned and explicitly unassigned",
    )
    _require(
        assigned_fields | set(unassigned) == allowed_fields,
        "every source field must be assigned or explicitly unassigned",
    )
    if status == "abstained":
        _require(not motifs, "an abstained source-motifs result cannot contain motifs")
        _string(result.get("rationale"), "result.rationale", maximum=4_000)
    else:
        _require(bool(motifs), "a proposed source-motifs result requires motifs")

    return {
        "valid": True,
        "work_id": work["work_id"],
        "work_kind": work["work_kind"],
        "status": status,
        "motifs": len(motifs),
        "assigned_fields": len(assigned_fields),
        "unassigned_fields": len(unassigned),
    }


def _validate_correspondence_result(
    work: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    packet = _object(work.get("input_packet"), "work.input_packet")
    allowed_verdicts = set(_array(packet.get("verdict_contract"), "verdict_contract"))
    status = result.get("status")
    _require(status in {"proposed", "abstained"}, "result.status is invalid")
    population_ids = _array(result.get("population_ids"), "result.population_ids")
    _require(
        population_ids == _array(work.get("population_ids"), "work.population_ids"),
        "result.population_ids must exactly match the bounded pair",
    )
    verdicts = _array(result.get("verdicts"), "result.verdicts")
    _require(bool(verdicts), "result.verdicts cannot be empty")
    _require(
        all(isinstance(verdict, str) and verdict in allowed_verdicts for verdict in verdicts),
        "result.verdicts contains an unsupported verdict",
    )
    _require(len(verdicts) == len(set(verdicts)), "result.verdicts contains duplicates")
    _require(
        "abstain" not in verdicts or verdicts == ["abstain"],
        "abstain cannot be combined with another correspondence verdict",
    )
    scores = _object(result.get("scores"), "result.scores")
    _require(
        set(scores) == allowed_verdicts,
        "result.scores must contain exactly the verdict contract",
    )
    for verdict, score in scores.items():
        _score(score, f"result.scores.{verdict}")
    confidence = _score(result.get("confidence"), "result.confidence")
    uncertainty = _score(result.get("uncertainty"), "result.uncertainty")
    _require(
        abs((confidence + uncertainty) - 1.0) <= 1e-6,
        "result confidence and uncertainty must sum to one",
    )
    _require(
        abs(confidence - max(float(scores[verdict]) for verdict in verdicts)) <= 1e-6,
        "result confidence must equal the strongest selected-verdict score",
    )
    if result.get("rationale") is not None:
        _string(result.get("rationale"), "result.rationale", maximum=4_000)
    if status == "abstained":
        _require(verdicts == ["abstain"], "an abstained result requires the abstain verdict")
    else:
        _require(verdicts != ["abstain"], "a proposed result cannot use only abstain")

    return {
        "valid": True,
        "work_id": work["work_id"],
        "work_kind": work["work_kind"],
        "status": status,
        "verdicts": verdicts,
        "confidence": confidence,
    }


def _validate_neighborhood_result(
    work: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    packet = _object(work.get("input_packet"), "work.input_packet")
    output_contract = _object(packet.get("output_contract"), "work.output_contract")
    allowed_node_kinds = set(_array(output_contract.get("node_kinds"), "node_kinds"))
    allowed_binding_roles = set(_array(output_contract.get("binding_roles"), "binding_roles"))
    allowed_populations = set(_array(work.get("population_ids"), "work.population_ids"))
    allowed_evidence = set(_array(work.get("depends_on"), "work.depends_on"))
    status = result.get("status")
    _require(status in {"proposed", "abstained"}, "result.status is invalid")
    canonical_name = result.get("canonical_name")
    if canonical_name is not None:
        _string(canonical_name, "result.canonical_name", maximum=120)
    if status == "proposed" and output_contract.get("requires_canonical_name") is True:
        _string(canonical_name, "result.canonical_name", maximum=120)

    node_rows = _array(result.get("nodes"), "result.nodes")
    binding_rows = _array(result.get("bindings"), "result.bindings")
    edge_rows = _array(result.get("edges"), "result.edges")
    max_nodes = int(output_contract.get("max_nodes", 16))
    max_bindings = int(output_contract.get("max_bindings", 48))
    max_edges = int(output_contract.get("max_edges", 24))
    _require(len(node_rows) <= max_nodes, f"result.nodes exceeds the {max_nodes} item budget")
    _require(
        len(binding_rows) <= max_bindings,
        f"result.bindings exceeds the {max_bindings} item budget",
    )
    _require(len(edge_rows) <= max_edges, f"result.edges exceeds the {max_edges} item budget")

    nodes: dict[str, Mapping[str, Any]] = {}
    for index, raw_node in enumerate(node_rows):
        node = _object(raw_node, f"result.nodes[{index}]")
        key = _string(node.get("node_key"), f"result.nodes[{index}].node_key", maximum=180)
        _require(key not in nodes, f"duplicate result node_key: {key}")
        _require(
            node.get("node_kind") in allowed_node_kinds,
            f"result node {key} has an unsupported node_kind",
        )
        _string(node.get("name"), f"result node {key}.name", maximum=180)
        if node.get("description") is not None:
            _string(node.get("description"), f"result node {key}.description", maximum=4_000)
        _score(node.get("confidence"), f"result node {key}.confidence")
        _object(node.get("properties", {}), f"result node {key}.properties")
        _evidence_ids(
            node.get("evidence_work_ids"),
            f"result node {key}.evidence_work_ids",
            allowed_evidence,
        )
        nodes[key] = node
    _validate_parent_graph(nodes)

    bound_populations: set[str] = set()
    bound_nodes: set[str] = set()
    binding_keys: set[tuple[str, str, str]] = set()
    bindings = binding_rows
    for index, raw_binding in enumerate(bindings):
        binding = _object(raw_binding, f"result.bindings[{index}]")
        node_key = binding.get("node_key")
        population_id = binding.get("population_id")
        role = binding.get("binding_role")
        _require(node_key in nodes, f"result binding {index} references an unknown node")
        _require(
            population_id in allowed_populations,
            f"result binding {index} references a population outside its unit",
        )
        _require(role in allowed_binding_roles, f"result binding {index} has an invalid role")
        authority = binding.get("authority_hint", "unknown")
        _require(
            authority in {"unknown", "primary", "secondary", "derived", "conflicting"},
            f"result binding {index} has an invalid authority_hint",
        )
        _score(binding.get("confidence"), f"result binding {index}.confidence")
        _evidence_ids(
            binding.get("evidence_work_ids"),
            f"result binding {index}.evidence_work_ids",
            allowed_evidence,
        )
        binding_key = (str(node_key), str(population_id), str(role))
        _require(binding_key not in binding_keys, f"duplicate result binding: {binding_key}")
        binding_keys.add(binding_key)
        bound_nodes.add(str(node_key))
        bound_populations.add(str(population_id))
    _require(set(nodes) == bound_nodes, "every proposed node must have at least one binding")

    edge_keys: set[tuple[str, str, str]] = set()
    edges = edge_rows
    for index, raw_edge in enumerate(edges):
        edge = _object(raw_edge, f"result.edges[{index}]")
        subject = edge.get("subject_node_key")
        object_key = edge.get("object_node_key")
        predicate = _string(edge.get("predicate"), f"result edge {index}.predicate", maximum=120)
        _require(subject in nodes and object_key in nodes, f"result edge {index} has unknown nodes")
        _require(subject != object_key, f"result edge {index} cannot be a self-edge")
        _score(edge.get("confidence"), f"result edge {index}.confidence")
        _evidence_ids(
            edge.get("evidence_work_ids"),
            f"result edge {index}.evidence_work_ids",
            allowed_evidence,
        )
        edge_key = (str(subject), predicate, str(object_key))
        _require(edge_key not in edge_keys, f"duplicate result edge: {edge_key}")
        edge_keys.add(edge_key)

    unbound = _array(result.get("unbound_population_ids"), "result.unbound_population_ids")
    _require(
        all(isinstance(item, str) and item in allowed_populations for item in unbound),
        "result.unbound_population_ids contains a population outside its unit",
    )
    _require(len(unbound) == len(set(unbound)), "result.unbound_population_ids has duplicates")
    _require(
        not bound_populations.intersection(unbound),
        "a population cannot be both bound and explicitly unbound",
    )
    _require(
        bound_populations | set(unbound) == allowed_populations,
        "every unit population must be bound or explicitly unbound",
    )
    if status == "abstained":
        _require(
            not nodes and not bindings and not edges, "an abstained result cannot propose items"
        )
        _string(result.get("rationale"), "result.rationale", maximum=4_000)
    else:
        _require(bool(nodes), "a proposed neighborhood result requires at least one node")

    return {
        "valid": True,
        "work_id": work["work_id"],
        "work_kind": work["work_kind"],
        "status": status,
        "nodes": len(nodes),
        "bindings": len(bindings),
        "edges": len(edges),
        "unbound_populations": len(unbound),
    }


def _node_ref(
    value: Any,
    path: str,
    synthesis_work_ids: set[str],
    prior_results: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str]:
    ref = _object(value, path)
    work_id = _string(ref.get("work_id"), f"{path}.work_id")
    node_key = _string(ref.get("node_key"), f"{path}.node_key", maximum=180)
    _require(work_id in synthesis_work_ids, f"{path} references a synthesis outside the bridge")
    if work_id in prior_results:
        nodes = {
            str(node.get("node_key"))
            for node in _array(
                prior_results[work_id].get("nodes"), f"prior_results.{work_id}.nodes"
            )
            if isinstance(node, Mapping)
        }
        _require(node_key in nodes, f"{path} references an unknown prior node")
    return work_id, node_key


def _validate_bridge_result(
    work: Mapping[str, Any],
    result: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    packet = _object(work.get("input_packet"), "work.input_packet")
    output_contract = _object(packet.get("output_contract"), "work.output_contract")
    allowed_outcomes = set(_array(output_contract.get("allowed_outcomes"), "allowed_outcomes"))
    synthesis_work_ids = set(
        _array(packet.get("neighborhood_synthesis_work_ids"), "synthesis_work_ids")
    )
    allowed_evidence = set(_array(work.get("depends_on"), "work.depends_on"))
    allowed_populations = set(_array(work.get("population_ids"), "work.population_ids"))
    status = result.get("status")
    _require(status in {"proposed", "abstained"}, "result.status is invalid")
    _require(
        result.get("merge_excavation_units", False) is False,
        "bridge results cannot merge excavation units",
    )

    finding_keys: set[str] = set()
    findings = _array(result.get("findings"), "result.findings")
    for index, raw_finding in enumerate(findings):
        finding = _object(raw_finding, f"result.findings[{index}]")
        key = _string(
            finding.get("finding_key"),
            f"result.findings[{index}].finding_key",
            maximum=180,
        )
        _require(key not in finding_keys, f"duplicate bridge finding_key: {key}")
        finding_keys.add(key)
        outcome = finding.get("outcome")
        _require(outcome in allowed_outcomes, f"bridge finding {key} has an invalid outcome")
        _score(finding.get("confidence"), f"bridge finding {key}.confidence")
        _evidence_ids(
            finding.get("evidence_work_ids"),
            f"bridge finding {key}.evidence_work_ids",
            allowed_evidence,
        )
        node_refs = [
            _node_ref(
                finding[name], f"bridge finding {key}.{name}", synthesis_work_ids, prior_results
            )
            for name in ("left_node_ref", "right_node_ref")
            if finding.get(name) is not None
        ]
        population_refs = [
            finding.get(name)
            for name in ("left_population_id", "right_population_id")
            if finding.get(name) is not None
        ]
        if node_refs:
            _require(len(node_refs) == 2, f"bridge finding {key} requires two node refs")
            _require(
                node_refs[0][0] != node_refs[1][0],
                f"bridge finding {key} node refs must cross synthesis units",
            )
        if population_refs:
            _require(
                len(population_refs) == 2, f"bridge finding {key} requires two population refs"
            )
            _require(
                all(ref in allowed_populations for ref in population_refs),
                f"bridge finding {key} references populations outside its probes",
            )
            _require(
                population_refs[0] != population_refs[1],
                f"bridge finding {key} population refs must be distinct",
            )
        if outcome not in {"unrelated", "abstain"}:
            _require(
                len(node_refs) == 2 or len(population_refs) == 2,
                f"bridge finding {key} requires bounded node or population refs",
            )
    if status == "abstained":
        _require(not findings, "an abstained bridge result cannot contain findings")
        _string(result.get("rationale"), "result.rationale", maximum=4_000)
    else:
        _require(bool(findings), "a proposed bridge result requires findings")

    return {
        "valid": True,
        "work_id": work["work_id"],
        "work_kind": work["work_kind"],
        "status": status,
        "findings": len(findings),
    }


def validate_excavation_result(
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    prior_results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate one model result against its exact bounded work item."""

    result = _object(result, "result")
    _walk_result(result)
    work_by_id = _work_index(plan)
    work_id = _string(result.get("work_id"), "result.work_id")
    _require(work_id in work_by_id, "result.work_id is outside the excavation plan")
    work = work_by_id[work_id]
    kind = work.get("work_kind")
    if kind == "source_motifs":
        _require(
            result.get("schema_version") == SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
            "source-motifs result has an unsupported schema_version",
        )
        return _validate_source_motifs_result(work, result)
    if kind == "correspondence":
        _require(
            result.get("schema_version") == CORRESPONDENCE_RESULT_SCHEMA_VERSION,
            "correspondence result has an unsupported schema_version",
        )
        return _validate_correspondence_result(work, result)
    if kind == "neighborhood_synthesis":
        _require(
            result.get("schema_version") == NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
            "neighborhood result has an unsupported schema_version",
        )
        return _validate_neighborhood_result(work, result)
    if kind == "bridge_synthesis":
        _require(
            result.get("schema_version") == BRIDGE_RESULT_SCHEMA_VERSION,
            "bridge result has an unsupported schema_version",
        )
        validated_prior: dict[str, Mapping[str, Any]] = {}
        for prior_work_id, prior in (prior_results or {}).items():
            _require(
                prior.get("work_id") == prior_work_id,
                "prior result key does not match its work_id",
            )
            prior_summary = validate_excavation_result(plan, prior)
            _require(
                prior_summary["work_kind"] == "neighborhood_synthesis",
                "bridge prior results must be neighborhood synthesis results",
            )
            validated_prior[prior_work_id] = prior
        return _validate_bridge_result(work, result, validated_prior)
    raise ContractError("unsupported excavation work kind")
