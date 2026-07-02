"""Build an explainable Rvbbit route profile from forced-path benchmark data.

Expected input is a normal benchmark `last_run.json` containing forced Rvbbit
execution candidates (`rvbbit_native_forced` or legacy `rvbbit_native`,
`rvbbit_duck_forced`,
`rvbbit_datafusion_forced`, `rvbbit_datafusion_mem_forced`,
`rvbbit_gpu_gqe_forced`, and optionally `rvbbit_pg_heap_forced`). The output
is a JSON profile keyed by query shape, not by benchmark query id.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

from rvbbit_route_model import (
    build_route_features,
    candidate_enabled,
    choose_fastest,
    extract_table_refs,
    min_confidence_for_candidate,
    path_for_candidate,
    ratio_text_many,
    ROUTABLE_CANDIDATES,
    speedup_confidence_many,
)


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN",
    "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff",
)
CANDIDATE_SYSTEMS = {
    "rvbbit_native": "rvbbit_native",
    "rvbbit_native_forced": "rvbbit_native",
    "rvbbit_duck_forced": "duck_vector",
    "rvbbit_duck_hive_forced": "duck_hive",
    "rvbbit_duck_vortex_forced": "duck_vortex",
    "rvbbit_datafusion_mem_forced": "datafusion_mem",
    "rvbbit_datafusion_forced": "datafusion_vector",
    "rvbbit_datafusion_hive_forced": "datafusion_hive",
    "rvbbit_gpu_gqe_forced": "gpu_gqe",
    "rvbbit_pg_heap_forced": "pg_rowstore",
    "rvbbit_pg_heap": "pg_rowstore",
    "pg_heap": "pg_rowstore",
}
_ALLOW_PG_HEAP_RAW = os.environ.get("RVBBIT_ROUTE_ALLOW_PG_HEAP_CHOICES")
ALLOW_PG_HEAP_CHOICES = (
    True
    if _ALLOW_PG_HEAP_RAW is None
    else _ALLOW_PG_HEAP_RAW.lower() in {"1", "true", "yes", "on"}
)


def _queries_for_suite(suite: str) -> dict[str, str]:
    if suite == "clickbench":
        sys.path.insert(0, "/bench/clickbench")
        from queries import QUERIES  # type: ignore

        return {qid: sql for qid, _desc, sql in QUERIES}
    if suite == "tpch":
        sys.path.insert(0, "/bench/tpch")
        from queries import base_queries, sql_for_system  # type: ignore

        return {qid: sql_for_system(sql, "rvbbit", qid) for qid, _desc, sql in base_queries()}
    raise ValueError(f"unknown suite: {suite}")


def _result_ms(query_result: dict[str, Any], system: str) -> float | None:
    value = query_result.get("results", {}).get(system)
    if not value or value[0] is None:
        return None
    return float(value[0])


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def _parse_ms(value: str) -> float | None:
    value = _strip_ansi(value).strip()
    if not value or value.upper().startswith("FAIL") or value == "-":
        return None
    if value.endswith("µs"):
        return float(value[:-2]) / 1000.0
    if value.endswith("us"):
        return float(value[:-2]) / 1000.0
    if value.endswith("ms"):
        return float(value[:-2])
    if value.endswith("s"):
        return float(value[:-1]) * 1000.0
    return float(value)


def _derive_scale_rows(path: str) -> int | None:
    name = Path(path).name
    match = re.search(r"clickbench_(\d+)_", name)
    if match:
        return int(match.group(1))
    return None


def _load_json_results(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        data = json.load(f)
    return list(data.get("queries", []))


def _load_text_results(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for raw_line in f:
            line = _strip_ansi(raw_line)
            if "│" not in line:
                continue
            parts = [part.strip() for part in line.split("│")[1:-1]]
            if len(parts) < 4 or not re.fullmatch(r"Q\d+", parts[0]):
                continue
            native_ms = _parse_ms(parts[2])
            duck_ms = _parse_ms(parts[3])
            datafusion_ms = _parse_ms(parts[4]) if len(parts) > 4 else None
            pg_ms = _parse_ms(parts[5]) if len(parts) > 5 else None
            rows.append(
                {
                    "qid": parts[0],
                    "description": parts[1],
                    "results": {
                        "rvbbit_native": [native_ms, "ok" if native_ms is not None else "missing"],
                        "rvbbit_duck_forced": [duck_ms, "ok" if duck_ms is not None else "missing"],
                        "rvbbit_datafusion_forced": [
                            datafusion_ms,
                            "ok" if datafusion_ms is not None else "missing",
                        ],
                        "rvbbit_pg_heap_forced": [pg_ms, "ok" if pg_ms is not None else "missing"],
                    },
                }
            )
    return rows


def _load_results(path: str) -> list[dict[str, Any]]:
    if path.endswith(".json"):
        return _load_json_results(path)
    return _load_text_results(path)


def _explain(conn: psycopg.Connection, sql: str) -> str | None:
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL rvbbit.duck_backend = off")
            cur.execute(("EXPLAIN " + sql).encode())  # type: ignore[arg-type]
            return "\n".join(row[0] for row in cur.fetchall())
    except Exception:
        conn.rollback()
        return None


def _query_table_metrics(
    conn: psycopg.Connection,
    sql: str,
    scale_rows: int | None,
) -> dict[str, Any]:
    refs = extract_table_refs(sql)
    metrics = {
        "rows": scale_rows or 0,
        "bytes": 0,
        "row_groups": 0,
        "text_columns": [],
    }
    if not refs:
        return metrics if scale_rows is not None else {}

    matched = False
    matched_oids: list[int] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lower(n.nspname), lower(c.relname), c.oid::bigint,
                       count(rg.*)::bigint,
                       coalesce(sum(rg.n_rows), 0)::bigint,
                       coalesce(sum(rg.n_bytes), 0)::bigint
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.oid
                WHERE coalesce(t.acceleration_enabled, true)
                GROUP BY n.nspname, c.relname, c.oid
                """.encode()
            )  # type: ignore[arg-type]
            for schema, relname, oid, row_groups, rows, bytes_ in cur.fetchall():
                if relname not in refs and f"{schema}.{relname}" not in refs:
                    continue
                matched = True
                matched_oids.append(int(oid))
                if scale_rows is None:
                    metrics["rows"] += int(rows or 0)
                metrics["row_groups"] += int(row_groups or 0)
                metrics["bytes"] += int(bytes_ or 0)
            if matched_oids:
                oid_array = ", ".join(f"{oid}::oid" for oid in sorted(set(matched_oids)))
                cur.execute(
                    f"""
                    SELECT DISTINCT lower(attname::text)
                    FROM pg_attribute
                    WHERE attrelid = ANY(ARRAY[{oid_array}]::oid[])
                      AND attnum > 0
                      AND NOT attisdropped
                      AND atttypid IN ('text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, 'name'::regtype)
                    """.encode(),
                )  # type: ignore[arg-type]
                metrics["text_columns"] = [row[0] for row in cur.fetchall() if row[0]]
    except Exception:
        conn.rollback()
    if matched or scale_rows is not None:
        return metrics
    return {}


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _choice_candidates(candidate_ms: dict[str, float]) -> dict[str, float]:
    allowed = set(ROUTABLE_CANDIDATES)
    if not ALLOW_PG_HEAP_CHOICES:
        allowed.discard("pg_rowstore")
    return {
        candidate: ms
        for candidate, ms in candidate_ms.items()
        if candidate in allowed and candidate_enabled(candidate)
    }


