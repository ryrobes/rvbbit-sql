"""Stable contracts for topology evaluation corpora and model packets.

Nothing in this module assumes PostgreSQL, a particular source vendor, or a
particular business vocabulary. Source adapters emit the packet versions below; the
evaluator works with opaque population identifiers and human-provided labels.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


CORPUS_SCHEMA_VERSION = "rvbbit.business-topology.eval-corpus.v1"

PACKET_SCHEMA_VERSIONS = frozenset(
    {
        "rvbbit.business-topology.profile-packet.v1",
        "rvbbit.business-topology.population.v1",
        "rvbbit.business-topology.source-motifs.v1",
        "rvbbit.business-topology.correspondence.v1",
        "rvbbit.business-topology.neighborhood-synthesis.v1",
        "rvbbit.business-topology.bridge-synthesis.v1",
    }
)

POPULATION_KINDS = frozenset(
    {
        "field",
        "record_context",
        "composite",
        "slice",
        "event_stream",
        "mention_set",
        "query_projection",
    }
)

# These are reusable structural roles.  Customer-specific concepts such as
# "Customer" or "Claim" belong in corpus labels/proposals, not this taxonomy.
POPULATION_ROLES = (
    "identity",
    "status",
    "category",
    "time",
    "money",
    "measure",
    "geography",
    "evidence",
)

CORRESPONDENCE_VERDICTS = (
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
    "unrelated",
    "abstain",
)

# A model-bound packet may contain distribution counts and shape summaries,
# but never the values or installation-local fingerprints that produced them.
_FORBIDDEN_VALUE_KEYS = frozenset(
    {
        "value_dist",
        "top_values",
        "values",
        "raw_sample",
        "raw_samples",
        "sample_values",
        "fingerprint",
        "fingerprints",
        "value_fingerprints",
        "value_fingerprint_signature",
    }
)


class ContractError(ValueError):
    """Raised when a packet or evaluation corpus violates its public contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _require_object(value: Any, path: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{path} must be an object")
    return value


def _require_string(value: Any, path: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{path} must be a non-empty string")
    return value


def _walk_forbidden_values(value: Any, path: str = "packet") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in _FORBIDDEN_VALUE_KEYS:
                raise ContractError(f"{child_path} is forbidden in a model-bound packet")
            if key_text in {"raw_values", "value_hashes"} and child is not False:
                raise ContractError(f"{child_path} must be false")
            _walk_forbidden_values(child, child_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _walk_forbidden_values(child, f"{path}[{index}]")


def validate_outbound_packet(packet: Mapping[str, Any]) -> None:
    """Validate packet version and the no-values/no-hashes privacy boundary."""

    packet = _require_object(packet, "packet")
    version = _require_string(packet.get("schema_version"), "packet.schema_version")
    _require(version in PACKET_SCHEMA_VERSIONS, f"unsupported packet schema_version: {version}")

    privacy = _require_object(packet.get("privacy"), "packet.privacy")
    _require(privacy.get("raw_values") is False, "packet.privacy.raw_values must be false")
    if "value_hashes" in privacy:
        _require(privacy.get("value_hashes") is False, "packet.privacy.value_hashes must be false")
    _walk_forbidden_values(packet)


def _validate_gold_roles(gold: Mapping[str, Any], path: str) -> None:
    roles = gold.get("roles", [])
    _require(isinstance(roles, list), f"{path}.roles must be an array")
    unknown = sorted(set(roles) - set(POPULATION_ROLES))
    _require(not unknown, f"{path}.roles contains unsupported roles: {', '.join(unknown)}")
    for optional_name in ("concept", "facet"):
        if optional_name in gold and gold[optional_name] is not None:
            _require_string(gold[optional_name], f"{path}.{optional_name}")


def validate_corpus(corpus: Mapping[str, Any], *, require_reviewed: bool = False) -> None:
    """Validate a reusable evaluation corpus.

    Labels may be absent while a review queue is being assembled.  Set
    ``require_reviewed`` before model evaluation or training.
    """

    corpus = _require_object(corpus, "corpus")
    _require(
        corpus.get("schema_version") == CORPUS_SCHEMA_VERSION,
        f"corpus.schema_version must be {CORPUS_SCHEMA_VERSION}",
    )
    _require_string(corpus.get("corpus_id"), "corpus.corpus_id")

    populations = corpus.get("populations", [])
    _require(isinstance(populations, list), "corpus.populations must be an array")
    population_ids: set[str] = set()
    for index, item in enumerate(populations):
        path = f"corpus.populations[{index}]"
        item = _require_object(item, path)
        population_id = _require_string(item.get("population_id"), f"{path}.population_id")
        _require(population_id not in population_ids, f"duplicate population_id: {population_id}")
        population_ids.add(population_id)
        _require_string(item.get("split_group"), f"{path}.split_group")
        packet = _require_object(item.get("packet"), f"{path}.packet")
        validate_outbound_packet(packet)
        population = _require_object(packet.get("population"), f"{path}.packet.population")
        kind = _require_string(population.get("kind"), f"{path}.packet.population.kind")
        _require(kind in POPULATION_KINDS, f"{path} has unsupported population kind: {kind}")
        gold = item.get("gold")
        if gold is not None:
            gold = _require_object(gold, f"{path}.gold")
            _validate_gold_roles(gold, f"{path}.gold")
        elif require_reviewed:
            raise ContractError(f"{path}.gold is required for reviewed evaluation")

    motifs = corpus.get("motifs", [])
    _require(isinstance(motifs, list), "corpus.motifs must be an array")
    motif_ids: set[str] = set()
    for index, item in enumerate(motifs):
        path = f"corpus.motifs[{index}]"
        item = _require_object(item, path)
        motif_id = _require_string(item.get("motif_id"), f"{path}.motif_id")
        _require(motif_id not in motif_ids, f"duplicate motif_id: {motif_id}")
        motif_ids.add(motif_id)
        _require_string(item.get("split_group"), f"{path}.split_group")
        validate_outbound_packet(_require_object(item.get("packet"), f"{path}.packet"))
        gold = item.get("gold")
        if gold is None:
            _require(not require_reviewed, f"{path}.gold is required for reviewed evaluation")
            continue
        gold = _require_object(gold, f"{path}.gold")
        expected = gold.get("populations", [])
        _require(isinstance(expected, list), f"{path}.gold.populations must be an array")
        for expected_index, expected_population in enumerate(expected):
            expected_path = f"{path}.gold.populations[{expected_index}]"
            expected_population = _require_object(expected_population, expected_path)
            kind = _require_string(expected_population.get("kind"), f"{expected_path}.kind")
            _require(
                kind in POPULATION_KINDS - {"field", "record_context"},
                f"{expected_path}.kind is not a discoverable motif",
            )
            columns = expected_population.get("columns", [])
            _require(
                isinstance(columns, list) and bool(columns),
                f"{expected_path}.columns must be a non-empty array",
            )
            for column_index, column in enumerate(columns):
                _require_string(column, f"{expected_path}.columns[{column_index}]")

    correspondences = corpus.get("correspondences", [])
    _require(isinstance(correspondences, list), "corpus.correspondences must be an array")
    pair_ids: set[str] = set()
    for index, item in enumerate(correspondences):
        path = f"corpus.correspondences[{index}]"
        item = _require_object(item, path)
        pair_id = _require_string(item.get("pair_id"), f"{path}.pair_id")
        _require(pair_id not in pair_ids, f"duplicate pair_id: {pair_id}")
        pair_ids.add(pair_id)
        _require_string(item.get("split_group"), f"{path}.split_group")
        left = _require_string(item.get("left_population_id"), f"{path}.left_population_id")
        right = _require_string(item.get("right_population_id"), f"{path}.right_population_id")
        _require(left != right, f"{path} cannot compare a population with itself")
        _require(left in population_ids, f"{path} references unknown left population: {left}")
        _require(right in population_ids, f"{path} references unknown right population: {right}")
        if "packet" in item:
            validate_outbound_packet(_require_object(item["packet"], f"{path}.packet"))
        gold = item.get("gold")
        if gold is None:
            _require(not require_reviewed, f"{path}.gold is required for reviewed evaluation")
            continue
        gold = _require_object(gold, f"{path}.gold")
        verdicts = gold.get("verdicts", [])
        _require(
            isinstance(verdicts, list) and bool(verdicts),
            f"{path}.gold.verdicts must be a non-empty array",
        )
        unknown = sorted(set(verdicts) - set(CORRESPONDENCE_VERDICTS))
        _require(
            not unknown, f"{path}.gold.verdicts contains unsupported verdicts: {', '.join(unknown)}"
        )
        _require(
            "abstain" not in verdicts or len(verdicts) == 1,
            f"{path}.gold.verdicts cannot combine abstain with another verdict",
        )
        if require_reviewed:
            _require(gold.get("reviewed") is True, f"{path}.gold.reviewed must be true")


def load_corpus(path: str | Path, *, require_reviewed: bool = False) -> dict[str, Any]:
    value = json.loads(Path(path).read_text())
    validate_corpus(value, require_reviewed=require_reviewed)
    return value


def write_corpus(path: str | Path, corpus: Mapping[str, Any]) -> None:
    validate_corpus(corpus)
    Path(path).write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n")
