from __future__ import annotations

import copy
import json
import os
import uuid
from collections import Counter

import psycopg
import pytest
from psycopg import sql

from business_topology.baseline import predict_correspondence
from business_topology.candidates import (
    build_candidate_queue,
    correspondence_packet_for_item,
    pair_key,
)
from business_topology.contracts import (
    ContractError,
    validate_corpus,
    validate_outbound_packet,
)
from business_topology.domains import (
    evaluate_source_neighborhood_controls,
    propose_source_neighborhoods,
)
from business_topology.extract import (
    extract_postgres_overlap_shadow,
    extract_postgres_shadow_corpus,
)
from business_topology.embedding import (
    budget_embedding_batches,
    build_embedding_evidence,
    embed_postgres_shadow,
    make_embedding_inputs,
)
from business_topology.excavation import (
    EXCAVATION_PLAN_SCHEMA_VERSION,
    build_excavation_plan,
    validate_excavation_plan,
)
from business_topology.labels import apply_label_overlay, make_label_template
from business_topology.linear import PortableOVR
from business_topology.metrics import (
    evaluate_candidate_recall,
    evaluate_corpus,
    grouped_split,
)
from business_topology.packets import make_population_packet, packet_text
from business_topology.results import (
    BRIDGE_RESULT_SCHEMA_VERSION,
    CORRESPONDENCE_RESULT_SCHEMA_VERSION,
    NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
    SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
    validate_excavation_result,
)
from business_topology.synthetic import make_synthetic_corpus
from business_topology.train import train_corpus_baseline
from business_topology.worker import (
    _compact_prompt_payload,
    _expand_result_identifiers,
    _fit_neighborhood_binding_budget,
    _json_object,
    _neighborhood_prompt,
    _normalize_neighborhood_result,
    _synthesis_population_frontier,
    correspondence_result,
    execute_plan,
    execution_preview,
    select_work,
)


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)


def test_synthetic_corpus_exercises_multi_object_sources_and_safe_packets():
    corpus = make_synthetic_corpus()
    validate_corpus(corpus, require_reviewed=True)

    event_populations = [
        item
        for item in corpus["populations"]
        if item["packet"].get("source", {}).get("relation") == "event_ledger"
        and item["packet"].get("field")
    ]
    assert {item["gold"]["concept"] for item in event_populations} == {
        "activity",
        "party",
        "place",
    }
    event_motif = next(
        item
        for item in corpus["motifs"]
        if item["packet"]["source"]["relation"] == "event_ledger"
    )
    assert len(event_motif["gold"]["populations"]) == 3
    assert (
        event_motif["packet"]["output_contract"]["allow_multiple_objects_per_source"]
        is True
    )

    for item in corpus["populations"]:
        validate_outbound_packet(item["packet"])
        rendered = packet_text(item["packet"])
        assert "value_fingerprints" not in rendered


def test_packet_contract_rejects_raw_values_and_hashes():
    packet = copy.deepcopy(make_synthetic_corpus()["populations"][0]["packet"])
    packet["field"]["values"] = ["must-never-leave-the-source"]
    with pytest.raises(ContractError, match="forbidden"):
        validate_outbound_packet(packet)

    packet = copy.deepcopy(make_synthetic_corpus()["populations"][0]["packet"])
    packet["privacy"]["value_hashes"] = True
    with pytest.raises(ContractError, match="must be false"):
        validate_outbound_packet(packet)


def test_core_accepts_nonrelational_population_adapters_without_source_branches():
    packets = [
        make_population_packet(
            population_key="document-source:mentions:organizations",
            population_kind="mention_set",
            source={
                "kind": "document_collection",
                "name": "support correspondence",
                "comment": "bounded organization mentions",
            },
            selector={"mention_type": "organization"},
        ),
        make_population_packet(
            population_key="service-source:activity",
            population_kind="event_stream",
            source={
                "kind": "external_object",
                "name": "service activity",
            },
            selector={"event_family": "status_change"},
        ),
    ]
    corpus = {
        "schema_version": "rvbbit.business-topology.eval-corpus.v1",
        "corpus_id": "synthetic-nonrelational-adapters",
        "populations": [
            {
                "population_id": packet["population"]["population_key"],
                "split_group": f"adapter:{index}",
                "packet": packet,
            }
            for index, packet in enumerate(packets)
        ],
        "motifs": [],
        "correspondences": [],
    }
    validate_corpus(corpus)
    inputs = make_embedding_inputs(corpus, channels=("focus", "context"))
    assert len(inputs) == 4
    assert {item["channel"] for item in inputs} == {"focus", "context"}
    assert any(
        "organization" in item["text"] for item in inputs if item["channel"] == "focus"
    )
    mention_inputs = make_embedding_inputs(
        corpus,
        channels=("focus",),
        kinds=("mention_set",),
    )
    assert [item["population_id"] for item in mention_inputs] == [
        "document-source:mentions:organizations"
    ]


def _source_neighborhood_fixture():
    specifications = [
        ("alpha-1", "relation", "record_context", "control-a", [1.0, 0.0, 0.0, 0.0]),
        ("alpha-2", "relation", "record_context", "control-a", [0.99, 0.10, 0.0, 0.0]),
        (
            "alpha-3",
            "external_object",
            "event_stream",
            "control-a",
            [0.98, 0.15, 0.0, 0.0],
        ),
        (
            "beta-1",
            "document_collection",
            "mention_set",
            "control-b",
            [0.60, 0.80, 0.0, 0.0],
        ),
        (
            "beta-2",
            "external_object",
            "event_stream",
            "control-b",
            [0.55, 0.83, 0.0, 0.0],
        ),
        ("gamma-1", "relation", "record_context", "control-c", [0.0, 0.0, 1.0, 0.0]),
        (
            "gamma-2",
            "document_collection",
            "mention_set",
            "control-c",
            [0.0, 0.0, 0.99, 0.10],
        ),
        (
            "isolated",
            "external_object",
            "event_stream",
            "control-d",
            [0.0, 0.0, 0.0, 1.0],
        ),
    ]
    populations = []
    vectors = []
    for name, source_kind, population_kind, split_group, vector in specifications:
        population_id = f"source-neighborhood:{name}"
        packet = make_population_packet(
            population_key=population_id,
            population_kind=population_kind,
            source={"kind": source_kind, "name": name},
            selector={"adapter_key": name}
            if population_kind != "record_context"
            else None,
            relation_context={"name": name, "fields": []},
            context_only=population_kind == "record_context",
        )
        populations.append(
            {
                "population_id": population_id,
                "split_group": split_group,
                "packet": packet,
            }
        )
        vectors.append(
            {
                "population_id": population_id,
                "channel": "context",
                "embedding": vector,
            }
        )
    return (
        {
            "schema_version": "rvbbit.business-topology.eval-corpus.v1",
            "corpus_id": "synthetic-source-neighborhoods",
            "populations": populations,
            "motifs": [],
            "correspondences": [],
        },
        vectors,
    )


