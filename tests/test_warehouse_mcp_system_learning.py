from __future__ import annotations

import importlib.util
import os
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
    assert breadcrumb["followups"][0]["tool"] == "ask_system_learning"
    assert breadcrumb["followups"][1]["tool"] == "run_sql"
    assert "rvbbit.system_learning_items" in breadcrumb["followups"][1]["sql"]

    summary = server._summary("system_learning_status", status)
    assert summary["indexed_items"] == status["indexed_items"]
    assert summary["docs"] == status["docs"]
    assert summary["groups"]
    assert summary["breadcrumbs"]


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
    assert all(hit.get("artifact", {}).get("uri", "").startswith("rvbbit:") for hit in result["hits"])
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
