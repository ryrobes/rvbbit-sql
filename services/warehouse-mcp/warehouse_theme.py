"""Shared browser-side appearance assets for the Warehouse UI.

The login page, artifact hub, and optional Calliope notebook are intentionally
framework-free, but they should still feel like one application.  This module
owns the public theme asset routes and the small HTML include all three shells
use. Published HTML/JS artifacts never inherit the Warehouse theme; the
system-owned Artifact Lens mounts its own isolated Shadow DOM when requested by
the serving shim, so authored styles remain untouched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse, Response


_ASSET_DIR = Path(__file__).resolve().parent / "theme"
_IMAGE_DIR = _ASSET_DIR / "images"
_FAVICON = _ASSET_DIR / "datarabbit.svg"
_ARTIFACT_LENS_JS = _ASSET_DIR / "artifact-lens.js"
_ARTIFACT_LENS_CSS = _ASSET_DIR / "artifact-lens.css"
_THEME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,160}$")


def head_assets() -> str:
    """The shared, public include used in each first-party Warehouse page."""
    return (
        '<link rel="icon" href="/theme/datarabbit.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/theme/warehouse-theme.css">'
        '<script src="/theme/warehouse-theme.js"></script>'
    )


def _label(image_id: str) -> str:
    if image_id.startswith("grok-"):
        return f"Grok {image_id[5:13]}"
    label = image_id
    if label.startswith("bvictor_"):
        label = label[len("bvictor_"):]
    if label.endswith("-high"):
        label = label[:-len("-high")]
    return " ".join(part.capitalize() for part in label.replace("_", " ").split())


def theme_library() -> list[dict[str, Any]]:
    """Return only pairs that have both a lightweight tile and full backdrop."""
    full_dir = _IMAGE_DIR / "full"
    thumb_dir = _IMAGE_DIR / "thumb"
    if not full_dir.is_dir() or not thumb_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for full in sorted(full_dir.glob("*.webp"), key=lambda p: p.name.casefold()):
        image_id = full.stem
        thumb = thumb_dir / full.name
        if not _THEME_ID.fullmatch(image_id) or not thumb.is_file():
            continue
        items.append(
            {
                "id": image_id,
                "label": _label(image_id),
                "thumb": f"/theme/images/thumb/{image_id}.webp",
                "url": f"/theme/images/full/{image_id}.webp",
                "size_bytes": full.stat().st_size,
            }
        )
    return items


def register_theme_routes(mcp: Any) -> None:
    """Register public assets.

    These routes must remain public: the saved appearance is restored on the
    login page, before a Warehouse session exists.  The files are static,
    non-user data and the path parameters are strictly jailed.
    """

    @mcp.custom_route("/theme/datarabbit.svg", methods=["GET"])
    async def datarabbit_favicon(_request):
        return FileResponse(
            _FAVICON,
            media_type="image/svg+xml",
            headers={
                "cache-control": "public, max-age=31536000, immutable",
                "x-content-type-options": "nosniff",
            },
        )

    @mcp.custom_route("/theme/warehouse-theme.css", methods=["GET"])
    async def warehouse_theme_css(_request):
        return FileResponse(
            _ASSET_DIR / "warehouse-theme.css",
            media_type="text/css",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @mcp.custom_route("/theme/warehouse-theme.js", methods=["GET"])
    async def warehouse_theme_js(_request):
        return FileResponse(
            _ASSET_DIR / "warehouse-theme.js",
            media_type="text/javascript",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @mcp.custom_route("/theme/artifact-lens.js", methods=["GET"])
    async def artifact_lens_js(_request):
        return FileResponse(
            _ARTIFACT_LENS_JS,
            media_type="text/javascript",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @mcp.custom_route("/theme/artifact-lens.css", methods=["GET"])
    async def artifact_lens_css(_request):
        return FileResponse(
            _ARTIFACT_LENS_CSS,
            media_type="text/css",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @mcp.custom_route("/theme/library", methods=["GET"])
    async def warehouse_theme_library(_request):
        return Response(
            json.dumps({"items": theme_library()}, separators=(",", ":")),
            media_type="application/json",
            headers={"cache-control": "public, max-age=300", "x-content-type-options": "nosniff"},
        )

    @mcp.custom_route("/theme/images/{variant}/{image_id}.webp", methods=["GET"])
    async def warehouse_theme_image(request):
        variant = request.path_params.get("variant", "")
        image_id = request.path_params.get("image_id", "")
        if variant not in {"thumb", "full"} or not _THEME_ID.fullmatch(image_id):
            return Response("not found", status_code=404)
        path = _IMAGE_DIR / variant / f"{image_id}.webp"
        if not path.is_file():
            return Response("not found", status_code=404)
        return FileResponse(
            path,
            media_type="image/webp",
            headers={
                "cache-control": "public, max-age=31536000, immutable",
                "x-content-type-options": "nosniff",
            },
        )