def test_source_neighborhoods_are_generic_sparse_and_do_not_force_noise():
    corpus, vectors = _source_neighborhood_fixture()
    proposal = propose_source_neighborhoods(
        corpus,
        vectors,
        tight_top_k=3,
        bridge_top_k=6,
        tight_quantile=0.70,
        bridge_quantile=0.40,
        minimum_tight_similarity=0.90,
        minimum_bridge_similarity=0.50,
        minimum_tight_local_lift=0.05,
        minimum_bridge_local_lift=0.03,
    )

    source_names = {
        source["source_key"]: source["source"]["name"] for source in proposal["sources"]
    }
    member_names = {
        frozenset(
            source_names[source_key]
            for source_key in neighborhood["member_source_keys"]
        )
        for neighborhood in proposal["neighborhoods"]
    }
    assert frozenset({"alpha-1", "alpha-2", "alpha-3"}) in member_names
    assert frozenset({"beta-1", "beta-2"}) in member_names
    assert frozenset({"gamma-1", "gamma-2"}) in member_names
    assert frozenset({"isolated"}) in member_names
    assert proposal["summary"] == {
        "sources": 8,
        "candidate_neighborhoods": 3,
        "singleton_sources": 1,
        "tight_affinities": proposal["summary"]["tight_affinities"],
        "bridges": proposal["summary"]["bridges"],
    }
    assert proposal["summary"]["tight_affinities"] >= 4
    assert proposal["summary"]["bridges"] >= 1
    assert {source["representation"] for source in proposal["sources"]} == {
        "record_context",
        "population_centroid",
    }
    assert proposal["inference_contract"] == {
        "asserts_business_domain": False,
        "uses_split_groups": False,
        "allows_singletons": True,
        "broad_links_merge_neighborhoods": False,
    }

    relabeled = copy.deepcopy(corpus)
    for index, item in enumerate(relabeled["populations"]):
        item["split_group"] = f"unrelated-control-{index}"
    assert (
        propose_source_neighborhoods(
            relabeled,
            vectors,
            tight_top_k=3,
            bridge_top_k=6,
            tight_quantile=0.70,
            bridge_quantile=0.40,
            minimum_tight_similarity=0.90,
            minimum_bridge_similarity=0.50,
            minimum_tight_local_lift=0.05,
            minimum_bridge_local_lift=0.03,
        )
        == proposal
    )

    controls = evaluate_source_neighborhood_controls(corpus, proposal)
    assert controls["tight_edge_same_control_rate"] == 1.0
    assert controls["pairwise_neighborhood"]["precision"] == 1.0
    assert controls["pairwise_neighborhood"]["recall"] == 1.0
    assert controls["cross_control_tight_edges"] == []


def test_source_neighborhoods_leave_an_incoherent_corpus_as_singletons():
    corpus, vectors = _source_neighborhood_fixture()
    corpus["populations"] = corpus["populations"][:4]
    vectors = [
        {
            **row,
            "embedding": [1.0 if position == index else 0.0 for position in range(4)],
        }
        for index, row in enumerate(vectors[:4])
    ]
    proposal = propose_source_neighborhoods(corpus, vectors)
    assert proposal["summary"]["candidate_neighborhoods"] == 0
    assert proposal["summary"]["singleton_sources"] == 4
    assert proposal["tight_affinities"] == []
    assert proposal["bridges"] == []


def _synthetic_excavation_fixture():
    corpus = make_synthetic_corpus()
    source_vectors = {
        "contacts": [1.0, 0.05, 0.0, 0.0],
        "members": [0.99, 0.10, 0.0, 0.0],
        "offices": [0.05, 1.0, 0.0, 0.0],
        "sites": [0.10, 0.99, 0.0, 0.0],
        "products": [0.0, 0.0, 1.0, 0.05],
        "catalog_items": [0.0, 0.0, 0.99, 0.10],
        "event_ledger": [0.58, 0.58, 0.0, 0.57],
    }
    vectors = [
        {
            "population_id": item["population_id"],
            "channel": "context",
            "embedding": source_vectors[item["packet"]["source"]["relation"]],
        }
        for item in corpus["populations"]
        if item["packet"]["population"]["kind"] == "record_context"
    ]
    neighborhoods = propose_source_neighborhoods(
        corpus,
        vectors,
        tight_top_k=2,
        bridge_top_k=4,
        tight_quantile=0.65,
        bridge_quantile=0.30,
        minimum_tight_similarity=0.90,
        minimum_bridge_similarity=0.45,
        minimum_tight_local_lift=0.05,
        minimum_bridge_local_lift=0.02,
    )
    evidence = {
        pair_key(item["left_population_id"], item["right_population_id"]): item[
            "local_evidence"
        ]
        for item in corpus["correspondences"]
    }
    return corpus, neighborhoods, evidence


def test_excavation_plan_is_bounded_multi_object_and_proposal_only():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=2,
        max_pairs_per_unit=16,
        max_pairs_per_link=6,
        max_population_fanout=4,
    )
    validate_excavation_plan(corpus, neighborhoods, plan)

    assert plan["summary"]["represented_sources"] == 7
    assert plan["summary"]["excavation_units"] == 4
    assert plan["summary"]["work_by_kind"]["source_motifs"] == 7
    assert plan["summary"]["work_by_kind"]["neighborhood_synthesis"] == 4
    assert plan["summary"]["work_by_kind"]["bridge_synthesis"] == 2
    assert all(unit["size"] <= 2 for unit in plan["excavation_units"])
    assert '"split_group":' not in json.dumps(plan)
    assert plan["inference_contract"] == {
        "uses_split_groups": False,
        "relation_is_business_object": False,
        "allows_multiple_objects_per_source": True,
        "materializes_topology": False,
        "requires_proposal_review": True,
    }

    event_motif = next(
        item
        for item in plan["work_items"]
        if item["work_kind"] == "source_motifs"
        and item["input_packet"]["source"]["relation"] == "event_ledger"
    )
    assert event_motif["input_packet"]["output_contract"] == {
        "populations": "declarative field bundles or slices",
        "allow_sql": False,
        "allow_multiple_objects_per_source": True,
        "allow_abstain": True,
    }
    synthesis = [
        item
        for item in plan["work_items"]
        if item["work_kind"] == "neighborhood_synthesis"
    ]
    assert all(
        item["input_packet"]["output_contract"]["result_is_proposal_only"] is True
        for item in synthesis
    )
    assert all(
        item["input_packet"]["output_contract"]["allow_multiple_objects_per_source"]
        is True
        for item in synthesis
    )
    assert all(
        item["input_packet"]["output_contract"]["requires_canonical_name"] is True
        for item in synthesis
    )

    work_by_id = {item["work_id"]: item for item in plan["work_items"]}
    assert all(
        work_by_id[dependency]["stage"] < item["stage"]
        for item in plan["work_items"]
        for dependency in item["depends_on"]
    )

    relabeled = copy.deepcopy(corpus)
    for index, item in enumerate(relabeled["populations"]):
        item["split_group"] = f"ignored-population-control-{index}"
    for index, item in enumerate(relabeled["motifs"]):
        item["split_group"] = f"ignored-motif-control-{index}"
    for index, item in enumerate(relabeled["correspondences"]):
        item["split_group"] = f"ignored-pair-control-{index}"
    assert (
        build_excavation_plan(
            relabeled,
            neighborhoods,
            evidence_by_pair=evidence,
            maximum_sources_per_unit=2,
            max_pairs_per_unit=16,
            max_pairs_per_link=6,
            max_population_fanout=4,
        )
        == plan
    )


