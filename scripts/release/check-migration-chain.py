#!/usr/bin/env python3
"""Assert the pg_rvbbit ALTER EXTENSION UPDATE chain is contiguous.

Postgres builds the update path from the directed graph of
`pg_rvbbit--FROM--TO.sql` files in crates/pg_rvbbit/sql/ and never invents
edges. A fresh CREATE EXTENSION always works (pgrx generates the base SQL at
default_version), but an existing install can only be upgraded in place if a
path of edges exists from its version to default_version.

This gate fails the release if any previously-shipped version (every
dist/release/<ver>/ directory) cannot reach the control file's default_version.
Run it in CI before publishing, and locally via `make migration-check`.
"""
from __future__ import annotations

import re
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "crates" / "pg_rvbbit" / "pg_rvbbit.control"
SQL_DIR = ROOT / "crates" / "pg_rvbbit" / "sql"
RELEASE_DIR = ROOT / "dist" / "release"

# First GA / supported release. Everything before this was a dev/preview build:
# those upgrade via DROP+CREATE (documented in PACKAGING.md), not in place. The
# gate only requires a contiguous ALTER EXTENSION UPDATE path for shipped
# versions in [SUPPORTED_FROM, default_version). Raise this only when you
# intentionally drop support for an old in-place-upgrade baseline.
SUPPORTED_FROM = "2.0.14"

EDGE_RE = re.compile(r"^pg_rvbbit--(.+?)--(.+?)\.sql$")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def semver(v: str):
    m = SEMVER_RE.match(v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def default_version() -> str:
    m = re.search(r"^default_version\s*=\s*'([^']+)'", CONTROL.read_text(), flags=re.MULTILINE)
    if not m:
        sys.exit(f"could not read default_version from {CONTROL}")
    return m.group(1)


def build_edges() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for f in SQL_DIR.glob("pg_rvbbit--*--*.sql"):
        m = EDGE_RE.match(f.name)
        if m:
            edges.setdefault(m.group(1), set()).add(m.group(2))
    return edges


def reachable(start: str, target: str, edges: dict[str, set[str]]) -> bool:
    if start == target:
        return True
    seen = {start}
    q = deque([start])
    while q:
        cur = q.popleft()
        for nxt in edges.get(cur, ()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return False


def shipped_versions() -> list[str]:
    if not RELEASE_DIR.is_dir():
        return []
    return sorted(p.name for p in RELEASE_DIR.iterdir() if p.is_dir())


def main() -> int:
    target = default_version()
    edges = build_edges()
    shipped = shipped_versions()
    if not shipped:
        print("no dist/release/<ver> directories found; nothing to check")
        return 0

    tgt_sv = semver(target)
    base_sv = semver(SUPPORTED_FROM)
    if tgt_sv is None or base_sv is None:
        sys.exit(f"cannot parse semver for default_version '{target}' / SUPPORTED_FROM '{SUPPORTED_FROM}'")
    # Only supported, in-range versions need an in-place path: SUPPORTED_FROM
    # (the first GA) up to but not including the current default_version.
    # Pre-GA previews and any tag at/above default (e.g. stray test tags) are skipped.
    in_scope = [
        v for v in shipped
        if (sv := semver(v)) is not None and base_sv <= sv < tgt_sv
    ]
    skipped = [v for v in shipped if v not in in_scope and v != target]
    if skipped:
        print(f"skipped (pre-GA / out-of-range, DROP+CREATE only): {', '.join(sorted(skipped))}")

    broken: list[str] = []
    for ver in in_scope:
        if not reachable(ver, target, edges):
            broken.append(ver)

    if broken:
        print(f"FAIL: no ALTER EXTENSION UPDATE path to default_version '{target}' from:")
        for ver in broken:
            print(f"  - {ver}  (add crates/pg_rvbbit/sql/pg_rvbbit--<...>--{target}.sql edges)")
        print(
            "\nFix: add the missing bridge migration script(s). For a no-SQL-change bump an\n"
            "empty stub is fine (bump-version.py auto-creates one); if the SQL surface changed,\n"
            "fill the stub with the upgrade DDL (diff `cargo pgrx schema` between the two versions)."
        )
        return 1

    print(f"OK: every supported version (>= {SUPPORTED_FROM}) has an update path to "
          f"default_version '{target}' ({len(in_scope)} in-scope versions checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
