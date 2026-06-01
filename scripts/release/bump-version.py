#!/usr/bin/env python3
"""Bump Rvbbit release versions across the repo and Lens sibling."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def clean_version(version: str) -> str:
    version = version.strip()
    if version.startswith("v"):
        version = version[1:]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?", version):
        raise SystemExit(f"invalid semver version: {version!r}")
    return version


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    next_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path}: expected one match for {pattern!r}, found {count}")
    if next_text != text:
        path.write_text(next_text)
        print(f"updated {path.relative_to(ROOT)}")


def update_json_version(path: Path, version: str) -> None:
    data = json.loads(path.read_text())
    data["version"] = version
    packages = data.get("packages")
    if isinstance(packages, dict) and isinstance(packages.get(""), dict):
        packages[""]["version"] = version
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        rel = path
    print(f"updated {rel}")


def refresh_cargo_locks() -> None:
    lockfiles = [
        (ROOT / "Cargo.toml", "Cargo.lock"),
        (ROOT / "crates" / "rvbbit_duck" / "Cargo.toml", "crates/rvbbit_duck/Cargo.lock"),
    ]
    for manifest, label in lockfiles:
        subprocess.run(
            ["cargo", "update", "--workspace", "--manifest-path", str(manifest)],
            cwd=ROOT,
            check=True,
        )
        print(f"updated {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version, with or without leading v")
    parser.add_argument(
        "--lens-dir",
        default=str(ROOT.parent / "rvbbit-lens"),
        help="rvbbit-lens checkout to bump when present",
    )
    args = parser.parse_args()
    version = clean_version(args.version)

    replace_once(
        ROOT / "Cargo.toml",
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
    )
    replace_once(
        ROOT / "crates" / "rvbbit_duck" / "Cargo.toml",
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
    )
    replace_once(
        ROOT / "crates" / "pg_rvbbit" / "pg_rvbbit.control",
        r"^default_version\s*=\s*'[^']+'",
        f"default_version = '{version}'",
    )
    refresh_cargo_locks()

    lens_dir = Path(args.lens_dir)
    if lens_dir.exists():
        package_json = lens_dir / "package.json"
        package_lock = lens_dir / "package-lock.json"
        if package_json.exists():
            update_json_version(package_json, version)
        if package_lock.exists():
            update_json_version(package_lock, version)
    else:
        print(f"skipped Lens version bump; not found: {lens_dir}")


if __name__ == "__main__":
    main()
