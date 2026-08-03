"""Focused contracts for the shared Warehouse image-theme shell."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "warehouse_theme_test_module",
    _HERE / "warehouse_theme.py",
)
warehouse_theme = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = warehouse_theme
_SPEC.loader.exec_module(warehouse_theme)


def test_lens_wallpaper_library_is_shipped_as_web_sized_pairs():
    items = warehouse_theme.theme_library()
    assert len(items) == 76
    assert len({item["id"] for item in items}) == len(items)
    assert all(item["thumb"].startswith("/theme/images/thumb/") for item in items)
    assert all(item["url"].startswith("/theme/images/full/") for item in items)

    by_id = {item["id"]: item for item in items}
    assert by_id["callie-bg-bright"]["label"] == "Callie Bright"
    assert by_id["callie-bg-dark"]["label"] == "Callie Dark"

    total = 0
    for item in items:
        full = warehouse_theme._IMAGE_DIR / "full" / f"{item['id']}.webp"
        thumb = warehouse_theme._IMAGE_DIR / "thumb" / f"{item['id']}.webp"
        assert full.is_file() and thumb.is_file()
        assert full.read_bytes()[:4] == b"RIFF"
        assert full.read_bytes()[8:12] == b"WEBP"
        assert thumb.read_bytes()[:4] == b"RIFF"
        total += full.stat().st_size + thumb.stat().st_size
    # The Lens originals are ~171 MB. Keep this standalone copy appropriate
    # for the Warehouse image/container rather than silently re-copying 4K.
    assert total < 30 * 1024 * 1024


def test_theme_assets_are_shared_by_every_first_party_warehouse_shell():
    assets = warehouse_theme.head_assets()
    assert '/theme/datarabbit.svg' in assets
    assert 'rel="icon"' in assets
    assert "/theme/warehouse-theme.css" in assets
    assert "/theme/warehouse-theme.js" in assets

    auth = (_HERE / "auth.py").read_text(encoding="utf-8")
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    calliope = (_HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    assert "warehouse_theme.head_assets()" in auth
    assert server.count("warehouse_theme.head_assets()") >= 2
    assert "/theme/warehouse-theme.js" in calliope
    assert "/theme/datarabbit.svg" in calliope
    assert all("data-warehouse-theme-anchor" in page for page in (auth, server, calliope))
    assert "auth.register_login_route(m, provider, _RABBIT_SVG)" in server
    assert "{_LOGIN_RABBIT_SVG}DATA RABBIT" in auth
    assert "var(--amber,#e8b572)" in auth
    favicon = warehouse_theme._FAVICON
    assert favicon.is_file()
    assert 'viewBox="0 0 32 32"' in favicon.read_text(encoding="utf-8")
    assert "/theme/datarabbit.svg" in server


def test_navigation_prepaints_the_desktop_and_never_uses_cross_document_snapshots():
    auth = (_HERE / "auth.py").read_text(encoding="utf-8")
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    calliope_css = (_HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    shared_css = (_HERE / "theme" / "warehouse-theme.css").read_text(encoding="utf-8")

    assert 'class="warehouse-desktop-background" aria-hidden="true"' in auth
    assert 'data-warehouse-background-url="/bg/{bg}.jpg"' in auth
    assert 'data-warehouse-background-opacity="{image_opacity}"' in auth
    assert "pick_background(scene_key" in auth
    assert "scene_key=viewer" in server
    assert "scene_key=owner" in (_HERE / "calliope.py").read_text(encoding="utf-8")
    assert "@view-transition" not in shared_css
    assert "::view-transition" not in shared_css
    assert "background: var(--void, #100d0b)" in shared_css
    assert "body > main" in shared_css
    assert "animation: warehouse-page-in" in shared_css
    assert "cubic-bezier(.16, 1, .3, 1) backwards" in shared_css
    assert "@keyframes warehouse-page-in" in shared_css
    assert "prefers-reduced-motion: reduce" in shared_css
    assert 'html[data-warehouse-background-bridge]::before' in shared_css
    assert 'html[data-warehouse-background-bridge="settling"]::before' in shared_css
    assert 'body[data-warehouse-page="calliope"] > main {' in shared_css
    assert 'body[data-warehouse-page="calliope"] > main > .stage-column > *' in shared_css
    assert '<style>html{background:#100d0b}body{background:transparent}</style>' in (
        _HERE / "calliope" / "index.html"
    ).read_text(encoding="utf-8")
    # No shell may re-enable compositor snapshots locally.
    assert "@view-transition" not in server
    assert "@view-transition" not in calliope_css


def test_theme_pipeline_uses_vibrant_tokens_and_browser_storage():
    source = (_HERE / "theme" / "warehouse-theme.src.js").read_text(encoding="utf-8")
    bundle = (_HERE / "theme" / "warehouse-theme.js").read_text(encoding="utf-8")
    css = (_HERE / "theme" / "warehouse-theme.css").read_text(encoding="utf-8")

    assert 'from "node-vibrant/browser"' in source
    assert "paletteToImagePalette" in source
    assert "deriveWarehouseTokens" in source
    assert "localStorage.setItem" in source
    assert "indexedDB.open" in source
    assert "data-theme-background-mode" in source
    assert "data-theme-solid-color" in source
    assert "backgroundChoice" in source
    assert "warehouseBackground" in source
    assert "rvbbit-warehouse-color-mode" in source
    assert "rvbbit-warehouse-background-bridge-v1" in source
    assert "sessionStorage.setItem(BACKGROUND_BRIDGE_KEY" in source
    assert "settleBackgroundBridge" in source
    assert "deriveLightWarehouseTokens" in source
    assert "warehouseColorMode" in source
    assert "warehouse-theme-mode-button" in source
    assert "Switch to light mode" in source
    assert "Switch to dark mode" in source
    assert "rvbbit-warehouse-theme-v1" in bundle
    assert "rvbbit-warehouse-color-mode" in bundle
    assert "rvbbit-warehouse-background-bridge-v1" in bundle
    assert "/theme/library" in bundle
    assert "data-theme-background-mode" in bundle
    assert "warehouseBackground" in bundle
    assert source.count('<svg viewBox="0 0 24 24"') == 2
    assert "warehouse-theme-button-thumb" in bundle
    assert (_HERE / "theme" / "VIBRANT-LICENSE").is_file()

    # Parent chrome and native charts are themeable; authored app iframe
    # documents remain their own CSS trees.
    assert "html[data-warehouse-theme] .chart-bar" in css
    assert "html[data-warehouse-theme] .artifact-frame iframe" not in css
    assert 'body[data-warehouse-page="calliope"] .surface' in css
    assert '[data-warehouse-background="solid"] .bg' in css
    assert "--warehouse-solid-background" in css
    assert 'body[data-warehouse-page="calliope"] .bg' in css
    assert ':root[data-warehouse-color-mode="light"]' in css
    assert '.warehouse-theme-mode-button' in css
    assert 'html[data-warehouse-color-mode="light"] body[data-warehouse-page="calliope"] .veil' in css
    assert 'html[data-warehouse-color-mode="light"] .artifact-frame iframe' not in css
    assert "position: fixed;" in css
    assert "margin: auto;" in css
    assert "scrollbar-color:" in css
    assert "::-webkit-scrollbar-thumb" in css


def test_container_and_unified_origin_ship_theme_assets():
    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    caddy = (_HERE.parent.parent / "docker" / "origin" / "Caddyfile").read_text(encoding="utf-8")
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    assert "COPY theme ./theme" in dockerfile
    assert "/theme/*" in caddy
    assert "/charts/*" in caddy
    assert "warehouse_theme.register_theme_routes(m)" in server
    assert warehouse_theme._ARTIFACT_LENS_JS.is_file()
    assert warehouse_theme._ARTIFACT_LENS_CSS.is_file()


def test_optional_tanstack_runtime_is_versioned_public_and_inlineable_for_captures():
    runtime = warehouse_theme._TANSTACK_CHARTS_JS
    assert warehouse_theme.TANSTACK_CHARTS_VERSION == "0.3.1"
    assert warehouse_theme.TANSTACK_CHARTS_SRC == "/charts/rvbbit-tanstack-charts-0.3.1.js"
    assert runtime.is_file()
    assert runtime.stat().st_size > 100_000
    assert "RVBBIT_CHARTS" in runtime.read_text(encoding="utf-8")

    ordinary = '<script src="https://cdn.example/chart.js"></script>'
    assert warehouse_theme.inline_chart_runtime(ordinary) == ordinary
    opted_in = (
        '<html><head><script defer src="/charts/rvbbit-tanstack-charts-0.3.1.js">'
        "</script></head><body></body></html>"
    )
    inlined = warehouse_theme.inline_chart_runtime(opted_in)
    assert warehouse_theme.TANSTACK_CHARTS_SRC not in inlined
    assert 'data-rvbbit-chart-runtime="0.3.1"' in inlined
    assert "RVBBIT_CHARTS" in inlined

    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY charts ./charts" in dockerfile
    assert "COPY examples ./examples" in dockerfile


def test_gallery_calliope_entry_is_a_floating_time_aware_avatar():
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    assert 'class="calliope-float"' in server
    assert 'class="calliope-float-avatar"' in server
    assert 'class="calliope-float-name">Calliope</span>' in server
    assert 'data-day-src="/calliope/callie-avatar-day.jpg"' in server
    assert 'data-night-src="/calliope/callie-avatar-night.jpg"' in server
    assert "family=Homemade+Apple&display=swap" in server
    assert "position:fixed;right:var(--calliope-edge);bottom:var(--calliope-edge)" in server
    assert "--gallery-rail-bg:color-mix(in oklch,var(--void) 85%,transparent)" in server
    assert server.count("background:var(--gallery-rail-bg)") == 1
    assert "--calliope-capsule-bg:color-mix(in oklch,#fffaf1 86%,var(--amber))" in server
    assert "--calliope-capsule-ink:color-mix(in oklch,#100d0b 91%,var(--amber))" in server
    assert "background:linear-gradient(135deg,#fff,var(--calliope-capsule-bg))" in server
    assert "color:var(--calliope-capsule-ink);font-family" in server
    assert "width:44px;height:44px" in server
    assert '<img alt="" width="44" height="44" decoding="async">' in server
    assert 'class="calliope-float-copy"' in server
    assert 'class="calliope-float-action">Open workspace' in server
    assert 'aria-label="Open the full Calliope workspace"' in server
    assert "view-transition" not in server
    assert "sparkGradientSequence" in server
    assert "metric-card-area-gradient-" in server
    assert 'class="metric-card-area-clear" offset="82%"' in server
    assert 'class="metric-card-area-floor" offset="100%"' in server
    assert "--metric-card-floor:var(--panel)" in server
    assert 'id="semantic-launch"' in server
    assert 'id="semantic-launch-button"' in server
    assert "/api/calliope/evidence-explorations" in server
    assert "e.key==='Enter'" in server
    shot_rule = server.split(".shot{", 1)[1].split("}", 1)[0]
    assert "border-bottom" not in shot_rule
    assert (
        '<span class="who"><span data-warehouse-theme-anchor></span>'
        "{_brief_link}{_inbox_link}{_app_link}"
    ) in server
    assert (
        '<span class="who"><span data-warehouse-theme-anchor></span>'
        "{_calliope_link}"
    ) not in server


def test_gallery_and_pinned_metric_charts_fade_beneath_legible_text():
    server = (_HERE / "server.py").read_text(encoding="utf-8")

    assert "homeSparkGradientSequence" in server
    assert "home-metric-area-gradient-" in server
    assert 'class="home-metric-area-clear" offset="82%"' in server
    assert 'class="home-metric-area-floor" offset="100%"' in server
    assert "--home-metric-floor:var(--void)" in server
    assert ".home-tile.metric .home-tile-main{text-shadow:0 1px 1px" in server
    assert ".metric-card-content{" in server
    assert "0 8px 22px color-mix(in oklch,var(--void) 84%,transparent)" in server
