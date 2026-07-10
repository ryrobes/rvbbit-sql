"""Warehouse MCP ergonomics — regression coverage for the field report where
agent glue-code (local file reads, oversized validation responses) was needed
around the tool surface, plus the describe_table lean crash.
"""
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


def test_describe_table_lean_survives_analyzed_stats(rvbbit, monkeypatch):
    """Regression: lean=True iterated _col_stats (a dict keyed by column) as a
    row list → "string indices must be integers, not 'str'" on every call."""
    server = _load_warehouse_mcp(monkeypatch)
    tbl = f"public.lean_probe_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {tbl} (status text, n int)")
    try:
        rvbbit.execute(
            f"INSERT INTO {tbl} SELECT (ARRAY['a','b','c'])[1 + i % 3], i FROM generate_series(1, 300) i")
        rvbbit.execute(f"ANALYZE {tbl}")
        full = server.tool_describe_table(tbl, lean=False)
        lean = server.tool_describe_table(tbl, lean=True)
        assert "error" not in lean and "error" not in full
        assert isinstance(lean["column_stats"], dict)
        # lean keeps ndv/null%, strips the top-values dictionary
        assert any("top" in v for v in full["column_stats"].values())
        assert not any("top" in v for v in lean["column_stats"].values())
        assert "samples" not in lean
    finally:
        rvbbit.execute(f"DROP TABLE {tbl}")


def test_run_sql_multi_summary_mode(rvbbit, monkeypatch):
    server = _load_warehouse_mcp(monkeypatch)
    tbl = f"public.summary_probe_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {tbl} (x int)")
    try:
        rvbbit.execute(f"INSERT INTO {tbl} SELECT i FROM generate_series(1, 50) i")
        out = server.tool_run_sql_multi(
            {"good": f"SELECT x FROM {tbl}",
             "bad": f"SELECT nope FROM {tbl}"},
            result_mode="summary", preview_rows=2)
        assert out["result_mode"] == "summary"
        good, bad = out["results"]["good"], out["results"]["bad"]
        assert good["row_count"] == 50
        assert good["columns"] == ["x"]
        assert len(good["preview"]) == 2
        assert "rows" not in good           # full rowset must NOT ride along
        assert bad["error"]["code"] == "INVALID_SQL"   # per-query isolation intact
        assert server.tool_run_sql_multi({"a": "SELECT 1"}, result_mode="bogus")["error"]["code"] == "BAD_RESULT_MODE"
    finally:
        rvbbit.execute(f"DROP TABLE {tbl}")


def test_artifact_upload_publish_roundtrip(rvbbit, monkeypatch):
    """upload_artifact (chunked) → publish by handle → served html matches;
    missing handles and empty publishes are structured errors."""
    rvbbit.execute("SELECT rvbbit.migrate()")
    server = _load_warehouse_mcp(monkeypatch)
    slug = None
    try:
        a = server.tool_upload_artifact("<html><body>artifact", name="ergo-test")
        assert a["artifact_id"] and a["bytes"] > 0
        b = server.tool_upload_artifact(" roundtrip</body></html>", artifact_id=a["artifact_id"], append=True)
        assert b["bytes"] > a["bytes"]

        pub = server.tool_publish_dashboard(
            f"ergo artifact dash {uuid.uuid4().hex[:8]}", source_artifact_id=a["artifact_id"])
        assert "error" not in pub
        slug = pub["slug"]
        got = server.tool_get_dashboard(slug)
        assert "artifact roundtrip" in got["version"]["html"]

        # a fresh version through update_dashboard by handle
        c = server.tool_upload_artifact("<html><body>v2</body></html>")
        upd = server.tool_update_dashboard(slug, source_artifact_id=c["artifact_id"])
        assert upd["version"] == 2

        missing = server.tool_publish_dashboard("nope", source_artifact_id="does-not-exist")
        assert missing["error"]["code"] == "ARTIFACT_NOT_FOUND"
        empty = server.tool_publish_dashboard("nope")
        assert empty["error"]["code"] == "EMPTY_HTML"
    finally:
        if slug:
            rvbbit.execute("DELETE FROM rvbbit.dashboards WHERE slug = %s", (slug,))


def test_logged_degrades_exceptions_to_structured_errors(monkeypatch):
    """Regression: _logged re-raised, so one buggy tool produced protocol-level
    errors that tripped client-side circuit breakers for the WHOLE server."""
    server = _load_warehouse_mcp(monkeypatch)

    def boom():
        raise TypeError("string indices must be integers, not 'str'")

    res = server._logged("ergo_boom", {}, boom)
    assert res["error"]["code"] == "EXCEPTION"
    assert "TypeError" in res["error"]["message"]
