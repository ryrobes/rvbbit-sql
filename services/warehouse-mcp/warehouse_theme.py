"""Shared browser-side appearance assets for the Warehouse UI.

The login page, artifact hub, and optional Calliope notebook are intentionally
framework-free, but they should still feel like one application.  This module
owns the public theme asset routes and the small HTML include all three shells
use. Published HTML/JS artifacts never inherit the Warehouse theme; the
system-owned Artifact Lens mounts its own isolated Shadow DOM when requested by
the serving shim. The explicit Adaptive Calliope Design Profile is the sole
opt-in exception: its artifact runtime receives a sanitized viewer snapshot.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse, Response


_ASSET_DIR = Path(__file__).resolve().parent / "theme"
_IMAGE_DIR = _ASSET_DIR / "images"
_FAVICON = _ASSET_DIR / "datarabbit.svg"
_ARTIFACT_LENS_JS = _ASSET_DIR / "artifact-lens.js"
_ARTIFACT_LENS_CSS = _ASSET_DIR / "artifact-lens.css"
_ADAPTIVE_ARTIFACT_JS = _ASSET_DIR / "adaptive-artifact.js"
_CHARTS_DIR = Path(__file__).resolve().parent / "charts"
TANSTACK_CHARTS_VERSION = "0.3.1"
TANSTACK_CHARTS_SRC = f"/charts/rvbbit-tanstack-charts-{TANSTACK_CHARTS_VERSION}.js"
_TANSTACK_CHARTS_JS = _CHARTS_DIR / f"rvbbit-tanstack-charts-{TANSTACK_CHARTS_VERSION}.js"
_THEME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,160}$")
_ACCOUNT_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}$")
_THEME_LABELS = {
    "callie-bg-bright": "Callie Bright",
    "callie-bg-dark": "Callie Dark",
}
_TANSTACK_SCRIPT_TAG = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*(['\"])"
    + re.escape(TANSTACK_CHARTS_SRC)
    + r"\1[^>]*>\s*</script\s*>",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _tanstack_runtime_source() -> str:
    return _TANSTACK_CHARTS_JS.read_text(encoding="utf-8")


def inline_chart_runtime(html: str) -> str:
    """Inline the opt-in chart runtime for Playwright ``set_content`` renders.

    Live artifact pages load the immutable public asset normally. Screenshot,
    PDF, thumbnail, and semantic-enrichment renders use ``about:blank`` and
    therefore cannot resolve root-relative script URLs; replacing only our
    exact versioned tag keeps those render paths equivalent without rewriting
    arbitrary authored dependencies.
    """
    document = html or ""
    if TANSTACK_CHARTS_SRC not in document:
        return document
    try:
        source = re.sub(r"</script", r"<\\/script", _tanstack_runtime_source(), flags=re.IGNORECASE)
    except OSError:
        return document
    replacement = (
        f'<script data-rvbbit-chart-runtime="{TANSTACK_CHARTS_VERSION}">'
        + source
        + "</script>"
    )
    return _TANSTACK_SCRIPT_TAG.sub(lambda _match: replacement, document)


def head_assets() -> str:
    """The shared, public include used in each first-party Warehouse page."""
    return (
        '<link rel="icon" href="/theme/datarabbit.svg" type="image/svg+xml">'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Homemade+Apple&family=IBM+Plex+Mono:wght@400;500;600;700&'
        'family=IBM+Plex+Sans:wght@400;500;600;700&'
        'family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&display=swap">'
        '<link rel="stylesheet" href="/theme/warehouse-theme.css">'
        '<script src="/theme/warehouse-theme.js"></script>'
    )


def _account_initials(name: str, identity: str) -> str:
    source = name or identity.split("@", 1)[0]
    words = [word for word in re.split(r"[^A-Za-z0-9]+", source) if word]
    if len(words) > 1:
        initials = words[0][0] + words[-1][0]
    elif words:
        initials = words[0][:2]
    else:
        initials = "?"
    return initials.upper()


def account_control(session: dict[str, Any] | None) -> str:
    """Render the shared right-aligned account menu for authenticated shells."""
    session = session or {}
    identity = str(session.get("identity") or session.get("sub") or "").strip()
    if not identity:
        return ""
    name = re.sub(r"\s+", " ", str(session.get("name") or "")).strip()[:160]
    via = str(session.get("via") or "password").strip().lower()
    provider = {
        "google": "Google",
        "pg": "Calliope account",
        "password": "Calliope account",
    }.get(via, "Calliope account")
    initials = _account_initials(name, identity)
    raw_email_avatar = (
        via in {"password", "pg"}
        and len(identity) <= 254
        and _ACCOUNT_EMAIL.fullmatch(identity) is not None
    )
    avatar = (
        '<img class="warehouse-account-avatar-image" src="/auth/avatar" alt="" '
        'width="28" height="28" decoding="async" referrerpolicy="no-referrer">'
        if session.get("picture") or raw_email_avatar else ""
    )
    heading = name or identity
    email_line = (
        f'<small class="warehouse-account-menu-email">{escape(identity)}</small>'
        if name and name.casefold() != identity.casefold() else ""
    )
    return (
        '<div class="warehouse-account" data-warehouse-account>'
        '<button class="warehouse-account-trigger" type="button" '
        'aria-haspopup="menu" aria-expanded="false" '
        f'aria-label="Open account menu for {escape(identity, quote=True)}">'
        f'<span class="warehouse-account-email">{escape(identity)}</span>'
        '<span class="warehouse-account-avatar" aria-hidden="true">'
        f'<span class="warehouse-account-initials">{escape(initials)}</span>{avatar}'
        '</span></button>'
        '<div class="warehouse-account-menu" role="menu" aria-label="Account options" hidden>'
        '<div class="warehouse-account-menu-profile" role="none">'
        f'<span>Signed in with {escape(provider)}</span>'
        f'<strong>{escape(heading)}</strong>{email_line}'
        '</div>'
        '<a class="warehouse-account-signout" href="/auth/logout" role="menuitem">'
        '<span>Sign out</span><span aria-hidden="true">→</span></a>'
        '</div></div>'
    )


def _label(image_id: str) -> str:
    if image_id in _THEME_LABELS:
        return _THEME_LABELS[image_id]
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

    @mcp.custom_route("/theme/adaptive-artifact.js", methods=["GET"])
    async def adaptive_artifact_js(_request):
        return FileResponse(
            _ADAPTIVE_ARTIFACT_JS,
            media_type="text/javascript",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @mcp.custom_route(TANSTACK_CHARTS_SRC, methods=["GET"])
    async def tanstack_charts_runtime(_request):
        return FileResponse(
            _TANSTACK_CHARTS_JS,
            media_type="text/javascript",
            headers={
                "cache-control": "public, max-age=31536000, immutable",
                "x-content-type-options": "nosniff",
            },
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
