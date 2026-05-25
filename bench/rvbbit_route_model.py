"""Feature extraction and profile-driven routing for Rvbbit hot paths.

This module intentionally stays dependency-free. The benchmark harness can
train profiles offline, while production code can later embed the generated
rules without pulling an ML runtime into a Postgres backend.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_PROFILE_PATH = "/bench/rvbbit_route_profile.json"
PROFILE_ENV = "RVBBIT_ROUTE_PROFILE"
TRACE_ENV = "RVBBIT_ROUTE_TRACE"
TRACE_LOG_ENV = "RVBBIT_ROUTE_LOG"
MIN_CONFIDENCE_ENV = "RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE"
PG_HEAP_MIN_CONFIDENCE_ENV = "RVBBIT_ROUTE_PG_HEAP_MIN_CONFIDENCE"
HIVE_MIN_CONFIDENCE_ENV = "RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE"


NATIVE_FUNCTION_MARKERS = [
    "vector_float_agg",
    "top_searchphrase_ordered",
    "count_text_contains",
    "top_phrase_min_url_for_url_contains",
    "top_phrase_url_title_rollup",
    "top_rows_text_contains_ordered_json",
    "top_text_transform_avg_len",
    "any_count_int_text",
    "top_count_1col",
    "count_distinct_int",
    "top_count_distinct_1col",
    "top_count_distinct_int_text",
    "top_rollup_2int",
    "top_rollup_1int_distinct",
    "top_count_int_minute_text",
    "top_count_filtered",
    "top_avg_len_by_int_col",
    "top_count_int_text",
]


@dataclass(frozen=True)
class RouteDecision:
    path: str
    reason: str
    source: str
    confidence: float | None = None
    entry: dict[str, Any] | None = None


def sql_stringless(sql: str) -> str:
    out: list[str] = []
    i = 0
    in_line_comment = False
    in_block_comment = False
    in_string = False
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                out.extend("  ")
                i += 2
            else:
                out.append(" ")
                i += 1
            continue
        if in_string:
            if ch == "'":
                if nxt == "'":
                    out.extend("  ")
                    i += 2
                    continue
                in_string = False
            out.append(" ")
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            out.extend("  ")
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            out.extend("  ")
            i += 2
            continue
        if ch == "'":
            in_string = True
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def normalize_sql(sql: str) -> str:
    lowered = sql_stringless(sql).lower()
    lowered = re.sub(r"\b\d+(?:\.\d+)?\b", "?", lowered)
    return re.sub(r"\s+", " ", lowered).strip().rstrip(";")


def sql_fingerprint(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode()).hexdigest()[:16]


def _limit_value(lowered: str) -> int | None:
    match = re.search(r"\blimit\s+(\d+)\b", lowered)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _offset_value(lowered: str) -> int | None:
    match = re.search(r"\boffset\s+(\d+)\b", lowered)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _bucket(value: int | None, cuts: list[int]) -> str:
    if value is None:
        return "unknown"
    for cut in cuts:
        if value <= cut:
            return f"<={cut}"
    return f">{cuts[-1]}"


def _metric_bucket(value: int | None) -> str:
    return _bucket(value, [10_000, 100_000, 1_000_000, 10_000_000, 100_000_000])


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.I))


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.I) is not None


def _strip_identifier_quotes(value: str) -> str:
    return value.strip().strip('"').lower()


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_identifier = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == '"':
            in_identifier = not in_identifier
        elif not in_identifier:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(value[start:i])
                start = i + 1
        i += 1
    parts.append(value[start:])
    return parts


def _keyword_at(value: str, index: int, keyword: str) -> bool:
    end = index + len(keyword)
    if value[index:end] != keyword:
        return False
    before = value[index - 1] if index > 0 else " "
    after = value[end] if end < len(value) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _top_level_from_clauses(value: str) -> list[str]:
    clauses: list[str] = []
    clause_end_keywords = [
        "where",
        "group by",
        "order by",
        "having",
        "limit",
        "offset",
        "union",
        "except",
        "intersect",
    ]
    depth = 0
    in_identifier = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == '"':
            in_identifier = not in_identifier
            i += 1
            continue
        if in_identifier:
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and _keyword_at(value, i, "from"):
            start = i + len("from")
            j = start
            nested_depth = 0
            nested_identifier = False
            while j < len(value):
                nested_ch = value[j]
                if nested_ch == '"':
                    nested_identifier = not nested_identifier
                    j += 1
                    continue
                if nested_identifier:
                    j += 1
                    continue
                if nested_ch == "(":
                    nested_depth += 1
                    j += 1
                    continue
                if nested_ch == ")":
                    if nested_depth == 0:
                        break
                    nested_depth -= 1
                    j += 1
                    continue
                if nested_depth == 0 and any(
                    _keyword_at(value, j, keyword) for keyword in clause_end_keywords
                ):
                    break
                j += 1
            clauses.append(value[start:j])
            i = j
            continue
        i += 1
    return clauses


def _nested_selects(value: str) -> list[str]:
    nested: list[str] = []
    stack: list[int] = []
    in_identifier = False
    for i, ch in enumerate(value):
        if ch == '"':
            in_identifier = not in_identifier
            continue
        if in_identifier:
            continue
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            inner = value[start + 1 : i].strip()
            if re.match(r"^(select|with)\b", inner):
                nested.append(inner)
    return nested


def _top_level_clause(value: str, keyword: str, end_keywords: list[str]) -> str:
    depth = 0
    in_identifier = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == '"':
            in_identifier = not in_identifier
            i += 1
            continue
        if in_identifier:
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and _keyword_at(value, i, keyword):
            start = i + len(keyword)
            j = start
            nested_depth = 0
            nested_identifier = False
            while j < len(value):
                nested_ch = value[j]
                if nested_ch == '"':
                    nested_identifier = not nested_identifier
                    j += 1
                    continue
                if nested_identifier:
                    j += 1
                    continue
                if nested_ch == "(":
                    nested_depth += 1
                    j += 1
                    continue
                if nested_ch == ")":
                    if nested_depth == 0:
                        break
                    nested_depth -= 1
                    j += 1
                    continue
                if nested_depth == 0 and any(_keyword_at(value, j, end) for end in end_keywords):
                    break
                j += 1
            return value[start:j].strip()
        i += 1
    return ""


def _expr_signature(value: str) -> str:
    if not value:
        return "none"
    normalized = re.sub(r"\s+", " ", value.strip())
    normalized = re.sub(r"\basc\b|\bdesc\b|\bnulls\s+(first|last)\b", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "none"
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def _clause_signature(value: str) -> tuple[int, str]:
    if not value:
        return 0, "none"
    exprs = [part.strip() for part in _split_top_level_commas(value) if part.strip()]
    return len(exprs), _expr_signature("|".join(exprs))


def _count_distinct_signature(lowered: str) -> str:
    match = re.search(r"\bcount\s*\(\s*distinct\s+(.+?)\)", lowered, flags=re.S)
    if not match:
        return "none"
    return _expr_signature(match.group(1))


def _add_table_ref(refs: set[str], first: str, second: str | None = None) -> None:
    rel = _strip_identifier_quotes(second or first)
    if not rel:
        return
    if rel in {"select", "unnest", "generate_series", "read_parquet"}:
        return
    refs.add(rel)
    if second:
        schema = _strip_identifier_quotes(first)
        if schema:
            refs.add(f"{schema}.{rel}")


def extract_table_refs(sql: str) -> set[str]:
    lowered = sql_stringless(sql).lower()
    refs: set[str] = set()
    ident = r'("[^"]+"|[a-zA-Z_][\w$]*)'
    qualified = rf"{ident}(?:\s*\.\s*{ident})?"

    # Explicit JOIN syntax.
    for match in re.finditer(rf"\bjoin\s+({qualified})", lowered):
        token = match.group(1)
        pieces = [piece.strip() for piece in re.split(r"\s*\.\s*", token, maxsplit=1)]
        if len(pieces) == 2:
            _add_table_ref(refs, pieces[0], pieces[1])
        else:
            _add_table_ref(refs, pieces[0])

    # Comma joins and single-table FROM clauses. TPC-H uses comma joins heavily,
    # so relying only on FROM/JOIN keyword counts makes multi-table queries look
    # like one-table scans to the route model.
    for from_clause in _top_level_from_clauses(lowered):
        for item in _split_top_level_commas(from_clause):
            item = item.strip()
            if not item:
                continue
            if item.startswith("("):
                refs.update(extract_table_refs(item[1:]))
                continue
            token_match = re.match(rf"({qualified})", item)
            if not token_match:
                continue
            token = token_match.group(1)
            pieces = [piece.strip() for piece in re.split(r"\s*\.\s*", token, maxsplit=1)]
            if len(pieces) == 2:
                _add_table_ref(refs, pieces[0], pieces[1])
            else:
                _add_table_ref(refs, pieces[0])
    for nested_sql in _nested_selects(lowered):
        if nested_sql != lowered:
            refs.update(extract_table_refs(nested_sql))
    return refs


def extract_sql_features(sql: str) -> dict[str, Any]:
    lowered = sql_stringless(sql).lower()
    raw_lowered = sql.lower()
    normalized = normalize_sql(sql)
    table_refs = extract_table_refs(sql)
    limit = _limit_value(lowered)
    offset = _offset_value(lowered)
    group_clause = _top_level_clause(
        lowered,
        "group by",
        ["order by", "having", "limit", "offset", "union", "except", "intersect"],
    )
    order_clause = _top_level_clause(
        lowered,
        "order by",
        ["limit", "offset", "union", "except", "intersect"],
    )
    group_expr_count, group_expr_signature = _clause_signature(group_clause)
    order_expr_count, order_expr_signature = _clause_signature(order_clause)
    aggregate_names = ["count", "sum", "avg", "min", "max"]
    aggregate_count = sum(_count(rf"\b{name}\s*\(", lowered) for name in aggregate_names)
    projected_cols = None
    select_match = re.match(r"\s*select\s+(.*?)\s+from\s+", lowered, flags=re.I | re.S)
    if select_match:
        projection = select_match.group(1)
        projected_cols = -1 if "*" in projection else projection.count(",") + 1

    return {
        "sql_hash": sql_fingerprint(sql),
        "normalized_sql": normalized,
        "starts_with_with": normalized.startswith("with "),
        "is_select": normalized.startswith("select ") or normalized.startswith("with "),
        "select_star": bool(re.match(r"\s*select\s+\*\s+from\s+", lowered, flags=re.I | re.S)),
        "projected_cols": projected_cols,
        "from_count": len(table_refs) or _count(r"\bfrom\b", lowered),
        "join_count": _count(r"\bjoin\b", lowered),
        "where": _has(r"\bwhere\b", lowered),
        "group_by": _has(r"\bgroup\s+by\b", lowered),
        "order_by": _has(r"\border\s+by\b", lowered),
        "having": _has(r"\bhaving\b", lowered),
        "distinct": _has(r"\bdistinct\b", lowered),
        "count_distinct_count": _count(r"\bcount\s*\(\s*distinct\b", lowered),
        "aggregate_count": aggregate_count,
        "sum_count": _count(r"\bsum\s*\(", lowered),
        "avg_count": _count(r"\bavg\s*\(", lowered),
        "count_count": _count(r"\bcount\s*\(", lowered),
        "min_count": _count(r"\bmin\s*\(", lowered),
        "max_count": _count(r"\bmax\s*\(", lowered),
        "exists_count": _count(r"\bexists\s*\(", lowered),
        "in_count": _count(r"\bin\s*\(", lowered),
        "between_count": _count(r"\bbetween\b", lowered),
        "or_count": _count(r"\bor\b", lowered),
        "and_count": _count(r"\band\b", lowered),
        "comparison_count": _count(r"(?:=|<>|!=|<=|>=|<|>)", lowered),
        "like_count": _count(r"\blike\b", lowered),
        "not_like_count": _count(r"\bnot\s+like\b", lowered),
        "fixed_contains_like_count": _count(r"\blike\s+'%([^%'_]|'')+%'", raw_lowered),
        "regex_count": _count(r"\b(regex_replace|regexp_replace)\s*\(", lowered),
        "limit": limit,
        "limit_present": _has(r"\blimit\b", lowered),
        "limit_bucket": _bucket(limit, [1, 10, 100, 1000, 10000]),
        "offset": offset,
        "offset_present": _has(r"\boffset\b", lowered),
        "offset_bucket": _bucket(offset, [0, 10, 100, 1000, 10000]),
        "group_expr_count": group_expr_count,
        "group_expr_count_bucket": _bucket(group_expr_count, [0, 1, 2, 4, 8, 16]),
        "group_expr_signature": group_expr_signature,
        "order_expr_count": order_expr_count,
        "order_expr_count_bucket": _bucket(order_expr_count, [0, 1, 2, 4, 8, 16]),
        "order_expr_signature": order_expr_signature,
        "count_distinct_signature": _count_distinct_signature(lowered),
        "wide_sum_bucket": _bucket(_count(r"\bsum\s*\(", lowered), [0, 1, 4, 16, 64]),
        "projected_cols_bucket": _bucket(
            projected_cols if projected_cols != -1 else None, [1, 2, 4, 8, 16, 64]
        ),
    }


def extract_plan_features(plan_text: str | None) -> dict[str, Any]:
    if not plan_text:
        return {
            "plan_available": False,
            "native_function": None,
            "has_native_function": False,
        }

    plan_lowered = plan_text.lower()
    function_scan_match = re.search(r"Function Scan on ([A-Za-z0-9_]+)", plan_text)
    native_function = function_scan_match.group(1) if function_scan_match else None
    rows = [int(v) for v in re.findall(r"\brows=(\d+)\b", plan_text)]
    widths = [int(v) for v in re.findall(r"\bwidth=(\d+)\b", plan_text)]
    return {
        "plan_available": True,
        "native_function": native_function,
        "has_native_function": native_function in NATIVE_FUNCTION_MARKERS,
        "plan_result_only": plan_text.strip().startswith("Result") and "->" not in plan_text,
        "plan_has_sort": "sort" in plan_lowered,
        "plan_has_group": "group" in plan_lowered or "aggregate" in plan_lowered,
        "plan_has_hash": "hash" in plan_lowered,
        "plan_has_join": "join" in plan_lowered,
        "plan_has_subplan": "subplan" in plan_lowered or "initplan" in plan_lowered,
        "plan_rows_max": max(rows) if rows else None,
        "plan_rows_bucket": _bucket(max(rows) if rows else None, [1, 10, 1000, 100000, 1000000]),
        "plan_width_max": max(widths) if widths else None,
        "plan_width_bucket": _bucket(max(widths) if widths else None, [16, 64, 256, 1024, 4096]),
    }


def build_route_features(
    sql: str,
    plan_text: str | None = None,
    table_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    features = extract_sql_features(sql)
    features.update(extract_plan_features(plan_text))
    table_metrics = table_metrics or {}
    features.update(
        {
            "table_rows": table_metrics.get("rows"),
            "table_rows_bucket": _metric_bucket(table_metrics.get("rows")),
            "table_bytes": table_metrics.get("bytes"),
            "table_bytes_bucket": _metric_bucket(table_metrics.get("bytes")),
            "row_group_count": table_metrics.get("row_groups"),
            "row_group_count_bucket": _bucket(table_metrics.get("row_groups"), [1, 4, 16, 64, 256]),
        }
    )
    features["shape_key"] = shape_key(features)
    return features


def shape_key(features: dict[str, Any]) -> str:
    native_cap = 1 if features.get("has_native_function") else 0
    parts = [
        f"native_cap={native_cap}",
        f"tables={_bucket(features.get('from_count'), [1, 2, 4, 8])}",
        f"joins={_bucket(features.get('join_count'), [0, 1, 2, 4, 8])}",
        f"agg={_bucket(features.get('aggregate_count'), [0, 1, 2, 4, 16, 64])}",
        f"cd={_bucket(features.get('count_distinct_count'), [0, 1, 2, 4])}",
        f"group={int(bool(features.get('group_by')))}",
        f"where={int(bool(features.get('where')))}",
        f"order={int(bool(features.get('order_by')))}",
        f"limit={features.get('limit_bucket')}",
        f"offset={int(bool(features.get('offset_present')))}",
        f"star={int(bool(features.get('select_star')))}",
        f"like={_bucket(features.get('like_count'), [0, 1, 2, 4])}",
        f"fixed_like={_bucket(features.get('fixed_contains_like_count'), [0, 1, 2, 4])}",
        f"regex={_bucket(features.get('regex_count'), [0, 1, 2])}",
        f"exists={_bucket(features.get('exists_count'), [0, 1, 2])}",
        f"in={_bucket(features.get('in_count'), [0, 1, 4])}",
        f"between={_bucket(features.get('between_count'), [0, 1, 4])}",
        f"or={_bucket(features.get('or_count'), [0, 1, 4, 16])}",
        f"group_keys={features.get('group_expr_count_bucket', 'unknown')}",
        f"group_sig={features.get('group_expr_signature', 'none')}",
        f"order_keys={features.get('order_expr_count_bucket', 'unknown')}",
        f"order_sig={features.get('order_expr_signature', 'none')}",
        f"cd_sig={features.get('count_distinct_signature', 'none')}",
        f"width={features.get('plan_width_bucket', 'unknown')}",
        f"table_rows={features.get('table_rows_bucket', 'unknown')}",
        f"plan_join={int(bool(features.get('plan_has_join')))}",
        f"subplan={int(bool(features.get('plan_has_subplan')))}",
    ]
    return "|".join(parts)


def _shape_family_key(key: str | None) -> str | None:
    if not key:
        return None
    return "|".join(part for part in key.split("|") if not part.startswith("table_rows="))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


PATH_TO_CANDIDATE = {
    "native": "rvbbit_native",
    "rvbbit_native": "rvbbit_native",
    "duck": "duck_vector",
    "duck_vector": "duck_vector",
    "duck_hive": "duck_hive",
    "duck-hive": "duck_hive",
    "datafusion": "datafusion_vector",
    "df": "datafusion_vector",
    "datafusion_vector": "datafusion_vector",
    "datafusion_hive": "datafusion_hive",
    "datafusion-hive": "datafusion_hive",
    "df_hive": "datafusion_hive",
    "pg": "pg_rowstore",
    "heap": "pg_rowstore",
    "pg_heap": "pg_rowstore",
    "pg_rowstore": "pg_rowstore",
    "postgres": "pg_rowstore",
    "postgres_rowstore": "pg_rowstore",
}
CANDIDATE_TO_PATH = {
    "rvbbit_native": "native",
    "duck_vector": "duck",
    "duck_hive": "duck_hive",
    "datafusion_vector": "datafusion",
    "datafusion_hive": "datafusion_hive",
    "pg_rowstore": "pg_heap",
}
ROUTABLE_CANDIDATES = {
    "rvbbit_native",
    "duck_vector",
    "duck_hive",
    "datafusion_vector",
    "datafusion_hive",
    "pg_rowstore",
}


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def candidate_enabled(candidate: str | None) -> bool:
    candidate = canonical_candidate(candidate)
    if candidate == "duck_vector":
        return _env_enabled("RVBBIT_ROUTE_DUCK_VECTOR", True)
    if candidate == "datafusion_vector":
        return _env_enabled("RVBBIT_ROUTE_DATAFUSION_VECTOR", True)
    if candidate == "duck_hive":
        return _env_enabled("RVBBIT_ROUTE_HIVE", True) and _env_enabled(
            "RVBBIT_ROUTE_DUCK_HIVE", True
        )
    if candidate == "datafusion_hive":
        return _env_enabled("RVBBIT_ROUTE_HIVE", True) and _env_enabled(
            "RVBBIT_ROUTE_DATAFUSION_HIVE", True
        )
    if candidate == "pg_rowstore":
        return _env_enabled("RVBBIT_ROUTE_PG_ROWSTORE", True)
    if candidate == "rvbbit_native":
        return _env_enabled("RVBBIT_ROUTE_RVBBIT_NATIVE", True)
    return False


def canonical_candidate(value: str | None) -> str | None:
    if not value:
        return None
    return PATH_TO_CANDIDATE.get(str(value).strip().lower())


def path_for_candidate(value: str | None) -> str | None:
    candidate = canonical_candidate(value)
    if not candidate:
        return None
    return CANDIDATE_TO_PATH.get(candidate)


def min_confidence_for_candidate(candidate: str | None) -> float:
    candidate = canonical_candidate(candidate)
    if candidate == "pg_rowstore":
        return float(os.environ.get(PG_HEAP_MIN_CONFIDENCE_ENV, "0.25"))
    if candidate in {"duck_hive", "datafusion_hive"}:
        return float(os.environ.get(HIVE_MIN_CONFIDENCE_ENV, "0.08"))
    return float(os.environ.get(MIN_CONFIDENCE_ENV, "0.05"))


def observation_candidate_ms(observation: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    raw = observation.get("candidate_ms")
    if isinstance(raw, dict):
        for key, value in raw.items():
            candidate = canonical_candidate(str(key))
            if not candidate:
                continue
            try:
                ms = float(value)
            except (TypeError, ValueError):
                continue
            if ms > 0:
                out[candidate] = ms
    legacy_fields = {
        "native_ms": "rvbbit_native",
        "duck_ms": "duck_vector",
        "duck_hive_ms": "duck_hive",
        "datafusion_ms": "datafusion_vector",
        "datafusion_hive_ms": "datafusion_hive",
        "pg_ms": "pg_rowstore",
    }
    for field, candidate in legacy_fields.items():
        if candidate in out or observation.get(field) is None:
            continue
        try:
            ms = float(observation[field])
        except (TypeError, ValueError):
            continue
        if ms > 0:
            out[candidate] = ms
    return out


def choose_fastest(candidate_ms: dict[str, float]) -> tuple[str | None, float | None, float | None]:
    ordered = sorted(
        ((candidate, ms) for candidate, ms in candidate_ms.items() if ms > 0),
        key=lambda item: item[1],
    )
    if not ordered:
        return None, None, None
    best_candidate, best_ms = ordered[0]
    next_ms = ordered[1][1] if len(ordered) > 1 else None
    return best_candidate, best_ms, next_ms


def speedup_confidence_many(candidate_ms: dict[str, float]) -> float:
    _best, best_ms, next_ms = choose_fastest(candidate_ms)
    if best_ms is None or next_ms is None or next_ms <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (best_ms / next_ms)))


def ratio_text_many(candidate_ms: dict[str, float], choice: str) -> str:
    candidate = canonical_candidate(choice)
    if not candidate or candidate not in candidate_ms:
        return f"{choice} selected"
    ordered = sorted(
        ((name, ms) for name, ms in candidate_ms.items() if ms > 0),
        key=lambda item: item[1],
    )
    if len(ordered) < 2:
        return f"{CANDIDATE_TO_PATH.get(candidate, candidate)} selected"
    best_name, best_ms = ordered[0]
    second_ms = ordered[1][1]
    path = CANDIDATE_TO_PATH.get(best_name, best_name)
    ratio = second_ms / best_ms if best_ms > 0 else math.inf
    return f"{path} {ratio:.2f}x faster than next candidate"


class RouteProfile:
    def __init__(self, data: dict[str, Any] | None = None, source_path: str | None = None):
        self.data = data or {}
        self.source_path = source_path
        self.entries = self.data.get("entries", {})
        self.observations = self.data.get("observations", [])
        self.families = self._build_families()

    def _build_families(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, dict[int, list[dict[str, float]]]] = {}
        for observation in self.observations:
            features = observation.get("features") or {}
            family_key = _shape_family_key(features.get("shape_key") or observation.get("shape_key"))
            rows = features.get("table_rows") or observation.get("scale_rows")
            candidate_ms = observation_candidate_ms(observation)
            if not family_key or not rows or len(candidate_ms) < 2:
                continue
            try:
                row_count = int(rows)
            except (TypeError, ValueError):
                continue
            if row_count <= 0:
                continue
            grouped.setdefault(family_key, {}).setdefault(row_count, []).append(candidate_ms)

        families: dict[str, list[dict[str, Any]]] = {}
        for family_key, by_rows in grouped.items():
            anchors: list[dict[str, Any]] = []
            for row_count, values in by_rows.items():
                candidates = sorted(set().union(*(value.keys() for value in values)))
                candidate_medians = {
                    candidate: _median(
                        [value[candidate] for value in values if value.get(candidate, 0) > 0]
                    )
                    for candidate in candidates
                }
                anchors.append(
                    {
                        "rows": row_count,
                        "candidate_ms": candidate_medians,
                        "native_ms": candidate_medians.get("rvbbit_native"),
                        "duck_ms": candidate_medians.get("duck_vector"),
                        "duck_hive_ms": candidate_medians.get("duck_hive"),
                        "datafusion_ms": candidate_medians.get("datafusion_vector"),
                        "datafusion_hive_ms": candidate_medians.get("datafusion_hive"),
                        "pg_ms": candidate_medians.get("pg_rowstore"),
                        "observations": len(values),
                    }
                )
            anchors.sort(key=lambda item: item["rows"])
            if len(anchors) >= 3:
                families[family_key] = anchors
        return families

    @classmethod
    def load(cls, path: str | None = None) -> "RouteProfile":
        path = path or os.environ.get(PROFILE_ENV) or DEFAULT_PROFILE_PATH
        if not path or not Path(path).exists():
            return cls(source_path=path)
        try:
            with open(path) as f:
                return cls(json.load(f), source_path=path)
        except Exception:
            return cls(source_path=path)

    def choose(self, features: dict[str, Any]) -> RouteDecision | None:
        curve_decision = self._choose_from_curve(features)
        if curve_decision:
            return curve_decision

        entry = self.entries.get(features.get("shape_key"))
        if not entry:
            return None
        choice = path_for_candidate(entry.get("choice"))
        if choice not in {"native", "duck", "duck_hive", "datafusion", "datafusion_hive", "pg_heap"}:
            return None
        if not candidate_enabled(entry.get("choice")):
            return None
        confidence = float(entry.get("confidence", 0.0))
        if confidence < min_confidence_for_candidate(entry.get("choice")):
            return None
        return RouteDecision(
            path=choice,
            reason=entry.get("reason") or f"profile prefers {choice}",
            source="profile",
            confidence=confidence,
            entry=entry,
        )

    def _choose_from_curve(self, features: dict[str, Any]) -> RouteDecision | None:
        family_key = _shape_family_key(features.get("shape_key"))
        if not family_key:
            return None
        try:
            rows = int(features.get("table_rows") or 0)
        except (TypeError, ValueError):
            return None
        if rows <= 0:
            return None
        anchors = self.families.get(family_key)
        if not anchors or rows < anchors[0]["rows"] or rows > anchors[-1]["rows"]:
            return None

        lower = upper = None
        for left, right in zip(anchors, anchors[1:]):
            if left["rows"] <= rows <= right["rows"]:
                lower, upper = left, right
                break
        if not lower or not upper:
            return None

        lower_ms = dict(lower.get("candidate_ms") or {})
        upper_ms = dict(upper.get("candidate_ms") or {})
        candidates = sorted(set(lower_ms) & set(upper_ms))
        if len(candidates) < 2:
            return None
        if lower["rows"] == upper["rows"]:
            predicted = {candidate: float(lower_ms[candidate]) for candidate in candidates}
        else:
            position = (rows - lower["rows"]) / (upper["rows"] - lower["rows"])
            predicted = {
                candidate: float(lower_ms[candidate])
                + position * (float(upper_ms[candidate]) - float(lower_ms[candidate]))
                for candidate in candidates
            }

        routable_predicted = {
            candidate: ms
            for candidate, ms in predicted.items()
            if candidate in ROUTABLE_CANDIDATES and candidate_enabled(candidate)
        }
        choice_candidate, _best_ms, _next_ms = choose_fastest(routable_predicted)
        if (
            choice_candidate == "pg_rowstore"
            and speedup_confidence_many(routable_predicted)
            < min_confidence_for_candidate("pg_rowstore")
        ):
            routable_predicted.pop("pg_rowstore", None)
            choice_candidate, _best_ms, _next_ms = choose_fastest(routable_predicted)
        choice = path_for_candidate(choice_candidate)
        if not choice:
            return None
        confidence = speedup_confidence_many(routable_predicted)
        if confidence < min_confidence_for_candidate(choice_candidate):
            return None

        entry = {
            "choice": choice,
            "confidence": confidence,
            "candidate_ms_predicted": predicted,
            "native_ms_predicted": predicted.get("rvbbit_native"),
            "duck_ms_predicted": predicted.get("duck_vector"),
            "duck_hive_ms_predicted": predicted.get("duck_hive"),
            "datafusion_ms_predicted": predicted.get("datafusion_vector"),
            "datafusion_hive_ms_predicted": predicted.get("datafusion_hive"),
            "pg_ms_predicted": predicted.get("pg_rowstore"),
            "lower_anchor": lower,
            "upper_anchor": upper,
            "family_observations": sum(int(anchor.get("observations", 0)) for anchor in anchors),
        }
        return RouteDecision(
            path=choice,
            reason=(
                f"route curve: predicted {ratio_text_many(predicted, choice)} "
                f"between {lower['rows']} and {upper['rows']} rows"
            ),
            source="profile-curve",
            confidence=confidence,
            entry=entry,
        )


def route_trace_enabled() -> bool:
    return os.environ.get(TRACE_ENV, "").lower() in {"1", "on", "true", "yes"}


def append_route_log(record: dict[str, Any]) -> None:
    path = os.environ.get(TRACE_LOG_ENV)
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record.setdefault("ts", time.time())
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def speedup_confidence(native_ms: float, duck_ms: float) -> float:
    slower = max(native_ms, duck_ms)
    faster = min(native_ms, duck_ms)
    if slower <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (faster / slower)))


def ratio_text(native_ms: float, duck_ms: float, choice: str) -> str:
    if choice == "native":
        ratio = duck_ms / native_ms if native_ms > 0 else math.inf
        return f"native {ratio:.2f}x faster than duck"
    if choice == "datafusion":
        slower = max(native_ms, duck_ms)
        return f"datafusion selected; best native/duck was {slower:.1f}ms"
    ratio = native_ms / duck_ms if duck_ms > 0 else math.inf
    return f"duck {ratio:.2f}x faster than native"
