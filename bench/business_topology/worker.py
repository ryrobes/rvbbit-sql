"""Resumable, proposal-only execution for a bounded excavation plan.

The worker deliberately keeps bulk correspondence scoring local and sends only
the genuinely generative motif/synthesis frontier through the tenant's
registered Hutch-backed Clover LLM.  Every successful item is validated against
its exact work packet before a mode-0600 receipt is written.  This module never
enqueues PostgreSQL jobs, inserts proposals, or materializes topology.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .baseline import predict_correspondence
from .contracts import ContractError, validate_outbound_packet
from .excavation import EXCAVATION_PLAN_SCHEMA_VERSION
from .results import (
    BRIDGE_RESULT_SCHEMA_VERSION,
    CORRESPONDENCE_RESULT_SCHEMA_VERSION,
    NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
    SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
    validate_excavation_result,
)


WORKER_VERSION = "business-topology-excavation-worker-v6"
EXECUTION_MANIFEST_SCHEMA_VERSION = "rvbbit.business-topology.execution-manifest.v1"
WORK_RECEIPT_SCHEMA_VERSION = "rvbbit.business-topology.work-receipt.v1"
PROMPT_CONTRACT_VERSION = "rvbbit.business-topology.excavation-prompts.v7"
LOCAL_CORRESPONDENCE_MODEL_VERSION = "deterministic-correspondence-baseline-v1"
_GENERATIVE_KINDS = frozenset({"source_motifs", "neighborhood_synthesis", "bridge_synthesis"})


class WorkExecutionError(ContractError):
    def __init__(self, message: str, *, attempts: int):
        super().__init__(message)
        self.attempts = attempts


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    if not isinstance(value, str):
        value = _canonical_json(value)
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(rendered)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


class ChatClient(Protocol):
    """Small seam used by the executor and deterministic tests."""

    backend_name: str
    model: str

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]: ...


@dataclass
class HutchChatClient:
    """Direct OpenAI-compatible Hutch client with exact response receipts."""

    endpoint_url: str
    token: str
    backend_name: str = "clover_llm"
    model: str = "clover"
    request_user: str = "business-topology-worker"
    timeout_seconds: float = 180.0
    transport_attempts: int = 3

    @classmethod
    def from_postgres(
        cls,
        dsn: str,
        *,
        backend_name: str = "clover_llm",
        model: str = "clover",
        request_user: str = "business-topology-worker",
        timeout_seconds: float = 180.0,
        transport_attempts: int = 3,
    ) -> HutchChatClient:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("psycopg is required to resolve a registered Hutch backend") from exc

        with psycopg.connect(dsn) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT endpoint_url,transport,auth_header_env,transport_opts
                  FROM rvbbit.backends
                 WHERE name=%s
                """,
                (backend_name,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"registered backend {backend_name!r} was not found")
            endpoint_url, transport, auth_header_env, transport_opts = row
            if transport != "openai_chat":
                raise RuntimeError(f"backend {backend_name!r} uses {transport!r}, not openai_chat")
            registered_model = (transport_opts or {}).get("model")
            if registered_model and model != registered_model:
                aliases = set((transport_opts or {}).get("model_aliases") or [])
                if model not in aliases:
                    raise RuntimeError(
                        f"model {model!r} is not registered for backend {backend_name!r}"
                    )
            token = os.environ.get(auth_header_env or "", "")
            if not token and auth_header_env:
                cursor.execute("SELECT rvbbit.get_secret(%s)", (auth_header_env,))
                token_row = cursor.fetchone()
                token = str(token_row[0] or "") if token_row else ""
        if not token:
            raise RuntimeError(
                f"backend {backend_name!r} has no usable token in its registered secret"
            )
        parsed = urlparse(str(endpoint_url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"backend {backend_name!r} has an invalid endpoint URL")
        return cls(
            endpoint_url=str(endpoint_url),
            token=token,
            backend_name=backend_name,
            model=model,
            request_user=request_user,
            timeout_seconds=timeout_seconds,
            transport_attempts=max(int(transport_attempts), 1),
        )

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("httpx is required for direct Hutch receipts") from exc

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max(max_tokens, 16),
            "user": self.request_user,
        }
        started = time.perf_counter()
        transport_errors: list[dict[str, Any]] = []
        response = None
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for attempt in range(1, self.transport_attempts + 1):
                try:
                    candidate = client.post(
                        self.endpoint_url,
                        headers={"Authorization": f"Bearer {self.token}"},
                        json=body,
                    )
                except httpx.RequestError as exc:
                    transport_errors.append(
                        {
                            "attempt": attempt,
                            "kind": "network",
                            "error": str(exc)[:500].replace(self.token, "<redacted>"),
                        }
                    )
                    if attempt >= self.transport_attempts:
                        raise RuntimeError(
                            f"Hutch backend {self.backend_name!r} exhausted "
                            f"{attempt} network attempts"
                        ) from exc
                    time.sleep(min(0.75 * (2 ** (attempt - 1)), 3.0))
                    continue
                if candidate.status_code < 400:
                    response = candidate
                    break
                detail = candidate.text[:800].replace(self.token, "<redacted>")
                retryable = candidate.status_code in {429, 500, 502, 503, 504}
                transport_errors.append(
                    {
                        "attempt": attempt,
                        "kind": "http",
                        "status": candidate.status_code,
                        "error": detail,
                    }
                )
                if not retryable or attempt >= self.transport_attempts:
                    raise RuntimeError(
                        f"Hutch backend {self.backend_name!r} returned HTTP "
                        f"{candidate.status_code}: {detail}"
                    )
                time.sleep(min(0.75 * (2 ** (attempt - 1)), 3.0))
        if response is None:  # pragma: no cover - loop always returns or raises
            raise RuntimeError(f"Hutch backend {self.backend_name!r} returned no response")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("Hutch returned no usable chat-completion content") from exc
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, Mapping) else str(part)
                for part in content
            )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Hutch returned empty chat-completion content")
        model_version = response.headers.get("x-hutch-model-version")
        if not model_version:
            raise RuntimeError("Hutch response omitted the exact x-hutch-model-version receipt")
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        safe_usage = {
            key: usage[key]
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost",
            )
            if isinstance(usage, Mapping) and key in usage
        }
        return content, {
            "schema_version": "rvbbit.business-topology.hutch-receipt.v1",
            "backend": self.backend_name,
            "service_model": payload.get("model", self.model),
            "model_version": model_version,
            "provider_request_id": payload.get("id"),
            "endpoint_host": urlparse(self.endpoint_url).netloc,
            "latency_ms": elapsed_ms,
            "transport_attempts": len(transport_errors) + 1,
            "transport_errors": transport_errors,
            "usage": safe_usage,
            "prompt_sha256": _sha256({"system": system, "user": user}),
            "response_sha256": _sha256(content),
        }


