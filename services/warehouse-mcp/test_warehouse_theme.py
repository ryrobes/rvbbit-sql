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
    assert len(items) == 74
    assert len({item["id"] for item in items}) == len(items)
    assert all(item["thumb"].startswith("/theme/images/thumb/") for item in items)
    assert all(item["url"].startswith("/theme/images/full/") for item in items)

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
    assert "/theme/warehouse-theme.css" in assets
    assert "/theme/warehouse-theme.js" in assets

    auth = (_HERE / "auth.py").read_text(encoding="utf-8")
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    calliope = (_HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    assert "warehouse_theme.head_assets()" in auth
    assert server.count("warehouse_theme.head_assets()") >= 2
    assert "/theme/warehouse-theme.js" in calliope
    assert all("data-warehouse-theme-anchor" in page for page in (auth, server, calliope))


def test_theme_pipeline_uses_vibrant_tokens_and_browser_storage():
    source = (_HERE / "theme" / "warehouse-theme.src.js").read_text(encoding="utf-8")
    bundle = (_HERE / "theme" / "warehouse-theme.js").read_text(encoding="utf-8")
    css = (_HERE / "theme" / "warehouse-theme.css").read_text(encoding="utf-8")

    assert 'from "node-vibrant/browser"' in source
    assert "paletteToImagePalette" in source
    assert "deriveWarehouseTokens" in source
    assert "localStorage.setItem" in source
    assert "indexedDB.open" in source
    assert "rvbbit-warehouse-theme-v1" in bundle
    assert "/theme/library" in bundle
    assert (_HERE / "theme" / "VIBRANT-LICENSE").is_file()

    # Parent chrome and native charts are themeable; authored app iframe
    # documents remain their own CSS trees.
    assert "html[data-warehouse-theme] .chart-bar" in css
    assert "html[data-warehouse-theme] .artifact-frame iframe" not in css
    assert 'body[data-warehouse-page="calliope"] .surface' in css


def test_container_and_unified_origin_ship_theme_assets():
    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    caddy = (_HERE.parent.parent / "docker" / "origin" / "Caddyfile").read_text(encoding="utf-8")
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    assert "COPY theme ./theme" in dockerfile
    assert "/theme/*" in caddy
    assert "warehouse_theme.register_theme_routes(m)" in server
