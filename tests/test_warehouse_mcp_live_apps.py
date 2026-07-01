from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import threading
import uuid
from pathlib import Path

import httpx

from conftest import RVBBIT_DSN


def _load_warehouse_mcp(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DSN", os.environ.get("RVBBIT_DSN", RVBBIT_DSN))
    path = Path(__file__).resolve().parents[1] / "services" / "warehouse-mcp" / "server.py"
    module_name = f"warehouse_mcp_server_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def _decorator(func):
            self.tools[name] = func
            return func

        return _decorator


def test_live_app_html_lifecycle(rvbbit, monkeypatch):
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)

    fake = _FakeMcp()
    server._register(fake)
    assert {
        "live_app_template",
        "create_live_app",
        "update_live_app",
        "list_live_apps",
        "get_live_app",
        "debug_live_app",
        "live_app_logs",
        "start_live_app",
        "stop_live_app",
        "live_app_status",
        "capture_live_app",
    }.issubset(fake.tools)
    assert "create_live_app" in server._INSTRUCTIONS

    table = f"live_app_src_{uuid.uuid4().hex[:8]}"
    slug = None
    rvbbit.execute(f"CREATE TABLE public.{table} (id int)")
    rvbbit.execute(f"INSERT INTO public.{table} VALUES (1), (2)")
    try:
        html = f"""
<!doctype html>
<html>
<body>
  <script>
    async function load() {{
      const result = await rvbbitQuery("select count(*) as n from public.{table}");
      document.body.dataset.rows = result.rows[0].n;
    }}
  </script>
</body>
</html>
"""
        created = server.tool_create_live_app(
            f"Live App Test {uuid.uuid4().hex[:8]}",
            html=html,
            description="pytest html app",
            manifest={"purpose": "pytest"},
        )
        assert "error" not in created
        slug = created["slug"]
        assert created["version"] == 1
        assert created["runtime_kind"] == "html"
        assert created["app_kind"] == "dashboard"
        assert created["health"]["state"] == "runnable"
        assert created["deps"]["queries"] == 1
        assert f"public.{table}" in created["deps"]["tables"]

        listed = server.tool_list_live_apps(search="Live App Test")
        assert any(app["slug"] == slug for app in listed["live_apps"])

        fetched = server.tool_get_live_app(slug, include_source=True)
        assert fetched["slug"] == slug
        assert fetched["manifest"]["purpose"] == "pytest"
        assert "rvbbitQuery" in fetched["version"]["html"]
        assert any(src["object_ref"] == f"public.{table}" for src in fetched["sources"])

        debugged = server.tool_debug_live_app(slug)
        assert debugged["health"]["ok"] is True
        assert debugged["deps"]["queries"] == 1
        assert not any(issue["code"] == "RECENT_QUERY_ERRORS" for issue in debugged["health"]["issues"])

        updated = server.tool_update_live_app(slug, html=html.replace("dataset.rows", "dataset.counts"))
        assert "error" not in updated
        assert updated["version"] == 2
        refetched = server.tool_get_live_app(slug, include_source=True)
        assert refetched["latest_version"] == 2
        assert "dataset.counts" in refetched["version"]["html"]
    finally:
        if slug:
            rvbbit.execute("DELETE FROM rvbbit.dashboards WHERE slug = %s", (slug,))
        rvbbit.execute(f"DROP TABLE IF EXISTS public.{table}")


def test_live_app_python_fastapi_is_versioned_and_debuggable(rvbbit, monkeypatch):
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)

    table = f"live_app_py_src_{uuid.uuid4().hex[:8]}"
    slug = None
    rvbbit.execute(f"CREATE TABLE public.{table} (id int)")
    try:
        source_files = {
            "app.py": f"""
from fastapi import FastAPI

app = FastAPI()
SQL = "select count(*) as n from public.{table}"
""",
            "requirements.txt": "fastapi\nuvicorn[standard]\n",
        }
        created = server.tool_create_live_app(
            f"Python Live App Test {uuid.uuid4().hex[:8]}",
            runtime_kind="python-fastapi",
            app_kind="analysis-app",
            source_files=source_files,
            manifest={"owner": "pytest"},
        )
        assert "error" not in created
        slug = created["slug"]
        assert created["runtime_kind"] == "python-fastapi"
        assert created["app_kind"] == "analysis-app"
        assert created["health"]["state"] == "stored"
        assert created["deps"]["queries"] == 1
        assert f"public.{table}" in created["deps"]["tables"]

        fetched = server.tool_get_live_app(slug)
        assert fetched["version"]["source_files"]["app.py"] == source_files["app.py"]
        assert fetched["manifest"]["runtime_kind"] == "python-fastapi"

        debugged = server.tool_debug_live_app(slug)
        assert debugged["health"]["ok"] is False
        assert any(issue["code"] == "PYTHON_RUNNER_STOPPED" for issue in debugged["health"]["issues"])
    finally:
        if slug:
            rvbbit.execute("DELETE FROM rvbbit.dashboards WHERE slug = %s", (slug,))
        rvbbit.execute(f"DROP TABLE IF EXISTS public.{table}")