def test_motif_and_correspondence_results_have_bounded_validation_contracts():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=2,
        max_pairs_per_unit=16,
        max_pairs_per_link=6,
        max_population_fanout=4,
    )
    motif_work = next(
        item for item in plan["work_items"] if item["work_kind"] == "source_motifs"
    )
    field_names = [
        field["name"]
        for field in motif_work["input_packet"]["relation_context"]["fields"]
    ]
    motif_result = {
        "schema_version": SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
        "work_id": motif_work["work_id"],
        "status": "proposed",
        "source_summary": "A cautious multi-field candidate.",
        "motifs": [
            {
                "motif_key": "candidate",
                "population_kind": "composite",
                "name": "Candidate",
                "field_names": field_names,
                "roles": ["identity"],
                "confidence": 0.71,
                "rationale": "The fields co-occur in the bounded source context.",
            }
        ],
        "unassigned_field_names": [],
    }
    assert validate_excavation_result(plan, motif_result)["motifs"] == 1
    escaped = copy.deepcopy(motif_result)
    escaped["motifs"][0]["field_names"].append("field_not_in_source")
    with pytest.raises(ContractError, match="outside its source"):
        validate_excavation_result(plan, escaped)

    pair_work = next(
        item for item in plan["work_items"] if item["work_kind"] == "correspondence"
    )
    pair_result, receipt = correspondence_result(pair_work)
    assert pair_result["schema_version"] == CORRESPONDENCE_RESULT_SCHEMA_VERSION
    assert validate_excavation_result(plan, pair_result)["valid"] is True
    assert receipt["model_version"] == "deterministic-correspondence-baseline-v1"
    wrong_pair = copy.deepcopy(pair_result)
    wrong_pair["population_ids"].reverse()
    with pytest.raises(ContractError, match="exactly match"):
        validate_excavation_result(plan, wrong_pair)


def test_excavation_worker_selection_and_local_resume_are_bounded(tmp_path):
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=2,
        max_pairs_per_unit=16,
        max_pairs_per_link=6,
        max_population_fanout=4,
    )
    unit = plan["excavation_units"][0]["excavation_unit_id"]
    closure = select_work(plan, unit_ids=[unit])
    preview = execution_preview(plan, closure)
    assert preview["work_by_kind"]["neighborhood_synthesis"] == 1
    assert preview["generative_calls_before_repairs"] >= 2
    assert preview["materialized_topology"] is False

    pair_work = next(
        item for item in plan["work_items"] if item["work_kind"] == "correspondence"
    )
    output_dir = tmp_path / "execution"
    progress_events = []
    first = execute_plan(
        plan,
        [pair_work["work_id"]],
        output_dir=output_dir,
        client=None,
        max_work_items=1,
        max_llm_calls=0,
        progress_callback=progress_events.append,
    )
    assert first["completed_now"] == 1
    assert first["local_correspondence_calls"] == 1
    assert first["submitted_proposals"] is False
    assert [event["event"] for event in progress_events] == [
        "started",
        "running",
        "completed",
        "finished",
    ]
    assert progress_events[-1]["completed_work_items"] == 1
    second = execute_plan(
        plan,
        [pair_work["work_id"]],
        output_dir=output_dir,
        client=None,
        max_work_items=1,
        max_llm_calls=0,
    )
    assert second["completed_now"] == 0
    assert second["resumed"] == 1
    assert oct(output_dir.stat().st_mode & 0o777) == "0o700"
    assert oct((output_dir / "execution.json").stat().st_mode & 0o777) == "0o600"

    reordered_neighborhoods = copy.deepcopy(neighborhoods)
    for family in ("sources", "neighborhoods", "tight_affinities", "bridges"):
        reordered_neighborhoods[family].reverse()
    assert (
        build_excavation_plan(
            corpus,
            reordered_neighborhoods,
            evidence_by_pair=evidence,
            maximum_sources_per_unit=2,
            max_pairs_per_unit=16,
            max_pairs_per_link=6,
            max_population_fanout=4,
        )
        == plan
    )


def test_worker_canonicalizes_only_invalid_outer_proposal_status():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=2,
        max_pairs_per_unit=16,
        max_pairs_per_link=6,
        max_population_fanout=4,
    )
    work = next(
        item
        for item in plan["work_items"]
        if item["work_kind"] == "neighborhood_synthesis"
    )
    evidence_work_id = work["depends_on"][0]
    population_id = work["population_ids"][0]
    payload = {
        "status": "completed",
        "nodes": [
            {
                "node_key": "candidate",
                "node_kind": "object",
                "name": "Candidate",
                "confidence": 0.72,
                "properties": {},
                "evidence_work_ids": [evidence_work_id],
            }
        ],
        "bindings": [
            {
                "node_key": "candidate",
                "population_id": population_id,
                "binding_role": "identity",
                "authority_hint": "unknown",
                "confidence": 0.72,
                "evidence_work_ids": [evidence_work_id],
            }
        ],
        "edges": [],
        "unbound_population_ids": [],
    }
    normalized = _normalize_neighborhood_result(
        work,
        {"status": "completed", "result": {**payload, "status": "proposed"}},
    )
    assert normalized["status"] == "proposed"
    assert normalized["canonical_name"] == "Candidate"
    assert validate_excavation_result(plan, normalized)["nodes"] == 1

    generic_status = _normalize_neighborhood_result(work, payload)
    assert generic_status["status"] == "proposed"
    assert validate_excavation_result(plan, generic_status)["nodes"] == 1

    explicitly_abstained = _normalize_neighborhood_result(
        work,
        {**payload, "status": "abstained"},
    )
    assert explicitly_abstained["status"] == "abstained"
    with pytest.raises(ContractError, match="abstained result cannot propose"):
        validate_excavation_result(plan, explicitly_abstained)


