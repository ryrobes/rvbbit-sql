"""Catalog pack metadata contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


PACKS = Path(__file__).resolve().parents[1] / "capabilities" / "packs"


def _pack_docs():
    for path in sorted(PACKS.rglob("rvbbit-pack.yaml")):
        with path.open(encoding="utf-8") as fh:
            yield path, yaml.safe_load(fh)


def test_builtin_packs_have_acceptance_sql():
    missing: list[str] = []
    invalid: list[str] = []
    for path, pack in _pack_docs():
        acceptance = pack.get("acceptance") or {}
        tests = acceptance.get("tests") or []
        if not tests:
            missing.append(str(path.relative_to(PACKS.parent.parent)))
            continue
        for test in tests:
            name = test.get("name") if isinstance(test, dict) else None
            sql = test.get("sql") if isinstance(test, dict) else None
            if not name or not isinstance(sql, str) or not sql.strip():
                invalid.append(f"{path.relative_to(PACKS.parent.parent)}:{name}")

    assert not missing, "built-in packs without acceptance.tests: " + ", ".join(missing)
    assert not invalid, "invalid acceptance tests: " + ", ".join(invalid)


def test_acceptance_targets_are_warren_selectors():
    bad: list[str] = []
    for path, pack in _pack_docs():
        acceptance = pack.get("acceptance") or {}
        target = acceptance.get("target_selector") or {}
        if not isinstance(target, dict):
            bad.append(str(path.relative_to(PACKS.parent.parent)))
            continue
        if acceptance.get("tests") and target.get("capability") is not True:
            bad.append(str(path.relative_to(PACKS.parent.parent)))

    assert not bad, "acceptance target_selector should include capability: true: " + ", ".join(bad)


def test_operator_runtime_packs_are_flagged():
    packs = {pack["id"]: (path, pack) for path, pack in _pack_docs()}
    expected = {"runtimes/python-runtime", "runtimes/mcp-gateway"}
    missing = expected - packs.keys()
    assert not missing, "missing operator runtime packs: " + ", ".join(sorted(missing))

    bad: list[str] = []
    for pack_id in expected:
        path, pack = packs[pack_id]
        tags = set(pack.get("tags") or [])
        if (
            pack.get("system_runtime") is not True
            or pack.get("capability_role") != "operator_runtime"
            or "system-runtime" not in tags
            or "operator-runtime" not in tags
        ):
            bad.append(str(path.relative_to(PACKS.parent.parent)))

    assert not bad, "operator runtime packs missing system-runtime metadata: " + ", ".join(bad)


def test_mcp_gateway_pack_is_self_contained():
    pack_dir = PACKS / "runtimes" / "mcp-gateway"
    required = {
        "rvbbit-pack.yaml",
        "capability.yaml",
        "Dockerfile",
        "main.py",
        "requirements.txt",
        "mcp-test-server/main.py",
    }
    missing = [
        rel
        for rel in sorted(required)
        if not (pack_dir / rel).exists()
    ]
    assert not missing, "mcp-gateway pack is missing files: " + ", ".join(missing)