def _passes_choice_threshold(candidate: str | None, confidence: float, min_gain_pct: float) -> bool:
    return confidence >= max(min_gain_pct, min_confidence_for_candidate(candidate))


def build_profile(
    suite: str,
    results_paths: list[str],
    output_path: str,
    min_gain_pct: float,
    min_observations: int,
    scale_rows: int | None = None,
) -> dict[str, Any]:
    queries_by_id = _queries_for_suite(suite)
    observations: list[dict[str, Any]] = []

    with psycopg.connect(RVBBIT_DSN) as conn:
        for results_path in results_paths:
            file_scale_rows = scale_rows if len(results_paths) == 1 else _derive_scale_rows(results_path)
            for item in _load_results(results_path):
                qid = item.get("qid")
                sql = queries_by_id.get(qid)
                if not sql:
                    continue
                candidate_ms: dict[str, float] = {}
                for system, candidate in CANDIDATE_SYSTEMS.items():
                    ms = _result_ms(item, system)
                    if ms is not None:
                        candidate_ms[candidate] = ms
                if len(candidate_ms) < 2:
                    continue

                plan_text = _explain(conn, sql)
                table_metrics = _query_table_metrics(conn, sql, file_scale_rows)
                features = build_route_features(sql, plan_text, table_metrics=table_metrics)
                winner_candidate, _best_ms, _next_ms = choose_fastest(candidate_ms)
                winner = path_for_candidate(winner_candidate) or str(winner_candidate)
                choice_candidate, _route_best_ms, _route_next_ms = choose_fastest(
                    _choice_candidates(candidate_ms)
                )
                choice = path_for_candidate(choice_candidate) or str(choice_candidate)
                observations.append(
                    {
                        "source_results": results_path,
                        "scale_rows": file_scale_rows,
                        "qid": qid,
                        "candidate_ms": candidate_ms,
                        "native_ms": candidate_ms.get("rvbbit_native"),
                        "duck_ms": candidate_ms.get("duck_vector"),
                        "duck_hive_ms": candidate_ms.get("duck_hive"),
                        "duck_vortex_ms": candidate_ms.get("duck_vortex"),
                        "datafusion_mem_ms": candidate_ms.get("datafusion_mem"),
                        "datafusion_ms": candidate_ms.get("datafusion_vector"),
                        "datafusion_hive_ms": candidate_ms.get("datafusion_hive"),
                        "gpu_gqe_ms": candidate_ms.get("gpu_gqe"),
                        "pg_ms": candidate_ms.get("pg_rowstore"),
                        "winner": winner,
                        "routable_winner": choice,
                        "confidence": speedup_confidence_many(candidate_ms),
                        "features": features,
                        "shape_key": features["shape_key"],
                    }
                )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        grouped.setdefault(obs["shape_key"], []).append(obs)

    entries: dict[str, Any] = {}
    rejected: dict[str, Any] = {}
    for key, rows in sorted(grouped.items()):
        candidates = sorted(set().union(*(row["candidate_ms"].keys() for row in rows)))
        candidate_medians = {
            candidate: _median(
                [row["candidate_ms"][candidate] for row in rows if candidate in row["candidate_ms"]]
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
        sources = sorted(set(row["source_results"] for row in rows))
        scales = sorted(set(row["scale_rows"] for row in rows if row["scale_rows"] is not None))
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
            "duck_vortex_ms_median": (
                round(candidate_medians["duck_vortex"], 4)
                if "duck_vortex" in candidate_medians
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
            "gpu_gqe_ms_median": (
                round(candidate_medians["gpu_gqe"], 4)
                if "gpu_gqe" in candidate_medians
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
            "representative_features": rows[0]["features"],
        }
        if (
            len(rows) >= min_observations
            and _passes_choice_threshold(choice_candidate, confidence, min_gain_pct)
        ):
            entries[key] = entry
        else:
            rejected[key] = entry

    profile = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "suite": suite,
        "source_results": results_paths,
        "native_system": "rvbbit_native_forced",
        "duck_system": "rvbbit_duck_forced",
        "duck_hive_system": "rvbbit_duck_hive_forced",
        "duck_vortex_system": "rvbbit_duck_vortex_forced",
        "datafusion_mem_system": "rvbbit_datafusion_mem_forced",
        "datafusion_system": "rvbbit_datafusion_forced",
        "datafusion_hive_system": "rvbbit_datafusion_hive_forced",
        "gpu_gqe_system": "rvbbit_gpu_gqe_forced",
        "pg_heap_system": "rvbbit_pg_heap_forced",
        "pg_heap_observation_only": not ALLOW_PG_HEAP_CHOICES,
        "min_gain_pct": min_gain_pct,
        "min_observations": min_observations,
        "entries": entries,
        "rejected": rejected,
        "observations": observations,
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(profile, f, indent=2, sort_keys=True)
    return profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["clickbench", "tpch"], required=True)
    parser.add_argument("--results", action="append", required=True)
    parser.add_argument("--output", default="/bench/rvbbit_route_profile.json")
    parser.add_argument("--min-gain-pct", type=float, default=0.05)
    parser.add_argument("--min-observations", type=int, default=1)
    parser.add_argument("--scale-rows", type=int)
    args = parser.parse_args()

    profile = build_profile(
        suite=args.suite,
        results_paths=args.results,
        output_path=args.output,
        min_gain_pct=args.min_gain_pct,
        min_observations=args.min_observations,
        scale_rows=args.scale_rows,
    )

    print(f"wrote {args.output}")
    print(f"accepted shapes: {len(profile['entries'])}")
    print(f"rejected shapes: {len(profile['rejected'])}")
    for key, entry in profile["entries"].items():
        qids = ",".join(entry["sample_qids"])
        print(
            f"  {entry['choice']:<10} conf={entry['confidence']:.2f} "
            f"native={entry.get('native_ms_median')}ms "
            f"duck={entry.get('duck_ms_median')}ms "
            f"duck_hive={entry.get('duck_hive_ms_median')}ms "
            f"duck_vortex={entry.get('duck_vortex_ms_median')}ms "
            f"datafusion_mem={entry.get('datafusion_mem_ms_median')}ms "
            f"datafusion={entry.get('datafusion_ms_median')}ms "
            f"datafusion_hive={entry.get('datafusion_hive_ms_median')}ms "
            f"gpu_gqe={entry.get('gpu_gqe_ms_median')}ms "
            f"pg_heap={entry.get('pg_ms_median')}ms "
            f"oracle={entry.get('oracle_choice')} qids={qids} key={key}"
        )


if __name__ == "__main__":
    main()
