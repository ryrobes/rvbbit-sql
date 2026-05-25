"""Evaluate Rvbbit route profiles and auto-routing benchmark runs."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rvbbit_route_model import RouteProfile, observation_candidate_ms, path_for_candidate


def _load_json(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _result_ms(query: dict[str, Any], system: str) -> float | None:
    value = query.get("results", {}).get(system)
    if not value or value[0] is None:
        return None
    return float(value[0])


def _route_path(status: str | None) -> str | None:
    if not status:
        return None
    if status.startswith("native"):
        return "native"
    if status.startswith("duck_hive"):
        return "duck_hive"
    if status.startswith("duck"):
        return "duck"
    if status.startswith("datafusion_hive"):
        return "datafusion_hive"
    if status.startswith("datafusion"):
        return "datafusion"
    if status.startswith("pg_heap") or status.startswith("pg_rowstore"):
        return "pg_heap"
    return None


def _fmt_ms(value: float) -> str:
    if value < 1:
        return f"{value * 1000:.0f}us"
    if value < 1000:
        return f"{value:.1f}ms"
    return f"{value / 1000:.2f}s"


def evaluate_profile(path: str, top: int) -> None:
    profile = _load_json(path)
    entries = profile.get("entries", {})
    observations = profile.get("observations", [])
    rejected = profile.get("rejected", {})
    route_profile = RouteProfile(profile, source_path=path)

    oracle_sum = 0.0
    profile_sum = 0.0
    covered = 0
    misses: list[tuple[float, float, dict[str, Any], dict[str, Any]]] = []
    conflicts: list[tuple[str, Counter[str], list[dict[str, Any]]]] = []
    by_shape: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runtime_choices: Counter[str | None] = Counter()

    for obs in observations:
        by_shape[obs["shape_key"]].append(obs)
        candidate_ms = observation_candidate_ms(obs)
        if len(candidate_ms) < 2:
            continue
        oracle_candidate, oracle = min(candidate_ms.items(), key=lambda item: item[1])
        oracle_path = path_for_candidate(oracle_candidate) or oracle_candidate
        oracle_sum += oracle
        decision = route_profile.choose(obs.get("features") or {})
        entry = decision.entry if decision else None
        if decision:
            covered += 1
            choice = decision.path
        else:
            choice = "duck"
        runtime_choices[choice if decision else None] += 1
        chosen_candidate = {
            "native": "rvbbit_native",
            "duck": "duck_vector",
            "duck_hive": "duck_hive",
            "datafusion": "datafusion_vector",
            "datafusion_hive": "datafusion_hive",
            "pg_heap": "pg_rowstore",
        }.get(choice)
        chosen = candidate_ms.get(chosen_candidate or "", candidate_ms.get("duck_vector", oracle))
        profile_sum += chosen
        if decision and choice != oracle_path:
            ratio = chosen / oracle if oracle > 0 else math.inf
            miss_entry = dict(entry or {})
            miss_entry.setdefault("choice", choice)
            miss_entry.setdefault("confidence", decision.confidence)
            miss_entry.setdefault("reason", decision.reason)
            misses.append((chosen - oracle, ratio, obs, miss_entry))

    for key, rows in by_shape.items():
        winners = Counter(row.get("winner") for row in rows)
        if len(winners) > 1:
            conflicts.append((key, winners, rows))

    print(f"profile: {path}")
    print(f"  suite              : {profile.get('suite')}")
    print(f"  observations       : {len(observations)}")
    print(f"  accepted shapes    : {len(entries)}")
    print(f"  rejected shapes    : {len(rejected)}")
    print(f"  covered observations: {covered} ({covered / len(observations) * 100:.1f}%)" if observations else "  covered observations: 0")
    print(f"  accepted choices   : {dict(Counter(e.get('choice') for e in entries.values()))}")
    print(f"  runtime choices    : {dict(runtime_choices)}")
    print(f"  observation winners: {dict(Counter(o.get('winner') for o in observations))}")
    if oracle_sum:
        regret = profile_sum - oracle_sum
        print(f"  oracle sum         : {_fmt_ms(oracle_sum)}")
        print(f"  profile sum        : {_fmt_ms(profile_sum)}")
        print(f"  profile regret     : {_fmt_ms(regret)} ({(profile_sum / oracle_sum - 1) * 100:.2f}%)")
    print(f"  conflicting shapes : {len(conflicts)}")

    if misses:
        print("\nworst covered misses:")
        for regret, ratio, obs, entry in sorted(misses, reverse=True)[:top]:
            print(
                "  "
                f"{obs.get('suite','?')} {obs.get('qid','?')} "
                f"scale={obs.get('scale_rows')} "
                f"winner={obs['winner']} profile={entry['choice']} "
                f"native={_fmt_ms(float(obs['native_ms'])) if obs.get('native_ms') is not None else '-'} "
                f"duck={_fmt_ms(float(obs['duck_ms'])) if obs.get('duck_ms') is not None else '-'} "
                f"duck_hive={_fmt_ms(float(obs['duck_hive_ms'])) if obs.get('duck_hive_ms') is not None else '-'} "
                f"datafusion={_fmt_ms(float(obs['datafusion_ms'])) if obs.get('datafusion_ms') is not None else '-'} "
                f"datafusion_hive={_fmt_ms(float(obs['datafusion_hive_ms'])) if obs.get('datafusion_hive_ms') is not None else '-'} "
                f"pg_heap={_fmt_ms(float(obs['pg_ms'])) if obs.get('pg_ms') is not None else '-'} "
                f"regret={_fmt_ms(regret)} ratio={ratio:.2f} "
                f"conf={entry.get('confidence')}"
            )

    if conflicts:
        print("\nlargest conflicting shapes:")
        for key, winners, rows in sorted(conflicts, key=lambda item: -len(item[2]))[:top]:
            sample = ", ".join(
                f"{row.get('suite','?')}:{row.get('qid','?')}:{row.get('winner')}"
                for row in rows[:8]
            )
            print(f"  {dict(winners)} obs={len(rows)} sample={sample}")
            print(f"    {key}")


def evaluate_results(path: str, top: int) -> None:
    data = _load_json(path)
    queries = data.get("queries", [])
    systems = set(data.get("systems", []))
    required = {"rvbbit", "rvbbit_native", "rvbbit_duck_forced"}
    missing = sorted(required - systems)
    if missing:
        raise SystemExit(f"{path} is missing systems needed for route evaluation: {', '.join(missing)}")

    oracle_sum = 0.0
    auto_sum = 0.0
    misses = []
    route_counts: Counter[str | None] = Counter()
    for query in queries:
        qid = query.get("qid")
        auto = _result_ms(query, "rvbbit")
        native = _result_ms(query, "rvbbit_native")
        duck = _result_ms(query, "rvbbit_duck_forced")
        datafusion = _result_ms(query, "rvbbit_datafusion_forced")
        pg_heap = (
            _result_ms(query, "rvbbit_pg_heap_forced")
            or _result_ms(query, "rvbbit_pg_heap")
            or _result_ms(query, "pg_heap")
        )
        status = query.get("results", {}).get("rvbbit", [None, None])[1]
        route = _route_path(status)
        route_counts[route] += 1
        if auto is None or native is None or duck is None:
            continue
        candidate_ms = {"native": native, "duck": duck}
        if datafusion is not None:
            candidate_ms["datafusion"] = datafusion
        if pg_heap is not None:
            candidate_ms["pg_heap"] = pg_heap
        winner, oracle = min(candidate_ms.items(), key=lambda item: item[1])
        oracle_sum += oracle
        auto_sum += auto
        if route and route != winner:
            misses.append((auto - oracle, auto / oracle if oracle > 0 else math.inf, qid, route, winner, auto, native, duck, datafusion, pg_heap, status))

    print(f"results: {path}")
    print(f"  suite       : {data.get('suite', 'ClickBench')}")
    print(f"  scale       : {data.get('scale', '?')}")
    print(f"  queries     : {len(queries)}")
    print(f"  route counts: {dict(route_counts)}")
    if oracle_sum:
        regret = auto_sum - oracle_sum
        print(f"  oracle sum  : {_fmt_ms(oracle_sum)}")
        print(f"  auto sum    : {_fmt_ms(auto_sum)}")
        print(f"  auto regret : {_fmt_ms(regret)} ({(auto_sum / oracle_sum - 1) * 100:.2f}%)")

    if misses:
        print("\nworst route misses:")
        for regret, ratio, qid, route, winner, auto, native, duck, datafusion, pg_heap, status in sorted(misses, reverse=True)[:top]:
            print(
                "  "
                f"{qid}: route={route} oracle={winner} "
                f"auto={_fmt_ms(auto)} native={_fmt_ms(native)} duck={_fmt_ms(duck)} "
                f"datafusion={_fmt_ms(datafusion) if datafusion is not None else '-'} "
                f"pg_heap={_fmt_ms(pg_heap) if pg_heap is not None else '-'} "
                f"regret={_fmt_ms(regret)} ratio={ratio:.2f} status={status}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", help="Evaluate a route profile's training observations")
    parser.add_argument("--results", help="Evaluate a benchmark result containing rvbbit/rvbbit_native/rvbbit_duck_forced[/rvbbit_datafusion_forced/rvbbit_pg_heap_forced]")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    if not args.profile and not args.results:
        raise SystemExit("pass --profile and/or --results")
    if args.profile:
        evaluate_profile(args.profile, args.top)
    if args.profile and args.results:
        print()
    if args.results:
        evaluate_results(args.results, args.top)


if __name__ == "__main__":
    main()
