"""Merge multiple Rvbbit route profiles into one profile.

Profiles are rebuilt from their raw observations so duplicate shape keys across
suites/scales are resolved by the same median winner logic as training.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from rvbbit_route_model import (
    candidate_enabled,
    choose_fastest,
    extract_sql_features,
    min_confidence_for_candidate,
    observation_candidate_ms,
    path_for_candidate,
    ratio_text_many,
    ROUTABLE_CANDIDATES,
    shape_key,
    speedup_confidence_many,
)


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _allow_pg_heap_choices() -> bool:
    import os

    raw = os.environ.get("RVBBIT_ROUTE_ALLOW_PG_HEAP_CHOICES")
    if raw is None:
        return True
    return raw.lower() in {"1", "true", "yes", "on"}


def _choice_candidates(candidate_ms: dict[str, float]) -> dict[str, float]:
    allowed = set(ROUTABLE_CANDIDATES)
    if not _allow_pg_heap_choices():
        allowed.discard("pg_rowstore")
    return {
        candidate: ms
        for candidate, ms in candidate_ms.items()
        if candidate in allowed and candidate_enabled(candidate)
    }


def _passes_choice_threshold(candidate: str | None, confidence: float, min_gain_pct: float) -> bool:
    return confidence >= max(min_gain_pct, min_confidence_for_candidate(candidate))


def _refresh_observation_features(obs: dict[str, Any]) -> dict[str, Any]:
    features = dict(obs.get("features") or {})
    normalized_sql = features.get("normalized_sql")
    if normalized_sql:
        refreshed = extract_sql_features(normalized_sql)
        for key, value in refreshed.items():
            if key in {"fixed_contains_like_count"} and features.get(key):
                continue
            if key in features and value in {None, "unknown"}:
                continue
            features[key] = value
    features["shape_key"] = shape_key(features)
    obs = dict(obs)
    obs["features"] = features
    obs["shape_key"] = features["shape_key"]
    return obs


def build_profile_from_observations(
    observations: list[dict[str, Any]],
    min_gain_pct: float,
    min_observations: int,
    source_profiles: list[str],
) -> dict[str, Any]:
    refreshed_observations: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        obs = _refresh_observation_features(obs)
        refreshed_observations.append(obs)
        grouped.setdefault(obs["shape_key"], []).append(obs)

    entries: dict[str, Any] = {}
    rejected: dict[str, Any] = {}
    for key, rows in sorted(grouped.items()):
        for row in rows:
            row.setdefault("candidate_ms", observation_candidate_ms(row))
        candidates = sorted(set().union(*(row["candidate_ms"].keys() for row in rows)))
        candidate_medians = {
            candidate: _median(
                [float(row["candidate_ms"][candidate]) for row in rows if candidate in row["candidate_ms"]]
            )
            for candidate in candidates
        }
        winner_candidate, _best_ms, _next_ms = choose_fastest(candidate_medians)
        winner = path_for_candidate(winner_candidate) or str(winner_candidate)
        choice_medians = _choice_candidates(candidate_medians)
        choice_candidate, _route_best_ms, _route_next_ms = choose_fastest(choice_medians)
        choice = path_for_candidate(choice_candidate) or str(choice_candidate)
        confidence = speedup_confidence_many(choice_medians)
        sample_qids = [row["qid"] for row in rows]
        scales = sorted(set(row.get("scale_rows") for row in rows if row.get("scale_rows") is not None))
        sources = sorted(set(row.get("source_results", "unknown") for row in rows))
        suites = sorted(set(row.get("suite", "unknown") for row in rows))
        entry = {
            "choice": choice,
            "confidence": round(confidence, 4),
            "observations": len(rows),
            "native_ms_median": (
                round(candidate_medians["rvbbit_native"], 4)
                if "rvbbit_native" in candidate_medians
                else None
            ),
            "duck_ms_median": (
                round(candidate_medians["duck_vector"], 4)
                if "duck_vector" in candidate_medians
                else None
            ),
            "duck_hive_ms_median": (
                round(candidate_medians["duck_hive"], 4)
                if "duck_hive" in candidate_medians
                else None
            ),
            "datafusion_mem_ms_median": (
                round(candidate_medians["datafusion_mem"], 4)
                if "datafusion_mem" in candidate_medians
                else None
            ),
            "datafusion_ms_median": (
                round(candidate_medians["datafusion_vector"], 4)
                if "datafusion_vector" in candidate_medians
                else None
            ),
            "datafusion_hive_ms_median": (
                round(candidate_medians["datafusion_hive"], 4)
                if "datafusion_hive" in candidate_medians
                else None
            ),
            "pg_ms_median": (
                round(candidate_medians["pg_rowstore"], 4)
                if "pg_rowstore" in candidate_medians
                else None
            ),
            "oracle_choice": winner,
            "candidate_medians": [
                {
                    "candidate": candidate,
                    "median_ms": round(ms, 4),
                    "observations": sum(1 for row in rows if candidate in row["candidate_ms"]),
                }
                for candidate, ms in sorted(candidate_medians.items(), key=lambda item: item[1])
            ],
            "reason": f"{ratio_text_many(choice_medians, choice)} over {len(rows)} observation(s)",
            "sample_qids": sample_qids,
            "sample_scales": scales,
            "source_results": sources,
            "suites": suites,
            "representative_features": rows[0]["features"],
        }
        if (
            len(rows) >= min_observations
            and _passes_choice_threshold(choice_candidate, confidence, min_gain_pct)
        ):
            entries[key] = entry
        else:
            rejected[key] = entry

    return {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "suite": "combined",
        "source_profiles": source_profiles,
        "native_system": "rvbbit_native_forced",
        "duck_system": "rvbbit_duck_forced",
        "duck_hive_system": "rvbbit_duck_hive_forced",
        "datafusion_mem_system": "rvbbit_datafusion_mem_forced",
        "datafusion_system": "rvbbit_datafusion_forced",
        "datafusion_hive_system": "rvbbit_datafusion_hive_forced",
        "pg_heap_system": "rvbbit_pg_heap_forced",
        "pg_heap_observation_only": not _allow_pg_heap_choices(),
        "min_gain_pct": min_gain_pct,
        "min_observations": min_observations,
        "entries": entries,
        "rejected": rejected,
        "observations": refreshed_observations,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--output", default="/bench/rvbbit_route_profile.json")
    parser.add_argument("--min-gain-pct", type=float, default=0.05)
    parser.add_argument("--min-observations", type=int, default=1)
    args = parser.parse_args()

    output_path = Path(args.output).expanduser().resolve()
    input_paths = [Path(profile).expanduser().resolve() for profile in args.profile]
    if output_path in input_paths:
        raise SystemExit(
            "refusing to merge a route profile into itself; choose an output path "
            "that is not also listed as --profile"
        )

    observations: list[dict[str, Any]] = []
    for profile_path in args.profile:
        with open(profile_path) as f:
            profile = json.load(f)
        suite = profile.get("suite", "unknown")
        for obs in profile.get("observations", []):
            obs = dict(obs)
            obs.setdefault("suite", suite)
            obs["source_profile"] = profile_path
            observations.append(obs)

    merged = build_profile_from_observations(
        observations=observations,
        min_gain_pct=args.min_gain_pct,
        min_observations=args.min_observations,
        source_profiles=args.profile,
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(merged, f, indent=2, sort_keys=True)

    print(f"wrote {args.output}")
    print(f"input profiles: {len(args.profile)}")
    print(f"observations: {len(merged['observations'])}")
    print(f"accepted shapes: {len(merged['entries'])}")
    print(f"rejected shapes: {len(merged['rejected'])}")


if __name__ == "__main__":
    main()
