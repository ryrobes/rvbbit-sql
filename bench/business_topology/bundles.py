"""Stage validated excavation skeletons as non-materializing proposal bundles."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import ContractError
from .results import (
    BRIDGE_RESULT_SCHEMA_VERSION,
    NEIGHBORHOOD_RESULT_SCHEMA_VERSION,
    validate_excavation_result,
)
from .worker import (
    EXECUTION_MANIFEST_SCHEMA_VERSION,
    WORK_RECEIPT_SCHEMA_VERSION,
    work_index,
)


STAGEABLE_WORK_KINDS = frozenset({"neighborhood_synthesis", "bridge_synthesis"})


def _sha256(value: Any) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(rendered.encode()).hexdigest()


def _result_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError("execution manifest result path escapes its private directory") from exc
    if not path.is_file():
        raise ContractError(f"completed execution result is missing: {relative}")
    return path


def _source_index(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for work in plan.get("work_items", []):
        if work.get("work_kind") != "source_motifs":
            continue
        source_keys = work.get("source_keys", [])
        if len(source_keys) != 1:
            raise ContractError("source motif work must identify exactly one source")
        source_key = str(source_keys[0])
        packet = work.get("input_packet", {})
        source = packet.get("source", {})
        relation = packet.get("relation_context", {})
        if not isinstance(source, Mapping) or not isinstance(relation, Mapping):
            raise ContractError("source motif packet lacks source context")
        sources[source_key] = {
            "source_key": source_key,
            "source": dict(source),
            "name": str(relation.get("name") or source_key),
            "motif_work_id": str(work["work_id"]),
        }
    return sources


def bundle_context(plan: Mapping[str, Any], work: Mapping[str, Any]) -> dict[str, Any]:
    """Build a readable, value-free context projection for DataRabbit."""

    sources = _source_index(plan)
    source_keys = [str(value) for value in work.get("source_keys", [])]
    missing = sorted(set(source_keys) - set(sources))
    if missing:
        raise ContractError(f"bundle sources lack motif context: {', '.join(missing)}")

    source_rows = [sources[source_key] for source_key in source_keys]
    populations: list[dict[str, Any]] = []
    source_inputs = work.get("input_packet", {}).get("source_inputs", [])
    if work.get("work_kind") == "neighborhood_synthesis":
        if not isinstance(source_inputs, list):
            raise ContractError("neighborhood synthesis source_inputs must be an array")
        for source_input in source_inputs:
            if not isinstance(source_input, Mapping):
                raise ContractError("neighborhood source input must be an object")
            source_key = str(source_input.get("source_key") or "")
            source_row = sources.get(source_key)
            if source_row is None:
                raise ContractError("neighborhood population references an unknown source")
            locator = source_row["source"]
            source_label = (
                ".".join(
                    str(value)
                    for value in (locator.get("schema"), locator.get("relation"))
                    if value
                )
                or source_row["name"]
            )
            for population_id in source_input.get("population_ids", []):
                population_id = str(population_id)
                field_name = (
                    population_id.rsplit("#field:", 1)[1]
                    if "#field:" in population_id
                    else population_id
                )
                populations.append(
                    {
                        "population_id": population_id,
                        "source_key": source_key,
                        "source_label": source_label,
                        "field_name": field_name,
                        "display_name": f"{source_label}.{field_name}",
                    }
                )

    return {
        "schema_version": "rvbbit.business-topology.bundle-context.v1",
        "scope": dict(work.get("input_packet", {}).get("scope", {})),
        "sources": source_rows,
        "populations": sorted(populations, key=lambda item: item["population_id"]),
        "dependency_work_ids": list(work.get("depends_on", [])),
        "privacy": {
            "raw_values": False,
            "value_hashes": False,
            "contains_profile_packets": False,
        },
    }


def load_stageable_receipts(
    plan: Mapping[str, Any],
    output_dir: str | Path,
    *,
    include_bridges: bool = False,
) -> list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]]:
    """Load and revalidate completed synthesis receipts from one exact plan."""

    root = Path(output_dir)
    manifest_path = root / "execution.json"
    if not manifest_path.is_file():
        raise ContractError("execution directory has no execution.json manifest")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != EXECUTION_MANIFEST_SCHEMA_VERSION:
        raise ContractError("execution directory has an unsupported manifest")
    plan_sha256 = _sha256(plan)
    if manifest.get("plan_sha256") != plan_sha256:
        raise ContractError("execution directory belongs to a different excavation plan")

    indexed = work_index(plan)
    prior_results: dict[str, dict[str, Any]] = {}
    receipts: list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]] = []
    completed = manifest.get("work", {})
    if not isinstance(completed, Mapping):
        raise ContractError("execution manifest work index must be an object")

    # Load validated neighborhood results first so bridge node references can
    # be rechecked even when only bridge bundles are being added later.
    ordered = sorted(
        completed.items(),
        key=lambda pair: (
            0 if pair[1].get("work_kind") == "neighborhood_synthesis" else 1,
            pair[0],
        ),
    )
    for work_id, entry in ordered:
        if not isinstance(entry, Mapping) or entry.get("status") != "completed":
            continue
        work = indexed.get(str(work_id))
        if work is None:
            raise ContractError(f"execution manifest references work outside plan: {work_id}")
        work_kind = str(work.get("work_kind"))
        if work_kind not in STAGEABLE_WORK_KINDS:
            continue
        relative = entry.get("result_file")
        if not isinstance(relative, str) or not relative:
            raise ContractError(f"completed synthesis work lacks result_file: {work_id}")
        receipt = json.loads(_result_file(root, relative).read_text())
        if receipt.get("schema_version") != WORK_RECEIPT_SCHEMA_VERSION:
            raise ContractError(f"work {work_id} has an unsupported receipt")
        if receipt.get("plan_sha256") != plan_sha256 or receipt.get("work_id") != work_id:
            raise ContractError(f"work {work_id} receipt identity does not match the plan")
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise ContractError(f"work {work_id} receipt has no result object")
        prior_for_validation = {
            key: value
            for key, value in prior_results.items()
            if value.get("schema_version") == NEIGHBORHOOD_RESULT_SCHEMA_VERSION
        }
        validation = validate_excavation_result(
            plan,
            result,
            prior_results=prior_for_validation,
        )
        if receipt.get("validation") != validation:
            raise ContractError(f"work {work_id} persisted validation no longer reproduces")
        if result.get("schema_version") == NEIGHBORHOOD_RESULT_SCHEMA_VERSION:
            prior_results[str(work_id)] = result
            receipts.append((work, receipt, bundle_context(plan, work)))
        elif result.get("schema_version") == BRIDGE_RESULT_SCHEMA_VERSION and include_bridges:
            receipts.append((work, receipt, bundle_context(plan, work)))
    return receipts


def stage_execution_bundles(
    conn: Any,
    plan: Mapping[str, Any],
    output_dir: str | Path,
    *,
    include_bridges: bool = False,
    proposed_by: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stage validated bundles in one transaction without topology promotion."""

    candidates = load_stageable_receipts(
        plan,
        output_dir,
        include_bridges=include_bridges,
    )
    by_kind = {
        kind: sum(work.get("work_kind") == kind for work, _, _ in candidates)
        for kind in sorted(STAGEABLE_WORK_KINDS)
    }
    if dry_run:
        return {
            "stageable_bundles": len(candidates),
            "by_kind": by_kind,
            "staged": 0,
            "dry_run": True,
            "materialized_topology": False,
        }

    bundle_ids: list[str] = []
    try:
        with conn.cursor() as cursor:
            for work, receipt, context in candidates:
                scope_kind = (
                    "excavation_unit"
                    if work.get("work_kind") == "neighborhood_synthesis"
                    else "boundary_link"
                )
                cursor.execute(
                    """
                    SELECT rvbbit.business_topology_stage_proposal_bundle(
                        %s::jsonb,%s,%s,%s::text[],%s::jsonb,%s,NULL
                    )
                    """,
                    (
                        json.dumps(receipt),
                        scope_kind,
                        str(work["scope_id"]),
                        list(work.get("source_keys", [])),
                        json.dumps(context),
                        proposed_by,
                    ),
                )
                bundle_ids.append(str(cursor.fetchone()[0]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "stageable_bundles": len(candidates),
        "by_kind": by_kind,
        "staged": len(bundle_ids),
        "bundle_ids": bundle_ids,
        "dry_run": False,
        "materialized_topology": False,
    }