def test_worker_compacts_model_ids_and_expands_them_before_validation():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=2,
        max_pairs_per_unit=16,
        max_pairs_per_link=6,
        max_population_fanout=4,
    )
    work = next(
        item
        for item in plan["work_items"]
        if item["work_kind"] == "neighborhood_synthesis"
    )
    prompt_payload = _compact_prompt_payload(
        work,
        {
            "work": {
                "population_ids": work["population_ids"],
                "input_packet": work["input_packet"],
            },
            "dependencies": [
                {"work_id": work_id, "evidence_work_ids": [work_id]}
                for work_id in work["depends_on"]
            ],
        },
    )
    rendered = json.dumps(prompt_payload)
    assert all(
        population_id not in rendered for population_id in work["population_ids"]
    )
    assert all(work_id not in rendered for work_id in work["depends_on"])
    assert len(prompt_payload["identifier_aliases"]["population_ids"]) == len(
        work["population_ids"]
    )

    population_alias = prompt_payload["identifier_aliases"]["population_ids"][0][
        "alias"
    ]
    dependency_alias = prompt_payload["identifier_aliases"]["dependency_work_ids"][0]
    expanded = _expand_result_identifiers(
        work,
        {
            "status": "proposed",
            "canonical_name": "Candidate Operations",
            "nodes": [
                {
                    "node_key": "candidate",
                    "node_kind": "object",
                    "name": "Candidate",
                    "confidence": 0.78,
                    "properties": {},
                    "evidence_work_ids": [dependency_alias],
                }
            ],
            "bindings": [
                {
                    "node_key": "candidate",
                    "population_id": population_alias,
                    "binding_role": "identity",
                    "authority_hint": "unknown",
                    "confidence": 0.78,
                    "evidence_work_ids": [dependency_alias],
                }
            ],
            "edges": [],
            "unbound_population_ids": [],
        },
    )
    normalized = _normalize_neighborhood_result(work, expanded)
    assert (
        normalized["bindings"][0]["population_id"] == sorted(work["population_ids"])[0]
    )
    assert normalized["nodes"][0]["evidence_work_ids"] == [work["depends_on"][0]]
    assert validate_excavation_result(plan, normalized)["valid"] is True

    limited_plan = copy.deepcopy(plan)
    limited_work = next(
        item
        for item in limited_plan["work_items"]
        if item["work_id"] == work["work_id"]
    )
    limited_work["input_packet"]["output_contract"]["max_bindings"] = 0
    with pytest.raises(ContractError, match="bindings exceeds the 0 item budget"):
        validate_excavation_result(limited_plan, normalized)


def test_worker_never_recovers_an_inner_object_from_truncated_json():
    assert _json_object(
        'result follows: {"status":"abstained","rationale":"bounded"}'
    ) == {
        "status": "abstained",
        "rationale": "bounded",
    }
    with pytest.raises(ContractError, match="complete JSON object"):
        _json_object('{"status":"proposed","nodes":[{"node_key":"inner"}')


def test_binding_budget_projection_preserves_every_node_and_abstains_on_the_rest():
    population_ids = [f"population:{index:02d}" for index in range(50)]
    evidence_work_id = "work:motif"
    work = {
        "work_id": "work:synthesis",
        "work_kind": "neighborhood_synthesis",
        "population_ids": population_ids,
        "depends_on": [evidence_work_id],
        "input_packet": {
            "output_contract": {
                "node_kinds": ["object"],
                "binding_roles": ["identity", "attribute"],
                "max_nodes": 16,
                "max_bindings": 48,
                "max_edges": 24,
                "requires_canonical_name": True,
            }
        },
    }
    payload = {
        "status": "proposed",
        "canonical_name": "Customers",
        "nodes": [
            {
                "node_key": "customer",
                "node_kind": "object",
                "name": "Customer",
                "confidence": 0.9,
                "properties": {},
                "parent_node_key": None,
                "evidence_work_ids": [evidence_work_id],
            }
        ],
        "bindings": [
            {
                "node_key": "customer",
                "population_id": population_id,
                "binding_role": "identity" if index == 0 else "attribute",
                "authority_hint": "unknown",
                "confidence": 0.1 if index == 48 else 0.8,
                "evidence_work_ids": [evidence_work_id],
            }
            for index, population_id in enumerate(population_ids[:49])
        ],
        "edges": [],
        "unbound_population_ids": [],
    }

    bounded, note = _fit_neighborhood_binding_budget(work, payload)
    assert note == {
        "policy": "precision-first-binding-budget-v1",
        "input_bindings": 49,
        "output_bindings": 48,
        "removed_bindings": 1,
        "retained_one_per_node": True,
    }
    assert population_ids[48] not in {
        binding["population_id"] for binding in bounded["bindings"]
    }
    normalized = _normalize_neighborhood_result(work, bounded)
    assert normalized["unbound_population_ids"] == sorted(
        [population_ids[48], population_ids[49]]
    )
    validation = validate_excavation_result(
        {
            "schema_version": EXCAVATION_PLAN_SCHEMA_VERSION,
            "work_items": [work],
        },
        normalized,
    )
    assert validation["bindings"] == 48


def test_wide_synthesis_frontier_is_balanced_bounded_and_alias_only():
    population_ids = [
        f"postgres_relation:wide#field:field_{index:03d}" for index in range(80)
    ]
    motif_work_id = "work:wide-motif"
    work = {
        "work_id": "work:wide-synthesis",
        "work_kind": "neighborhood_synthesis",
        "scope_id": "excavation:wide",
        "source_keys": ["source:wide"],
        "population_ids": population_ids,
        "depends_on": [motif_work_id],
        "input_packet": {
            "output_contract": {
                "max_nodes": 16,
                "max_bindings": 48,
                "max_edges": 24,
            },
            "source_inputs": [
                {
                    "source_key": "source:wide",
                    "population_ids": population_ids,
                    "source_motif_work_ids": [motif_work_id],
                }
            ],
            "evidence_work_ids": [motif_work_id],
        },
    }
    prior_results = {
        motif_work_id: {
            "schema_version": SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
            "work_id": motif_work_id,
            "status": "proposed",
            "motifs": [
                {
                    "motif_key": f"motif-{offset}",
                    "confidence": 0.9 - offset / 100,
                    "field_names": [
                        f"field_{index:03d}" for index in range(offset, 80, 4)
                    ],
                }
                for offset in range(4)
            ],
        }
    }
    frontier = _synthesis_population_frontier(work, prior_results, maximum=48)
    assert len(frontier) == 48
    assert all(
        any(population_id.endswith(f"field_{index:03d}") for population_id in frontier)
        for index in range(4)
    )
    prompt = _neighborhood_prompt(work, prior_results, frontier=frontier)
    assert all(population_id not in prompt for population_id in population_ids)
    assert '"selected_populations":48' in prompt
    assert '"total_scoped_populations":80' in prompt
    assert prompt.count('"alias":"p') == 48


