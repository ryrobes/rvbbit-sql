#!/usr/bin/env python3
"""Verify that release images are anonymously readable from GHCR."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "capabilities" / "tools" / "rvbbit-capability"
PACKS = ROOT / "capabilities" / "packs"
CORE_CAPABILITY_IDS = {
    "runtimes/python-runtime",
    "runtimes/mcp-gateway",
    "smoke/warren-echo",
}


def capture_json(cmd: list[str]) -> dict:
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def release_capability_images(
    image_prefix: str,
    version: str,
    *,
    include_all: bool,
) -> list[str]:
    doc = capture_json(
        [
            str(CLI),
            "catalog",
            "build",
            "--root",
            str(PACKS),
            "--image-prefix",
            image_prefix,
            "--image-tag",
            version,
        ]
    )
    images: set[str] = set()
    prefix = image_prefix.rstrip("/") + "/"
    for entry in doc["capabilities"]:
        if not include_all and entry.get("id") not in CORE_CAPABILITY_IDS:
            continue
        image = entry.get("runtime_image")
        if isinstance(image, str) and image.startswith(prefix):
            images.add(image)
    return sorted(images)


def product_images(image_prefix: str, version: str, args: argparse.Namespace) -> list[str]:
    names: list[str] = []
    if not args.skip_db:
        names.append("rvbbit-postgres")
    if not args.skip_lens:
        names.append("rvbbit-lens")
    if not args.skip_warren:
        names.append("rvbbit-warren-agent")
    return [f"{image_prefix.rstrip('/')}/{name}:{version}" for name in names]


def package_name(image: str) -> str:
    ref = image.rsplit("/", 1)[-1]
    return ref.rsplit(":", 1)[0]


def package_urls(owner: str, images: list[str]) -> list[str]:
    names = sorted({package_name(image) for image in images})
    return [
        f"https://github.com/users/{owner}/packages/container/package/{name}"
        for name in names
    ]


def check_image(image: str, docker_config: Path) -> tuple[bool, str]:
    cmd = ["docker", "--config", str(docker_config), "manifest", "inspect", image]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-prefix", required=True, help="registry namespace, e.g. ghcr.io/ryrobes")
    parser.add_argument("--version", required=True)
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument("--skip-lens", action="store_true")
    parser.add_argument("--skip-warren", action="store_true")
    parser.add_argument(
        "--with-capabilities",
        action="store_true",
        help="also verify every built-in capability image, not just core runtime/smoke images",
    )
    parser.add_argument(
        "--skip-capabilities",
        action="store_true",
        help="verify only product images; normally core runtime/smoke images are included",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="print images and package URLs without checking anonymous access",
    )
    args = parser.parse_args()

    images = product_images(args.image_prefix, args.version, args)
    if not args.skip_capabilities:
        images.extend(
            release_capability_images(
                args.image_prefix,
                args.version,
                include_all=args.with_capabilities,
            )
        )
    images = sorted(set(images))

    owner = args.image_prefix.rstrip("/").rsplit("/", 1)[-1]
    print(json.dumps({"images": images, "package_urls": package_urls(owner, images)}, indent=2))
    if args.list_only:
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="rvbbit-anon-docker-"))
    try:
        failures: list[dict[str, str]] = []
        for image in images:
            ok, error = check_image(image, temp_dir)
            print(("ok " if ok else "fail ") + image)
            if not ok:
                failures.append({"image": image, "error": error[-1000:]})
        if failures:
            print(json.dumps({"failures": failures}, indent=2))
            raise SystemExit(1)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
