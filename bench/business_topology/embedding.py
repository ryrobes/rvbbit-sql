"""Provider-neutral embedding inputs and bounded neighbor evidence.

The topology evaluator does not own an embedding provider.  It emits stable,
privacy-checked population text and accepts opaque numeric vectors back from a
local or customer-controlled adapter.  Exact blockwise neighbors are suitable
for evaluation corpora; production discovery can replace this implementation
with an ANN index without changing the evidence contract.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from .candidates import pair_key
from .contracts import POPULATION_KINDS, POPULATION_ROLES, ContractError, validate_corpus
from .packets import packet_text, stable_hash


def make_embedding_inputs(
    corpus: Mapping[str, Any],
    *,
    max_text_chars: int = 12_000,
    include_context: bool = False,
    roles: Sequence[str] | None = None,
    channels: Sequence[str] = ("combined",),
    kinds: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Render bounded model inputs without coupling to a provider or customer."""

    validate_corpus(corpus)
    if max_text_chars < 512:
        raise ValueError("max_text_chars must be at least 512")
    requested_roles = set(roles or [])
    unknown_roles = requested_roles - set(POPULATION_ROLES)
    if unknown_roles:
        raise ValueError(f"unknown structural roles: {sorted(unknown_roles)}")
    requested_kinds = set(kinds or [])
    unknown_kinds = requested_kinds - set(POPULATION_KINDS)
    if unknown_kinds:
        raise ValueError(f"unknown population kinds: {sorted(unknown_kinds)}")
    requested_channels = tuple(dict.fromkeys(channels))
    if not requested_channels or set(requested_channels) - {"combined", "focus", "context"}:
        raise ValueError("channels must contain combined, focus, or context")
    result: list[dict[str, Any]] = []
    for item in corpus.get("populations", []):
        packet = item["packet"]
        population_kind = packet.get("population", {}).get("kind")
        if requested_kinds and population_kind not in requested_kinds:
            continue
        if not requested_kinds and not include_context and population_kind == "record_context":
            continue
        field = packet.get("field")
        role_hints = (
            {str(role) for role in field.get("role_hints", []) if isinstance(role, str)}
            if isinstance(field, Mapping)
            else set()
        )
        if requested_roles and not requested_roles.intersection(role_hints):
            continue
        for channel in requested_channels:
            rendered = packet_text(packet, channel=channel)
            original_chars = len(rendered)
            if original_chars > max_text_chars:
                rendered = rendered[: max_text_chars - 32] + "\n[bounded context truncated]"
            result.append(
                {
                    "population_id": item["population_id"],
                    "channel": channel,
                    "text": rendered,
                    "text_hash": hashlib.sha256(rendered.encode()).hexdigest(),
                    "source_key": stable_hash(packet.get("source", {}))[:20],
                    "role_hints": sorted(role_hints),
                    "original_chars": original_chars,
                    "truncated": original_chars > max_text_chars,
                }
            )
    return result