def test_wide_synthesis_frontier_reserves_room_beyond_dense_pair_evidence():
    population_ids = [
        f"postgres_relation:wide#field:field_{index:03d}" for index in range(80)
    ]
    motif_work_id = "work:wide-motif"
    correspondence_work_ids = [f"work:pair-{index:03d}" for index in range(30)]
    work = {
        "work_id": "work:wide-synthesis",
        "work_kind": "neighborhood_synthesis",
        "scope_id": "excavation:wide",
        "source_keys": ["source:wide"],
        "population_ids": population_ids,
        "depends_on": [motif_work_id, *correspondence_work_ids],
        "input_packet": {
            "output_contract": {"max_nodes": 16, "max_bindings": 48, "max_edges": 24},
            "source_inputs": [
                {
                    "source_key": "source:wide",
                    "population_ids": population_ids,
                    "source_motif_work_ids": [motif_work_id],
                }
            ],
            "evidence_work_ids": [motif_work_id, *correspondence_work_ids],
        },
    }
    prior_results = {
        motif_work_id: {
            "schema_version": SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
            "work_id": motif_work_id,
            "status": "proposed",
            "motifs": [
                {
                    "motif_key": "late-business-motif",
                    "confidence": 0.8,
                    "field_names": [f"field_{index:03d}" for index in range(60, 80)],
                }
            ],
        }
    }
    prior_results.update(
        {
            work_id: {
                "schema_version": CORRESPONDENCE_RESULT_SCHEMA_VERSION,
                "work_id": work_id,
                "status": "proposed",
                "confidence": 1.0 - index / 100,
                "population_ids": [population_ids[index], population_ids[index + 30]],
            }
            for index, work_id in enumerate(correspondence_work_ids)
        }
    )

    frontier = _synthesis_population_frontier(work, prior_results, maximum=48)
    assert len(frontier) == 48
    assert any(population_id.endswith("field_079") for population_id in frontier)
    assert (
        sum(
            population_id.endswith(f"field_{index:03d}")
            for index in range(60, 80)
            for population_id in frontier
        )
        >= 10
    )


def test_excavation_plan_shards_oversized_neighborhoods_without_losing_boundaries():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=1,
        max_pairs_per_unit=4,
        max_pairs_per_link=4,
        max_population_fanout=3,
    )
    assert len(plan["excavation_units"]) == 7
    assert all(unit["size"] == 1 for unit in plan["excavation_units"])
    assert plan["summary"]["sharded_neighborhoods"] == 3
    assert (
        sum(link["link_kind"] == "neighborhood_boundary" for link in plan["links"]) == 3
    )
    assert sum(link["link_kind"] == "cross_neighborhood" for link in plan["links"]) == 2
    assert plan["summary"]["work_by_kind"]["bridge_synthesis"] == 5


def test_excavation_plan_bounds_wide_units_by_population_not_only_source_count():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=24,
        maximum_populations_per_unit=8,
        max_pairs_per_unit=8,
        max_pairs_per_link=4,
        max_population_fanout=3,
    )
    assert plan["policy"]["maximum_populations_per_unit"] == 8
    assert any(unit["shard_count"] > 1 for unit in plan["excavation_units"])
    assert all(
        unit["population_count"] <= 8 or unit["size"] == 1
        for unit in plan["excavation_units"]
    )
    assert all(
        unit["oversized_source"] == (unit["size"] == 1 and unit["population_count"] > 8)
        for unit in plan["excavation_units"]
    )

    invalid = copy.deepcopy(plan)
    invalid["excavation_units"][0]["population_count"] += 1
    with pytest.raises(ContractError, match="invalid population_count"):
        validate_excavation_plan(corpus, neighborhoods, invalid)


