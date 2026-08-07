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


def artifact_not_found_document(
    identity: str = "",
    session: dict[str, Any] | None = None,
) -> str:
    """Return the generic, viewer-adaptive browser page for missing artifacts.

    This is first-party system chrome, not a published dashboard. It therefore
    has no artifact row or version, while still using the same browser-stored
    palette, wallpaper, typography, and glass material as Adaptive Calliope.
    The copy intentionally does not distinguish missing, archived, or denied.
    """
    import auth

    identity = str(identity or "").strip().lower()
    background = auth.background_layer(
        0.50,
        "linear-gradient(135deg,"
        "color-mix(in oklch,var(--void,#0b1218) 70%,transparent),"
        "color-mix(in oklch,var(--void,#0b1218) 91%,transparent))",
        scene_key=identity or None,
    )
    account = account_control(
        session or ({"identity": identity, "via": "password"} if identity else {})
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="color-scheme" content="dark light">
<title>Artifact unavailable · Calliope</title>
<style>
*{{box-sizing:border-box}}
html{{background:var(--void,#0b1218)}}
body{{min-height:100vh;margin:0;background:transparent;color:var(--bone,#edf2f4);
  font-family:"IBM Plex Sans",ui-sans-serif,sans-serif;-webkit-font-smoothing:antialiased}}
a{{color:inherit;text-decoration:none}}
.artifact-not-found-nav{{position:relative;z-index:3;display:flex;align-items:center;gap:14px;
  min-height:58px;padding:8px clamp(18px,4vw,62px);border-bottom:1px solid var(--line,rgba(237,242,244,.15));
  background:color-mix(in oklch,var(--panel,#111d25) 46%,transparent);
  -webkit-backdrop-filter:blur(22px) saturate(1.18);backdrop-filter:blur(22px) saturate(1.18)}}
.artifact-not-found-nav .artifact-not-found-account{{margin-left:auto;display:flex;align-items:center;gap:12px}}
.artifact-not-found-main{{position:relative;isolation:isolate;min-height:calc(100vh - 58px);display:grid;
  place-items:center;padding:clamp(28px,6vw,88px)}}
.artifact-not-found-main::before{{content:"";position:absolute;z-index:-1;inset:0;pointer-events:none;
  background-image:linear-gradient(var(--line,rgba(237,242,244,.12)) 1px,transparent 1px),
    linear-gradient(90deg,var(--line,rgba(237,242,244,.12)) 1px,transparent 1px);
  background-size:72px 72px;opacity:.28;
  -webkit-mask-image:radial-gradient(ellipse at center,#000,transparent 72%);mask-image:radial-gradient(ellipse at center,#000,transparent 72%)}}
.artifact-not-found-panel{{position:relative;width:min(920px,100%);overflow:hidden;
  border:1px solid var(--line,rgba(237,242,244,.15));
  background:color-mix(in oklch,var(--panel,#111d25) 79%,transparent);
  -webkit-backdrop-filter:blur(22px) saturate(1.15);backdrop-filter:blur(22px) saturate(1.15);
  box-shadow:0 26px 90px color-mix(in oklch,var(--void,#0b1218) 58%,transparent)}}
.artifact-not-found-panel::before{{content:"";position:absolute;inset:0 0 auto;height:2px;
  background:linear-gradient(90deg,var(--main,#68c7b2),var(--rvbbit-accent,#f5b446),transparent 82%)}}
.artifact-not-found-masthead{{display:flex;align-items:center;justify-content:space-between;gap:20px;
  padding:18px clamp(22px,4vw,46px);border-bottom:1px solid var(--line,rgba(237,242,244,.15));
  color:var(--fog,#9aa8ae);font:600 10px/1 "IBM Plex Mono",ui-monospace,monospace;
  letter-spacing:.16em;text-transform:uppercase}}
.artifact-not-found-status{{display:inline-flex;align-items:center;gap:9px}}
.artifact-not-found-status::before{{content:"";width:7px;height:7px;border:1px solid var(--main,#68c7b2);
  background:color-mix(in oklch,var(--main,#68c7b2) 24%,transparent);box-shadow:0 0 14px color-mix(in oklch,var(--main,#68c7b2) 38%,transparent)}}
.artifact-not-found-content{{display:grid;grid-template-columns:minmax(0,1fr) minmax(190px,.38fr);
  gap:clamp(28px,5vw,72px);align-items:end;padding:clamp(34px,6vw,72px) clamp(24px,6vw,68px)}}
.artifact-not-found-kicker{{margin-bottom:14px;color:var(--main,#68c7b2);
  font:600 10px/1.2 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.16em;text-transform:uppercase}}
.artifact-not-found-copy h1{{max-width:680px;margin:0;color:var(--bone-bright,#fff);
  font:500 clamp(38px,6.2vw,76px)/.96 "Newsreader",Georgia,serif;letter-spacing:-.035em}}
.artifact-not-found-copy p{{max-width:620px;margin:24px 0 0;color:var(--fog,#9aa8ae);
  font-size:clamp(15px,1.65vw,19px);line-height:1.55}}
.artifact-not-found-code{{align-self:start;color:color-mix(in oklch,var(--bone,#edf2f4) 16%,transparent);
  font:500 clamp(72px,13vw,150px)/.72 "IBM Plex Sans",ui-sans-serif,sans-serif;
  font-variant-numeric:tabular-nums;letter-spacing:-.08em;text-align:right;user-select:none}}
.artifact-not-found-actions{{display:flex;flex-wrap:wrap;gap:10px;padding:0 clamp(24px,6vw,68px) clamp(30px,5vw,52px)}}
.artifact-not-found-actions a{{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:0 17px;
  border:1px solid var(--line,rgba(237,242,244,.15));color:var(--fog,#9aa8ae);
  background:color-mix(in oklch,var(--panel-raised,#172731) 62%,transparent);
  font:600 10px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;
  transition:transform .18s ease,border-color .18s ease,color .18s ease,background .18s ease}}
.artifact-not-found-actions a:first-child{{border-color:color-mix(in oklch,var(--main,#68c7b2) 54%,var(--line));
  color:var(--bone-bright,#fff);background:color-mix(in oklch,var(--main,#68c7b2) 12%,transparent)}}
.artifact-not-found-actions a:hover{{transform:translateY(-2px);border-color:var(--main,#68c7b2);color:var(--bone-bright,#fff)}}
.artifact-not-found-actions a:focus-visible{{outline:2px solid var(--main,#68c7b2);outline-offset:3px}}
@media (max-width:680px){{
  .artifact-not-found-content{{grid-template-columns:1fr}}
  .artifact-not-found-code{{position:absolute;right:22px;top:92px;font-size:86px;opacity:.58}}
  .artifact-not-found-copy h1{{max-width:82%}}
}}
@media (prefers-reduced-motion:reduce){{.artifact-not-found-actions a{{transition:none}}}}
</style>
{head_assets()}
</head><body data-warehouse-page="artifact-not-found">
{background}
<nav class="artifact-not-found-nav" data-warehouse-header>
  <a class="calliope-brand" href="/gallery" aria-label="Calliope Gallery">
    <span class="calliope-brand-name">Calliope</span>
    <span class="calliope-brand-byline">by RVBBIT.AI</span>
  </a>
  <div class="artifact-not-found-account"><span data-warehouse-theme-anchor></span>{account}</div>
</nav>
<main class="artifact-not-found-main">
  <article class="artifact-not-found-panel" aria-labelledby="artifact-not-found-title">
    <header class="artifact-not-found-masthead">
      <span class="artifact-not-found-status">Artifact route</span>
      <span>Not available</span>
    </header>
    <div class="artifact-not-found-content">
      <div class="artifact-not-found-copy">
        <div class="artifact-not-found-kicker">Nothing to render</div>
        <h1 id="artifact-not-found-title">This page isn&rsquo;t available here.</h1>
        <p>This does not exist or you might not have permission.</p>
      </div>
      <div class="artifact-not-found-code" aria-hidden="true">404</div>
    </div>
    <footer class="artifact-not-found-actions">
      <a href="/gallery">Browse your Gallery <span aria-hidden="true">&nbsp;→</span></a>
      <a href="/calliope">Ask Calliope</a>
    </footer>
  </article>
</main>
</body></html>"""


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
