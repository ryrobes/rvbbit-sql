"""Synthetic multi-system corpus for portable topology regression tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import CORPUS_SCHEMA_VERSION, validate_corpus
from .packets import expand_postgres_relation_profile


def _field(
    name: str,
    *,
    data_type: str = "text",
    type_family: str = "text",
    roles: list[str] | None = None,
    sensitivity: str = "unknown",
    shapes: dict[str, int] | None = None,
    cardinality: float = 0.5,
    comment: str | None = None,
    ordinal: int = 1,
) -> dict[str, Any]:
    return {
        "name": name,
        "ordinal": ordinal,
        "data_type": data_type,
        "type_family": type_family,
        "nullable": True,
        "comment": comment,
        "declared_pk": False,
        "declared_fk": False,
        "sample_rows": 128,
        "null_fraction": 0.02,
        "sample_distinct": max(round(128 * cardinality), 1),
        "cardinality_ratio": cardinality,
        "average_length": 18 if type_family == "text" else None,
        "maximum_length": 64 if type_family == "text" else None,
        "value_shapes": shapes or ({"token": 128} if type_family == "text" else {}),
        "frequency_profile": [{"n": 1}] * min(max(round(128 * cardinality), 1), 16),
        "sensitivity_hint": sensitivity,
        "role_hints": roles or [],
        "name_tokens": [
            token
            for token in name.lower().replace("-", "_").split("_")
            if token not in {"id", "key", "code"}
        ],
    }


def _profile(schema: str, relation: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_fields = []
    for ordinal, field in enumerate(fields, 1):
        item = deepcopy(field)
        item["ordinal"] = ordinal
        normalized_fields.append(item)
    return {
        "schema_version": "rvbbit.business-topology.profile-packet.v1",
        "privacy": {
            "raw_values": False,
            "value_hashes": False,
            "bounded_sample": True,
        },
        "source": {
            "kind": "postgres_relation",
            "database": "synthetic_company",
            "schema": schema,
            "relation": relation,
            "relation_kind": "table",
            "comment": "Synthetic source used to test reusable topology inference",
        },
        "sample": {
            "rows": 128,
            "limit": 128,
            "method": "synthetic",
            "row_estimate": 128,
            "row_estimate_known": True,
        },
        "relation_context": {
            "name": relation,
            "name_tokens": [token for token in relation.split("_") if token],
            "field_count": len(normalized_fields),
            "fields": normalized_fields,
            "pair_field_limit": 48,
            "pair_fields_profiled": len(normalized_fields),
            "field_pairs": [],
        },
    }


def make_synthetic_corpus() -> dict[str, Any]:
    """Create a client-neutral corpus with mirrors, events, and mixed tables."""

    profiles = [
        _profile(
            "crm",
            "contacts",
            [
                _field(
                    "person_ref",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=1.0,
                ),
                _field(
                    "email",
                    roles=["identity"],
                    sensitivity="direct_identifier",
                    shapes={"email": 128},
                    cardinality=0.98,
                ),
                _field("lifecycle_status", roles=["status"], cardinality=0.04),
                _field(
                    "created_at",
                    data_type="timestamptz",
                    type_family="time",
                    roles=["time"],
                    shapes={"date_or_timestamp": 128},
                    cardinality=0.92,
                ),
                _field(
                    "notes",
                    roles=["evidence"],
                    shapes={"long_text": 96, "empty": 32},
                    cardinality=0.72,
                ),
            ],
        ),
        _profile(
            "operations",
            "members",
            [
                _field(
                    "member_key",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=1.0,
                ),
                _field(
                    "email_address",
                    roles=["identity"],
                    sensitivity="direct_identifier",
                    shapes={"email": 128},
                    cardinality=0.98,
                ),
                _field("member_state", roles=["status"], cardinality=0.05),
                _field(
                    "site_key",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=0.12,
                ),
                _field(
                    "joined_on",
                    data_type="date",
                    type_family="time",
                    roles=["time"],
                    shapes={"date_or_timestamp": 128},
                    cardinality=0.88,
                ),
            ],
        ),
        _profile(
            "crm",
            "offices",
            [
                _field(
                    "office_ref",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=1.0,
                ),
                _field("city", roles=["geography"], cardinality=0.16),
                _field("region", roles=["geography"], cardinality=0.05),
            ],
        ),
        _profile(
            "operations",
            "sites",
            [
                _field(
                    "site_key",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=1.0,
                ),
                _field("municipality", roles=["geography"], cardinality=0.16),
                _field("service_region", roles=["geography"], cardinality=0.05),
            ],
        ),
        _profile(
            "commerce",
            "products",
            [
                _field(
                    "product_ref",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"token": 128},
                    cardinality=1.0,
                ),
                _field(
                    "unit_price",
                    data_type="numeric",
                    type_family="number",
                    roles=["money"],
                    shapes={"decimal": 128},
                    cardinality=0.42,
                ),
                _field("product_category", roles=["category"], cardinality=0.08),
            ],
        ),
        _profile(
            "fulfillment",
            "catalog_items",
            [
                _field(
                    "item_key",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"token": 128},
                    cardinality=1.0,
                ),
                _field(
                    "price_amount",
                    data_type="numeric",
                    type_family="number",
                    roles=["money"],
                    shapes={"decimal": 128},
                    cardinality=0.42,
                ),
                _field("item_class", roles=["category"], cardinality=0.08),
            ],
        ),
        # One physical relation intentionally contains an event, two object
        # references, a measurement, time, and evidence.  It must never be
        # labeled as one monolithic "ledger entry" object.
        _profile(
            "activity",
            "event_ledger",
            [
                _field(
                    "event_ref",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=1.0,
                ),
                _field(
                    "actor_ref",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=0.94,
                ),
                _field(
                    "site_ref",
                    type_family="identifier",
                    roles=["identity"],
                    sensitivity="identifier",
                    shapes={"uuid": 128},
                    cardinality=0.18,
                ),
                _field("activity_kind", roles=["category"], cardinality=0.06),
                _field(
                    "occurred_at",
                    data_type="timestamptz",
                    type_family="time",
                    roles=["time"],
                    shapes={"date_or_timestamp": 128},
                    cardinality=1.0,
                ),
                _field(
                    "score",
                    data_type="numeric",
                    type_family="number",
                    roles=["measure"],
                    shapes={"decimal": 128},
                    cardinality=0.52,
                ),
                _field(
                    "narrative",
                    roles=["evidence"],
                    shapes={"long_text": 110, "empty": 18},
                    cardinality=0.82,
                ),
            ],
        ),
    ]

    populations: list[dict[str, Any]] = []
    motifs: list[dict[str, Any]] = []
    lookup: dict[tuple[str, str, str], str] = {}
    role_labels: dict[tuple[str, str, str], tuple[list[str], str, str]] = {}

    concept_by_relation_field = {
        ("crm", "contacts", "person_ref"): (["identity"], "party", "primary_identifier"),
        ("crm", "contacts", "email"): (["identity"], "party", "email"),
        ("crm", "contacts", "lifecycle_status"): (["status"], "party", "status"),
        ("crm", "contacts", "created_at"): (["time"], "party", "created_time"),
        ("crm", "contacts", "notes"): (["evidence"], "party", "notes"),
        ("operations", "members", "member_key"): (["identity"], "party", "primary_identifier"),
        ("operations", "members", "email_address"): (["identity"], "party", "email"),
        ("operations", "members", "member_state"): (["status"], "party", "status"),
        ("operations", "members", "site_key"): (["identity"], "place", "primary_identifier"),
        ("operations", "members", "joined_on"): (["time"], "party", "created_time"),
        ("crm", "offices", "office_ref"): (["identity"], "place", "primary_identifier"),
        ("crm", "offices", "city"): (["geography"], "place", "city"),
        ("crm", "offices", "region"): (["geography"], "place", "region"),
        ("operations", "sites", "site_key"): (["identity"], "place", "primary_identifier"),
        ("operations", "sites", "municipality"): (["geography"], "place", "city"),
        ("operations", "sites", "service_region"): (["geography"], "place", "region"),
        ("commerce", "products", "product_ref"): (["identity"], "offering", "primary_identifier"),
        ("commerce", "products", "unit_price"): (["money"], "offering", "price"),
        ("commerce", "products", "product_category"): (["category"], "offering", "category"),
        ("fulfillment", "catalog_items", "item_key"): (
            ["identity"],
            "offering",
            "primary_identifier",
        ),
        ("fulfillment", "catalog_items", "price_amount"): (["money"], "offering", "price"),
        ("fulfillment", "catalog_items", "item_class"): (["category"], "offering", "category"),
        ("activity", "event_ledger", "event_ref"): (["identity"], "activity", "primary_identifier"),
        ("activity", "event_ledger", "actor_ref"): (["identity"], "party", "primary_identifier"),
        ("activity", "event_ledger", "site_ref"): (["identity"], "place", "primary_identifier"),
        ("activity", "event_ledger", "activity_kind"): (["category"], "activity", "category"),
        ("activity", "event_ledger", "occurred_at"): (["time"], "activity", "event_time"),
        ("activity", "event_ledger", "score"): (["measure"], "activity", "score"),
        ("activity", "event_ledger", "narrative"): (["evidence"], "activity", "narrative"),
    }
    role_labels.update(concept_by_relation_field)

    for profile in profiles:
        schema = str(profile["source"]["schema"])
        relation = str(profile["source"]["relation"])
        expanded = expand_postgres_relation_profile(profile)
        for item in expanded["populations"]:
            field = item["packet"].get("field")
            if field:
                field_name = str(field["name"])
                roles, concept, facet = role_labels[(schema, relation, field_name)]
                item["split_group"] = concept
                item["gold"] = {
                    "roles": roles,
                    "concept": concept,
                    "facet": facet,
                    "reviewed": True,
                }
                lookup[(schema, relation, field_name)] = item["population_id"]
            else:
                item["split_group"] = f"motif:{schema}.{relation}"
                item["gold"] = {"roles": [], "reviewed": True}
            populations.append(item)
        motif = expanded["motifs"][0]
        motif["split_group"] = f"motif:{schema}.{relation}"
        if relation == "event_ledger":
            motif["gold"] = {
                "populations": [
                    {
                        "kind": "event_stream",
                        "concept": "activity",
                        "columns": [
                            "event_ref",
                            "actor_ref",
                            "site_ref",
                            "activity_kind",
                            "occurred_at",
                            "score",
                            "narrative",
                        ],
                    },
                    {
                        "kind": "composite",
                        "concept": "party_reference",
                        "columns": ["actor_ref"],
                    },
                    {
                        "kind": "composite",
                        "concept": "place_reference",
                        "columns": ["site_ref"],
                    },
                ],
                "abstain": False,
                "reviewed": True,
            }
        elif relation in {"contacts", "members"}:
            identity_columns = (
                ["person_ref", "email"]
                if relation == "contacts"
                else ["member_key", "email_address"]
            )
            motif["gold"] = {
                "populations": [
                    {
                        "kind": "composite",
                        "concept": "party_identity",
                        "columns": identity_columns,
                    }
                ],
                "abstain": False,
                "reviewed": True,
            }
        else:
            motif["gold"] = {"populations": [], "abstain": True, "reviewed": True}
        motifs.append(motif)

    def ref(schema: str, relation: str, field: str) -> str:
        return lookup[(schema, relation, field)]

    correspondences: list[dict[str, Any]] = []

    def pair(
        name: str,
        group: str,
        left: tuple[str, str, str],
        right: tuple[str, str, str],
        verdicts: list[str],
        *,
        shared: int = 0,
        left_count: int = 0,
        right_count: int = 0,
        embedding: float = 0.0,
        query_joins: int = 0,
    ) -> None:
        union = left_count + right_count - shared
        containment = max(
            shared / left_count if left_count else 0.0,
            shared / right_count if right_count else 0.0,
        )
        correspondences.append(
            {
                "pair_id": f"synthetic:{name}",
                "split_group": group,
                "left_population_id": ref(*left),
                "right_population_id": ref(*right),
                "local_evidence": {
                    "shared_fingerprints": shared,
                    "left_fingerprints": left_count,
                    "right_fingerprints": right_count,
                    "jaccard": shared / union if union else 0.0,
                    "containment": containment,
                    "embedding_similarity": embedding,
                    "query_join_count": query_joins,
                },
                "gold": {
                    "verdicts": verdicts,
                    "reviewed": True,
                    "rationale": "Synthetic contract fixture",
                },
            }
        )

    pair(
        "party-key",
        "party",
        ("crm", "contacts", "person_ref"),
        ("operations", "members", "member_key"),
        ["same_concept", "same_facet", "same_instance_key", "joinable"],
        shared=122,
        left_count=128,
        right_count=126,
        embedding=0.76,
        query_joins=8,
    )
    pair(
        "party-email",
        "party",
        ("crm", "contacts", "email"),
        ("operations", "members", "email_address"),
        ["same_concept", "same_facet", "same_instance_key", "joinable"],
        shared=120,
        left_count=125,
        right_count=124,
        embedding=0.91,
        query_joins=5,
    )
    pair(
        "party-status",
        "party",
        ("crm", "contacts", "lifecycle_status"),
        ("operations", "members", "member_state"),
        ["same_concept", "same_facet"],
        shared=4,
        left_count=5,
        right_count=6,
        embedding=0.83,
    )
    pair(
        "party-time",
        "party",
        ("crm", "contacts", "created_at"),
        ("operations", "members", "joined_on"),
        ["same_concept", "same_facet"],
        shared=0,
        left_count=64,
        right_count=64,
        embedding=0.84,
    )
    pair(
        "place-key",
        "place",
        ("crm", "offices", "office_ref"),
        ("operations", "sites", "site_key"),
        ["same_concept", "same_facet", "same_instance_key", "joinable"],
        shared=117,
        left_count=120,
        right_count=119,
        embedding=0.78,
        query_joins=7,
    )
    pair(
        "place-city",
        "place",
        ("crm", "offices", "city"),
        ("operations", "sites", "municipality"),
        ["same_concept", "same_facet"],
        shared=17,
        left_count=20,
        right_count=22,
        embedding=0.87,
    )
    pair(
        "place-region",
        "place",
        ("crm", "offices", "region"),
        ("operations", "sites", "service_region"),
        ["same_concept", "same_facet"],
        shared=5,
        left_count=6,
        right_count=6,
        embedding=0.89,
    )
    pair(
        "offering-key",
        "offering",
        ("commerce", "products", "product_ref"),
        ("fulfillment", "catalog_items", "item_key"),
        ["same_concept", "same_facet", "same_instance_key", "joinable"],
        shared=121,
        left_count=128,
        right_count=124,
        embedding=0.77,
        query_joins=6,
    )
    pair(
        "offering-price",
        "offering",
        ("commerce", "products", "unit_price"),
        ("fulfillment", "catalog_items", "price_amount"),
        ["same_concept", "same_facet"],
        shared=0,
        left_count=50,
        right_count=50,
        embedding=0.91,
    )
    pair(
        "offering-category",
        "offering",
        ("commerce", "products", "product_category"),
        ("fulfillment", "catalog_items", "item_class"),
        ["same_concept", "same_facet"],
        shared=8,
        left_count=10,
        right_count=11,
        embedding=0.86,
    )
    pair(
        "event-actor",
        "activity",
        ("activity", "event_ledger", "actor_ref"),
        ("operations", "members", "member_key"),
        ["same_concept", "same_facet", "same_instance_key", "joinable"],
        shared=112,
        left_count=118,
        right_count=126,
        embedding=0.72,
        query_joins=9,
    )
    pair(
        "event-place",
        "activity",
        ("activity", "event_ledger", "site_ref"),
        ("operations", "sites", "site_key"),
        ["same_concept", "same_facet", "same_instance_key", "joinable"],
        shared=20,
        left_count=22,
        right_count=119,
        embedding=0.71,
        query_joins=9,
    )
    pair(
        "negative-identity-shape",
        "hard-negative-identity",
        ("operations", "members", "member_key"),
        ("operations", "sites", "site_key"),
        ["unrelated"],
        shared=0,
        left_count=126,
        right_count=119,
        embedding=0.18,
    )
    pair(
        "negative-number-shape",
        "hard-negative-number",
        ("commerce", "products", "unit_price"),
        ("activity", "event_ledger", "score"),
        ["unrelated"],
        shared=0,
        left_count=50,
        right_count=62,
        embedding=0.12,
    )
    pair(
        "negative-text-shape",
        "hard-negative-text",
        ("crm", "contacts", "email"),
        ("activity", "event_ledger", "narrative"),
        ["unrelated"],
        shared=0,
        left_count=125,
        right_count=100,
        embedding=0.05,
    )
    pair(
        "ambiguous-status-category",
        "ambiguous",
        ("operations", "members", "member_state"),
        ("activity", "event_ledger", "activity_kind"),
        ["abstain"],
        shared=0,
        left_count=6,
        right_count=8,
        embedding=0.41,
    )

    corpus = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_id": "rvbbit-synthetic-multisystem-v1",
        "description": "Client-neutral mirrors, events, multi-object relations, and hard negatives",
        "populations": populations,
        "motifs": motifs,
        "correspondences": correspondences,
        "provenance": {
            "kind": "synthetic",
            "contains_customer_data": False,
            "raw_values": False,
        },
    }
    validate_corpus(corpus, require_reviewed=True)
    return corpus