def test_excavation_results_are_grounded_scoped_and_cannot_merge_units():
    corpus, neighborhoods, evidence = _synthetic_excavation_fixture()
    plan = build_excavation_plan(
        corpus,
        neighborhoods,
        evidence_by_pair=evidence,
        maximum_sources_per_unit=2,
        max_pairs_per_unit=16,
        max_pairs_per_link=6,
        max_population_fanout=4,
    )
    population_by_id = {item["population_id"]: item for item in corpus["populations"]}
    event_synthesis = next(
        item
        for item in plan["work_items"]
        if item["work_kind"] == "neighborhood_synthesis"
        and any(
            population_by_id[population_id]["packet"]["source"].get("relation")
            == "event_ledger"
            for population_id in item["population_ids"]
        )
    )
    event_fields = {
        population_by_id[population_id]["packet"]["field"]["name"]: population_id
        for population_id in event_synthesis["population_ids"]
    }
    motif_evidence = next(
        dependency
        for dependency in event_synthesis["depends_on"]
        if next(work for work in plan["work_items"] if work["work_id"] == dependency)[
            "work_kind"
        ]
        == "source_motifs"
    )

    nodes = [
        {
            "node_key": "activity",
            "node_kind": "event",
            "name": "Activity",
            "confidence": 0.91,
            "properties": {},
            "evidence_work_ids": [motif_evidence],
        },
        {
            "node_key": "party-reference",
            "node_kind": "object",
            "name": "Party reference",
            "confidence": 0.84,
            "properties": {},
            "evidence_work_ids": [motif_evidence],
        },
        {
            "node_key": "place-reference",
            "node_kind": "object",
            "name": "Place reference",
            "confidence": 0.82,
            "properties": {},
            "evidence_work_ids": [motif_evidence],
        },
    ]

    def binding(node_key, field, role, confidence=0.9):
        return {
            "node_key": node_key,
            "population_id": event_fields[field],
            "binding_role": role,
            "authority_hint": "unknown",
            "confidence": confidence,
            "evidence_work_ids": [motif_evidence],
        }

    result = {
        "schema_version": NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
        "work_id": event_synthesis["work_id"],
        "status": "proposed",
        "canonical_name": "Activity Operations",
        "nodes": nodes,
        "bindings": [
            binding("activity", "event_ref", "identity"),
            binding("activity", "actor_ref", "context", 0.72),
            binding("party-reference", "actor_ref", "identity"),
            binding("place-reference", "site_ref", "identity"),
            binding("activity", "activity_kind", "category"),
            binding("activity", "occurred_at", "time"),
            binding("activity", "score", "measure"),
            binding("activity", "narrative", "evidence"),
        ],
        "edges": [
            {
                "subject_node_key": "activity",
                "predicate": "about_party",
                "object_node_key": "party-reference",
                "confidence": 0.78,
                "evidence_work_ids": [motif_evidence],
            },
            {
                "subject_node_key": "activity",
                "predicate": "at_place",
                "object_node_key": "place-reference",
                "confidence": 0.76,
                "evidence_work_ids": [motif_evidence],
            },
        ],
        "unbound_population_ids": [],
    }
    summary = validate_excavation_result(plan, result)
    assert summary == {
        "valid": True,
        "work_id": event_synthesis["work_id"],
        "work_kind": "neighborhood_synthesis",
        "status": "proposed",
        "nodes": 3,
        "bindings": 8,
        "edges": 2,
        "unbound_populations": 0,
    }

    unnamed = copy.deepcopy(result)
    unnamed.pop("canonical_name")
    with pytest.raises(ContractError, match="canonical_name"):
        validate_excavation_result(plan, unnamed)

    legacy_plan = copy.deepcopy(plan)
    legacy_work = next(
        item
        for item in legacy_plan["work_items"]
        if item["work_id"] == event_synthesis["work_id"]
    )
    legacy_work["input_packet"]["output_contract"].pop("requires_canonical_name")
    assert validate_excavation_result(legacy_plan, unnamed)["nodes"] == 3

    escaped = copy.deepcopy(result)
    escaped["bindings"][0]["population_id"] = next(
        population_id
        for population_id in population_by_id
        if population_id not in event_synthesis["population_ids"]
    )
    with pytest.raises(ContractError, match="outside its unit"):
        validate_excavation_result(plan, escaped)

    executable = copy.deepcopy(result)
    executable["nodes"][0]["properties"]["sql"] = "select 1"
    with pytest.raises(ContractError, match="forbidden"):
        validate_excavation_result(plan, executable)

    bridge_work = next(
        item for item in plan["work_items"] if item["work_kind"] == "bridge_synthesis"
    )
    synthesis_ids = bridge_work["input_packet"]["neighborhood_synthesis_work_ids"]

    def prior_result(work_id, node_key):
        work = next(item for item in plan["work_items"] if item["work_id"] == work_id)
        population_id = work["population_ids"][0]
        evidence_work_id = work["depends_on"][0]
        return {
            "schema_version": NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
            "work_id": work_id,
            "status": "proposed",
            "canonical_name": node_key.replace("-", " ").title(),
            "nodes": [
                {
                    "node_key": node_key,
                    "node_kind": "object",
                    "name": node_key,
                    "confidence": 0.8,
                    "properties": {},
                    "evidence_work_ids": [evidence_work_id],
                }
            ],
            "bindings": [
                {
                    "node_key": node_key,
                    "population_id": population_id,
                    "binding_role": "attribute",
                    "confidence": 0.8,
                    "evidence_work_ids": [evidence_work_id],
                }
            ],
            "edges": [],
            "unbound_population_ids": [
                candidate
                for candidate in work["population_ids"]
                if candidate != population_id
            ],
        }

    prior_results = {
        synthesis_ids[0]: prior_result(synthesis_ids[0], "left-object"),
        synthesis_ids[1]: prior_result(synthesis_ids[1], "right-object"),
    }
    bridge_result = {
        "schema_version": BRIDGE_RESULT_SCHEMA_VERSION,
        "work_id": bridge_work["work_id"],
        "status": "proposed",
        "merge_excavation_units": False,
        "findings": [
            {
                "finding_key": "related-object-proposal",
                "outcome": "related_objects",
                "left_node_ref": {
                    "work_id": synthesis_ids[0],
                    "node_key": "left-object",
                },
                "right_node_ref": {
                    "work_id": synthesis_ids[1],
                    "node_key": "right-object",
                },
                "confidence": 0.74,
                "evidence_work_ids": synthesis_ids,
            }
        ],
    }
    assert (
        validate_excavation_result(
            plan,
            bridge_result,
            prior_results=prior_results,
        )["findings"]
        == 1
    )
    bridge_result["merge_excavation_units"] = True
    with pytest.raises(ContractError, match="cannot merge"):
        validate_excavation_result(plan, bridge_result, prior_results=prior_results)


def test_candidate_queue_is_bounded_diverse_and_unreviewed():
    corpus = make_synthetic_corpus()
    corpus["correspondences"] = []
    queued = build_candidate_queue(
        corpus,
        max_pairs=40,
        max_fanout=3,
        minimum_priority=0.0,
    )
    validate_corpus(queued)
    assert 0 < len(queued["correspondences"]) <= 40
    fanout: Counter[str] = Counter()
    for pair in queued["correspondences"]:
        fanout[pair["left_population_id"]] += 1
        fanout[pair["right_population_id"]] += 1
        assert "gold" not in pair
        assert pair["review"]["status"] == "unreviewed"
    assert max(fanout.values()) <= 3
    assert any(
        pair["review"]["stratum"] == "hard_negative_probe"
        for pair in queued["correspondences"]
    )
    with pytest.raises(ValueError, match="retain at least one"):
        build_candidate_queue(corpus, allowed_strata=[])


def test_external_evidence_cannot_bypass_cross_source_candidate_scope():
    corpus = make_synthetic_corpus()
    same_source = [
        item
        for item in corpus["populations"]
        if item["packet"].get("source", {}).get("relation") == "contacts"
        and item["packet"]["population"]["kind"] == "field"
    ][:2]
    left = same_source[0]["population_id"]
    right = same_source[1]["population_id"]
    queued = build_candidate_queue(
        {**corpus, "correspondences": []},
        max_pairs=40,
        max_fanout=4,
        evidence_by_pair={
            pair_key(left, right): {
                "shared_fingerprints": 128,
                "left_fingerprints": 128,
                "right_fingerprints": 128,
                "jaccard": 1.0,
                "containment": 1.0,
            }
        },
    )
    assert all(
        frozenset((item["left_population_id"], item["right_population_id"]))
        != frozenset((left, right))
        for item in queued["correspondences"]
    )