def _json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        first_newline = value.find("\n")
        if first_newline >= 0:
            value = value[first_newline + 1 :]
        if value.endswith("```"):
            value = value[:-3].rstrip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        # Permit a short prose prefix, but only ever decode the first root
        # object. Scanning later braces can turn one complete inner node from a
        # truncated response into an apparently valid top-level result.
        decoder = json.JSONDecoder()
        offset = value.find("{")
        if offset < 0:
            raise ContractError("model response did not contain one valid JSON object")
        try:
            candidate, consumed = decoder.raw_decode(value[offset:])
        except json.JSONDecodeError as exc:
            raise ContractError("model response did not contain one complete JSON object") from exc
        if value[offset + consumed :].strip():
            raise ContractError("model response contained content after its JSON object")
        parsed = candidate
    if not isinstance(parsed, dict):
        raise ContractError("model response must be a JSON object")
    return parsed


def _field_summary(field: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: field[key]
        for key in (
            "name",
            "data_type",
            "type_family",
            "nullable",
            "null_fraction",
            "sample_rows",
            "sample_distinct",
            "cardinality_ratio",
            "average_length",
            "maximum_length",
            "role_hints",
            "sensitivity_hint",
            "value_shapes",
            "declared_pk",
            "declared_fk",
        )
        if key in field
    }
    frequencies = field.get("frequency_profile")
    if isinstance(frequencies, list):
        counts = [
            float(item["n"])
            for item in frequencies
            if isinstance(item, Mapping) and isinstance(item.get("n"), (int, float))
        ]
        if counts:
            result["frequency_shape"] = {
                "buckets": len(counts),
                "maximum_count": max(counts),
                "singleton_buckets": sum(count == 1 for count in counts),
            }
    return result


def _pair_interest(pair: Mapping[str, Any]) -> tuple[float, str, str]:
    score = max(
        float(pair.get("left_to_right_strength", 0) or 0),
        float(pair.get("right_to_left_strength", 0) or 0),
        float(pair.get("equal_value_fraction", 0) or 0),
    )
    return (-score, str(pair.get("left", "")), str(pair.get("right", "")))


def compact_source_motif_input(
    packet: Mapping[str, Any], *, max_pairs: int = 256
) -> dict[str, Any]:
    """Remove repetitive histograms while retaining every field and strong pair clue."""

    validate_outbound_packet(packet)
    context = packet.get("relation_context")
    if not isinstance(context, Mapping):
        raise ContractError("source-motifs packet has no relation_context")
    fields = context.get("fields")
    if not isinstance(fields, list):
        raise ContractError("source-motifs relation_context.fields must be an array")
    pairs = context.get("field_pairs", [])
    if not isinstance(pairs, list):
        raise ContractError("source-motifs relation_context.field_pairs must be an array")
    return {
        "schema_version": packet["schema_version"],
        "privacy": packet["privacy"],
        "source": packet.get("source", {}),
        "sample": packet.get("sample", {}),
        "relation_context": {
            key: context[key]
            for key in ("name", "kind", "estimated_rows", "sample_rows", "field_count")
            if key in context
        }
        | {
            "fields": [_field_summary(field) for field in fields if isinstance(field, Mapping)],
            "strongest_field_pairs": [
                dict(pair)
                for pair in sorted(
                    (pair for pair in pairs if isinstance(pair, Mapping)),
                    key=_pair_interest,
                )[:max_pairs]
            ],
        },
        "output_contract": packet.get("output_contract", {}),
    }


_SYSTEM_PROMPT = """You are RVBBIT's Business Topology excavation specialist.
Infer cautiously from bounded structural and behavioral summaries. Think like an
archaeologist, not a DBA: a source can contain several business objects; two
systems can describe the same concept without matching IDs; schema names,
foreign keys, and table boundaries are clues, never truth. Prefer a small,
useful semantic org chart over a sprawling graph. Abstain when evidence is weak.
Nodes are durable business nouns, lifecycles, events, measures, or genuinely
reusable facets—not a renamed table, column, label, comment, timestamp, or key.
Return exactly one JSON object with no markdown. Never emit SQL, raw/example
values, fingerprints, private controls, or IDs that were not in the input."""


def _normalize_proposal_status(result: dict[str, Any], proposal_key: str) -> None:
    """Repair only an invalid outer envelope status from proposal presence.

    Some OpenAI-compatible reasoning backends consistently return otherwise
    valid contract objects with generic statuses such as ``completed``.  The
    proposal collection already determines the only compatible contract status,
    so this canonicalization does not invent or alter a semantic claim.  Valid
    explicit statuses remain untouched and all other fields still pass through
    deterministic validation.
    """

    if result.get("status") in {"proposed", "abstained"}:
        return
    proposals = result.get(proposal_key)
    result["status"] = "proposed" if isinstance(proposals, list) and proposals else "abstained"


def _unwrap_result_envelope(payload: Mapping[str, Any], proposal_key: str) -> dict[str, Any]:
    """Accept a harmless generic ``result`` wrapper around the exact contract."""

    nested = payload.get("result")
    if proposal_key not in payload and isinstance(nested, Mapping):
        return dict(nested)
    return dict(payload)


def _response_contract_shape(payload: Mapping[str, Any]) -> str:
    """Return value-free response-shape diagnostics for failed contracts."""

    summaries: list[str] = []
    for location, value in (("root", payload),) + tuple(
        (key, payload.get(key)) for key in ("result", "output", "data", "response")
    ):
        if not isinstance(value, Mapping):
            if location != "root" and value is not None:
                summaries.append(f"{location}=<{type(value).__name__}>")
            continue
        keys = sorted(str(key) for key in value)[:32]
        arrays = {
            key: len(value[key])
            for key in ("motifs", "nodes", "bindings", "edges", "findings")
            if isinstance(value.get(key), list)
        }
        status = value.get("status")
        status_hint = (
            status if isinstance(status, str) and len(status) <= 40 else type(status).__name__
        )
        summaries.append(f"{location}(keys={keys},status={status_hint!r},arrays={arrays})")
    return "; ".join(summaries)


@dataclass(frozen=True)
class _IdentifierAliases:
    """Per-call compact aliases for long opaque plan identifiers.

    Aliases exist only in the model prompt and response. They are expanded back
    to the exact plan identifiers before normalization, deterministic
    validation, or receipt persistence.
    """

    populations: dict[str, str]
    dependencies: dict[str, str]

    @property
    def forward(self) -> dict[str, str]:
        return {**self.populations, **self.dependencies}

    @property
    def population_reverse(self) -> dict[str, str]:
        return {alias: identifier for identifier, alias in self.populations.items()}

    @property
    def dependency_reverse(self) -> dict[str, str]:
        return {alias: identifier for identifier, alias in self.dependencies.items()}