def test_live_app_python_runner_lifecycle(rvbbit, monkeypatch, tmp_path):
    monkeypatch.setenv("WAREHOUSE_LIVE_APP_ROOT", str(tmp_path / "apps"))
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)

    table = f"live_app_runner_src_{uuid.uuid4().hex[:8]}"
    slug = None
    rvbbit.execute(f"CREATE TABLE public.{table} (id int)")
    rvbbit.execute(f"INSERT INTO public.{table} VALUES (1), (2), (3)")
    try:
        source_files = {
            "app.py": f"""
from fastapi import FastAPI
from rvbbit_live import rvbbit_query

app = FastAPI()


@app.get("/health")
async def health():
    return {{"ok": True}}


@app.get("/")
async def index():
    result = await rvbbit_query("select cast(count(*) as int) as n from public.{table}")
    return {{"n": result["rows"][0]["n"], "row_count": result["row_count"]}}
""",
            "requirements.txt": "fastapi\nuvicorn[standard]\npsycopg[binary]\n",
        }
        created = server.tool_create_live_app(
            f"Python Runner Live App Test {uuid.uuid4().hex[:8]}",
            runtime_kind="python-fastapi",
            source_files=source_files,
        )
        assert "error" not in created
        slug = created["slug"]

        started = server.tool_start_live_app(slug)
        assert "error" not in started
        assert started["running"] is True
        assert started["state"] == "running"
        assert started["path"] == f"/apps/{slug}"

        response = httpx.get(started["endpoint_url"], timeout=5)
        assert response.status_code == 200
        assert response.json()["n"] == 3

        status = server.tool_live_app_status(slug)
        assert status["running"] is True
        assert status["version"] == 1

        debugged = server.tool_debug_live_app(slug)
        assert debugged["health"]["ok"] is True
        assert debugged["runner"]["running"] is True

        stopped = server.tool_stop_live_app(slug)
        assert stopped["running"] is False
        assert server.tool_live_app_status(slug)["state"] == "stopped"
    finally:
        if slug:
            server.tool_stop_live_app(slug)
            rvbbit.execute("DELETE FROM rvbbit.dashboards WHERE slug = %s", (slug,))
        rvbbit.execute(f"DROP TABLE IF EXISTS public.{table}")


def test_live_app_capture_contract_uses_stored_html(rvbbit, monkeypatch, tmp_path):
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)

    slug = None
    try:
        created = server.tool_create_live_app(
            f"Capture Live App Test {uuid.uuid4().hex[:8]}",
            html="<html><body><h1>capture</h1></body></html>",
        )
        assert "error" not in created
        slug = created["slug"]

        def fake_capture(html, path, width, height, full_page, wait_ms):
            assert "capture" in html
            Path(path).write_bytes(b"fakepng")

        monkeypatch.setattr(server, "_capture_html_with_playwright", fake_capture)
        out = tmp_path / "capture.png"
        captured = server.tool_capture_live_app(slug, path=str(out), width=800, height=600)
        assert "error" not in captured
        assert captured["path"] == str(out)
        assert captured["bytes"] == len(b"fakepng")
        assert captured["source"] == "stored-html"
    finally:
        if slug:
            rvbbit.execute("DELETE FROM rvbbit.dashboards WHERE slug = %s", (slug,))


def test_mcp_capture_wrapper_offloads_sync_playwright(monkeypatch):
    server = _load_warehouse_mcp(monkeypatch)
    main_thread = threading.get_ident()
    seen = {}

    def fake_logged(name, args, fn):
        seen["name"] = name
        seen["logged_thread"] = threading.get_ident()
        return fn()

    def fake_capture(*args):
        seen["capture_thread"] = threading.get_ident()
        return {"ok": True, "slug": args[0]}

    monkeypatch.setattr(server, "_logged", fake_logged)
    monkeypatch.setattr(server, "tool_capture_live_app", fake_capture)

    result = asyncio.run(server._mcp_capture_live_app("capture-thread-test"))

    assert result == {"ok": True, "slug": "capture-thread-test"}
    assert seen["name"] == "capture_live_app"
    assert seen["logged_thread"] != main_thread
    assert seen["capture_thread"] != main_thread


def test_playwright_chromium_auto_install_retries_launch(monkeypatch):
    server = _load_warehouse_mcp(monkeypatch)
    calls = []

    class _Chromium:
        def __init__(self):
            self.launches = 0

        def launch(self):
            self.launches += 1
            if self.launches == 1:
                raise RuntimeError("Executable doesn't exist at /missing/chromium. Please run playwright install")
            return "browser"

    class _Playwright:
        def __init__(self):
            self.chromium = _Chromium()

    def fake_run(cmd, text, capture_output, timeout, check):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="installed", stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setenv("WAREHOUSE_PLAYWRIGHT_AUTO_INSTALL", "1")

    assert server._launch_playwright_chromium(_Playwright()) == "browser"
    assert calls == [[server.sys.executable, "-m", "playwright", "install", "chromium"]]