def test_embedding_adapter_is_budgeted_provider_neutral_and_cross_source():
    corpus = make_synthetic_corpus()
    inputs = make_embedding_inputs(corpus, max_text_chars=2_000)
    identity_inputs = make_embedding_inputs(
        corpus,
        max_text_chars=2_000,
        roles=["identity"],
        channels=("focus", "context"),
    )
    assert identity_inputs
    assert all("identity" in item["role_hints"] for item in identity_inputs)
    assert {item["channel"] for item in identity_inputs} == {"focus", "context"}
    assert len(identity_inputs) < 2 * len(inputs)
    batches = budget_embedding_batches(inputs, max_items=4, max_chars=5_000)
    assert sum(map(len, batches)) == len(inputs)
    assert all(len(batch) <= 4 for batch in batches)
    assert all(sum(len(item["text"]) for item in batch) <= 5_000 for batch in batches)
    assert all(len(item["text"]) <= 2_000 for item in inputs)

    # Numeric vectors are an adapter output. The core neither knows nor cares
    # which model/provider produced them.
    concept_axes: dict[str, int] = {}
    rows = []
    for item in corpus["populations"]:
        if item["packet"]["population"]["kind"] == "record_context":
            continue
        concept = item["gold"]["concept"]
        axis = concept_axes.setdefault(concept, len(concept_axes))
        vector = [0.0] * 8
        vector[axis % len(vector)] = 1.0
        rows.extend(
            [
                {
                    "population_id": item["population_id"],
                    "channel": channel,
                    "embedding": vector,
                }
                for channel in ("focus", "context")
            ]
        )
    evidence = build_embedding_evidence(
        corpus,
        rows,
        top_k=3,
        minimum_similarity=0.99,
    )
    assert evidence
    sources = {
        item["population_id"]: item["packet"]["source"]
        for item in corpus["populations"]
    }
    assert all(
        sources[item["left_population_id"]] != sources[item["right_population_id"]]
        for item in evidence
    )
    assert all(
        item["local_evidence"]["embedding_similarity"] == 1.0 for item in evidence
    )


def test_group_split_never_leaks_one_family_across_train_and_test():
    groups = ["party", "party", "place", "place", "offering", "activity"]
    first = grouped_split(groups, test_fraction=0.4, seed="fixed")
    second = grouped_split(reversed(groups), test_fraction=0.4, seed="fixed")
    assert first == second
    assert set(first.values()) == {"train", "test"}
    assert len(first) == len(set(groups))


def test_private_label_overlay_only_changes_gold_labels():
    corpus = make_synthetic_corpus()
    unlabeled = copy.deepcopy(corpus)
    for family in ("populations", "motifs", "correspondences"):
        for item in unlabeled[family]:
            item.pop("gold", None)
    template = make_label_template(unlabeled)
    population_id = unlabeled["populations"][0]["population_id"]
    template["population_labels"][0]["split_group"] = "family:private-concept"
    template["population_labels"][0]["gold"] = {
        "roles": ["identity"],
        "concept": "private_concept_label",
        "facet": "private_facet_label",
        "reviewed": True,
    }
    before_packet = copy.deepcopy(unlabeled["populations"][0]["packet"])
    labeled, applied = apply_label_overlay(unlabeled, template)

    assert applied == {"populations": 1, "motifs": 0, "correspondences": 0}
    assert labeled["populations"][0]["packet"] == before_packet
    assert labeled["populations"][0]["population_id"] == population_id
    assert labeled["populations"][0]["split_group"] == "family:private-concept"
    assert labeled["populations"][0]["gold"]["concept"] == "private_concept_label"


def test_private_controls_can_measure_candidate_pool_recall_outside_sampled_queue():
    corpus = make_synthetic_corpus()
    existing = {
        frozenset((item["left_population_id"], item["right_population_id"]))
        for item in corpus["correspondences"]
    }
    left = right = None
    for left_item in corpus["populations"]:
        for right_item in corpus["populations"]:
            candidate = frozenset(
                (left_item["population_id"], right_item["population_id"])
            )
            if len(candidate) == 2 and candidate not in existing:
                left = left_item["population_id"]
                right = right_item["population_id"]
                break
        if left is not None:
            break
    assert left is not None and right is not None

    template = make_label_template(corpus)
    template["correspondence_controls"] = [
        {
            "left_population_id": left,
            "right_population_id": right,
            "split_group": "control:opaque-family",
            "gold": {
                "verdicts": ["same_concept"],
                "reviewed": True,
                "rationale": "private reviewed anchor",
            },
        }
    ]
    controlled, applied = apply_label_overlay(corpus, template)
    assert applied["correspondences"] == 1
    control = next(
        item
        for item in controlled["correspondences"]
        if {item["left_population_id"], item["right_population_id"]} == {left, right}
    )
    assert control["split_group"] == "control:opaque-family"
    assert control["packet"]["privacy"]["raw_values"] is False

    report = evaluate_candidate_recall(
        controlled,
        {pair_key(left, right): {"embedding_similarity": 0.8}},
    )
    assert report["positive_controls"] >= 1
    assert report["by_verdict"]["same_concept"]["recalled"] >= 1


def test_deterministic_floor_is_precision_first_on_synthetic_holdout_contract():
    corpus = make_synthetic_corpus()
    report = evaluate_corpus(corpus)

    assert report["population_roles"]["micro_f1"] == 1.0
    assert report["correspondences"]["micro_precision"] >= 0.85
    assert report["correspondences"]["micro_recall"] >= 0.75
    assert report["correspondences"]["coverage"] < 1.0
    assert report["correspondences"]["hard_negative_false_edge_rate"] == 0.0

    ambiguous = next(
        item
        for item in corpus["correspondences"]
        if item["pair_id"] == "synthetic:ambiguous-status-category"
    )
    prediction = predict_correspondence(
        correspondence_packet_for_item(corpus, ambiguous)
    )
    assert prediction["verdicts"] == ["abstain"]


def test_portable_linear_floor_has_group_receipts_and_no_concept_features():
    checkpoint, report = train_corpus_baseline(
        make_synthetic_corpus(),
        task="correspondence",
        test_fraction=0.25,
        seed="portable-test",
    )
    restored = PortableOVR.from_dict(checkpoint.to_dict())
    train_groups = set(checkpoint.receipt["train_groups"])
    test_groups = set(checkpoint.receipt["test_groups"])

    assert train_groups
    assert test_groups
    assert train_groups.isdisjoint(test_groups)
    assert restored.feature_names == checkpoint.feature_names
    assert restored.labels == checkpoint.labels
    assert report["metrics"]["rows"] == checkpoint.receipt["test_rows"]
    assert not any(
        concept in feature
        for feature in checkpoint.feature_names
        for concept in ("party", "place", "offering", "activity")
    )