def _identifier_aliases(work: Mapping[str, Any]) -> _IdentifierAliases:
    populations = sorted({str(value) for value in work.get("population_ids", [])})
    dependencies = [str(value) for value in work.get("depends_on", [])]
    population_width = max(2, len(str(len(populations))))
    dependency_width = max(2, len(str(len(dependencies))))
    return _IdentifierAliases(
        populations={
            identifier: f"p{index:0{population_width}d}"
            for index, identifier in enumerate(populations, start=1)
        },
        dependencies={
            identifier: f"d{index:0{dependency_width}d}"
            for index, identifier in enumerate(dependencies, start=1)
        },
    )


def _alias_prompt_value(value: Any, aliases: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _alias_prompt_value(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_alias_prompt_value(item, aliases) for item in value]
    if isinstance(value, tuple):
        return [_alias_prompt_value(item, aliases) for item in value]
    if isinstance(value, str):
        return aliases.get(value, value)
    return value


def _population_field_name(population_id: str) -> str:
    if "#field:" in population_id:
        return population_id.rsplit("#field:", 1)[1]
    return population_id.rsplit("#", 1)[-1]


def _compact_prompt_payload(
    work: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    population_ids: Sequence[str] | None = None,
    dependency_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Replace repeat-heavy identifiers and retain a value-free field legend."""

    aliases = _identifier_aliases(work)
    compact = _alias_prompt_value(payload, aliases.forward)
    if not isinstance(compact, dict):  # pragma: no cover - mapping input guarantees this
        raise ContractError("compact prompt payload must remain an object")
    population_filter = (
        set(population_ids) if population_ids is not None else set(aliases.populations)
    )
    dependency_filter = (
        set(dependency_ids) if dependency_ids is not None else set(aliases.dependencies)
    )
    compact["identifier_aliases"] = {
        "population_ids": [
            {
                "alias": alias,
                "field_name": _population_field_name(identifier),
            }
            for identifier, alias in aliases.populations.items()
            if identifier in population_filter
        ],
        "dependency_work_ids": [
            alias
            for identifier, alias in aliases.dependencies.items()
            if identifier in dependency_filter
        ],
    }
    return compact


def _expand_result_identifiers(
    work: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Expand aliases only in contract ID slots, never in model-authored names."""

    aliases = _identifier_aliases(work)
    population_reverse = aliases.population_reverse
    dependency_reverse = aliases.dependency_reverse

    def expand(value: Any, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {str(item_key): expand(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [expand(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if key in {
            "population_id",
            "population_ids",
            "unbound_population_ids",
            "left_population_id",
            "right_population_id",
        }:
            return population_reverse.get(value, value)
        if key in {"work_id", "evidence_work_ids"}:
            return dependency_reverse.get(value, value)
        return value

    expanded = expand(payload)
    if not isinstance(expanded, dict):  # pragma: no cover - mapping input guarantees this
        raise ContractError("expanded model result must remain an object")
    return expanded


def _motif_prompt(work: Mapping[str, Any]) -> str:
    packet = compact_source_motif_input(work["input_packet"])
    return f"""TASK: Discover candidate populations or motifs within one source.

Return this object shape:
{{
  "status": "proposed" | "abstained",
  "source_summary": "short cautious summary",
  "motifs": [{{
    "motif_key": "stable short key local to this result",
    "population_kind": "composite" | "slice" | "event_stream" | "query_projection",
    "name": "business-readable singular or collective name",
    "description": "what the motif represents",
    "field_names": ["exact input field name"],
    "roles": ["identity" | "status" | "category" | "time" | "money" | "measure" | "geography" | "evidence"],
    "confidence": 0.0,
    "rationale": "brief evidence-based reason"
  }}],
  "unassigned_field_names": ["every exact field not used by any motif"],
  "rationale": "required when abstained"
}}

Fields may participate in more than one defensible motif, but every input field
must appear in at least one motif or in unassigned_field_names. Do not force one
motif to represent the entire source. Name business concepts, not database
shapes such as "table", "row", "record", or "dimension".

BOUNDED INPUT:
{_canonical_json(packet)}"""


def _dependency_payload(
    work: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for work_id in work.get("depends_on", []):
        prior = prior_results.get(str(work_id))
        if prior is None:
            raise ContractError(f"work {work['work_id']} is missing dependency {work_id}")
        payload.append({"work_id": work_id, "result": prior})
    return payload


def _synthesis_population_frontier(
    work: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
    *,
    maximum: int = 48,
) -> list[str]:
    """Choose a balanced, evidence-led binding frontier for a wide unit.

    The exact plan scope remains unchanged. Populations outside this prompt
    frontier are deterministically recorded as unbound after model output.
    """

    allowed = sorted({str(value) for value in work.get("population_ids", [])})
    if len(allowed) <= maximum:
        return allowed
    allowed_set = set(allowed)
    selected: list[str] = []
    selected_set: set[str] = set()

    def add(population_id: Any) -> None:
        value = str(population_id)
        if value in allowed_set and value not in selected_set and len(selected) < maximum:
            selected.append(value)
            selected_set.add(value)

    # Start with populations that survived an independent correspondence
    # nomination, strongest first. This is evidence, not a semantic verdict.
    correspondence_results = [
        prior_results[str(work_id)]
        for work_id in work.get("depends_on", [])
        if str(work_id) in prior_results
        and prior_results[str(work_id)].get("schema_version")
        == CORRESPONDENCE_RESULT_SCHEMA_VERSION
    ]
    correspondence_results.sort(
        key=lambda result: (
            result.get("status") != "proposed",
            -float(result.get("confidence") or 0.0),
            str(result.get("work_id") or ""),
        )
    )
    # Reserve half the prompt for motif/source coverage. Otherwise a dense
    # patch of pair evidence can consume the entire frontier before the
    # source's other plausible business motifs receive one representative.
    correspondence_budget = max(1, maximum // 2) if maximum > 0 else 0
    for result in correspondence_results:
        for population_id in result.get("population_ids", []):
            if len(selected) >= correspondence_budget:
                break
            add(population_id)
        if len(selected) >= correspondence_budget:
            break

    # Then sample motif field lists round-robin. A wide source can describe
    # several objects; taking one motif to exhaustion would recreate the
    # table-as-object bias this subsystem is designed to avoid.
    motif_queues: list[list[str]] = []
    raw_source_inputs = work.get("input_packet", {}).get("source_inputs", [])
    source_inputs = raw_source_inputs if isinstance(raw_source_inputs, list) else []
    for source_input in source_inputs:
        if not isinstance(source_input, Mapping):
            continue
        field_populations: dict[str, list[str]] = {}
        for population_id in source_input.get("population_ids", []):
            population_id = str(population_id)
            if population_id not in allowed_set:
                continue
            field_populations.setdefault(_population_field_name(population_id), []).append(
                population_id
            )
        motif_work_ids = source_input.get("source_motif_work_ids", [])
        motif_result = (
            prior_results.get(str(motif_work_ids[0]))
            if isinstance(motif_work_ids, list) and motif_work_ids
            else None
        )
        motifs = motif_result.get("motifs", []) if isinstance(motif_result, Mapping) else []
        ranked_motifs = sorted(
            (motif for motif in motifs if isinstance(motif, Mapping)),
            key=lambda motif: (
                -float(motif.get("confidence") or 0.0),
                str(motif.get("motif_key") or ""),
            ),
        )
        for motif in ranked_motifs:
            queue: list[str] = []
            for field_name in motif.get("field_names", []):
                for population_id in field_populations.get(str(field_name), []):
                    if population_id not in queue:
                        queue.append(population_id)
            if queue:
                motif_queues.append(queue)

    while motif_queues and len(selected) < maximum:
        progressed = False
        for queue in motif_queues:
            while queue and queue[0] in selected_set:
                queue.pop(0)
            if not queue:
                continue
            add(queue.pop(0))
            progressed = True
            if len(selected) >= maximum:
                break
        if not progressed:
            break

    # Finally fill any remaining capacity round-robin across sources rather
    # than alphabetically exhausting one system.
    source_queues = [
        sorted(
            str(population_id)
            for population_id in source_input.get("population_ids", [])
            if str(population_id) in allowed_set and str(population_id) not in selected_set
        )
        for source_input in source_inputs
        if isinstance(source_input, Mapping)
    ]
    while source_queues and len(selected) < maximum:
        progressed = False
        for queue in source_queues:
            while queue and queue[0] in selected_set:
                queue.pop(0)
            if not queue:
                continue
            add(queue.pop(0))
            progressed = True
            if len(selected) >= maximum:
                break
        if not progressed:
            break

    for population_id in allowed:
        add(population_id)
    return sorted(selected)


def _neighborhood_dependency_payload(
    work: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
    frontier: set[str],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for dependency in _dependency_payload(work, prior_results):
        result = dependency["result"]
        if result.get("schema_version") == CORRESPONDENCE_RESULT_SCHEMA_VERSION:
            populations = {str(value) for value in result.get("population_ids", [])}
            if not populations.issubset(frontier):
                continue
        payload.append(dependency)
    return payload


def _neighborhood_prompt(
    work: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
    *,
    frontier: Sequence[str] | None = None,
) -> str:
    output_contract = work.get("input_packet", {}).get("output_contract", {})
    max_nodes = int(output_contract.get("max_nodes", 16))
    max_bindings = int(output_contract.get("max_bindings", 48))
    max_edges = int(output_contract.get("max_edges", 24))
    selected_populations = list(
        frontier
        if frontier is not None
        else _synthesis_population_frontier(
            work,
            prior_results,
            maximum=max_bindings,
        )
    )
    selected_set = set(selected_populations)
    dependency_results = _neighborhood_dependency_payload(
        work,
        prior_results,
        selected_set,
    )
    included_dependencies = [str(item["work_id"]) for item in dependency_results]
    input_packet = dict(work["input_packet"])
    input_packet["source_inputs"] = [
        {
            **source_input,
            "population_ids": [
                population_id
                for population_id in source_input.get("population_ids", [])
                if population_id in selected_set
            ],
        }
        for source_input in input_packet.get("source_inputs", [])
    ]
    input_packet["evidence_work_ids"] = [
        work_id
        for work_id in input_packet.get("evidence_work_ids", [])
        if work_id in set(included_dependencies)
    ]
    payload = _compact_prompt_payload(
        work,
        {
            "work": {
                "work_id": work["work_id"],
                "scope_id": work["scope_id"],
                "source_keys": work["source_keys"],
                "population_ids": selected_populations,
                "input_packet": input_packet,
            },
            "synthesis_frontier": {
                "selected_populations": len(selected_populations),
                "total_scoped_populations": len(work.get("population_ids", [])),
                "selection_policy": "correspondence evidence, then balanced source motifs",
                "outside_frontier_becomes_unbound": True,
            },
            "dependency_results": dependency_results,
        },
        population_ids=selected_populations,
        dependency_ids=included_dependencies,
    )
    return f"""TASK: Compose a candidate semantic skeleton for one bounded excavation unit.

Return this object shape:
{{
  "status": "proposed" | "abstained",
  "canonical_name": "short business-readable name for this entire semantic tree",
  "nodes": [{{
    "node_key": "stable short key local to this result",
    "node_kind": "object" | "facet" | "lifecycle" | "event" | "measure" | "category",
    "name": "business-readable name",
    "description": "brief meaning",
    "confidence": 0.0,
    "properties": {{}},
    "parent_node_key": null,
    "evidence_work_ids": ["exact dependency work id"]
  }}],
  "bindings": [{{
    "node_key": "exact node_key from this result",
    "population_id": "exact scoped population id",
    "binding_role": "identity" | "attribute" | "event" | "measure" | "category" | "status" | "time" | "geography" | "evidence" | "context",
    "authority_hint": "unknown" | "primary" | "secondary" | "derived" | "conflicting",
    "confidence": 0.0,
    "evidence_work_ids": ["exact dependency work id"]
  }}],
  "edges": [{{
    "subject_node_key": "exact node_key",
    "predicate": "short relationship phrase",
    "object_node_key": "different exact node_key",
    "confidence": 0.0,
    "evidence_work_ids": ["exact dependency work id"]
  }}],
  "unbound_population_ids": ["exact scoped population id"],
  "rationale": "required when abstained"
}}

Every node needs a binding. Bind only populations in the presented synthesis
frontier. The executor automatically marks every unmentioned or out-of-frontier
scoped population as unbound, so do not repeat uncertain aliases merely for
coverage. Population reuse is allowed. Cite only dependency work IDs. Parent
links must be acyclic. A relation boundary is not an object boundary.
Keep the proposal compact: at most {max_nodes} nodes, {max_bindings} bindings,
and {max_edges} edges. Prefer
identity, lifecycle, event, measure, and other structurally useful bindings;
ordinary descriptive fields can remain unbound until later evidence warrants
promotion.
For a proposed result, canonical_name is required. Use two to six words that
name the business concept or area represented by the whole tree. It may be
broader than any single root node. Never derive it from schema, table, or
relation names, and avoid generic labels such as "data", "database", "table",
"topology", or "semantic tree".
Bind ordinary names, descriptions, comments, statuses, dates, and keys directly
to their durable noun with the appropriate binding role. Never create a node
solely to account for one field. A facet must be a reusable business slicing
concept, not a decorative wrapper around an attribute. For a small reference
pair, one to three meaningful nodes is usually enough. A foreign/reference key
may bind both as an attribute of one noun and as identity/context for another.

Compact identifiers beginning with `p` are exact population aliases and those
beginning with `d` are exact dependency-work aliases. Use those aliases in all
output ID fields. The executor expands them to the stable plan IDs before it
validates or stores the result.

BOUNDED INPUT AND PRIOR RECEIPTS:
{_canonical_json(payload)}"""


def _bridge_prompt(
    work: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
) -> str:
    payload = _compact_prompt_payload(
        work,
        {
            "work": {
                key: work[key]
                for key in (
                    "work_id",
                    "scope_kind",
                    "scope_id",
                    "source_keys",
                    "population_ids",
                    "input_packet",
                )
            },
            "dependency_results": _dependency_payload(work, prior_results),
        },
    )
    return f"""TASK: Inspect one nominated boundary between two already-bounded skeletons.

Return this object shape:
{{
  "status": "proposed" | "abstained",
  "merge_excavation_units": false,
  "findings": [{{
    "finding_key": "stable short key local to this result",
    "outcome": "shared_object" | "related_objects" | "joinable_populations" | "correlated" | "unrelated" | "abstain",
    "confidence": 0.0,
    "evidence_work_ids": ["exact dependency work id"],
    "left_node_ref": {{"work_id": "left synthesis work id", "node_key": "exact node key"}},
    "right_node_ref": {{"work_id": "right synthesis work id", "node_key": "exact node key"}}
  }}],
  "rationale": "required when abstained"
}}

For a population-level finding, use left_population_id and right_population_id
instead of node refs. Non-trivial findings require a complete pair of bounded
refs. Never request or imply that the excavation units be merged.

Compact identifiers beginning with `p` are exact population aliases and those
beginning with `d` are exact dependency-work aliases. Use those aliases in all
output ID fields. The executor expands them to the stable plan IDs before it
validates or stores the result.

BOUNDED INPUT AND PRIOR RECEIPTS:
{_canonical_json(payload)}"""


def _normalize_motif_result(work: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _unwrap_result_envelope(payload, "motifs")
    result.pop("schema_version", None)
    result.pop("work_id", None)
    result = {
        "schema_version": SOURCE_MOTIFS_RESULT_SCHEMA_VERSION,
        "work_id": work["work_id"],
        **result,
    }
    result.setdefault("motifs", [])
    _normalize_proposal_status(result, "motifs")
    for motif in result["motifs"] if isinstance(result["motifs"], list) else []:
        if isinstance(motif, dict):
            for key in ("roles", "field_names"):
                values = motif.setdefault(key, [])
                if isinstance(values, list):
                    deduplicated: list[Any] = []
                    for value in values:
                        if value not in deduplicated:
                            deduplicated.append(value)
                    motif[key] = deduplicated
    context = work.get("input_packet", {}).get("relation_context", {})
    allowed_fields = {
        str(field["name"])
        for field in context.get("fields", [])
        if isinstance(field, Mapping) and isinstance(field.get("name"), str)
    }
    assigned = {
        str(name)
        for motif in result["motifs"]
        if isinstance(motif, Mapping)
        for name in motif.get("field_names", [])
        if isinstance(name, str) and name in allowed_fields
    }
    supplied_unassigned = result.get("unassigned_field_names", [])
    if not isinstance(supplied_unassigned, list):
        supplied_unassigned = []
    result["unassigned_field_names"] = sorted(
        ({str(name) for name in supplied_unassigned if name in allowed_fields} | allowed_fields)
        - assigned
    )
    return result


def _canonical_name_from_roots(nodes: Any) -> str | None:
    """Build a cautious semantic fallback when a model omits the tree name.

    The fallback deliberately uses proposed semantic roots, never source or
    relation labels.  New prompt contracts still require a model-composed name;
    this only keeps an otherwise valid result usable when a provider misses the
    newly introduced field.
    """

    if not isinstance(nodes, list):
        return None
    semantic_nodes = [node for node in nodes if isinstance(node, Mapping)]
    node_keys = {
        str(node["node_key"]) for node in semantic_nodes if isinstance(node.get("node_key"), str)
    }
    roots = [
        node
        for node in semantic_nodes
        if not isinstance(node.get("parent_node_key"), str)
        or node.get("parent_node_key") not in node_keys
    ]
    durable_roots = [
        node for node in roots if node.get("node_kind") in {"object", "lifecycle", "event"}
    ]
    candidates = durable_roots or roots or semantic_nodes
    names: list[str] = []
    seen: set[str] = set()
    for node in candidates:
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        cleaned = " ".join(name.split())
        identity = cleaned.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        names.append(cleaned)
        if len(names) == 2:
            break
    if not names:
        return None
    return " & ".join(names)[:120].rstrip(" &")


def _fit_neighborhood_binding_budget(
    work: Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Precision-first projection for an otherwise well-formed binding overflow.

    Models sometimes reuse one population across several defensible nodes and
    exceed the hard binding cap by a small amount. Validate every binding's
    bounded references first, retain the strongest binding for every node,
    then fill remaining capacity by confidence and structural role. Dropped
    populations are handled by the normal deterministic unbound projection.
    """

    result = _unwrap_result_envelope(payload, "nodes")
    bindings = result.get("bindings")
    nodes = result.get("nodes")
    contract = work.get("input_packet", {}).get("output_contract", {})
    max_bindings = int(contract.get("max_bindings", 48))
    if (
        not isinstance(bindings, list)
        or len(bindings) <= max_bindings
        or not isinstance(nodes, list)
        or max_bindings < 1
    ):
        return dict(payload), None
    if not all(isinstance(binding, Mapping) for binding in bindings):
        return dict(payload), None

    node_keys = [
        str(node.get("node_key"))
        for node in nodes
        if isinstance(node, Mapping) and isinstance(node.get("node_key"), str)
    ]
    if not node_keys or len(node_keys) != len(set(node_keys)) or len(node_keys) > max_bindings:
        return dict(payload), None

    allowed_populations = {str(value) for value in work.get("population_ids", [])}
    allowed_evidence = {str(value) for value in work.get("depends_on", [])}
    allowed_roles = set(contract.get("binding_roles", []))
    allowed_authority = {"unknown", "primary", "secondary", "derived", "conflicting"}
    binding_keys: set[tuple[str, str, str]] = set()
    for binding in bindings:
        node_key = binding.get("node_key")
        population_id = binding.get("population_id")
        role = binding.get("binding_role")
        confidence = binding.get("confidence")
        evidence = binding.get("evidence_work_ids")
        authority = binding.get("authority_hint", "unknown")
        binding_key = (str(node_key), str(population_id), str(role))
        if (
            not isinstance(node_key, str)
            or node_key not in node_keys
            or not isinstance(population_id, str)
            or population_id not in allowed_populations
            or not isinstance(role, str)
            or role not in allowed_roles
            or authority not in allowed_authority
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0.0 <= float(confidence) <= 1.0
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) for item in evidence)
            or len(evidence) != len(set(evidence))
            or any(item not in allowed_evidence for item in evidence)
            or binding_key in binding_keys
        ):
            return dict(payload), None
        binding_keys.add(binding_key)

    role_priority = {
        "identity": 0,
        "event": 1,
        "measure": 2,
        "status": 3,
        "time": 4,
        "geography": 5,
        "category": 6,
        "attribute": 7,
        "evidence": 8,
        "context": 9,
    }

    def rank(index: int) -> tuple[Any, ...]:
        binding = bindings[index]
        return (
            -float(binding["confidence"]),
            role_priority.get(str(binding["binding_role"]), 99),
            str(binding["population_id"]),
            str(binding["node_key"]),
            index,
        )

    selected_indexes: set[int] = set()
    for node_key in node_keys:
        candidates = [
            index for index, binding in enumerate(bindings) if binding.get("node_key") == node_key
        ]
        if not candidates:
            return dict(payload), None
        selected_indexes.add(min(candidates, key=rank))

    remaining = sorted(
        (index for index in range(len(bindings)) if index not in selected_indexes),
        key=rank,
    )
    selected_indexes.update(remaining[: max_bindings - len(selected_indexes)])
    bounded = dict(result)
    bounded["bindings"] = [
        binding for index, binding in enumerate(bindings) if index in selected_indexes
    ]
    return bounded, {
        "policy": "precision-first-binding-budget-v1",
        "input_bindings": len(bindings),
        "output_bindings": len(bounded["bindings"]),
        "removed_bindings": len(bindings) - len(bounded["bindings"]),
        "retained_one_per_node": True,
    }


def _normalize_neighborhood_result(
    work: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    result = _unwrap_result_envelope(payload, "nodes")
    result.pop("schema_version", None)
    result.pop("work_id", None)
    result = {
        "schema_version": NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
        "work_id": work["work_id"],
        **result,
    }
    for key in ("nodes", "bindings", "edges"):
        result.setdefault(key, [])
    _normalize_proposal_status(result, "nodes")
    for node in result["nodes"] if isinstance(result["nodes"], list) else []:
        if isinstance(node, dict):
            node.setdefault("properties", {})
    canonical_name = result.get("canonical_name")
    if isinstance(canonical_name, str) and canonical_name.strip():
        result["canonical_name"] = " ".join(canonical_name.split())
    elif result["status"] == "proposed":
        fallback_name = _canonical_name_from_roots(result["nodes"])
        if fallback_name:
            result["canonical_name"] = fallback_name
    allowed = {str(item) for item in work.get("population_ids", [])}
    bound = {
        str(binding.get("population_id"))
        for binding in result["bindings"]
        if isinstance(binding, Mapping) and binding.get("population_id") in allowed
    }
    supplied_unbound = result.get("unbound_population_ids", [])
    if not isinstance(supplied_unbound, list):
        supplied_unbound = []
    result["unbound_population_ids"] = sorted(
        ({str(item) for item in supplied_unbound if item in allowed} | allowed) - bound
    )
    return result


def _normalize_bridge_result(work: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _unwrap_result_envelope(payload, "findings")
    result.pop("schema_version", None)
    result.pop("work_id", None)
    result = {
        "schema_version": BRIDGE_RESULT_SCHEMA_VERSION,
        "work_id": work["work_id"],
        **result,
    }
    result["merge_excavation_units"] = False
    result.setdefault("findings", [])
    _normalize_proposal_status(result, "findings")
    return result


def correspondence_result(work: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prediction = predict_correspondence(work["input_packet"])
    verdicts = list(prediction["verdicts"])
    result = {
        "schema_version": CORRESPONDENCE_RESULT_SCHEMA_VERSION,
        "work_id": work["work_id"],
        "status": "abstained" if verdicts == ["abstain"] else "proposed",
        "population_ids": list(work["population_ids"]),
        "verdicts": verdicts,
        "scores": prediction["scores"],
        "confidence": prediction["confidence"],
        "uncertainty": prediction["uncertainty"],
        "rationale": (
            "Precision-first deterministic structural, semantic-neighbor, and local-overlap floor."
        ),
    }
    receipt = {
        "schema_version": "rvbbit.business-topology.local-model-receipt.v1",
        "backend": "local",
        "model": "semantic_correspondence_v1",
        "model_version": LOCAL_CORRESPONDENCE_MODEL_VERSION,
        "packet_sha256": _sha256(work["input_packet"]),
    }
    return result, receipt


def work_index(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if plan.get("schema_version") != EXCAVATION_PLAN_SCHEMA_VERSION:
        raise ContractError("unsupported excavation-plan schema")
    result: dict[str, Mapping[str, Any]] = {}
    for work in plan.get("work_items", []):
        if not isinstance(work, Mapping) or not isinstance(work.get("work_id"), str):
            raise ContractError("every excavation work item requires a work_id")
        work_id = str(work["work_id"])
        if work_id in result:
            raise ContractError(f"duplicate excavation work id: {work_id}")
        result[work_id] = work
    return result


def select_work(
    plan: Mapping[str, Any],
    *,
    work_ids: Sequence[str] = (),
    unit_ids: Sequence[str] = (),
    link_ids: Sequence[str] = (),
    all_work: bool = False,
) -> list[str]:
    """Resolve requested terminal work plus its complete dependency closure."""

    indexed = work_index(plan)
    targets: set[str] = set()
    if all_work:
        targets.update(indexed)
    for work_id in work_ids:
        if work_id not in indexed:
            raise ContractError(f"requested work id is outside the plan: {work_id}")
        targets.add(work_id)
    for unit_id in unit_ids:
        matches = [
            work_id
            for work_id, work in indexed.items()
            if work.get("work_kind") == "neighborhood_synthesis" and work.get("scope_id") == unit_id
        ]
        if len(matches) != 1:
            raise ContractError(f"requested excavation unit was not found exactly once: {unit_id}")
        targets.update(matches)
    for link_id in link_ids:
        matches = [
            work_id
            for work_id, work in indexed.items()
            if work.get("work_kind") == "bridge_synthesis" and work.get("scope_id") == link_id
        ]
        if len(matches) != 1:
            raise ContractError(f"requested boundary link was not found exactly once: {link_id}")
        targets.update(matches)
    if not targets:
        raise ContractError("select --work-id, --unit-id, --link-id, or --all")

    selected: set[str] = set()

    def add_with_dependencies(work_id: str, path: set[str]) -> None:
        if work_id in selected:
            return
        if work_id in path:
            raise ContractError("excavation work dependencies contain a cycle")
        path = {*path, work_id}
        for dependency in indexed[work_id].get("depends_on", []):
            if dependency not in indexed:
                raise ContractError(f"work {work_id} has an unknown dependency: {dependency}")
            add_with_dependencies(str(dependency), path)
        selected.add(work_id)

    for target in sorted(targets):
        add_with_dependencies(target, set())
    return sorted(
        selected,
        key=lambda work_id: (
            int(indexed[work_id].get("stage", 0)),
            work_id,
        ),
    )


class ExecutionStore:
    def __init__(self, root: str | Path, plan: Mapping[str, Any]):
        self.root = Path(root)
        self.plan = plan
        self.plan_sha256 = _sha256(plan)
        self.manifest_path = self.root / "execution.json"
        self.results_dir = self.root / "results"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.results_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.results_dir, 0o700)
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text())
            if self.manifest.get("schema_version") != EXECUTION_MANIFEST_SCHEMA_VERSION:
                raise ContractError("execution directory has an unsupported manifest")
            if self.manifest.get("plan_sha256") != self.plan_sha256:
                raise ContractError("execution directory belongs to a different excavation plan")
            if self.manifest.get("worker_version") != WORKER_VERSION:
                raise ContractError("execution directory belongs to a different worker version")
            if self.manifest.get("prompt_contract_version") != PROMPT_CONTRACT_VERSION:
                raise ContractError("execution directory pins a different prompt contract")
        else:
            self.manifest = {
                "schema_version": EXECUTION_MANIFEST_SCHEMA_VERSION,
                "worker_version": WORKER_VERSION,
                "prompt_contract_version": PROMPT_CONTRACT_VERSION,
                "plan_sha256": self.plan_sha256,
                "created_at": _now(),
                "updated_at": _now(),
                "work": {},
            }
            self._save_manifest()

    def _result_path(self, work_id: str) -> Path:
        return self.results_dir / f"{_sha256(work_id)[:24]}.json"

    def _save_manifest(self) -> None:
        self.manifest["updated_at"] = _now()
        _write_private_json(self.manifest_path, self.manifest)

    def completed(self, work_id: str) -> bool:
        entry = self.manifest.get("work", {}).get(work_id, {})
        return entry.get("status") == "completed" and self._result_path(work_id).exists()

    def load_result(self, work_id: str) -> dict[str, Any]:
        envelope = json.loads(self._result_path(work_id).read_text())
        if envelope.get("schema_version") != WORK_RECEIPT_SCHEMA_VERSION:
            raise ContractError(f"work receipt {work_id} has an unsupported schema")
        if envelope.get("plan_sha256") != self.plan_sha256:
            raise ContractError(f"work receipt {work_id} belongs to a different plan")
        result = envelope.get("result")
        if not isinstance(result, dict) or result.get("work_id") != work_id:
            raise ContractError(f"work receipt {work_id} has a mismatched result")
        return result

    def record_success(
        self,
        work: Mapping[str, Any],
        result: Mapping[str, Any],
        validation: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> None:
        work_id = str(work["work_id"])
        path = self._result_path(work_id)
        envelope = {
            "schema_version": WORK_RECEIPT_SCHEMA_VERSION,
            "worker_version": WORKER_VERSION,
            "prompt_contract_version": PROMPT_CONTRACT_VERSION,
            "plan_sha256": self.plan_sha256,
            "work_id": work_id,
            "work_kind": work["work_kind"],
            "input_packet_sha256": _sha256(work["input_packet"]),
            "completed_at": _now(),
            "result": result,
            "validation": validation,
            "execution_receipts": list(receipts),
        }
        _write_private_json(path, envelope)
        self.manifest.setdefault("work", {})[work_id] = {
            "status": "completed",
            "work_kind": work["work_kind"],
            "result_file": str(path.relative_to(self.root)),
            "completed_at": envelope["completed_at"],
            "attempts": len(receipts),
            "model_versions": sorted(
                {
                    str(receipt.get("model_version"))
                    for receipt in receipts
                    if receipt.get("model_version")
                }
            ),
        }
        self._save_manifest()

    def record_failure(self, work: Mapping[str, Any], error: Exception, attempts: int) -> None:
        work_id = str(work["work_id"])
        old = self.manifest.setdefault("work", {}).get(work_id, {})
        self.manifest["work"][work_id] = {
            "status": "failed",
            "work_kind": work["work_kind"],
            "attempts": int(old.get("attempts", 0)) + attempts,
            "failed_at": _now(),
            "error": str(error)[:2_000],
        }
        self._save_manifest()


def _execute_generative(
    work: Mapping[str, Any],
    plan: Mapping[str, Any],
    prior_results: Mapping[str, Mapping[str, Any]],
    client: ChatClient,
    *,
    repair_attempts: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    kind = work["work_kind"]
    synthesis_frontier: list[str] | None = None
    if kind == "source_motifs":
        prompt = _motif_prompt(work)
        normalize = _normalize_motif_result
        field_count = len(
            work.get("input_packet", {}).get("relation_context", {}).get("fields", [])
        )
        max_tokens = min(7_000, max(3_000, 1_500 + field_count * 30))
    elif kind == "neighborhood_synthesis":
        output_contract = work.get("input_packet", {}).get("output_contract", {})
        synthesis_frontier = _synthesis_population_frontier(
            work,
            prior_results,
            maximum=int(output_contract.get("max_bindings", 48)),
        )
        prompt = _neighborhood_prompt(
            work,
            prior_results,
            frontier=synthesis_frontier,
        )
        normalize = _normalize_neighborhood_result
        max_tokens = 7_000
    elif kind == "bridge_synthesis":
        prompt = _bridge_prompt(work, prior_results)
        normalize = _normalize_bridge_result
        max_tokens = 4_000
    else:  # pragma: no cover - guarded by caller
        raise ContractError(f"unsupported generative work kind: {kind}")

    receipts: list[dict[str, Any]] = []
    validation_error: str | None = None
    previous_response: str | None = None
    for attempt in range(repair_attempts + 1):
        user_prompt = prompt
        if validation_error is not None:
            clipped_response = (previous_response or "")[:24_000]
            user_prompt += f"""

CORRECTION REQUIRED. Your prior JSON failed the deterministic contract:
{validation_error}

PRIOR JSON (correct it; do not explain):
{clipped_response}"""
        try:
            content, receipt = client.complete_json(
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise WorkExecutionError(str(exc), attempts=len(receipts) + 1) from exc
        receipt = dict(receipt)
        receipt["attempt"] = attempt + 1
        receipt["requested_max_tokens"] = max_tokens
        receipt["prompt_contract_version"] = PROMPT_CONTRACT_VERSION
        if synthesis_frontier is not None:
            receipt["synthesis_frontier"] = {
                "selected_populations": len(synthesis_frontier),
                "total_scoped_populations": len(work.get("population_ids", [])),
                "population_ids_sha256": _sha256(synthesis_frontier),
                "outside_frontier_becomes_unbound": True,
            }
        receipts.append(receipt)
        previous_response = content
        decoded: Mapping[str, Any] | None = None
        try:
            decoded = _json_object(content)
            if kind in {"neighborhood_synthesis", "bridge_synthesis"}:
                decoded = _expand_result_identifiers(work, decoded)
            if kind == "neighborhood_synthesis":
                decoded, bounded_normalization = _fit_neighborhood_binding_budget(work, decoded)
                if bounded_normalization is not None:
                    receipt["bounded_normalization"] = bounded_normalization
            result = normalize(work, decoded)
            prior_for_validation = {
                work_id: prior
                for work_id, prior in prior_results.items()
                if prior.get("schema_version") == NEIGHBORHOOD_RESULT_SCHEMA_VERSION
            }
            validation = validate_excavation_result(
                plan,
                result,
                prior_results=prior_for_validation,
            )
            return result, validation, receipts
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            shape = _response_contract_shape(decoded) if decoded is not None else "unparsed"
            validation_error = f"{exc}; response shape: {shape}"
            receipt["validation_error"] = validation_error[:2_000]
    raise WorkExecutionError(
        f"{work['work_id']} remained invalid after {repair_attempts + 1} model attempt(s): "
        f"{validation_error}",
        attempts=len(receipts),
    )


def execute_plan(
    plan: Mapping[str, Any],
    selected_work_ids: Sequence[str],
    *,
    output_dir: str | Path,
    client: ChatClient | None,
    max_work_items: int = 100,
    max_llm_calls: int = 16,
    repair_attempts: int = 1,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute a selected dependency closure and return a non-sensitive summary."""

    indexed = work_index(plan)
    selected = list(selected_work_ids)
    unknown = sorted(set(selected) - set(indexed))
    if unknown:
        raise ContractError(f"selected work ids are outside the plan: {', '.join(unknown)}")
    if len(selected) > max_work_items:
        raise ContractError(
            f"selected closure has {len(selected)} items; max_work_items is {max_work_items}"
        )
    store = ExecutionStore(output_dir, plan)
    pending_generations = sum(
        indexed[work_id].get("work_kind") in _GENERATIVE_KINDS and not store.completed(work_id)
        for work_id in selected
    )
    if pending_generations > max_llm_calls:
        raise ContractError(
            f"selected closure needs {pending_generations} new LLM calls before repairs; "
            f"max_llm_calls is {max_llm_calls}"
        )
    if pending_generations and client is None:
        raise ContractError("selected work requires a Hutch chat client")

    completed_now = 0
    resumed = 0
    local_calls = 0
    hutch_calls = 0
    results: dict[str, dict[str, Any]] = {}
    if progress_callback is not None:
        progress_callback({
            "event": "started",
            "total_work_items": len(selected),
            "completed_work_items": 0,
            "pending_generative_calls": pending_generations,
        })
    for work_id in selected:
        work = indexed[work_id]
        if store.completed(work_id):
            result = store.load_result(work_id)
            prior_for_validation = {
                key: value
                for key, value in results.items()
                if value.get("schema_version") == NEIGHBORHOOD_RESULT_SCHEMA_VERSION
            }
            validate_excavation_result(plan, result, prior_results=prior_for_validation)
            results[work_id] = result
            resumed += 1
            if progress_callback is not None:
                progress_callback({
                    "event": "resumed",
                    "work_id": work_id,
                    "work_kind": work.get("work_kind"),
                    "total_work_items": len(selected),
                    "completed_work_items": resumed + completed_now,
                    "resumed_work_items": resumed,
                    "local_correspondence_calls": local_calls,
                    "hutch_llm_attempts": hutch_calls,
                })
            continue
        missing = [
            dependency for dependency in work.get("depends_on", []) if dependency not in results
        ]
        if missing:
            raise ContractError(
                f"work {work_id} cannot run before dependencies: {', '.join(missing)}"
            )
        if progress_callback is not None:
            progress_callback({
                "event": "running",
                "work_id": work_id,
                "work_kind": work.get("work_kind"),
                "total_work_items": len(selected),
                "completed_work_items": resumed + completed_now,
                "resumed_work_items": resumed,
                "local_correspondence_calls": local_calls,
                "hutch_llm_attempts": hutch_calls,
            })
        try:
            if work["work_kind"] == "correspondence":
                result, receipt = correspondence_result(work)
                validation = validate_excavation_result(plan, result)
                receipts = [receipt]
                local_calls += 1
            else:
                assert client is not None
                result, validation, receipts = _execute_generative(
                    work,
                    plan,
                    results,
                    client,
                    repair_attempts=repair_attempts,
                )
                hutch_calls += len(receipts)
            store.record_success(work, result, validation, receipts)
            results[work_id] = result
            completed_now += 1
            if progress_callback is not None:
                progress_callback({
                    "event": "completed",
                    "work_id": work_id,
                    "work_kind": work.get("work_kind"),
                    "total_work_items": len(selected),
                    "completed_work_items": resumed + completed_now,
                    "resumed_work_items": resumed,
                    "local_correspondence_calls": local_calls,
                    "hutch_llm_attempts": hutch_calls,
                })
        except Exception as exc:
            store.record_failure(work, exc, int(getattr(exc, "attempts", 1)))
            if progress_callback is not None:
                progress_callback({
                    "event": "failed",
                    "work_id": work_id,
                    "work_kind": work.get("work_kind"),
                    "total_work_items": len(selected),
                    "completed_work_items": resumed + completed_now,
                    "error": str(exc)[:2_000],
                })
            raise

    by_kind = {
        kind: sum(indexed[work_id].get("work_kind") == kind for work_id in selected)
        for kind in (
            "source_motifs",
            "correspondence",
            "neighborhood_synthesis",
            "bridge_synthesis",
        )
    }
    summary = {
        "schema_version": "rvbbit.business-topology.execution-summary.v1",
        "worker_version": WORKER_VERSION,
        "plan_sha256": store.plan_sha256,
        "output_dir": str(store.root),
        "selected_work_items": len(selected),
        "work_by_kind": by_kind,
        "completed_now": completed_now,
        "resumed": resumed,
        "local_correspondence_calls": local_calls,
        "hutch_llm_attempts": hutch_calls,
        "materialized_topology": False,
        "submitted_proposals": False,
    }
    if progress_callback is not None:
        progress_callback({
            "event": "finished",
            "total_work_items": len(selected),
            "completed_work_items": resumed + completed_now,
            "resumed_work_items": resumed,
            "local_correspondence_calls": local_calls,
            "hutch_llm_attempts": hutch_calls,
        })
    return summary


def execution_preview(plan: Mapping[str, Any], selected_work_ids: Sequence[str]) -> dict[str, Any]:
    indexed = work_index(plan)
    return {
        "schema_version": "rvbbit.business-topology.execution-preview.v1",
        "selected_work_items": len(selected_work_ids),
        "work_by_kind": {
            kind: sum(indexed[work_id].get("work_kind") == kind for work_id in selected_work_ids)
            for kind in (
                "source_motifs",
                "correspondence",
                "neighborhood_synthesis",
                "bridge_synthesis",
            )
        },
        "generative_calls_before_repairs": sum(
            indexed[work_id].get("work_kind") in _GENERATIVE_KINDS for work_id in selected_work_ids
        ),
        "materialized_topology": False,
        "submitted_proposals": False,
    }
