from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import sys
import threading
import uuid
from pathlib import Path

from conftest import RVBBIT_DSN


def _load_warehouse_mcp(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DSN", os.environ.get("RVBBIT_DSN", RVBBIT_DSN))
    path = Path(__file__).resolve().parents[1] / "services" / "warehouse-mcp" / "server.py"
    module_name = f"warehouse_mcp_server_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Python 3.14's dataclass annotation handling resolves the defining module
    # through sys.modules while the class decorator runs.
    sys.modules[module_name] = module
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


def test_system_learning_tools_are_registered_and_summarized(rvbbit, monkeypatch):
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)

    fake = _FakeMcp()
    server._register(fake)

    assert {"system_learning_status", "sync_system_learning", "ask_system_learning"}.issubset(
        fake.tools
    )
    assert "ask_system_learning" in server._INSTRUCTIONS

    status = server.tool_system_learning_status()
    assert status["installed"] is True
    assert status["enabled"] is True
    assert status["source"] == "RVBBIT System Learning"
    assert status["doc_type"] == "system_learning"
    assert status["indexed_items"] >= 1
    assert "run_sql" in status["agent_tools"]
    assert status["graph_edges"]
    assert status["readiness"]["state"] in {"ready", "partial", "needs_sync"}
    assert status["suggested_prompts"]
    assert any("accelerate" in p["query"].lower() for p in status["suggested_prompts"])
    assert status["answer_contract"]["required_citations"] == ["hit.title", "artifact.uri"]
    assert any("rvbbit.system_learning_item_summary" in f.get("sql", "") for f in status["followups"])

    groups = {row["object_type"]: row["items"] for row in status["summary"]}
    assert any(
        groups.get(kind, 0) >= 1
        for kind in (
            "workload_layout",
            "route_shape",
            "acceleration_state",
            "heap_acceleration_candidate",
            "operator",
        )
    )
    assert status["breadcrumbs"]
    breadcrumb = status["breadcrumbs"][0]
    assert breadcrumb["uri"].startswith("rvbbit:")
    assert breadcrumb["title"]
    assert breadcrumb["object_type"] in groups
    assert breadcrumb["handles"]
    assert "rvbbit.system_learning_items" in breadcrumb["inspect_sql"]
    assert breadcrumb["followups"][0]["tool"] == "ask_system_learning"
    assert breadcrumb["followups"][1]["tool"] == "run_sql"
    assert "rvbbit.system_learning_items" in breadcrumb["followups"][1]["sql"]

    summary = server._summary("system_learning_status", status)
    assert summary["indexed_items"] == status["indexed_items"]
    assert summary["docs"] == status["docs"]
    assert summary["groups"]
    assert summary["breadcrumbs"]


def test_sync_mcp_registrations_do_not_block_event_loop(rvbbit, monkeypatch):
    """No synchronous MCP handler may stall Calliope's shared ASGI loop."""
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)
    fake = _FakeMcp()
    server._register(fake)

    started = threading.Event()
    release = threading.Event()

    def slow_logged(tool, args, thunk):
        assert tool == "system_learning_status"
        started.set()
        assert release.wait(timeout=3)
        return {"forwarded": server._FORWARDED_CALLER.get()}

    monkeypatch.setattr(server, "_logged", slow_logged)
    registered = fake.tools["system_learning_status"]
    assert inspect.iscoroutinefunction(registered)
    assert all(inspect.iscoroutinefunction(tool) for tool in fake.tools.values())

    async def exercise():
        marker = {"email": "person@example.com", "platform": "google_chat"}
        token = server._FORWARDED_CALLER.set(marker)
        try:
            task = asyncio.create_task(registered())
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            # Reaching this point while the worker is deliberately blocked
            # proves another ASGI task can still be scheduled.
            assert not task.done()
            release.set()
            assert await asyncio.wait_for(task, timeout=3) == {"forwarded": marker}
        finally:
            server._FORWARDED_CALLER.reset(token)

    asyncio.run(exercise())


def test_system_learning_mcp_sync_and_search_shortcut(rvbbit, monkeypatch):
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)

    sync = server.tool_sync_system_learning()
    assert "error" not in sync
    assert sync["source"] == "RVBBIT System Learning"
    assert sync["status"]["indexed_items"] >= 1
    assert sync["status"]["docs"] >= 1

    result = server.tool_ask_system_learning("RVBBIT acceleration routing operator workload", 5)
    assert "error" not in result
    assert result["filters"] == {"type": ["system_learning"]}
    assert result["as"] == "mcp-system-learning@rvbbit.local"
    assert result["count"] >= 1
    assert result["types"].get("system_learning", 0) == result["count"]
    assert all(hit["doc_type"] == "system_learning" for hit in result["hits"])
    assert result["breadcrumbs"]
    assert "run_sql" in result["next_tools"]
    assert result["readiness"]["ready"] is True
    assert result["suggested_prompts"]
    assert result["answer_contract"]["style"] == "grounded_context_not_synthesis"
    assert any("rvbbit.system_learning_items" in f.get("sql", "") for f in result["followups"])
    assert all(hit.get("artifact", {}).get("uri", "").startswith("rvbbit:") for hit in result["hits"])
    assert all("rvbbit.system_learning_items" in hit.get("artifact", {}).get("inspect_sql", "") for hit in result["hits"])
    assert any(
        followup.get("tool") == "run_sql" and "rvbbit.system_learning_items" in followup.get("sql", "")
        for breadcrumb in result["breadcrumbs"]
        for followup in breadcrumb.get("followups", [])
    )
    assert any(doc.get("artifact", {}).get("handles") for doc in result["documents"])

    logged_objects = server._objects("ask_system_learning", {"query": "routing"}, result)
    assert logged_objects == ["rvbbit.system_learning_items"]

    summary = server._summary("ask_system_learning", result)
    assert summary["count"] == result["count"]
    assert summary["hits"]