def budget_embedding_batches(
    inputs: Sequence[Mapping[str, Any]],
    *,
    max_items: int = 16,
    max_chars: int = 24_000,
) -> list[list[Mapping[str, Any]]]:
    """Group inputs by both item count and payload size.

    Model servers commonly expose a nominal batch size but still reject a batch
    whose combined token or byte payload is too large.  Character budgeting is
    deliberately provider-neutral and conservative.
    """

    if max_items < 1:
        raise ValueError("max_items must be positive")
    if max_chars < 512:
        raise ValueError("max_chars must be at least 512")
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    current_chars = 0
    for item in inputs:
        text = item.get("text")
        if not isinstance(text, str):
            raise ContractError("embedding input text must be a string")
        text_chars = len(text)
        if text_chars > max_chars:
            raise ContractError(
                "one embedding input exceeds max_chars; lower max_text_chars when rendering"
            )
        if current and (len(current) >= max_items or current_chars + text_chars > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += text_chars
    if current:
        batches.append(current)
    return batches


def embed_postgres_shadow(
    conn: Any,
    inputs: Sequence[Mapping[str, Any]],
    *,
    specialist: str = "embed",
    mode: str = "document",
    max_batch_items: int = 16,
    max_batch_chars: int = 24_000,
    statement_timeout_ms: int = 300_000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Embed through pg_rvbbit, then unconditionally roll cache writes back.

    A failed batch is recursively bisected under savepoints. This isolates one
    model-server rejection without losing successful neighbors or leaving the
    transaction aborted. The caller decides whether a partial vector set is
    acceptable; the CLI fails closed by default.
    """

    batches = budget_embedding_batches(
        inputs,
        max_items=max_batch_items,
        max_chars=max_batch_chars,
    )
    vectors: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    savepoint_counter = 0

    def embed_batch(cursor: Any, batch: Sequence[Mapping[str, Any]]) -> None:
        nonlocal savepoint_counter
        savepoint_counter += 1
        savepoint = f"bt_embedding_{savepoint_counter}"
        cursor.execute(f"SAVEPOINT {savepoint}".encode())  # type: ignore[arg-type]
        texts = [str(item["text"]) for item in batch]
        try:
            cursor.execute(
                "SELECT rvbbit.embed_batch(%s::text[],%s,%s)",
                (texts, specialist, mode),
            )
            cursor.fetchone()
            cursor.execute(
                """
                SELECT input_ordinality,rvbbit.embed(input_text,%s,%s)
                FROM unnest(%s::text[]) WITH ORDINALITY AS input(input_text,input_ordinality)
                ORDER BY input_ordinality
                """,
                (specialist, mode, texts),
            )
            embedded = cursor.fetchall()
            if len(embedded) != len(batch):
                raise RuntimeError("embedding adapter returned a different row count")
            cursor.execute(f"RELEASE SAVEPOINT {savepoint}".encode())  # type: ignore[arg-type]
            for item, (_, vector) in zip(batch, embedded, strict=True):
                vectors.append(
                    {
                        "population_id": item["population_id"],
                        "channel": item.get("channel", "combined"),
                        "text_hash": item["text_hash"],
                        "embedding": vector,
                    }
                )
        except Exception as exc:
            cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}".encode())  # type: ignore[arg-type]
            cursor.execute(f"RELEASE SAVEPOINT {savepoint}".encode())  # type: ignore[arg-type]
            if len(batch) > 1:
                midpoint = len(batch) // 2
                embed_batch(cursor, batch[:midpoint])
                embed_batch(cursor, batch[midpoint:])
            else:
                failures.append(
                    {
                        "population_id": str(batch[0]["population_id"]),
                        "error_class": type(exc).__name__,
                    }
                )

    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout={int(statement_timeout_ms)}".encode())  # type: ignore[arg-type]
            for batch in batches:
                embed_batch(cursor, batch)
    finally:
        # Embedding cache population is useful in production, but evaluation
        # against a live customer must remain a non-persisting shadow run.
        conn.rollback()

    vectors.sort(key=lambda item: str(item["population_id"]))
    return vectors, {
        "inputs": len(inputs),
        "vectors": len(vectors),
        "failures": failures,
        "initial_batches": len(batches),
        "max_batch_items": max_batch_items,
        "max_batch_chars": max_batch_chars,
        "specialist": specialist,
        "mode": mode,
        "transaction_rolled_back": True,
    }


def _validated_vector_channels(
    corpus: Mapping[str, Any],
    vector_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[str], dict[str, np.ndarray], list[str]]:
    populations = {
        str(item["population_id"]): item
        for item in corpus.get("populations", [])
        if item.get("packet", {}).get("population", {}).get("kind") != "record_context"
    }
    vectors_by_channel: dict[str, dict[str, list[float]]] = {}
    dimensions: dict[str, int] = {}
    for row in vector_rows:
        if not isinstance(row, Mapping):
            raise ContractError("embedding vector rows must be objects")
        population_id = row.get("population_id")
        channel = row.get("channel", "combined")
        vector = row.get("embedding", row.get("vector"))
        if not isinstance(population_id, str) or population_id not in populations:
            raise ContractError("embedding row references a population outside the corpus")
        if not isinstance(channel, str) or not channel:
            raise ContractError("embedding row channel must be a non-empty string")
        channel_vectors = vectors_by_channel.setdefault(channel, {})
        if population_id in channel_vectors:
            raise ContractError(f"duplicate {channel} embedding row for {population_id}")
        if not isinstance(vector, list) or not vector:
            raise ContractError(f"embedding for {population_id} must be a non-empty array")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector):
            raise ContractError(f"embedding for {population_id} must contain finite numbers")
        expected_dim = dimensions.setdefault(channel, len(vector))
        if len(vector) != expected_dim:
            raise ContractError(f"all {channel} embedding vectors must have the same dimension")
        channel_vectors[population_id] = [float(value) for value in vector]
    if not vectors_by_channel:
        raise ContractError("at least two population embeddings are required")
    id_sets = {channel: set(vectors) for channel, vectors in vectors_by_channel.items()}
    first_ids = next(iter(id_sets.values()))
    if len(first_ids) < 2:
        raise ContractError("at least two population embeddings are required")
    if any(ids != first_ids for ids in id_sets.values()):
        raise ContractError("every embedding channel must cover the same populations")

    ids = sorted(first_ids)
    matrices: dict[str, np.ndarray] = {}
    for channel, channel_vectors in vectors_by_channel.items():
        matrix = np.asarray(
            [channel_vectors[population_id] for population_id in ids], dtype=np.float32
        )
        norms = np.linalg.norm(matrix, axis=1)
        if np.any(norms == 0):
            raise ContractError(f"zero-length {channel} embedding vectors are not valid")
        matrices[channel] = matrix / norms[:, None]
    source_keys = [
        stable_hash(populations[population_id]["packet"].get("source", {}))[:20]
        for population_id in ids
    ]
    return ids, matrices, source_keys


def resolve_channel_weights(
    channels: Sequence[str],
    requested: Mapping[str, float] | None,
) -> dict[str, float]:
    if requested is None:
        if set(channels) == {"focus", "context"}:
            weights = {"focus": 0.55, "context": 0.45}
        else:
            weights = {channel: 1.0 for channel in channels}
    else:
        if set(requested) != set(channels):
            raise ValueError("channel_weights must name every and only supplied vector channel")
        weights = {channel: float(requested[channel]) for channel in channels}
    if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
        raise ValueError("channel weights must be finite and non-negative")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("at least one channel weight must be positive")
    return {channel: weight / total for channel, weight in weights.items()}


def build_embedding_evidence(
    corpus: Mapping[str, Any],
    vector_rows: Iterable[Mapping[str, Any]],
    *,
    top_k: int = 32,
    minimum_similarity: float = 0.35,
    block_size: int = 256,
    channel_weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Build bounded cross-source nearest-neighbor pair evidence."""

    validate_corpus(corpus)
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if block_size < 1:
        raise ValueError("block_size must be positive")
    if not -1.0 <= minimum_similarity <= 1.0:
        raise ValueError("minimum_similarity must be between -1 and 1")
    ids, matrices, source_keys = _validated_vector_channels(corpus, vector_rows)
    weights = resolve_channel_weights(sorted(matrices), channel_weights)
    source_array = np.asarray(source_keys, dtype=object)
    evidence: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), block_size):
        stop = min(start + block_size, len(ids))
        similarities = np.zeros((stop - start, len(ids)), dtype=np.float32)
        for channel, matrix in matrices.items():
            similarities += weights[channel] * (matrix[start:stop] @ matrix.T)
        for local_index, population_index in enumerate(range(start, stop)):
            scores = similarities[local_index]
            scores[source_array == source_keys[population_index]] = -np.inf
            available = int(np.isfinite(scores).sum())
            if not available:
                continue
            count = min(top_k, available)
            neighbor_indices = np.argpartition(scores, -count)[-count:]
            neighbor_indices = sorted(
                neighbor_indices,
                key=lambda index: (-float(scores[index]), ids[index]),
            )
            for neighbor_index in neighbor_indices:
                similarity = float(scores[neighbor_index])
                if not math.isfinite(similarity) or similarity < minimum_similarity:
                    continue
                left, right = sorted((ids[population_index], ids[neighbor_index]))
                key = pair_key(left, right)
                previous = evidence.get(key)
                if (
                    previous is None
                    or similarity > previous["local_evidence"]["embedding_similarity"]
                ):
                    evidence[key] = {
                        "left_population_id": left,
                        "right_population_id": right,
                        "local_evidence": {
                            "embedding_similarity": round(similarity, 8),
                        },
                    }
    return sorted(
        evidence.values(),
        key=lambda item: (
            -item["local_evidence"]["embedding_similarity"],
            item["left_population_id"],
            item["right_population_id"],
        ),
    )
