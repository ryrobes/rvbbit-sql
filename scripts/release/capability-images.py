#!/usr/bin/env python3
"""Build and optionally push all Warren capability images."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "capabilities" / "tools" / "rvbbit-capability"
PACKS = ROOT / "capabilities" / "packs"


def run(cmd: list[str], cwd: Path | None = None, dry_run: bool = False) -> None:
    print("+ " + " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def capture_json(cmd: list[str]) -> dict:
    result = subprocess.run(cmd, cwd=str(ROOT), check=True, text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def catalog(image_prefix: str | None, image_tag: str) -> list[dict]:
    cmd = [str(CLI), "catalog", "build", "--root", str(PACKS)]
    if image_prefix:
        cmd.extend(["--image-prefix", image_prefix, "--image-tag", image_tag])
    return capture_json(cmd)["capabilities"]


def included(entry: dict, visibilities: set[str], only: set[str], skip: set[str]) -> bool:
    catalog_id = str(entry.get("id") or "")
    if only and catalog_id not in only and str(entry.get("name") or "") not in only:
        return False
    if catalog_id in skip or str(entry.get("name") or "") in skip:
        return False
    if str(entry.get("catalog_visibility") or "public") not in visibilities:
        return False
    if entry.get("install_warren") is not True or entry.get("install_docker") is not True:
        return False
    if entry.get("runtime_image"):
        return False
    return True


def scaffold_source(entry: dict) -> Path:
    pack_path = entry.get("pack_path")
    if pack_path:
        return ROOT / str(pack_path)
    manifest_path = entry.get("manifest_path")
    if manifest_path:
        return ROOT / str(manifest_path)
    raise SystemExit(f"{entry.get('id')}: no pack_path or manifest_path")


def safe_dir_name(entry: dict) -> str:
    return str(entry["id"]).replace("/", "__").replace(".", "_")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-prefix", required=True, help="registry namespace, e.g. ghcr.io/ryrobes")
    parser.add_argument("--version", required=True, help="image tag/version")
    parser.add_argument("--out-dir", default=str(ROOT / "dist" / "capability-images"))
    parser.add_argument("--platform", default=os.environ.get("RVBBIT_CAPABILITY_PLATFORM", "linux/amd64"))
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--tag-latest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--visibility", default="public,example,internal")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--skip", action="append", default=[])
    parser.add_argument("--plan-output", default="")
    args = parser.parse_args()

    if not args.push and "," in args.platform:
        raise SystemExit("multi-platform buildx builds require --push")

    base_catalog = catalog(None, args.version)
    image_catalog = {entry["id"]: entry for entry in catalog(args.image_prefix, args.version)}
    visibilities = {part.strip() for part in args.visibility.split(",") if part.strip()}
    only = set(args.only)
    skip = set(args.skip)
    out_dir = Path(args.out_dir)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    plan: list[dict] = []
    for entry in base_catalog:
        if not included(entry, visibilities, only, skip):
            continue
        image_entry = image_catalog[entry["id"]]
        image = image_entry.get("runtime_image")
        if not image:
            raise SystemExit(f"{entry['id']}: release catalog did not produce runtime_image")
        plan.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "title": entry["title"],
                "image": image,
                "source": str(scaffold_source(entry).relative_to(ROOT)),
            }
        )

    if args.plan_output:
        plan_path = Path(args.plan_output)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")

    for item in plan:
        entry = next(entry for entry in base_catalog if entry["id"] == item["id"])
        build_dir = out_dir / safe_dir_name(entry)
        if build_dir.exists() and not args.dry_run:
            shutil.rmtree(build_dir)
        run(
            [str(CLI), "scaffold", str(scaffold_source(entry)), str(build_dir), "--force"],
            dry_run=args.dry_run,
        )
        tags = ["-t", item["image"]]
        if args.tag_latest and not str(item["image"]).endswith(":latest"):
            latest = str(item["image"]).rsplit(":", 1)[0] + ":latest"
            tags.extend(["-t", latest])
        cmd = ["docker", "buildx", "build", "--platform", args.platform, *tags]
        cmd.extend(
            [
                "--label",
                f"org.opencontainers.image.source=https://github.com/ryrobes/rvbbit-sql",
                "--label",
                f"org.opencontainers.image.title={item['name']}",
                "--label",
                f"dev.rvbbit.capability.id={item['id']}",
            ]
        )
        cmd.append("--push" if args.push else "--load")
        cmd.append(str(build_dir))
        run(cmd, dry_run=args.dry_run)

    print(json.dumps({"count": len(plan), "images": plan}, indent=2))


if __name__ == "__main__":
    main()