def test_postgres_shadow_extraction_rolls_back_and_matches_queue_packet_shapes(rvbbit):
    suffix = uuid.uuid4().hex[:10]
    table_name = f"bt_shadow_{suffix}"
    relation = f"public.{table_name}"
    source_id = None
    try:
        rvbbit.execute(
            sql.SQL(
                """
                CREATE TABLE {} (
                    member_key uuid,
                    email_address text,
                    member_state text,
                    joined_at timestamptz,
                    amount numeric
                )
                """
            ).format(sql.Identifier(table_name))
        )
        rvbbit.execute(
            sql.SQL(
                """
                INSERT INTO {} VALUES
                  ('00000000-0000-4000-8000-000000000001','one@example.test','active',now(),10.25),
                  ('00000000-0000-4000-8000-000000000002','two@example.test','paused',now(),20.50),
                  ('00000000-0000-4000-8000-000000000003','three@example.test','active',now(),30.75)
                """
            ).format(sql.Identifier(table_name))
        )
        rvbbit.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table_name)))

        with psycopg.connect(RVBBIT_DSN) as conn:
            shadow, audit = extract_postgres_shadow_corpus(
                conn,
                [relation],
                corpus_id=f"pytest-shadow-{suffix}",
                sample_rows=64,
            )
        assert audit["ledger_unchanged"] is True
        assert shadow["provenance"]["transaction_rolled_back"] is True
        assert len(shadow["populations"]) == 6  # five fields plus context
        assert len(shadow["motifs"]) == 1
        assert (
            rvbbit.execute(
                """
            SELECT count(*)
            FROM rvbbit.business_topology_sources
            WHERE schema_name='public' AND relation_name=%s
            """,
                (table_name,),
            ).fetchone()[0]
            == 0
        )

        shadow_packets = {
            item["population_id"]: item["packet"] for item in shadow["populations"]
        }
        excavated = rvbbit.execute(
            "SELECT rvbbit.business_topology_excavate_relation(%s::regclass,64,true,false)",
            (relation,),
        ).fetchone()[0]
        source_id = excavated["source_id"]
        queued_packets = rvbbit.execute(
            """
            SELECT j.task_kind,j.input_packet
            FROM rvbbit.business_topology_inference_jobs j
            JOIN rvbbit.business_topology_populations p
              ON p.population_id=j.population_id
            WHERE p.source_id=%s
            ORDER BY j.task_kind,j.input_hash
            """,
            (source_id,),
        ).fetchall()
        queued_populations = {
            packet["population"]["population_key"]: packet
            for task_kind, packet in queued_packets
            if task_kind == "population_embedding"
        }
        queued_motif = next(
            packet
            for task_kind, packet in queued_packets
            if task_kind == "source_motifs"
        )
        assert queued_populations == shadow_packets
        assert queued_motif == shadow["motifs"][0]["packet"]
    finally:
        if source_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_sources WHERE source_id=%s",
                (source_id,),
            )
        rvbbit.execute(
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                sql.Identifier(table_name)
            )
        )


def test_postgres_embedding_adapter_rolls_cache_writes_back(rvbbit):
    specialist = f"bt_stub_embed_{uuid.uuid4().hex[:10]}"
    rvbbit.execute(
        """
        SELECT rvbbit.register_backend(
          backend_name => %s,
          backend_endpoint => 'stub://32',
          backend_transport => 'stub'
        )
        """,
        (specialist,),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    try:
        corpus = make_synthetic_corpus()
        inputs = make_embedding_inputs(corpus, max_text_chars=2_000)[:5]
        with psycopg.connect(RVBBIT_DSN) as conn:
            vectors, audit = embed_postgres_shadow(
                conn,
                inputs,
                specialist=specialist,
                max_batch_items=3,
                max_batch_chars=4_000,
            )
        assert len(vectors) == len(inputs)
        assert audit["failures"] == []
        assert audit["transaction_rolled_back"] is True
        assert all(len(item["embedding"]) == 32 for item in vectors)
        assert (
            rvbbit.execute(
                "SELECT count(*) FROM rvbbit.embedding_cache WHERE specialist=%s",
                (specialist,),
            ).fetchone()[0]
            == 0
        )
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name=%s", (specialist,))
        rvbbit.execute("SELECT rvbbit.embedding_purge(%s)", (specialist,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_postgres_overlap_adapter_emits_aggregates_and_rolls_back(rvbbit):
    suffix = uuid.uuid4().hex[:10]
    left_table = f"bt_overlap_left_{suffix}"
    right_table = f"bt_overlap_right_{suffix}"
    distractor_table = f"bt_overlap_distractor_{suffix}"
    try:
        for table_name in (left_table, right_table, distractor_table):
            rvbbit.execute(
                sql.SQL("CREATE TABLE {} (member_ref text, state text)").format(
                    sql.Identifier(table_name)
                )
            )
        for table_name in (left_table, right_table, distractor_table):
            rvbbit.execute(
                sql.SQL(
                    """
                    INSERT INTO {} VALUES
                      ('member-001','active'),('member-002','active'),
                      ('member-003','paused'),('member-004','active')
                    """
                ).format(sql.Identifier(table_name))
            )
            rvbbit.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table_name)))

        relations = [
            f"public.{left_table}",
            f"public.{right_table}",
            f"public.{distractor_table}",
        ]
        with psycopg.connect(RVBBIT_DSN) as conn:
            corpus, _ = extract_postgres_shadow_corpus(
                conn,
                relations,
                corpus_id=f"pytest-overlap-{suffix}",
                sample_rows=64,
            )
        member_ids = {
            item["packet"]["source"]["relation"]: item["population_id"]
            for item in corpus["populations"]
            if item["packet"].get("field", {}).get("name") == "member_ref"
        }
        expected = {member_ids[left_table], member_ids[right_table]}
        with psycopg.connect(RVBBIT_DSN) as conn:
            evidence, audit = extract_postgres_overlap_shadow(
                conn,
                corpus,
                probe_pairs=[(member_ids[left_table], member_ids[right_table])],
                sample_rows=64,
                min_shared=2,
                max_fingerprint_fanout=2,
            )

        match = next(
            item
            for item in evidence
            if {item["left_population_id"], item["right_population_id"]} == expected
        )
        assert match["local_evidence"]["shared_fingerprints"] == 4
        assert match["local_evidence"]["containment"] == 1.0
        assert audit["transaction_rolled_back"] is True
        assert audit["ledger_counts_unchanged"] is True
        assert audit["targeted_probe_pairs"] == 1
        assert audit["targeted_overlap_pairs"] == 1
        assert audit["global_candidate_pairs"] == 0
        assert (
            rvbbit.execute(
                """
                SELECT count(*) FROM rvbbit.business_topology_sources
                WHERE schema_name='public' AND relation_name=ANY(%s::text[])
                """,
                ([left_table, right_table, distractor_table],),
            ).fetchone()[0]
            == 0
        )
    finally:
        for table_name in (left_table, right_table, distractor_table):
            rvbbit.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(table_name)
                )
            )
