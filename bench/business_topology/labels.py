"""Private label overlays for reusable topology corpora.

Extracted packets and customer review decisions can live in a protected
workspace while the evaluator and synthetic regressions remain public.  An
overlay never changes packet features; it only attaches reviewed gold labels
and leakage-safe family groups by opaque item identifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .candidates import pair_id
from .contracts import ContractError, validate_corpus
from .packets import make_correspondence_packet


LABEL_OVERLAY_SCHEMA_VERSION = "rvbbit.business-topology.label-overlay.v1"


def make_label_template(corpus: Mapping[str, Any]) -> dict[str, Any]:
    validate_corpus(corpus)
    return {
        "schema_version": LABEL_OVERLAY_SCHEMA_VERSION,
        "corpus_id": corpus["corpus_id"],
        "population_labels": [
            {
                "population_id": item["population_id"],
                "split_group": item["split_group"],
                "gold": None,
            }
            for item in corpus.get("populations", [])
        ],
        "motif_labels": [
            {
                "motif_id": item["motif_id"],
                "split_group": item["split_group"],
                "gold": None,
            }
            for item in corpus.get("motifs", [])
        ],
        "correspondence_labels": [
            {
                "pair_id": item["pair_id"],
                "split_group": item["split_group"],
                "gold": None,
            }
            for item in corpus.get("correspondences", [])
        ],
        "correspondence_controls": [],
        "instructions": {
            "split_group": (
                "assign one opaque family id to semantically related examples across systems; "
                "the evaluator keeps the whole family in one split"
            ),
            "population_gold": {
                "roles": "zero or more structural roles",
                "concept": "customer-specific business concept or null",
                "facet": "customer-specific facet or null",
                "reviewed": True,
            },
            "correspondence_gold": {
                "verdicts": "one or more contract verdicts; abstain stands alone",
                "reviewed": True,
                "rationale": "short evidence-based explanation",
            },
            "correspondence_controls": (
                "optional reviewed anchor pairs with left_population_id, "
                "right_population_id, split_group, and gold; controls need not "
                "have appeared in the sampled review queue"
            ),
            "motif_gold": {
                "populations": "declarative kind/concept/columns entries",
                "abstain": False,
                "reviewed": True,
            },
        },
    }


def _apply_family(
    items: list[dict[str, Any]],
    labels: Any,
    *,
    id_key: str,
    family_name: str,
) -> int:
    if labels is None:
        return 0
    if not isinstance(labels, list):
        raise ContractError(f"label overlay {family_name} must be an array")
    index = {str(item[id_key]): item for item in items}
    applied = 0
    seen: set[str] = set()
    for label_item in labels:
        if not isinstance(label_item, Mapping):
            raise ContractError(f"label overlay {family_name} entries must be objects")
        item_id = label_item.get(id_key)
        if not isinstance(item_id, str) or not item_id:
            raise ContractError(f"label overlay {family_name} entry requires {id_key}")
        if item_id in seen:
            raise ContractError(f"duplicate overlay label for {item_id}")
        seen.add(item_id)
        if item_id not in index:
            raise ContractError(f"label overlay references unknown {family_name} id: {item_id}")
        changed = False
        if "split_group" in label_item:
            split_group = label_item.get("split_group")
            if not isinstance(split_group, str) or not split_group.strip():
                raise ContractError(
                    f"label overlay split_group for {item_id} must be a non-empty string"
                )
            if split_group != index[item_id]["split_group"]:
                index[item_id]["split_group"] = split_group
                changed = True

        gold = label_item.get("gold")
        if gold is not None:
            if not isinstance(gold, Mapping):
                raise ContractError(f"label overlay gold for {item_id} must be an object or null")
            index[item_id]["gold"] = deepcopy(dict(gold))
            changed = True
        if changed:
            applied += 1
    return applied


def _apply_correspondence_controls(
    result: dict[str, Any],
    controls: Any,
) -> int:
    if controls is None:
        return 0
    if not isinstance(controls, list):
        raise ContractError("label overlay correspondence_controls must be an array")
    populations = {str(item["population_id"]): item for item in result.get("populations", [])}
    correspondences = result.setdefault("correspondences", [])
    by_pair = {
        frozenset((str(item["left_population_id"]), str(item["right_population_id"]))): item
        for item in correspondences
    }
    applied = 0
    seen: set[frozenset[str]] = set()
    for index, control in enumerate(controls):
        if not isinstance(control, Mapping):
            raise ContractError(f"correspondence control {index} must be an object")
        left = control.get("left_population_id")
        right = control.get("right_population_id")
        split_group = control.get("split_group")
        gold = control.get("gold")
        if not isinstance(left, str) or left not in populations:
            raise ContractError(f"correspondence control {index} has an unknown left population")
        if not isinstance(right, str) or right not in populations or right == left:
            raise ContractError(f"correspondence control {index} has an invalid right population")
        if not isinstance(split_group, str) or not split_group.strip():
            raise ContractError(f"correspondence control {index} requires split_group")
        if not isinstance(gold, Mapping):
            raise ContractError(f"correspondence control {index} requires a gold object")
        key = frozenset((left, right))
        if key in seen:
            raise ContractError(f"duplicate correspondence control at index {index}")
        seen.add(key)
        item = by_pair.get(key)
        if item is None:
            left_id, right_id = sorted((left, right))
            item = {
                "pair_id": pair_id(left_id, right_id),
                "split_group": split_group,
                "left_population_id": left_id,
                "right_population_id": right_id,
                "packet": make_correspondence_packet(
                    populations[left_id]["packet"],
                    populations[right_id]["packet"],
                ),
                "review": {"status": "reviewed_control"},
            }
            correspondences.append(item)
            by_pair[key] = item
        item["split_group"] = split_group
        item["gold"] = deepcopy(dict(gold))
        applied += 1
    return applied


def apply_label_overlay(
    corpus: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    validate_corpus(corpus)
    if overlay.get("schema_version") != LABEL_OVERLAY_SCHEMA_VERSION:
        raise ContractError(f"overlay.schema_version must be {LABEL_OVERLAY_SCHEMA_VERSION}")
    if overlay.get("corpus_id") != corpus.get("corpus_id"):
        raise ContractError("overlay.corpus_id does not match the corpus")

    result = deepcopy(dict(corpus))
    applied = {
        "populations": _apply_family(
            result.get("populations", []),
            overlay.get("population_labels"),
            id_key="population_id",
            family_name="population",
        ),
        "motifs": _apply_family(
            result.get("motifs", []),
            overlay.get("motif_labels"),
            id_key="motif_id",
            family_name="motif",
        ),
        "correspondences": _apply_family(
            result.get("correspondences", []),
            overlay.get("correspondence_labels"),
            id_key="pair_id",
            family_name="correspondence",
        ),
    }
    applied["correspondences"] += _apply_correspondence_controls(
        result,
        overlay.get("correspondence_controls"),
    )
    result.setdefault("provenance", {})["label_overlay"] = {
        "schema_version": LABEL_OVERLAY_SCHEMA_VERSION,
        "applied": applied,
    }
    validate_corpus(result)
    return result, applied
